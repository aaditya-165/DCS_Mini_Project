"""
test_scenario.py
Automated end-to-end test for the distributed file system.

This script:
1. Starts a PRIMARY server.
2. Starts a BACKUP server.
3. Writes a file via the client.
4. Reads it back and asserts correctness.
5. Kills the PRIMARY server.
6. Reads the file again via the client (should fail over to backup).
7. Writes another file while primary is dead.
8. Restarts the old PRIMARY server (it should become a backup and sync state).
9. Verifies a fresh client skips the recovered backup and reaches the current primary.
10. Kills the current PRIMARY (old backup) to force fail-back.
11. Reads all files from the recovered server.

Run with:
    python test_scenario.py
"""

import os
import shutil
import signal
import subprocess
import sys
import time

import config
from client import DFSClient

# Paths where the two servers will store files
STORAGE_A = "./test_storage_a"
STORAGE_B = "./test_storage_b"

# Server command templates (Python executable)
PYTHON = sys.executable

SERVER_CMD = [
    PYTHON, "server.py",
    "--storage", STORAGE_A,
    "--client-port", str(config.PRIMARY_CLIENT_PORT),
    "--repl-port",   str(config.PRIMARY_REPL_PORT),
]

SERVER_CMD_B = [
    PYTHON, "server.py",
    "--storage", STORAGE_B,
    "--client-port", str(config.BACKUP_CLIENT_PORT),
    "--repl-port",   str(config.BACKUP_REPL_PORT),
]


def log(msg: str):
    now = time.strftime("%H:%M:%S")
    print(f"[TEST {now}] {msg}", flush=True)


def wait_for_server(host: str, port: int, timeout: float = 10.0) -> bool:
    """Poll until a TCP connection to host:port succeeds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            time.sleep(0.2)
    return False


def cleanup():
    for d in (STORAGE_A, STORAGE_B):
        if os.path.isdir(d):
            shutil.rmtree(d)


def main():
    cleanup()
    os.makedirs(STORAGE_A, exist_ok=True)
    os.makedirs(STORAGE_B, exist_ok=True)

    log("=" * 60)
    log("STEP 1: Start PRIMARY server")
    log("=" * 60)
    proc_a = subprocess.Popen(
        SERVER_CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert wait_for_server("127.0.0.1", config.PRIMARY_CLIENT_PORT), "Primary client port did not open"
    assert wait_for_server("127.0.0.1", config.PRIMARY_REPL_PORT), "Primary repl port did not open"
    log("PRIMARY is up (client + repl ports ready).")

    log("=" * 60)
    log("STEP 2: Start BACKUP server")
    log("=" * 60)
    proc_b = subprocess.Popen(
        SERVER_CMD_B,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert wait_for_server("127.0.0.1", config.BACKUP_CLIENT_PORT), "Backup did not start"
    log("BACKUP is up.")
    time.sleep(5)  # Give backup time to discover primary and connect

    client = DFSClient()

    log("=" * 60)
    log("STEP 3: WRITE file via client")
    log("=" * 60)
    resp = client.write("demo.txt", "Hello from primary")
    log(f"WRITE response: {resp}")
    assert resp.startswith("OK"), f"WRITE failed: {resp}"

    log("=" * 60)
    log("STEP 4: READ file back")
    log("=" * 60)
    resp = client.read("demo.txt")
    log(f"READ response: {resp}")
    assert "Hello from primary" in resp, f"READ returned wrong data: {resp}"

    log("=" * 60)
    log("STEP 5: KILL PRIMARY server")
    log("=" * 60)
    proc_a.terminate()
    try:
        proc_a.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc_a.kill()
        proc_a.wait()
    log("PRIMARY killed.")
    time.sleep(config.HEARTBEAT_MISSES_BEFORE_PROMOTION * config.HEARTBEAT_INTERVAL_SECONDS + 1)

    log("=" * 60)
    log("STEP 6: READ file from BACKUP (failover)")
    log("=" * 60)
    client2 = DFSClient()  # fresh client to ensure clean state
    resp = client2.read("demo.txt")
    log(f"READ response: {resp}")
    assert "Hello from primary" in resp, f"Failover READ failed: {resp}"

    log("=" * 60)
    log("STEP 7: WRITE new file while primary is dead")
    log("=" * 60)
    resp = client2.write("after_crash.txt", "Written while primary was dead")
    log(f"WRITE response: {resp}")
    assert resp.startswith("OK"), f"WRITE after crash failed: {resp}"

    log("=" * 60)
    log("STEP 8: Restart old PRIMARY (should become backup + sync)")
    log("=" * 60)
    proc_a2 = subprocess.Popen(
        SERVER_CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert wait_for_server("127.0.0.1", config.PRIMARY_CLIENT_PORT), "Old primary did not restart"
    log("Old PRIMARY restarted. Waiting for sync...")
    time.sleep(3)

    log("=" * 60)
    log("STEP 9: READ/WRITE while Host A is BACKUP and Host B is PRIMARY")
    log("=" * 60)
    client_after_recovery = DFSClient()
    resp = client_after_recovery.read("demo.txt")
    log(f"READ via current primary after recovery: {resp}")
    assert "Hello from primary" in resp, f"Client did not skip recovered backup: {resp}"
    resp = client_after_recovery.write("after_recovery.txt", "Written while Host A was backup")
    log(f"WRITE via current primary after recovery: {resp}")
    assert resp.startswith("OK"), f"WRITE with Host B as primary failed: {resp}"

    log("=" * 60)
    log("STEP 10: Kill current PRIMARY (old backup) to force fail-back")
    log("=" * 60)
    proc_b.terminate()
    try:
        proc_b.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc_b.kill()
        proc_b.wait()
    log("Current PRIMARY killed.")
    time.sleep(config.HEARTBEAT_MISSES_BEFORE_PROMOTION * config.HEARTBEAT_INTERVAL_SECONDS + 2)

    log("=" * 60)
    log("STEP 11: READ all files from recovered server")
    log("=" * 60)
    client3 = DFSClient()
    resp1 = client3.read("demo.txt")
    resp2 = client3.read("after_crash.txt")
    resp3 = client3.read("after_recovery.txt")
    log(f"READ demo.txt: {resp1}")
    log(f"READ after_crash.txt: {resp2}")
    log(f"READ after_recovery.txt: {resp3}")
    assert "Hello from primary" in resp1, f"Recovered server missing demo.txt: {resp1}"
    assert "Written while primary was dead" in resp2, f"Recovered server missing after_crash.txt: {resp2}"
    assert "Written while Host A was backup" in resp3, f"Recovered server missing after_recovery.txt: {resp3}"

    log("=" * 60)
    log("ALL TESTS PASSED")
    log("=" * 60)

    # Cleanup processes
    for proc in (proc_a2,):
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    cleanup()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        log(f"TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
