"""
protocol.py
Message formatting helpers for the distributed file system.

The wire format is plain-text, pipe-delimited, newline-terminated:
    COMMAND|arg1|arg2|...\n
File contents and OK payloads are tagged as base64 values so pipes and
newlines in user data do not break the line-oriented framing.
This makes debugging trivial -- you can literally telnet into a server and
see the message types and paths.
"""

import base64

from config import MESSAGE_DELIMITER, MESSAGE_ENCODING


CONTENT_PREFIX = "b64:"


def _encode_content(content: str) -> str:
    raw = content.encode(MESSAGE_ENCODING)
    return CONTENT_PREFIX + base64.b64encode(raw).decode("ascii")


def _decode_content(value: str) -> str:
    if not value.startswith(CONTENT_PREFIX):
        return value
    raw = value[len(CONTENT_PREFIX):].encode("ascii")
    return base64.b64decode(raw).decode(MESSAGE_ENCODING)


# ---------------------------------------------------------------------------
# Low-level framing
# ---------------------------------------------------------------------------

def encode(msg: str) -> bytes:
    """Turn a string into the bytes that go on the wire."""
    return msg.encode(MESSAGE_ENCODING) + MESSAGE_DELIMITER


def decode(data: bytes) -> str:
    """Turn wire bytes back into a string (strip trailing newline)."""
    return data.decode(MESSAGE_ENCODING).rstrip("\n\r")


# ---------------------------------------------------------------------------
# Command builders (Client -> Primary)
# ---------------------------------------------------------------------------

def cmd_read(path: str) -> str:
    return f"READ|{path}"


def cmd_write(path: str, content: str) -> str:
    return f"WRITE|{path}|{_encode_content(content)}"


def cmd_update(path: str, content: str) -> str:
    return f"UPDATE|{path}|{_encode_content(content)}"


def cmd_delete(path: str) -> str:
    return f"DELETE|{path}"


# ---------------------------------------------------------------------------
# Response builders (Primary -> Client)
# ---------------------------------------------------------------------------

def resp_ok(payload: str = "") -> str:
    return f"OK|{_encode_content(payload)}"


def resp_ok_plain(payload: str = "") -> str:
    """Return a user-facing OK response with decoded payload text."""
    return f"OK|{payload}"


def resp_error(reason: str) -> str:
    return f"ERR|{reason}"


def resp_not_primary() -> str:
    return resp_error("NOT_PRIMARY")


# ---------------------------------------------------------------------------
# Replication builders (Primary -> Backup)
# ---------------------------------------------------------------------------

def repl_write(path: str, content: str) -> str:
    return f"REPLICATE_WRITE|{path}|{_encode_content(content)}"


def repl_update(path: str, content: str) -> str:
    return f"REPLICATE_UPDATE|{path}|{_encode_content(content)}"


def repl_delete(path: str) -> str:
    return f"REPLICATE_DELETE|{path}"


def repl_ack() -> str:
    return "ACK"


# ---------------------------------------------------------------------------
# Recovery / discovery builders
# ---------------------------------------------------------------------------

def disco_who_are_you() -> str:
    return "WHO_ARE_YOU"


def disco_i_am_primary(host: str, port: int) -> str:
    return f"I_AM_PRIMARY|{host}:{port}"


def disco_i_am_backup(host: str, port: int) -> str:
    return f"I_AM_BACKUP|{host}:{port}"


def sync_request() -> str:
    return "SYNC_REQUEST"


def sync_file(path: str, content: str) -> str:
    return f"SYNC_FILE|{path}|{_encode_content(content)}"


def sync_done() -> str:
    return "SYNC_DONE"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse(msg: str):
    """
    Split a message into (command, *args).
    Returns a tuple where the first element is the upper-cased command
    and the remaining elements are the arguments.
    """
    command = msg.split("|", 1)[0].upper()
    if command in {"WRITE", "UPDATE", "REPLICATE_WRITE", "REPLICATE_UPDATE", "SYNC_FILE"}:
        parts = msg.split("|", 2)
        if len(parts) == 3:
            return (command, parts[1], _decode_content(parts[2]))
        return tuple([command] + parts[1:])
    if command == "OK":
        parts = msg.split("|", 1)
        if len(parts) == 2:
            return (command, _decode_content(parts[1]))
        return (command, "")

    parts = msg.split("|")
    return tuple([parts[0].upper()] + parts[1:])
