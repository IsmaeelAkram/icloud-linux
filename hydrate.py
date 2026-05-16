#!/usr/bin/env python3
"""
hydrate.py — force local hydration of all files under sync_paths,
respecting the exclude_paths deny-list from config.yaml.

Reads config.yaml for sync_paths and exclude_paths so the same rules
that govern the FUSE driver apply here — single source of truth.

Opens each unhydrated file through the FUSE mount (not the mirror cache)
so the driver's lazy-download machinery does the actual pull.  Blocks
until all eligible files are hydrated.  Safe to interrupt and re-run.

Usage:
  ./icloudctl hydrate [--config PATH] [--dry-run] [--verbose]
  .venv/bin/python hydrate.py [--config PATH] [--dry-run] [--verbose]

Exit codes:
  0  All eligible files hydrated successfully
  1  One or more files failed to hydrate (logged to stderr)
  2  Configuration or mount error
"""

import argparse
import os
import sqlite3
import sys
import time
import yaml


# ── path filtering (mirrors ICloudSyncEngine._path_allowed) ──────────────────

def _normalise(paths):
    """Ensure all paths start with /."""
    if not paths:
        return []
    return [p if p.startswith("/") else "/" + p for p in paths]


def path_allowed(path, sync_paths, exclude_paths):
    """Return True if this path should be hydrated.

    Mirrors ICloudSyncEngine._path_allowed() exactly:
      1. exclude_paths deny-list wins — matching prefix blocks hydration.
      2. sync_paths allow-list — if set, only matching prefixes hydrate.
         None means allow all (minus exclusions).
    """
    # 1. Deny-list
    for prefix in exclude_paths:
        if path == prefix or path.startswith(prefix + "/"):
            return False

    # 2. Allow-list
    if sync_paths is None:
        return True
    for prefix in sync_paths:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


# ── config ────────────────────────────────────────────────────────────────────

def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_unhydrated(db_path, sync_paths, exclude_paths):
    """Return list of (icloud_path, size) for all eligible unhydrated files."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT path, size FROM entries
        WHERE type = 'file'
          AND hydrated = 0
          AND tombstone = 0
        ORDER BY path
        """
    ).fetchall()
    conn.close()
    return [
        (row["path"], row["size"])
        for row in rows
        if path_allowed(row["path"], sync_paths, exclude_paths)
    ]


def get_hydrated_count(db_path, sync_paths, exclude_paths):
    """Count already-hydrated eligible files (for progress display)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT path FROM entries
        WHERE type = 'file'
          AND hydrated = 1
          AND tombstone = 0
        """
    ).fetchall()
    conn.close()
    return sum(
        1 for row in rows
        if path_allowed(row["path"], sync_paths, exclude_paths)
    )


# ── hydration ─────────────────────────────────────────────────────────────────

def hydrate_file(mount_path, icloud_path, verbose):
    """Open the file through the FUSE mount to trigger hydration.

    We read just the first byte — enough to force the driver to pull the
    full file before returning.  For zero-byte files an open() alone is
    sufficient.
    """
    local = mount_path.rstrip("/") + icloud_path
    try:
        with open(local, "rb") as f:
            f.read(1)
        return True
    except OSError as exc:
        if verbose:
            print(f"  WARN: {icloud_path}: {exc}", file=sys.stderr)
        return False


