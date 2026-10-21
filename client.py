"""
client.py
Command-line client for the distributed file system.

Supports automatic failover: if the primary server does not respond within
CLIENT_TIMEOUT_SECONDS, the client retries with the backup server.

Usage:
    python client.py
    > WRITE hello.txt world
    OK|written
    > READ hello.txt
    OK|world
    > DELETE hello.txt
    OK|deleted
    > quit
"""

import socket
import sys
import time

import config
import protocol as p


def log(msg: str):
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


class DFSClient:
    """
    Client front-end that talks to the distributed file system.
    Maintains an ordered list of server addresses and swaps on failure.
    """

    def __init__(self, servers=None):
        # servers is a list of (host, port) for client connections
        if servers is None:
            servers = [(h, cp) for h, cp, _ in config.PEERS]
        self.servers = servers
        self.current_index = 0
        self.sock: socket.socket = None
        self.fileobj = None

    def _connect(self, index: int) -> bool:
        """Try to open a TCP connection to the server at the given index."""
        host, port = self.servers[index]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(config.CLIENT_TIMEOUT_SECONDS)
            s.connect((host, port))
            self.sock = s
            self.fileobj = s.makefile("rwb")
            return True
        except Exception as e:
            if self.sock:
                self.sock.close()
                self.sock = None
            return False

    def _close(self):
        if self.fileobj:
            try:
                self.fileobj.close()
            except Exception:
                pass
            self.fileobj = None
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _send_and_receive(self, msg: str) -> str:
        """Send a message and return the response string."""
        self.fileobj.write(p.encode(msg))
        self.fileobj.flush()
        raw = self.fileobj.readline()
        if not raw:
            raise ConnectionError("Server closed connection")
        response = p.decode(raw)
        parsed = p.parse(response)
        if parsed[0] == "OK":
            payload = parsed[1] if len(parsed) > 1 else ""
            return p.resp_ok_plain(payload)
        return response

    def request(self, msg: str) -> str:
        """
        Send a request, with automatic failover across the server list.
        Returns the response string (e.g. 'OK|...' or 'ERR|...').
        """
        attempts = 0
        max_attempts = len(self.servers)

        while attempts < max_attempts:
            # Ensure we have a connection to the current server
            if self.sock is None:
                if not self._connect(self.current_index):
                    log(f"(!) Cannot connect to {self.servers[self.current_index]}")
                    self._failover()
                    attempts += 1
                    continue

            try:
                response = self._send_and_receive(msg)
            except Exception as e:
                log(f"(!) Error talking to {self.servers[self.current_index]}: {e}")
                self._close()
                self._failover()
                attempts += 1
                continue

            # If the backup says it is not primary yet, wait briefly and retry
            if response.startswith("ERR|NOT_PRIMARY"):
                log(f"(!) {self.servers[self.current_index]} is not primary; trying next server...")
                self._close()
                self._failover()
                attempts += 1
                continue

            return response

        return p.resp_error("All servers unreachable")

    def _failover(self):
        """Rotate to the next server in the list."""
        old = self.servers[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.servers)
        new = self.servers[self.current_index]
        if old != new:
            log(f"-> Failing over: {old} -> {new}")

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def read(self, path: str) -> str:
        return self.request(p.cmd_read(path))

    def write(self, path: str, content: str) -> str:
        return self.request(p.cmd_write(path, content))

    def update(self, path: str, content: str) -> str:
        return self.request(p.cmd_update(path, content))

    def delete(self, path: str) -> str:
        return self.request(p.cmd_delete(path))


def interactive_shell():
    client = DFSClient()
    print("Distributed File System Client")
    print("Commands: READ <path> | WRITE <path> <content> | UPDATE <path> <content> | DELETE <path> | quit")
    print("-" * 50)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not line:
            continue
        parts = line.split(None, 1)
        cmd = parts[0].upper()

        if cmd == "QUIT":
            print("Bye!")
            break
        elif cmd == "READ":
            if len(parts) < 2:
                print("Usage: READ <path>")
                continue
            print(client.read(parts[1]))
        elif cmd in {"WRITE", "UPDATE"}:
            rest = parts[1] if len(parts) > 1 else ""
            # split into path and content at first space
            sub = rest.split("|", 1)
            if len(sub) < 2:
                # also try space split
                sub = rest.split(None, 1)
            if len(sub) < 2:
                print(f"Usage: {cmd} <path> <content>   (or {cmd} path|content)")
                continue
            if cmd == "WRITE":
                print(client.write(sub[0], sub[1]))
            else:
                print(client.update(sub[0], sub[1]))
        elif cmd == "DELETE":
            if len(parts) < 2:
                print("Usage: DELETE <path>")
                continue
            print(client.delete(parts[1]))
        else:
            print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    interactive_shell()
