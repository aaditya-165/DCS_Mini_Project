"""
config.py
Shared configuration for the distributed file system.
All nodes (client, primary, backup) import settings from here.
"""

import os

# ---------------------------------------------------------------------------
# Network settings
# ---------------------------------------------------------------------------

# Hostnames / IPs used in the project.  Use 127.0.0.1 so everything can run
# on a single laptop for demo / testing purposes.
PRIMARY_HOST   = os.environ.get("DFS_PRIMARY_HOST", "127.0.0.1")
BACKUP_HOST    = os.environ.get("DFS_BACKUP_HOST",  "127.0.0.1")

# Ports
PRIMARY_CLIENT_PORT = int(os.environ.get("DFS_PRIMARY_CLIENT_PORT", 9001))
PRIMARY_REPL_PORT   = int(os.environ.get("DFS_PRIMARY_REPL_PORT",   9002))
BACKUP_CLIENT_PORT  = int(os.environ.get("DFS_BACKUP_CLIENT_PORT",  9101))
BACKUP_REPL_PORT    = int(os.environ.get("DFS_BACKUP_REPL_PORT",    9102))

# Ordered list of (host, client_port, repl_port) tuples used by the server
# for auto-discovery and by the client for failover.
PEERS = [
    (PRIMARY_HOST, PRIMARY_CLIENT_PORT, PRIMARY_REPL_PORT),
    (BACKUP_HOST,  BACKUP_CLIENT_PORT,  BACKUP_REPL_PORT),
]

# ---------------------------------------------------------------------------
# Timing / fault-tolerance settings
# ---------------------------------------------------------------------------

# How long the client waits for a response before assuming the server died.
CLIENT_TIMEOUT_SECONDS = 2.0

# Legacy demo timing used by test_scenario.py to wait after failover events.
# Crash detection is performed by the backup's persistent TCP replication
# socket closing, not by explicit heartbeat messages.
HEARTBEAT_INTERVAL_SECONDS = 1.0

# Number of wait intervals the test leaves for promotion to complete.
HEARTBEAT_MISSES_BEFORE_PROMOTION = 3

# How long a recovering server waits between reconnection attempts.
RECOVERY_RETRY_INTERVAL_SECONDS = 2.0

# ---------------------------------------------------------------------------
# Storage settings
# ---------------------------------------------------------------------------

# Base directory where each server stores its files.
# The server script overrides this via a CLI flag, but a sensible default
# is provided here so tests can import it directly.
DEFAULT_STORAGE_ROOT = os.environ.get("DFS_STORAGE_ROOT", "./dfs_storage")

# Maximum bytes to read into memory at once during state sync.
SYNC_CHUNK_SIZE = 8192

# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

# Every message ends with this delimiter (newline).  Using a simple newline
# makes it trivial to read line-oriented messages with socket.makefile().
MESSAGE_DELIMITER = b"\n"
MESSAGE_ENCODING  = "utf-8"