def format_size(n_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} TB"


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Force local hydration of all eligible iCloud files."
    )
    parser.add_argument(
        "--config",
        default=os.path.expanduser("~/.config/icloud-linux/config.yaml"),
        help="Path to config.yaml (default: ~/.config/icloud-linux/config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be hydrated without downloading anything.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each file as it is hydrated.",
    )
    args = parser.parse_args()

    # ── load config ──────────────────────────────────────────────────────────
    if not os.path.exists(args.config):
        print(f"ERROR: config not found: {args.config}", file=sys.stderr)
        sys.exit(2)

    cfg = load_config(args.config)
    sync_paths    = _normalise(cfg.get("sync_paths"))    or None  # None = allow all
    exclude_paths = _normalise(cfg.get("exclude_paths") or [])
    cache_dir     = os.path.expanduser(cfg.get("cache_dir", "~/.cache/icloud-linux"))
    db_path       = os.path.join(cache_dir, "state.sqlite3")

    # Mount dir: prefer config key, fall back to icloud.env, then default ~/iCloud
    mount_dir = cfg.get("mount_dir")
    if not mount_dir:
        env_file = os.path.expanduser("~/.config/icloud-linux/icloud.env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ICLOUD_MOUNT="):
                        mount_dir = line.split("=", 1)[1].strip().strip('"')
                        break
    if not mount_dir:
        mount_dir = os.path.expanduser("~/iCloud")
    mount_dir = os.path.expanduser(mount_dir)

    # ── validate mount ───────────────────────────────────────────────────────
    if not os.path.isdir(mount_dir):
        print(f"ERROR: mount point not found: {mount_dir}", file=sys.stderr)
        print("Is the icloud service running?  Try: icloudctl start", file=sys.stderr)
        sys.exit(2)

    # Quick sanity check — the mount should be a FUSE mount, not just an
    # empty directory.  We check by seeing if the root lists anything.
    try:
        os.listdir(mount_dir)
    except OSError as exc:
        print(f"ERROR: cannot read mount at {mount_dir}: {exc}", file=sys.stderr)
        sys.exit(2)

    if not os.path.exists(db_path):
        print(f"ERROR: state DB not found: {db_path}", file=sys.stderr)
        sys.exit(2)

    # ── report config ────────────────────────────────────────────────────────
    print("icloud-linux hydrate")
    print(f"  Config:       {args.config}")
    print(f"  Mount:        {mount_dir}")
    print(f"  DB:           {db_path}")
    print(f"  sync_paths:   {sync_paths or '(all)'}")
    print(f"  exclude_paths:{exclude_paths or '(none)'}")
    print()

    # ── find unhydrated files ────────────────────────────────────────────────
    print("Scanning state DB for unhydrated files...", end=" ", flush=True)
    pending = get_unhydrated(db_path, sync_paths, exclude_paths)
    already_done = get_hydrated_count(db_path, sync_paths, exclude_paths)
    total_eligible = len(pending) + already_done
    total_bytes = sum(size for _, size in pending)

    print(f"done.")
    print(f"  Eligible files:  {total_eligible:,}")
    print(f"  Already local:   {already_done:,}")
    print(f"  Need download:   {len(pending):,}  ({format_size(total_bytes)})")
    print()

    if not pending:
        print("All eligible files are already hydrated. Nothing to do.")
        sys.exit(0)

    if args.dry_run:
        print("DRY RUN — files that would be hydrated:")
        for path, size in pending:
            print(f"  {format_size(size):>10}  {path}")
        sys.exit(0)

    # ── hydrate ──────────────────────────────────────────────────────────────
    print(f"Starting hydration of {len(pending):,} files...")
    print("(This will block until all files are downloaded.  Ctrl-C to pause.)\n")

    completed = 0
    failed = 0
    start = time.time()
    last_report = start

    for i, (icloud_path, size) in enumerate(pending, 1):
        if args.verbose:
            print(f"  [{i}/{len(pending)}] {icloud_path} ({format_size(size)})")

        ok = hydrate_file(mount_dir, icloud_path, args.verbose)
        if ok:
            completed += 1
        else:
            failed += 1

        # Progress report every 25 files or every 30 seconds
        now = time.time()
        if (i % 25 == 0 or i == len(pending) or now - last_report >= 30):
            elapsed = now - start
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (len(pending) - i) / rate if rate > 0 else 0
            eta_str = f"{int(eta)}s" if eta < 120 else f"{int(eta/60)}m {int(eta%60)}s"
            print(
                f"  Progress: {i}/{len(pending)} files"
                f"  ({completed} ok, {failed} failed)"
                f"  elapsed {int(elapsed)}s"
                f"  ETA ~{eta_str}"
            )
            last_report = now

    elapsed = time.time() - start
    print()
    print(f"Hydration complete in {int(elapsed)}s.")
    print(f"  Succeeded: {completed:,}")
    print(f"  Failed:    {failed:,}")

    if failed:
        print(
            f"\nWARNING: {failed} file(s) failed to hydrate. "
            "Re-run to retry, or check logs with: icloudctl logs",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
