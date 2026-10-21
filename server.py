"""
server.py
Distributed file server that can run as PRIMARY, BACKUP, or auto-discover.

Usage:
    python server.py --storage ./storage_a --client-port 9001 --repl-port 9002
    python server.py --storage ./storage_b --client-port 9101 --repl-port 9102

The first server to start becomes PRIMARY (unless another already exists).
The second becomes BACKUP, receives replication traffic, and promotes itself
if the primary disappears.
"""

import argparse
import os
import socket
import sys
import threading
import time
import traceback
from typing import Optional

import config
import protocol as p
from storage import Storage


# ---------------------------------------------------------------------------
# Logging helper (timestamped, thread-safe)
# ---------------------------------------------------------------------------

def log(msg: str):
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Server class
# ---------------------------------------------------------------------------

class FileServer:
    def __init__(
        self,
        storage_root: str,
        client_host: str,
        client_port: int,
        repl_host: str,
        repl_port: int,
        peers: list,          # list of (host, client_port, repl_port)
    ):
        # --- identity ---
        self.storage = Storage(storage_root)
        self.client_host = client_host
        self.client_port = client_port
        self.repl_host = repl_host
        self.repl_port = repl_port
        self.peers = [peer for peer in peers if not self._is_me(peer)]

        # --- role ---
        self.role: str = "UNKNOWN"   # PRIMARY or BACKUP
        self.running = True

        # --- replication sockets ---
        self.primary_repl_sock: Optional[socket.socket] = None   # as backup, connection TO primary
        self.backup_repl_socks: list = []                        # as primary, connections FROM backups
        self.backup_repl_lock = threading.Lock()
        self.operation_lock = threading.Lock()

        # --- threads ---
        self.threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_me(self, peer) -> bool:
        """Check whether a peer tuple refers to this server."""
        h, cp, rp = peer
        return (h == self.client_host or h == "127.0.0.1") and cp == self.client_port

    def _bind_listener(self, host: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR is intentionally omitted on Windows because it allows
        # multiple processes to bind to the same port, which causes silent
        # conflicts with stale server processes.
        sock.bind((host, port))
        sock.listen(5)
        return sock

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        log("=" * 50)
        log(f"Storage root : {self.storage.root}")
        log(f"Client port  : {self.client_host}:{self.client_port}")
        log(f"Repl port    : {self.repl_host}:{self.repl_port}")
        log("=" * 50)

        # 1) Auto-discovery
        discovered_primary = self._discover_peers()
        if discovered_primary:
            self._become_backup(discovered_primary)
        else:
            self._become_primary()

        # 2) Start listener threads
        self._start_thread(self._client_listener_loop, "client-listener")
        self._start_thread(self._repl_listener_loop, "repl-listener")

        log("Server running. Press Ctrl+C to stop.")
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            log("Shutdown requested.")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running = False
        # Close sockets to unblock accept() calls
        try:
            # dummy connect to ourselves to wake up listeners
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((self.client_host, self.client_port))
            s.close()
        except Exception:
            pass
        for t in self.threads:
            t.join(timeout=2.0)
        log("Shutdown complete.")

    def _start_thread(self, target, name: str):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self.threads.append(t)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover_peers(self) -> Optional[tuple]:
        """
        Ask every peer whether it is PRIMARY.  Return the first primary found
        as (host, repl_port), or None if no primary exists.
        Retries a few times to handle the race where the primary's repl
        listener hasn't finished binding yet.
        """
        log("Discovering peers...")
        for attempt in range(1, 4):
            for h, cp, rp in self.peers:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1.0)
                    sock.connect((h, rp))
                    sock.sendall(p.encode(p.disco_who_are_you()))
                    data = sock.recv(4096)
                    sock.close()
                    if not data:
                        continue
                    msg = p.decode(data)
                    cmd, *args = p.parse(msg)
                    if cmd == "I_AM_PRIMARY":
                        log(f"  Found primary at {h}:{rp}")
                        return (h, rp)
                except Exception:
                    continue
            if attempt < 3:
                time.sleep(0.5)
        log("  No primary found.")
        return None

    # ------------------------------------------------------------------
    # Role transitions
    # ------------------------------------------------------------------

    def _become_primary(self):
        self.role = "PRIMARY"
        log("*** ROLE: PRIMARY ***")

    def _become_backup(self, primary_addr: tuple):
        self.role = "BACKUP"
        self.primary_repl_host, self.primary_repl_port = primary_addr
        log(f"*** ROLE: BACKUP (primary at {self.primary_repl_host}:{self.primary_repl_port}) ***")
        self._connect_to_primary()

    def _connect_to_primary(self):
        """Backup opens a replication channel to the primary."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((self.primary_repl_host, self.primary_repl_port))
            sock.settimeout(None)  # Remove timeout for long-lived replication link

            # 1) Request full state sync (needed when recovering from crash)
            sock.sendall(p.encode(p.sync_request()))
            fileobj = sock.makefile("rwb")
            self.storage.clear_all()
            while self.running:
                raw = fileobj.readline()
                if not raw:
                    break
                msg = p.decode(raw)
                cmd, *args = p.parse(msg)
                if cmd == "SYNC_FILE":
                    path, content = args[0], args[1]
                    self.storage.write(path, content)
                elif cmd == "SYNC_DONE":
                    log("State sync complete.")
                    break

            # 2) Register as a live backup
            sock.sendall(p.encode(p.disco_i_am_backup(self.repl_host, self.repl_port)))
            self.primary_repl_sock = sock
            log("Connected to primary replication port.")

            # 3) Start replication reader for future commands
            self._start_thread(lambda: self._backup_repl_reader(sock), "backup-repl-reader")
        except Exception as e:
            log(f"Failed to connect to primary: {e}")

    def _promote_to_primary(self):
        log("*** BACKUP PROMOTING TO PRIMARY ***")
        self.role = "PRIMARY"
        if self.primary_repl_sock:
            try:
                self.primary_repl_sock.close()
            except Exception:
                pass
            self.primary_repl_sock = None
        log("Now accepting client requests.")

    # ------------------------------------------------------------------
    # Client listener (PRIMARY accepts clients; BACKUP rejects them)
    # ------------------------------------------------------------------

    def _client_listener_loop(self):
        listener = self._bind_listener(self.client_host, self.client_port)
        log(f"Client listener on {self.client_host}:{self.client_port}")
        while self.running:
            try:
                conn, addr = listener.accept()
            except OSError:
                break
            t = threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True)
            t.start()
        listener.close()

    def _handle_client(self, conn: socket.socket, addr):
        try:
            fileobj = conn.makefile("rwb")
            while self.running:
                raw = fileobj.readline()
                if not raw:
                    break
                msg = p.decode(raw)
                cmd, *args = p.parse(msg)

                if self.role != "PRIMARY":
                    response = p.resp_not_primary()
                elif cmd in {"READ", "WRITE", "UPDATE", "DELETE"}:
                    with self.operation_lock:
                        if cmd == "READ":
                            response = self._do_read(args)
                        elif cmd == "WRITE":
                            response = self._do_write(args)
                        elif cmd == "UPDATE":
                            response = self._do_update(args)
                        else:
                            response = self._do_delete(args)
                else:
                    response = p.resp_error(f"Unknown command: {cmd}")

                fileobj.write(p.encode(response))
                fileobj.flush()
        except Exception as e:
            log(f"Client handler error: {e}")
        finally:
            conn.close()

    def _do_read(self, args):
        if len(args) < 1:
            return p.resp_error("Missing path")
        path = args[0]
        try:
            content = self.storage.read(path)
            return p.resp_ok(content)
        except FileNotFoundError as e:
            return p.resp_error(str(e))
        except Exception as e:
            return p.resp_error(str(e))

    def _do_write(self, args):
        if len(args) < 2:
            return p.resp_error("Missing path or content")
        path, content = args[0], args[1]
        try:
            self.storage.write(path, content)
            # Replicate to backups synchronously before acknowledging the client.
            if not self._replicate_to_backups(p.repl_write(path, content)):
                return p.resp_error("Replication failed")
            return p.resp_ok("written")
        except Exception as e:
            return p.resp_error(str(e))

    def _do_update(self, args):
        if len(args) < 2:
            return p.resp_error("Missing path or content")
        path, content = args[0], args[1]
        try:
            self.storage.update(path, content)
            if not self._replicate_to_backups(p.repl_update(path, content)):
                return p.resp_error("Replication failed")
            return p.resp_ok("updated")
        except FileNotFoundError as e:
            return p.resp_error(str(e))
        except Exception as e:
            return p.resp_error(str(e))

    def _do_delete(self, args):
        if len(args) < 1:
            return p.resp_error("Missing path")
        path = args[0]
        try:
            self.storage.delete(path)
            if not self._replicate_to_backups(p.repl_delete(path)):
                return p.resp_error("Replication failed")
            return p.resp_ok("deleted")
        except FileNotFoundError as e:
            return p.resp_error(str(e))
        except Exception as e:
            return p.resp_error(str(e))

    # ------------------------------------------------------------------
    # Replication (Primary -> Backup)
    # ------------------------------------------------------------------

    def _replicate_to_backups(self, repl_msg: str) -> bool:
        """
        Send a replication message to every connected backup.
        The primary waits for an ACK from each registered backup before
        reporting success to the client. Dead backups are removed and cause
        this operation to fail so the client does not receive a false success.
        """
        with self.backup_repl_lock:
            socks = list(self.backup_repl_socks)
        if not socks:
            return True  # no backups, nothing to wait for

        data = p.encode(repl_msg)
        dead_socks = []
        for sock in socks:
            try:
                sock.sendall(data)
                ack = self._recv_repl_ack(sock)
                if ack != "ACK":
                    dead_socks.append(sock)
            except Exception:
                dead_socks.append(sock)

        if dead_socks:
            with self.backup_repl_lock:
                for sock in dead_socks:
                    if sock in self.backup_repl_socks:
                        self.backup_repl_socks.remove(sock)
                    try:
                        sock.close()
                    except Exception:
                        pass
        return not dead_socks

    def _recv_repl_ack(self, sock: socket.socket) -> str:
        previous_timeout = sock.gettimeout()
        sock.settimeout(config.CLIENT_TIMEOUT_SECONDS)
        chunks = []
        try:
            while True:
                chunk = sock.recv(1)
                if not chunk:
                    raise ConnectionError("Backup closed replication socket")
                if chunk in (b"\n", b"\r"):
                    break
                chunks.append(chunk)
            return p.decode(b"".join(chunks))
        finally:
            sock.settimeout(previous_timeout)

    # ------------------------------------------------------------------
    # Replication listener (accepts connections from backups)
    # ------------------------------------------------------------------

    def _repl_listener_loop(self):
        listener = self._bind_listener(self.repl_host, self.repl_port)
        log(f"Replication listener on {self.repl_host}:{self.repl_port}")
        while self.running:
            try:
                conn, addr = listener.accept()
            except OSError:
                break
            t = threading.Thread(target=self._handle_repl_conn, args=(conn, addr), daemon=True)
            t.start()
        listener.close()

    def _handle_repl_conn(self, conn: socket.socket, addr):
        log(f"Backup connected from {addr}")
        try:
            fileobj = conn.makefile("rwb")
            while self.running:
                raw = fileobj.readline()
                if not raw:
                    break
                msg = p.decode(raw)
                cmd, *args = p.parse(msg)

                if cmd == "SYNC_REQUEST":
                    self._handle_sync_request(fileobj)
                elif cmd == "WHO_ARE_YOU":
                    if self.role == "PRIMARY":
                        fileobj.write(p.encode(p.disco_i_am_primary(self.client_host, self.client_port)))
                    else:
                        fileobj.write(p.encode(p.disco_i_am_backup(self.client_host, self.client_port)))
                    fileobj.flush()
                elif cmd == "I_AM_BACKUP":
                    # Backup registered; stop reading from this socket so that
                    # _replicate_to_backups can send commands and read ACKs
                    # directly without competing with this thread.
                    log(f"Backup registered: {args}")
                    with self.backup_repl_lock:
                        if conn not in self.backup_repl_socks:
                            self.backup_repl_socks.append(conn)
                    while self.running:
                        time.sleep(1)
                else:
                    # ignore unknown on repl channel
                    pass
        except Exception as e:
            log(f"Replication handler error: {e}")
        finally:
            with self.backup_repl_lock:
                if conn in self.backup_repl_socks:
                    self.backup_repl_socks.remove(conn)
            try:
                conn.close()
            except Exception:
                pass
            log(f"Backup disconnected from {addr}")

    def _handle_sync_request(self, fileobj):
        """Primary sends all its files to a recovering backup."""
        log("Handling SYNC_REQUEST from recovering backup.")
        files = self.storage.list_all_files()
        for rel_path, content in files:
            fileobj.write(p.encode(p.sync_file(rel_path, content)))
            fileobj.flush()
        fileobj.write(p.encode(p.sync_done()))
        fileobj.flush()
        log("Sync complete.")

    # ------------------------------------------------------------------
    # Backup replication reader (reads REPLICATE_WRITE / REPLICATE_DELETE)
    # ------------------------------------------------------------------

    def _backup_repl_reader(self, sock: socket.socket):
        """
        Runs in the backup and continuously reads replication commands
        from the primary.  If the connection closes (primary crashed),
        the backup promotes itself automatically.
        """
        try:
            fileobj = sock.makefile("rwb")
            while self.running and self.role == "BACKUP":
                raw = fileobj.readline()
                if not raw:
                    # Primary closed the connection -> it has crashed
                    log("Replication link closed by primary.")
                    break
                msg = p.decode(raw)
                cmd, *args = p.parse(msg)

                if cmd == "REPLICATE_WRITE":
                    path, content = args[0], args[1]
                    self.storage.write(path, content)
                    fileobj.write(p.encode(p.repl_ack()))
                    fileobj.flush()
                elif cmd == "REPLICATE_UPDATE":
                    path, content = args[0], args[1]
                    self.storage.update(path, content)
                    fileobj.write(p.encode(p.repl_ack()))
                    fileobj.flush()
                elif cmd == "REPLICATE_DELETE":
                    path = args[0]
                    try:
                        self.storage.delete(path)
                    except FileNotFoundError:
                        pass
                    fileobj.write(p.encode(p.repl_ack()))
                    fileobj.flush()
                elif cmd == "SYNC_FILE":
                    path, content = args[0], args[1]
                    self.storage.write(path, content)
                elif cmd == "SYNC_DONE":
                    log("Backup sync complete.")
                else:
                    # ignore unknown on repl channel
                    pass
        except Exception as e:
            log(f"Backup repl reader ended: {e}")
        finally:
            # If we lost the replication link, promote ourselves
            if self.role == "BACKUP":
                self._promote_to_primary()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Distributed File Server")
    parser.add_argument("--storage", default=config.DEFAULT_STORAGE_ROOT, help="Directory to store files")
    parser.add_argument("--client-host", default="127.0.0.1")
    parser.add_argument("--client-port", type=int, default=config.PRIMARY_CLIENT_PORT)
    parser.add_argument("--repl-host", default="127.0.0.1")
    parser.add_argument("--repl-port", type=int, default=config.PRIMARY_REPL_PORT)
    parser.add_argument(
        "--peer",
        action="append",
        help="Extra peer in form host:client_port:repl_port (can be repeated)",
    )
    args = parser.parse_args()

    # Build peer list from defaults + CLI extras
    peers = list(config.PEERS)
    if args.peer:
        for peer_str in args.peer:
            parts = peer_str.split(":")
            if len(parts) == 3:
                peers.append((parts[0], int(parts[1]), int(parts[2])))

    # Ensure storage dir is unique per port so two servers on same machine
    # don't collide unless explicitly told to share.
    storage_root = args.storage

    server = FileServer(
        storage_root=storage_root,
        client_host=args.client_host,
        client_port=args.client_port,
        repl_host=args.repl_host,
        repl_port=args.repl_port,
        peers=peers,
    )
    server.start()


if __name__ == "__main__":
    main()
