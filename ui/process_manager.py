from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config
from storage import Storage

from .state import event_bus


@dataclass(frozen=True)
class NodeConfig:
    name: str
    label: str
    storage: Path
    client_port: int
    repl_port: int


class ProcessManager:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.nodes = {
            "primary": NodeConfig(
                "primary",
                "Primary Server",
                project_root / "ui_storage_primary",
                config.PRIMARY_CLIENT_PORT,
                config.PRIMARY_REPL_PORT,
            ),
            "backup": NodeConfig(
                "backup",
                "Backup Server",
                project_root / "ui_storage_backup",
                config.BACKUP_CLIENT_PORT,
                config.BACKUP_REPL_PORT,
            ),
        }
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._roles = {name: "DOWN" for name in self.nodes}
        self._lock = threading.Lock()

    def start(self, node_name: str) -> dict[str, Any]:
        node = self._node(node_name)
        with self._lock:
            proc = self._processes.get(node.name)
            if proc and proc.poll() is None:
                return self.node_status(node.name)

            busy_ports = [
                port for port in (node.client_port, node.repl_port)
                if not self._port_available(port)
            ]
            if busy_ports:
                ports = ", ".join(str(port) for port in busy_ports)
                event_bus.emit("process", f"{node.label} cannot start; port busy: {ports}", node.name)
                return self.node_status(node.name)

            if node.name == "backup":
                primary = self._processes.get("primary")
                if primary and primary.poll() is None:
                    self._wait_for_role("primary", {"PRIMARY"}, timeout=6.0)

            node.storage.mkdir(parents=True, exist_ok=True)
            self._roles[node.name] = "STARTING"
            cmd = [
                sys.executable,
                "server.py",
                "--storage",
                str(node.storage),
                "--client-port",
                str(node.client_port),
                "--repl-port",
                str(node.repl_port),
            ]
            proc = subprocess.Popen(
                cmd,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._processes[node.name] = proc
            event_bus.emit("process", f"{node.label} started", node.name)
            threading.Thread(
                target=self._stream_output,
                args=(node, proc),
                daemon=True,
            ).start()
        return self.node_status(node.name)

    def stop(self, node_name: str) -> dict[str, Any]:
        node = self._node(node_name)
        with self._lock:
            proc = self._processes.get(node.name)
            if not proc or proc.poll() is not None:
                return self.node_status(node.name)
            event_bus.emit("process", f"{node.label} stopping", node.name)
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
            event_bus.emit("process", f"{node.label} killed", node.name)
        else:
            event_bus.emit("process", f"{node.label} stopped", node.name)
        return self.node_status(node.name)

    def restart(self, node_name: str) -> dict[str, Any]:
        self.stop(node_name)
        return self.start(node_name)

    def wait_for_role(self, node_name: str, roles: set[str], timeout: float = 6.0) -> str:
        return self._wait_for_role(node_name, roles, timeout)

    def status(self) -> dict[str, Any]:
        return {
            "nodes": {
                name: self.node_status(name)
                for name in self.nodes
            },
            "events": event_bus.recent(),
        }

    def node_status(self, node_name: str) -> dict[str, Any]:
        node = self._node(node_name)
        proc = self._processes.get(node.name)
        running = bool(proc and proc.poll() is None)
        if running:
            role = self._query_role(node)
        elif not self._port_available(node.client_port) or not self._port_available(node.repl_port):
            role = "PORT BUSY"
        else:
            role = "DOWN"
            self._roles[node.name] = "DOWN"
        return {
            "name": node.name,
            "label": node.label,
            "running": running,
            "role": role,
            "pid": proc.pid if running and proc else None,
            "client_port": node.client_port,
            "repl_port": node.repl_port,
            "storage": str(node.storage),
            "files": self.storage_files(node.name),
        }

    def storage_files(self, node_name: str) -> list[dict[str, Any]]:
        node = self._node(node_name)
        if not node.storage.exists():
            return []
        items = []
        for path, content in Storage(str(node.storage)).list_all_files():
            items.append({
                "path": path,
                "preview": content[:160],
                "bytes": len(content.encode("utf-8")),
            })
        return sorted(items, key=lambda item: item["path"])

    def reset_storage(self) -> dict[str, Any]:
        stopped = []
        for name in self.nodes:
            status = self.stop(name)
            stopped.append(status["name"])
        for node in self.nodes.values():
            Storage(str(node.storage)).clear_all()
            event_bus.emit("storage", f"{node.label} storage cleared", node.name)
        return {"cleared": stopped}

    def _stream_output(self, node: NodeConfig, proc: subprocess.Popen[str]) -> None:
        if not proc.stdout:
            return
        for line in proc.stdout:
            text = line.strip()
            if text:
                self._update_role_from_log(node.name, text)
                event_bus.emit("log", text, node.name)
        code = proc.poll()
        if code is not None:
            self._roles[node.name] = "DOWN"
            event_bus.emit("process", f"{node.label} exited with code {code}", node.name)

    def _query_role(self, node: NodeConfig) -> str:
        return self._roles.get(node.name, "STARTING")

    def _update_role_from_log(self, node_name: str, text: str) -> None:
        if "*** ROLE: PRIMARY ***" in text or "BACKUP PROMOTING TO PRIMARY" in text:
            self._roles[node_name] = "PRIMARY"
        elif "*** ROLE: BACKUP" in text:
            self._roles[node_name] = "BACKUP"

    def _port_available(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def _wait_for_role(self, node_name: str, roles: set[str], timeout: float) -> str:
        node = self._node(node_name)
        deadline = time.time() + timeout
        last_role = "STARTING"
        while time.time() < deadline:
            last_role = self._query_role(node)
            if last_role in roles:
                return last_role
            time.sleep(0.2)
        return last_role

    def _node(self, node_name: str) -> NodeConfig:
        try:
            return self.nodes[node_name]
        except KeyError:
            raise ValueError(f"Unknown node: {node_name}") from None
