#!/usr/bin/env python3
"""
sync.py — trigger a one-shot on-demand iCloud sync in the running driver.

Sends SIGUSR1 to the running icloud.service process, then waits for the
completion marker written by the driver when the crawl finishes.  Exits
when the sync is done or if it times out.

Usage:
  ./icloudctl sync [--timeout SECONDS] [--quiet]
  .venv/bin/python sync.py [--timeout SECONDS] [--quiet]

Exit codes:
  0  Sync completed successfully
  1  Sync timed out or driver did not respond
  2  Could not locate running driver process
"""

import argparse
import os
import subprocess
import sys
import time


STATE_DIR = os.path.expanduser("~/.local/state/icloud-linux")
MARKER_FILE = os.path.join(STATE_DIR, "sync_done")
DEFAULT_TIMEOUT = 900  # seconds (15 min — iCloud crawl of 19k+ entries takes several minutes)


def get_driver_pid():
    """Return PID of the running icloud.service process, or None."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "icloud.service", "--property=MainPID", "--value"],
            capture_output=True,
            text=True,
            check=True,
        )
        pid_str = result.stdout.strip()
        pid = int(pid_str)
        return pid if pid > 0 else None
    except (subprocess.CalledProcessError, ValueError):
        return None


def send_sigusr1(pid):
    """Send SIGUSR1 to the driver process."""
    import signal
    os.kill(pid, signal.SIGUSR1)


def main():
    parser = argparse.ArgumentParser(
        description="Trigger an on-demand iCloud sync in the running driver."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Seconds to wait for sync completion (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output.",
    )
    args = parser.parse_args()

    def log(msg):
        if not args.quiet:
            print(msg, flush=True)

    # ── find driver ──────────────────────────────────────────────────────────
    pid = get_driver_pid()
    if pid is None:
        print("ERROR: icloud.service is not running. Start it with: icloudctl start", file=sys.stderr)
        sys.exit(2)

    log(f"icloud.service PID: {pid}")

    # ── remove stale marker so we can detect a fresh completion ─────────────
    try:
        os.remove(MARKER_FILE)
    except FileNotFoundError:
        pass

    # ── send signal ──────────────────────────────────────────────────────────
    log("Sending sync signal to driver...")
    try:
        send_sigusr1(pid)
    except ProcessLookupError:
        print("ERROR: Driver process not found (PID may be stale).", file=sys.stderr)
        sys.exit(2)
    except PermissionError:
        print("ERROR: Permission denied sending signal to driver.", file=sys.stderr)
        sys.exit(2)

    # ── wait for completion marker ───────────────────────────────────────────
    log(f"Waiting for sync to complete (timeout: {args.timeout}s)...")
    deadline = time.time() + args.timeout
    poll_interval = 2

    while time.time() < deadline:
        if os.path.exists(MARKER_FILE):
            # Read the timestamp inside to confirm it's a fresh marker
            try:
                with open(MARKER_FILE) as fh:
                    ts = float(fh.read().strip())
                # Allow 5s clock skew
                if ts >= time.time() - (args.timeout + 5):
                    elapsed = int(time.time() - (deadline - args.timeout))
                    log(f"Sync complete in ~{elapsed}s.")
                    sys.exit(0)
            except (ValueError, OSError):
                pass
        time.sleep(poll_interval)

    print(
        f"ERROR: Sync did not complete within {args.timeout}s. "
        "Check logs with: icloudctl logs",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
