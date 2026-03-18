#!/usr/bin/env python3

import atexit
import datetime
import errno
import hashlib
import json
import logging
import os
import signal
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import fuse
import yaml
from fuse import Fuse
from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloud2SARequiredException,
    PyiCloudAPIResponseException,
    PyiCloudAuthRequiredException,
    PyiCloudFailedLoginException,
)
from pyicloud.services.drive import DriveNode


if not hasattr(fuse, "__version__"):
    fuse.__version__ = "0.2"

fuse.fuse_python_api = (0, 2)


ROOT_DRIVEWSID = "FOLDER::com.apple.CloudDocs::root"


class Stat(fuse.Stat):
    def __init__(self):
        self.st_mode = 0
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 0
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0


class IgnoreIcdrsWarning(logging.Filter):
    def filter(self, record):
        return "ICDRS is not disabled; requestWebAccessState=" not in record.getMessage()


def parse_remote_time(value):
    if not value:
        return int(time.time())
    try:
        parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return int(time.time())
    return int(calendar_timegm(parsed.timetuple()))


def calendar_timegm(timetuple):
    return int(datetime.datetime(*timetuple[:6], tzinfo=datetime.timezone.utc).timestamp())


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_to_dict(row):
    return dict(row) if row is not None else None


class SyncState:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    path TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    parent_path TEXT NOT NULL,
                    remote_drivewsid TEXT,
                    remote_docwsid TEXT,
                    remote_etag TEXT,
                    remote_zone TEXT,
                    remote_shareid TEXT,
                    size INTEGER NOT NULL DEFAULT 0,
                    mtime INTEGER NOT NULL DEFAULT 0,
                    hydrated INTEGER NOT NULL DEFAULT 0,
                    dirty INTEGER NOT NULL DEFAULT 0,
                    tombstone INTEGER NOT NULL DEFAULT 0,
                    local_sha256 TEXT,
                    last_synced_at INTEGER,
                    synced_path TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_entries_remote_drivewsid
                    ON entries(remote_drivewsid);
                CREATE INDEX IF NOT EXISTS idx_entries_dirty
                    ON entries(dirty, tombstone);
                CREATE TABLE IF NOT EXISTS pending_ops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op TEXT NOT NULL,
                    path TEXT NOT NULL,
                    target_path TEXT,
                    queued_at INTEGER NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                """
            )
            columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(entries)").fetchall()
            }
            if "remote_shareid" not in columns:
                self.conn.execute("ALTER TABLE entries ADD COLUMN remote_shareid TEXT")
            self.conn.commit()

    def upsert_entry(self, entry):
        payload = {
            "path": entry["path"],
            "type": entry["type"],
            "parent_path": entry["parent_path"],
            "remote_drivewsid": entry.get("remote_drivewsid"),
            "remote_docwsid": entry.get("remote_docwsid"),
            "remote_etag": entry.get("remote_etag"),
            "remote_zone": entry.get("remote_zone"),
            "remote_shareid": self._encode_shareid(entry.get("remote_shareid")),
            "size": int(entry.get("size", 0) or 0),
            "mtime": int(entry.get("mtime", 0) or 0),
            "hydrated": int(bool(entry.get("hydrated", False))),
            "dirty": int(bool(entry.get("dirty", False))),
            "tombstone": int(bool(entry.get("tombstone", False))),
            "local_sha256": entry.get("local_sha256"),
            "last_synced_at": entry.get("last_synced_at"),
            "synced_path": entry.get("synced_path", entry["path"]),
        }
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO entries (
                    path, type, parent_path, remote_drivewsid, remote_docwsid, remote_etag,
                    remote_zone, remote_shareid, size, mtime, hydrated, dirty, tombstone, local_sha256,
                    last_synced_at, synced_path
                ) VALUES (
                    :path, :type, :parent_path, :remote_drivewsid, :remote_docwsid, :remote_etag,
                    :remote_zone, :remote_shareid, :size, :mtime, :hydrated, :dirty, :tombstone, :local_sha256,
                    :last_synced_at, :synced_path
                )
                ON CONFLICT(path) DO UPDATE SET
                    type = excluded.type,
                    parent_path = excluded.parent_path,
                    remote_drivewsid = excluded.remote_drivewsid,
                    remote_docwsid = excluded.remote_docwsid,
                    remote_etag = excluded.remote_etag,
                    remote_zone = excluded.remote_zone,
                    remote_shareid = excluded.remote_shareid,
                    size = excluded.size,
                    mtime = excluded.mtime,
                    hydrated = excluded.hydrated,
                    dirty = excluded.dirty,
                    tombstone = excluded.tombstone,
                    local_sha256 = excluded.local_sha256,
                    last_synced_at = excluded.last_synced_at,
                    synced_path = excluded.synced_path
                """,
                payload,
            )
            self.conn.commit()

    def get_entry(self, path):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM entries WHERE path = ?",
                (path,),
            ).fetchone()
        return self._decode_entry(row_to_dict(row))

    def get_entry_by_remote_id(self, remote_drivewsid):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM entries WHERE remote_drivewsid = ?",
                (remote_drivewsid,),
            ).fetchone()
        return self._decode_entry(row_to_dict(row))

    def list_entries(self):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM entries ORDER BY path").fetchall()
        return [self._decode_entry(dict(row)) for row in rows]

    def count_entries(self):
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS count FROM entries").fetchone()
        return int(row["count"])

    def list_unhydrated_paths(self):
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT path FROM entries
                WHERE type = 'file' AND tombstone = 0 AND hydrated = 0
                ORDER BY path
                """
            ).fetchall()
        return [row["path"] for row in rows]

    def list_dirty_entries(self):
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT * FROM entries
                WHERE dirty = 1 OR tombstone = 1
                ORDER BY path
                """
            ).fetchall()
        return [self._decode_entry(dict(row)) for row in rows]

    def mark_hydrated(self, path, local_sha256=None, size=None, mtime=None):
        with self.lock:
            self.conn.execute(
                """
                UPDATE entries
                SET hydrated = 1,
                    local_sha256 = COALESCE(?, local_sha256),
                    size = COALESCE(?, size),
                    mtime = COALESCE(?, mtime)
                WHERE path = ?
                """,
                (local_sha256, size, mtime, path),
            )
            self.conn.commit()

    def mark_dirty(self, path, size=None, mtime=None, hydrated=None, local_sha256=None):
        with self.lock:
            self.conn.execute(
                """
                UPDATE entries
                SET dirty = 1,
                    tombstone = 0,
                    size = COALESCE(?, size),
                    mtime = COALESCE(?, mtime),
                    hydrated = COALESCE(?, hydrated),
                    local_sha256 = COALESCE(?, local_sha256)
                WHERE path = ?
                """,
                (size, mtime, hydrated, local_sha256, path),
            )
            self.conn.commit()

    def mark_tombstone(self, path):
        with self.lock:
            self.conn.execute(
                """
                UPDATE entries
                SET tombstone = 1,
                    dirty = 1
                WHERE path = ?
                """,
                (path,),
            )
            self.conn.commit()

    def mark_clean(self, path, remote_meta=None, local_sha256=None):
        remote_meta = remote_meta or {}
        with self.lock:
            self.conn.execute(
                """
                UPDATE entries
                SET dirty = 0,
                    tombstone = 0,
                    hydrated = CASE
                        WHEN type = 'file' THEN hydrated
                        ELSE 1
                    END,
                    remote_drivewsid = COALESCE(?, remote_drivewsid),
                    remote_docwsid = COALESCE(?, remote_docwsid),
                    remote_etag = COALESCE(?, remote_etag),
                    remote_zone = COALESCE(?, remote_zone),
                    size = COALESCE(?, size),
                    mtime = COALESCE(?, mtime),
                    local_sha256 = COALESCE(?, local_sha256),
                    last_synced_at = ?,
                    synced_path = path
                WHERE path = ?
                """,
                (
                    remote_meta.get("remote_drivewsid"),
                    remote_meta.get("remote_docwsid"),
                    remote_meta.get("remote_etag"),
                    remote_meta.get("remote_zone"),
                    remote_meta.get("size"),
                    remote_meta.get("mtime"),
                    local_sha256,
                    int(time.time()),
                    path,
                ),
            )
            self.conn.execute(
                "DELETE FROM pending_ops WHERE path = ? OR target_path = ?",
                (path, path),
            )
            self.conn.commit()

    def remove_entry(self, path):
        with self.lock:
            self.conn.execute("DELETE FROM entries WHERE path = ?", (path,))
            self.conn.execute(
                "DELETE FROM pending_ops WHERE path = ? OR target_path = ?",
                (path, path),
            )
            self.conn.commit()

    def remove_subtree(self, path):
        prefix = path.rstrip("/") + "/"
        with self.lock:
            self.conn.execute(
                "DELETE FROM entries WHERE path = ? OR path LIKE ?",
                (path, prefix + "%"),
            )
            self.conn.execute(
                "DELETE FROM pending_ops WHERE path = ? OR path LIKE ? OR target_path = ? OR target_path LIKE ?",
                (path, prefix + "%", path, prefix + "%"),
            )
            self.conn.commit()

    def rename_tree(self, oldpath, newpath, root_dirty=True, update_synced=False):
        entries = self._fetch_subtree(oldpath)
        if not entries:
            return
        prefix = oldpath.rstrip("/") + "/"
        with self.lock:
            for entry in entries:
                current = entry["path"]
                suffix = "" if current == oldpath else current[len(prefix) :]
                updated = newpath if not suffix else newpath.rstrip("/") + "/" + suffix
                updated_parent = os.path.dirname(updated) or "/"
                dirty = 1 if (root_dirty and current == oldpath) else entry["dirty"]
                self.conn.execute(
                    """
                    UPDATE entries
                    SET path = ?,
                        parent_path = ?,
                        dirty = ?,
                        synced_path = CASE
                            WHEN ? = 1 AND synced_path = ? THEN ?
                            WHEN ? = 1 AND synced_path LIKE ? THEN ? || substr(synced_path, ?)
                            ELSE synced_path
                        END
                    WHERE path = ?
                    """,
                    (
                        updated,
                        updated_parent,
                        dirty,
                        int(update_synced),
                        oldpath,
                        newpath,
                        int(update_synced),
                        prefix + "%",
                        newpath.rstrip("/") + "/",
                        len(prefix) + 1,
                        current,
                    ),
                )
            self.conn.execute(
                """
                UPDATE pending_ops
                SET path = CASE
                    WHEN path = ? THEN ?
                    WHEN path LIKE ? THEN ? || substr(path, ?)
                    ELSE path
                END,
                target_path = CASE
                    WHEN target_path = ? THEN ?
                    WHEN target_path LIKE ? THEN ? || substr(target_path, ?)
                    ELSE target_path
                END
                """,
                (
                    oldpath,
                    newpath,
                    prefix + "%",
                    newpath.rstrip("/") + "/",
                    len(prefix) + 1,
                    oldpath,
                    newpath,
                    prefix + "%",
                    newpath.rstrip("/") + "/",
                    len(prefix) + 1,
                ),
            )
            self.conn.commit()

    def mark_synced_subtree(self, path):
        prefix = path.rstrip("/") + "/"
        with self.lock:
            self.conn.execute(
                """
                UPDATE entries
                SET synced_path = path,
                    dirty = CASE
                        WHEN path = ? THEN 0
                        ELSE dirty
                    END,
                    tombstone = CASE
                        WHEN path = ? THEN 0
                        ELSE tombstone
                    END,
                    last_synced_at = ?
                WHERE path = ? OR path LIKE ?
                """,
                (path, path, int(time.time()), path, prefix + "%"),
            )
            self.conn.commit()

    def detach_subtree_as_conflict(self, oldpath, newpath):
        entries = self._fetch_subtree(oldpath)
        if not entries:
            return
        prefix = oldpath.rstrip("/") + "/"
        with self.lock:
            for entry in entries:
                current = entry["path"]
                suffix = "" if current == oldpath else current[len(prefix) :]
                updated = newpath if not suffix else newpath.rstrip("/") + "/" + suffix
                updated_parent = os.path.dirname(updated) or "/"
                self.conn.execute(
                    """
                    UPDATE entries
                    SET path = ?,
                        parent_path = ?,
                        remote_drivewsid = NULL,
                        remote_docwsid = NULL,
                        remote_etag = NULL,
                        remote_zone = NULL,
                        remote_shareid = NULL,
                        synced_path = NULL,
                        dirty = 1,
                        tombstone = 0
                    WHERE path = ?
                    """,
                    (updated, updated_parent, current),
                )
            self.conn.commit()

    def clear_remote_identity(self, path):
        with self.lock:
            self.conn.execute(
                """
                UPDATE entries
                SET remote_drivewsid = NULL,
                    remote_docwsid = NULL,
                    remote_etag = NULL,
                    remote_zone = NULL,
                    remote_shareid = NULL,
                    synced_path = NULL,
                    dirty = 1,
                    tombstone = 0
                WHERE path = ?
                """,
                (path,),
            )
            self.conn.commit()

    def queue_op(self, op, path, target_path=None):
        now = int(time.time())
        with self.lock:
            if op == "delete":
                existing_create = self.conn.execute(
                    "SELECT id FROM pending_ops WHERE path = ? AND op IN ('create', 'mkdir')",
                    (path,),
                ).fetchone()
                if existing_create:
                    self.conn.execute("DELETE FROM pending_ops WHERE path = ?", (path,))
                    self.conn.commit()
                    return
            self.conn.execute(
                """
                INSERT INTO pending_ops (op, path, target_path, queued_at)
                VALUES (?, ?, ?, ?)
                """,
                (op, path, target_path, now),
            )
            self.conn.commit()

    def _fetch_subtree(self, path):
        prefix = path.rstrip("/") + "/"
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT * FROM entries
                WHERE path = ? OR path LIKE ?
                ORDER BY LENGTH(path) ASC, path ASC
                """,
                (path, prefix + "%"),
            ).fetchall()
        return [self._decode_entry(dict(row)) for row in rows]

    def _encode_shareid(self, shareid):
        if not shareid:
            return None
        return json.dumps(shareid, sort_keys=True)

    def _decode_entry(self, entry):
        if entry is None:
            return None
        shareid = entry.get("remote_shareid")
        if isinstance(shareid, str) and shareid:
            try:
                entry["remote_shareid"] = json.loads(shareid)
            except json.JSONDecodeError:
                entry["remote_shareid"] = None
        return entry


class LocalMirror:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.root = os.path.join(cache_dir, "mirror")
        self.tmp_dir = os.path.join(cache_dir, "tmp")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)

    def local_path(self, path):
        normalized = os.path.normpath(path)
        if normalized == ".":
            normalized = "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        relative = normalized.lstrip("/")
        local = os.path.abspath(os.path.join(self.root, relative))
        if local != self.root and not local.startswith(self.root + os.sep):
            raise ValueError(f"Path escapes mirror root: {path}")
        return local

    def ensure_dir(self, path):
        os.makedirs(self.local_path(path), exist_ok=True)

    def ensure_parent(self, path):
        parent = os.path.dirname(path) or "/"
        os.makedirs(self.local_path(parent), exist_ok=True)

    def materialize_placeholder(self, path, size, mtime):
        local = self.local_path(path)
        self.ensure_parent(path)
        if os.path.isdir(local):
            shutil.rmtree(local)
        with open(local, "wb") as handle:
            handle.truncate(int(size or 0))
        os.utime(local, (mtime, mtime))

    def write_atomic_bytes(self, path, content, mtime=None):
        self.ensure_parent(path)
        local = self.local_path(path)
        fd, tmp_path = tempfile.mkstemp(dir=self.tmp_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp_path, local)
            if mtime is not None:
                os.utime(local, (mtime, mtime))
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def read(self, path, size, offset):
        local = self.local_path(path)
        with open(local, "rb") as handle:
            handle.seek(offset)
            return handle.read(size)

    def write(self, path, buf, offset):
        self.ensure_parent(path)
        local = self.local_path(path)
        mode = "r+b" if os.path.exists(local) else "w+b"
        with open(local, mode) as handle:
            handle.seek(offset)
            handle.write(buf)
            handle.flush()
        return len(buf)

    def truncate(self, path, length):
        self.ensure_parent(path)
        local = self.local_path(path)
        mode = "r+b" if os.path.exists(local) else "w+b"
        with open(local, mode) as handle:
            handle.truncate(length)

    def create_file(self, path):
        self.ensure_parent(path)
        local = self.local_path(path)
        with open(local, "ab"):
            pass

    def listdir(self, path):
        return os.listdir(self.local_path(path))

    def exists(self, path):
        return os.path.exists(self.local_path(path))

    def is_dir(self, path):
        return os.path.isdir(self.local_path(path))

    def remove_file(self, path):
        os.unlink(self.local_path(path))

    def remove_dir(self, path):
        os.rmdir(self.local_path(path))

    def remove_tree(self, path):
        local = self.local_path(path)
        if os.path.isdir(local):
            shutil.rmtree(local)
        elif os.path.exists(local):
            os.unlink(local)

    def rename_path(self, oldpath, newpath):
        self.ensure_parent(newpath)
        os.replace(self.local_path(oldpath), self.local_path(newpath))

    def stat_local(self, path):
        return os.lstat(self.local_path(path))

    def statvfs(self):
        return os.statvfs(self.root)

    def set_mtime(self, path, mtime):
        local = self.local_path(path)
        os.utime(local, (mtime, mtime))

    def file_sha256(self, path):
        return sha256_file(self.local_path(path))


class ICloudSyncEngine:
    def __init__(
        self,
        api,
        mirror,
        state,
        logger,
        warmup_mode="background",
        conflict_mode="copy",
        upload_interval_seconds=30,
        remote_refresh_interval_seconds=300,
        warmup_workers=1,
    ):
        self.api = api
        self.mirror = mirror
        self.state = state
        self.logger = logger
        self.warmup_mode = warmup_mode if warmup_mode in {"background", "lazy"} else "background"
        self.conflict_mode = conflict_mode if conflict_mode in {"copy"} else "copy"
        self.upload_interval_seconds = upload_interval_seconds
        self.remote_refresh_interval_seconds = remote_refresh_interval_seconds
        self.warmup_workers = max(1, int(warmup_workers))
        self.executor = ThreadPoolExecutor(max_workers=self.warmup_workers, thread_name_prefix="warmup")
        self.stop_event = threading.Event()
        self.path_locks = {}
        self.path_locks_lock = threading.Lock()
        self.scheduled_downloads = set()
        self.downloads_lock = threading.Lock()
        self.download_retry_attempts = {}
        self.download_retry_timers = {}
        self.threads = []
        self.hydration_total = 0
        self.hydration_completed = 0
        self.hydration_progress_lock = threading.Lock()
        self.shutdown_lock = threading.Lock()
        self.is_shutdown = False
        # PyiCloud downloads appear sensitive to concurrent use of one session.
        self.download_semaphore = threading.Semaphore(1)

    def _log_sync(self, event, level=logging.INFO, **fields):
        details = " ".join(f"{key}={value!r}" for key, value in fields.items() if value is not None)
        if details:
            self.logger.log(level, "sync %s %s", event, details)
            return
        self.logger.log(level, "sync %s", event)

    def start(self):
        if self.has_persistent_cache():
            self.logger.info("Using persistent local cache from %s", self.mirror.root)
            self._reconcile_persistent_cache()
            if self.warmup_mode == "background":
                self._schedule_all_unhydrated()
        else:
            self.logger.info("Persistent cache not initialized yet; performing first remote crawl")
            self.initial_scan()
            if self.warmup_mode == "background":
                self._schedule_all_unhydrated()
        self._start_background_threads()

    def _start_background_threads(self):
        upload_thread = threading.Thread(target=self._upload_loop, name="icloud-upload", daemon=True)
        refresh_thread = threading.Thread(target=self._refresh_loop, name="icloud-refresh", daemon=True)
        upload_thread.start()
        refresh_thread.start()
        self.threads.extend([upload_thread, refresh_thread])

    def shutdown(self):
        with self.shutdown_lock:
            if self.is_shutdown:
                return
            self.is_shutdown = True
            self.stop_event.set()
            with self.downloads_lock:
                timers = list(self.download_retry_timers.values())
                self.download_retry_timers.clear()
                self.scheduled_downloads.clear()
            for timer in timers:
                timer.cancel()
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self.executor.shutdown(wait=False)
            for thread in list(self.threads):
                thread.join(timeout=1)

    def has_persistent_cache(self):
        return self.state.count_entries() > 0 and os.path.isdir(self.mirror.root)

    def initial_scan(self):
        snapshot = self._crawl_remote_snapshot()
        self._apply_remote_snapshot(snapshot)

    def _reconcile_persistent_cache(self):
        entries = self.state.list_entries()
        missing_files = 0
        recreated_dirs = 0

        for entry in entries:
            path = entry["path"]
            if entry["tombstone"]:
                continue
            if entry["type"] == "folder":
                if not self.mirror.exists(path):
                    self.mirror.ensure_dir(path)
                    recreated_dirs += 1
                continue

            if self.mirror.exists(path):
                stats = self.mirror.stat_local(path)
                checksum = entry.get("local_sha256")
                hydrated = bool(entry["hydrated"])
                if entry["type"] == "file" and (hydrated or not entry["remote_drivewsid"]):
                    checksum = self.mirror.file_sha256(path)
                    hydrated = True
                self.state.upsert_entry(
                    {
                        **entry,
                        "size": stats.st_size,
                        "mtime": int(stats.st_mtime),
                        "hydrated": hydrated,
                        "local_sha256": checksum,
                    }
                )
                continue

            missing_files += 1
            if entry["remote_drivewsid"]:
                self.mirror.materialize_placeholder(path, entry["size"], entry["mtime"])
                self.state.upsert_entry({**entry, "hydrated": entry["size"] == 0})
            else:
                self.mirror.create_file(path)
                stats = self.mirror.stat_local(path)
                checksum = self.mirror.file_sha256(path)
                self.state.upsert_entry(
                    {
                        **entry,
                        "size": stats.st_size,
                        "mtime": int(stats.st_mtime),
                        "hydrated": True,
                        "local_sha256": checksum,
                    }
                )

        self.logger.info(
            "Persistent cache ready: %s entries, %s directories recreated, %s files queued for hydration",
            len(entries),
            recreated_dirs,
            missing_files,
        )

    def ensure_local_file(self, path):
        entry = self.state.get_entry(path)
        if not entry or entry["type"] != "file" or entry["tombstone"]:
            return
        if entry["hydrated"] and self.mirror.exists(path):
            return

        lock = self._path_lock(path)
        with lock:
            entry = self.state.get_entry(path)
            if not entry or entry["type"] != "file" or entry["tombstone"]:
                return
            if entry["hydrated"] and self.mirror.exists(path):
                return
            if not entry["remote_drivewsid"]:
                self._log_sync("hydrate-local", level=logging.DEBUG, path=path)
                if not self.mirror.exists(path):
                    self.mirror.create_file(path)
                checksum = self.mirror.file_sha256(path)
                stats = self.mirror.stat_local(path)
                self.state.mark_hydrated(path, checksum, stats.st_size, int(stats.st_mtime))
                self._log_sync(
                    "hydrate-complete",
                    level=logging.INFO,
                    path=path,
                    source="local",
                    size=stats.st_size,
                )
                return

            self._log_sync(
                "hydrate-start",
                level=logging.INFO,
                path=path,
                drivewsid=entry.get("remote_drivewsid"),
                size=entry.get("size"),
            )
            self.logger.debug("Hydrating %s", path)
            with self.download_semaphore:
                self.logger.debug(
                    "Hydrating file path=%s drivewsid=%s docwsid=%s zone=%s size=%s",
                    path,
                    entry.get("remote_drivewsid"),
                    entry.get("remote_docwsid"),
                    entry.get("remote_zone"),
                    entry.get("size"),
                )
                node = self._node_from_entry(entry)
                content = node.open(stream=True).raw.read()
            self.mirror.write_atomic_bytes(path, content, entry["mtime"])
            stats = self.mirror.stat_local(path)
            checksum = self.mirror.file_sha256(path)
            self.state.mark_hydrated(path, checksum, stats.st_size, int(stats.st_mtime))
            self._log_sync("hydrate-complete", level=logging.INFO, path=path, source="remote", size=stats.st_size)

    def _crawl_remote_snapshot(self):
        self.logger.info("Starting remote metadata crawl")
        snapshot = {}
        queue = deque()
        root = self.api.drive.root
        queue.append((root, "/"))
        started_at = time.time()
        last_progress_log = started_at
        scanned_folders = 0

        while queue:
            node, path = queue.popleft()
            scanned_folders += 1
            try:
                children = node.get_children(force=True)
            except Exception as exc:
                self.logger.error("Failed to enumerate %s: %s", path, exc)
                continue

            for child in children:
                child_path = "/" + child.name if path == "/" else path.rstrip("/") + "/" + child.name
                meta = self._node_to_meta(child, child_path)
                snapshot[meta["remote_drivewsid"]] = meta
                if meta["type"] == "folder":
                    queue.append((child, child_path))

            now = time.time()
            if scanned_folders == 1 or scanned_folders % 25 == 0 or now - last_progress_log >= 5:
                self.logger.info(
                    "Remote metadata crawl progress: %s folders scanned, %s entries discovered, %s folders queued",
                    scanned_folders,
                    len(snapshot),
                    len(queue),
                )
                last_progress_log = now

        self.logger.info(
            "Remote metadata crawl complete: %s entries across %s folders in %.1fs",
            len(snapshot),
            scanned_folders,
            time.time() - started_at,
        )
        return snapshot

    def _apply_remote_snapshot(self, snapshot):
        remote_ids = set(snapshot.keys())

        for meta in snapshot.values():
            existing = self.state.get_entry_by_remote_id(meta["remote_drivewsid"])
            if existing and existing["dirty"] and self._entry_conflicts(existing, meta):
                self._resolve_conflict(existing)
                existing = None

            if existing is None:
                path_entry = self.state.get_entry(meta["path"])
                if path_entry and path_entry["dirty"]:
                    self._resolve_conflict(path_entry)
                self._materialize_remote_entry(meta)
                continue

            if existing["dirty"]:
                continue

            self._refresh_clean_entry(existing, meta)

        for entry in self.state.list_entries():
            remote_id = entry["remote_drivewsid"]
            if not remote_id or remote_id in remote_ids:
                continue
            if entry["dirty"]:
                self.logger.warning("Remote deleted dirty path %s; keeping local copy for upload", entry["path"])
                self.state.clear_remote_identity(entry["path"])
                continue
            self.logger.info("Removing clean path deleted remotely: %s", entry["path"])
            self.mirror.remove_tree(entry["path"])
            self.state.remove_subtree(entry["path"])

    def _materialize_remote_entry(self, meta):
        local_path = meta["path"]
        self._log_sync(
            "remote-materialize",
            path=local_path,
            entry_type=meta["type"],
            drivewsid=meta.get("remote_drivewsid"),
            size=meta.get("size"),
        )
        if meta["type"] == "folder":
            self.mirror.ensure_dir(local_path)
            hydrated = True
        else:
            self.mirror.materialize_placeholder(local_path, meta["size"], meta["mtime"])
            hydrated = meta["size"] == 0
        self.state.upsert_entry(
            {
                **meta,
                "hydrated": hydrated,
                "dirty": False,
                "tombstone": False,
                "synced_path": local_path,
            }
        )
        if meta["type"] == "file" and not hydrated:
            self._schedule_download(local_path)

    def _refresh_clean_entry(self, entry, meta):
        oldpath = entry["path"]
        newpath = meta["path"]
        if oldpath != newpath and self.mirror.exists(oldpath):
            self._log_sync("remote-rename", path=oldpath, target_path=newpath, entry_type=meta["type"])
            self.mirror.rename_path(oldpath, newpath)
            self.state.rename_tree(oldpath, newpath, root_dirty=False, update_synced=True)
            entry = self.state.get_entry(newpath)
        elif oldpath != newpath:
            self._log_sync("remote-rename", path=oldpath, target_path=newpath, entry_type=meta["type"])
            self.state.rename_tree(oldpath, newpath, root_dirty=False, update_synced=True)
            entry = self.state.get_entry(newpath)

        if meta["type"] == "folder":
            self.mirror.ensure_dir(newpath)
            self.state.upsert_entry(
                {
                    **meta,
                    "hydrated": True,
                    "dirty": False,
                    "tombstone": False,
                    "local_sha256": entry.get("local_sha256") if entry else None,
                    "last_synced_at": entry.get("last_synced_at") if entry else None,
                    "synced_path": newpath,
                }
            )
            return

        should_replace = (
            entry is None
            or entry["remote_etag"] != meta["remote_etag"]
            or entry["size"] != meta["size"]
            or entry["mtime"] != meta["mtime"]
        )
        hydrated = bool(entry and entry["hydrated"] and not should_replace)
        if should_replace:
            self._log_sync(
                "remote-update",
                path=newpath,
                old_etag=entry.get("remote_etag") if entry else None,
                new_etag=meta.get("remote_etag"),
                size=meta.get("size"),
            )
            self.mirror.materialize_placeholder(newpath, meta["size"], meta["mtime"])
            hydrated = meta["size"] == 0
        self.state.upsert_entry(
            {
                **meta,
                "hydrated": hydrated,
                "dirty": False,
                "tombstone": False,
                "local_sha256": entry.get("local_sha256") if hydrated and entry else None,
                "last_synced_at": entry.get("last_synced_at") if entry else None,
                "synced_path": newpath,
            }
        )
        if not hydrated:
            self._schedule_download(newpath)

    def _resolve_conflict(self, entry):
        if self.conflict_mode != "copy":
            self.logger.warning("Unsupported conflict mode %s; falling back to copy", self.conflict_mode)
        conflict_path = self._conflict_path(entry["path"])
        self.logger.warning("Conflict on %s; preserving local version as %s", entry["path"], conflict_path)
        if self.mirror.exists(entry["path"]):
            self.mirror.rename_path(entry["path"], conflict_path)
        self.state.detach_subtree_as_conflict(entry["path"], conflict_path)
        subtree = self.state._fetch_subtree(conflict_path)
        for child in subtree:
            self.state.queue_op("conflict-copy", child["path"])

    def _schedule_all_unhydrated(self):
        paths = self.state.list_unhydrated_paths()
        total = len(paths)
        with self.hydration_progress_lock:
            self.hydration_total = total
            self.hydration_completed = 0
        if total:
            self.logger.info("Background cache warmup scheduled for %s files", total)
        else:
            self.logger.info("Background cache warmup skipped; all files already hydrated")
        for path in paths:
            self._schedule_download(path)

    def _schedule_download(self, path):
        self._schedule_download_with_delay(path, 0)

    def _schedule_download_with_delay(self, path, delay_seconds):
        if self.stop_event.is_set() or self.is_shutdown:
            return

        with self.downloads_lock:
            if path in self.scheduled_downloads:
                return
            self.scheduled_downloads.add(path)

        self._log_sync(
            "download-scheduled",
            level=logging.DEBUG if delay_seconds <= 0 else logging.INFO,
            path=path,
            delay_seconds=delay_seconds,
        )

        if delay_seconds <= 0:
            try:
                self.executor.submit(self._download_job, path)
            except RuntimeError:
                with self.downloads_lock:
                    self.scheduled_downloads.discard(path)
            return

        timer = threading.Timer(delay_seconds, self._submit_retry_download, args=(path,))
        timer.daemon = True
        with self.downloads_lock:
            self.download_retry_timers[path] = timer
        timer.start()

    def _submit_retry_download(self, path):
        with self.downloads_lock:
            self.download_retry_timers.pop(path, None)
        if self.stop_event.is_set() or self.is_shutdown:
            with self.downloads_lock:
                self.scheduled_downloads.discard(path)
            return
        try:
            self.executor.submit(self._download_job, path)
        except RuntimeError:
            with self.downloads_lock:
                self.scheduled_downloads.discard(path)

    def _retry_delay_for_attempt(self, attempt):
        return min(300, 5 * (2 ** max(0, attempt - 1)))

    def _is_auth_error(self, exc):
        if isinstance(
            exc,
            (
                PyiCloud2FARequiredException,
                PyiCloud2SARequiredException,
                PyiCloudAuthRequiredException,
                PyiCloudFailedLoginException,
            ),
        ):
            return True
        return False

    def _download_job(self, path):
        retry_delay = None
        try:
            self.ensure_local_file(path)
            with self.downloads_lock:
                self.download_retry_attempts.pop(path, None)
            self._log_sync("download-complete", level=logging.INFO, path=path)
            with self.hydration_progress_lock:
                self.hydration_completed += 1
                completed = self.hydration_completed
                total = self.hydration_total
            if total and (completed == 1 or completed == total or completed % 25 == 0):
                self.logger.info(
                    "Background cache warmup progress: %s/%s files hydrated",
                    completed,
                    total,
                )
        except Exception as exc:
            if self._is_auth_error(exc):
                self.logger.error(
                    "Warmup download blocked by expired iCloud authentication for %s: %s. "
                    "Run './icloudctl auth' and then './icloudctl restart'.",
                    path,
                    exc,
                )
                with self.downloads_lock:
                    self.download_retry_attempts.pop(path, None)
                return
            with self.downloads_lock:
                attempt = self.download_retry_attempts.get(path, 0) + 1
                self.download_retry_attempts[path] = attempt
            retry_delay = self._retry_delay_for_attempt(attempt)
            self.logger.error(
                "Warmup download failed for %s (attempt %s): %s; retrying in %ss",
                path,
                attempt,
                exc,
                retry_delay,
            )
        finally:
            with self.downloads_lock:
                self.scheduled_downloads.discard(path)
                self.download_retry_timers.pop(path, None)
            if retry_delay is not None:
                self._schedule_download_with_delay(path, retry_delay)

    def _upload_loop(self):
        while not self.stop_event.wait(self.upload_interval_seconds):
            try:
                self.sync_dirty_entries()
            except Exception as exc:
                self.logger.error("Upload loop failed: %s", exc)

    def _refresh_loop(self):
        immediate = self.has_persistent_cache()
        if immediate:
            try:
                self.logger.info("Starting background remote refresh from persistent cache")
                snapshot = self._crawl_remote_snapshot()
                self._apply_remote_snapshot(snapshot)
            except Exception as exc:
                self.logger.error("Initial background refresh failed: %s", exc)
        while not self.stop_event.wait(self.remote_refresh_interval_seconds):
            try:
                snapshot = self._crawl_remote_snapshot()
                self._apply_remote_snapshot(snapshot)
            except Exception as exc:
                self.logger.error("Refresh loop failed: %s", exc)

    def sync_dirty_entries(self):
        dirty_entries = self.state.list_dirty_entries()
        if not dirty_entries:
            return

        self._log_sync("dirty-scan", dirty_count=len(dirty_entries))

        tombstones = sorted(
            [entry for entry in dirty_entries if entry["tombstone"]],
            key=lambda entry: (entry["path"].count("/"), entry["path"]),
            reverse=True,
        )
        regular = sorted(
            [entry for entry in dirty_entries if not entry["tombstone"]],
            key=lambda entry: (entry["type"] != "folder", entry["path"].count("/"), entry["path"]),
        )

        for entry in tombstones:
            self._sync_tombstone(entry)

        for entry in regular:
            fresh = self.state.get_entry(entry["path"])
            if fresh is None or fresh["tombstone"] or not fresh["dirty"]:
                continue
            if fresh["type"] == "folder":
                self._sync_directory(fresh)
            else:
                self._sync_file(fresh)

    def _sync_tombstone(self, entry):
        self._log_sync("delete-start", path=entry["path"], remote=bool(entry["remote_drivewsid"]))
        if entry["remote_drivewsid"]:
            try:
                node = self._node_from_entry(entry)
                node.delete()
            except Exception as exc:
                self.logger.error("Failed deleting remote path %s: %s", entry["path"], exc)
                return
        self.state.remove_subtree(entry["path"])
        self._log_sync("delete-complete", path=entry["path"])

    def _sync_directory(self, entry):
        parent_node = self._ensure_remote_parent(entry["path"])
        if parent_node is None:
            return

        try:
            self._log_sync(
                "directory-sync-start",
                path=entry["path"],
                remote_exists=bool(entry["remote_drivewsid"]),
                synced_path=entry.get("synced_path"),
            )
            if not entry["remote_drivewsid"]:
                parent_node.mkdir(os.path.basename(entry["path"]))
                meta = self._refresh_child_meta(os.path.dirname(entry["path"]) or "/", os.path.basename(entry["path"]))
                self.state.mark_clean(entry["path"], meta)
                self._log_sync("directory-create-complete", path=entry["path"])
                return

            if entry["synced_path"] and entry["synced_path"] != entry["path"]:
                self._sync_move_or_rename(entry)
            self.state.mark_synced_subtree(entry["path"])
            self._log_sync("directory-sync-complete", path=entry["path"])
        except Exception as exc:
            self.logger.error("Failed syncing directory %s: %s", entry["path"], exc)

    def _sync_file(self, entry):
        parent_node = self._ensure_remote_parent(entry["path"])
        if parent_node is None:
            return

        try:
            self._log_sync(
                "file-sync-start",
                path=entry["path"],
                remote_exists=bool(entry["remote_drivewsid"]),
                synced_path=entry.get("synced_path"),
            )
            if not self.mirror.exists(entry["path"]):
                self.state.mark_tombstone(entry["path"])
                self._log_sync("file-missing-marked-tombstone", path=entry["path"])
                return

            self.ensure_local_file(entry["path"])

            if entry["remote_drivewsid"] and entry["synced_path"] and entry["synced_path"] != entry["path"]:
                self._sync_move_or_rename(entry)
                entry = self.state.get_entry(entry["path"])

            if entry["remote_drivewsid"]:
                try:
                    self._node_from_entry(entry).delete()
                except Exception:
                    pass

            with open(self.mirror.local_path(entry["path"]), "rb") as handle:
                file_obj = BytesIO(handle.read())
            file_obj.name = os.path.basename(entry["path"])
            parent_node.upload(file_obj)

            meta = self._refresh_child_meta(os.path.dirname(entry["path"]) or "/", os.path.basename(entry["path"]))
            checksum = self.mirror.file_sha256(entry["path"])
            self.state.mark_clean(entry["path"], meta, checksum)
            self._log_sync("file-sync-complete", path=entry["path"], size=meta.get("size"))
        except Exception as exc:
            self.logger.error("Failed syncing file %s: %s", entry["path"], exc)

    def _sync_move_or_rename(self, entry):
        synced_path = entry["synced_path"]
        if not synced_path:
            return
        old_parent = os.path.dirname(synced_path) or "/"
        new_parent = os.path.dirname(entry["path"]) or "/"
        old_name = os.path.basename(synced_path)
        new_name = os.path.basename(entry["path"])

        self._log_sync("move-start", path=synced_path, target_path=entry["path"])
        node = self._node_from_entry(entry)
        if old_parent != new_parent:
            destination = self._remote_node_for_path(new_parent)
            if destination is None:
                raise RuntimeError(f"Remote parent not available for {new_parent}")
            self.api.drive.move_nodes_to_node([node], destination)
            node = self._refresh_node_by_id(
                entry["remote_drivewsid"],
                entry.get("remote_shareid"),
            )
        if old_name != new_name:
            node.rename(new_name)
        self._log_sync("move-complete", path=synced_path, target_path=entry["path"])

    def _ensure_remote_parent(self, path):
        parent_path = os.path.dirname(path) or "/"
        if parent_path == "/":
            return self.api.drive.root
        parent_entry = self.state.get_entry(parent_path)
        if not parent_entry:
            return None
        if parent_entry["dirty"]:
            self._sync_directory(parent_entry)
            parent_entry = self.state.get_entry(parent_path)
        if not parent_entry or not parent_entry["remote_drivewsid"]:
            return None
        return self._node_from_entry(parent_entry)

    def _refresh_child_meta(self, parent_path, child_name):
        parent = self._remote_node_for_path(parent_path)
        if parent is None:
            raise RuntimeError(f"Missing remote parent: {parent_path}")
        for child in parent.get_children(force=True):
            if child.name == child_name:
                return self._node_to_meta(
                    child,
                    "/" + child.name if parent_path == "/" else parent_path.rstrip("/") + "/" + child.name,
                )
        raise KeyError(f"Missing child {child_name} under {parent_path}")

    def _remote_node_for_path(self, path):
        if path == "/" or path == "":
            return self.api.drive.root
        entry = self.state.get_entry(path)
        if not entry or not entry["remote_drivewsid"]:
            return None
        return self._node_from_entry(entry)

    def _refresh_node_by_id(self, remote_drivewsid, remote_shareid=None):
        data = self.api.drive.get_node_data(remote_drivewsid, remote_shareid)
        return DriveNode(self.api.drive, data)

    def _node_from_entry(self, entry):
        data = {
            "drivewsid": entry["remote_drivewsid"],
            "docwsid": entry.get("remote_docwsid"),
            "etag": entry.get("remote_etag"),
            "zone": entry.get("remote_zone"),
            "shareID": entry.get("remote_shareid"),
            "size": int(entry.get("size", 0) or 0),
            "type": entry.get("type", "file").upper(),
            "name": os.path.basename(entry["path"].rstrip("/")) or "root",
        }
        return DriveNode(self.api.drive, data)

    def _node_to_meta(self, node, path):
        data = node.data
        node_type = data.get("type", "FILE").lower()
        if node_type == "folder":
            size = 0
        else:
            size = int(data.get("size", 0) or 0)
        return {
            "path": path,
            "type": node_type,
            "parent_path": os.path.dirname(path) or "/",
            "remote_drivewsid": data.get("drivewsid"),
            "remote_docwsid": data.get("docwsid"),
            "remote_etag": data.get("etag"),
            "remote_zone": data.get("zone"),
            "remote_shareid": data.get("shareID"),
            "size": size,
            "mtime": parse_remote_time(data.get("dateModified")),
        }

    def _entry_conflicts(self, entry, meta):
        return (
            (entry.get("synced_path") and entry["synced_path"] != meta["path"])
            or (entry.get("remote_etag") and entry["remote_etag"] != meta["remote_etag"])
        )

    def _conflict_path(self, path):
        dirname = os.path.dirname(path) or "/"
        basename = os.path.basename(path)
        stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        return (
            "/" + f"{basename}.local-conflict-{stamp}"
            if dirname == "/"
            else dirname.rstrip("/") + "/" + f"{basename}.local-conflict-{stamp}"
        )

    def _path_lock(self, path):
        with self.path_locks_lock:
            lock = self.path_locks.get(path)
            if lock is None:
                lock = threading.Lock()
                self.path_locks[path] = lock
            return lock


class ICloudFS(Fuse):
    def __init__(self, *args, **kw):
        super(ICloudFS, self).__init__(*args, **kw)
        self.logger = logging.getLogger("icloud")
        self.username = None
        self.password = None
        self.cache_dir = None
        self.api = None
        self.mirror = None
        self.state = None
        self.sync_engine = None

    def _log_file_op(self, op, path=None, level=logging.INFO, **fields):
        payload = {}
        if path is not None:
            payload["path"] = path
        payload.update(fields)
        details = " ".join(f"{key}={value!r}" for key, value in payload.items() if value is not None)
        if details:
            self.logger.log(level, "file-op %s %s", op, details)
            return
        self.logger.log(level, "file-op %s", op)

    def shutdown(self):
        if self.sync_engine is not None:
            self.sync_engine.shutdown()

    def init_icloud(self, username, password, cache_dir, cookie_dir=None):
        self.username = username
        self.password = password
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        if cookie_dir:
            os.makedirs(cookie_dir, exist_ok=True)

        try:
            self.api = PyiCloudService(username, password, cookie_directory=cookie_dir)
            if self.api.requires_2fa:
                if sys.stdin.isatty():
                    print("Two-factor authentication required.")
                    code = input("Enter the verification code: ").strip()
                    result = self.api.validate_2fa_code(code)
                    print("Result: %s" % result)
                    if result and not self.api.is_trusted_session:
                        self.api.trust_session()
                else:
                    raise RuntimeError(
                        "2FA required, but no interactive terminal is available. "
                        "Run './icloudctl auth' first to establish a trusted session."
                    )

            if self.api.requires_2sa:
                if sys.stdin.isatty():
                    print("Two-step authentication required.")
                    devices = self.api.trusted_devices
                    for index, device in enumerate(devices):
                        label = device.get("deviceName") or f"SMS to {device.get('phoneNumber', 'unknown')}"
                        print(f"{index}: {label}")
                    selected = int(input("Select device index: ").strip() or "0")
                    device = devices[selected]
                    self.api.send_verification_code(device)
                    code = input("Enter the verification code: ").strip()
                    if not self.api.validate_verification_code(device, code):
                        raise RuntimeError("Failed to verify 2SA code")
                else:
                    raise RuntimeError(
                        "2SA required, but no interactive terminal is available. "
                        "Run './icloudctl auth' first to establish a trusted session."
                    )

            if self.api.requires_2fa or self.api.requires_2sa:
                raise RuntimeError("Additional authentication still required after code verification.")
        except Exception as exc:
            self.logger.error("Failed to connect to iCloud: %s", exc)
            raise

    def init_local_cache(
        self,
        cache_dir,
        warmup_mode,
        conflict_mode,
        upload_interval_seconds,
        remote_refresh_interval_seconds,
        warmup_workers,
    ):
        self.mirror = LocalMirror(cache_dir)
        state_path = os.path.join(cache_dir, "state.sqlite3")
        self.state = SyncState(state_path)
        self.sync_engine = ICloudSyncEngine(
            self.api,
            self.mirror,
            self.state,
            self.logger,
            warmup_mode=warmup_mode,
            conflict_mode=conflict_mode,
            upload_interval_seconds=upload_interval_seconds,
            remote_refresh_interval_seconds=remote_refresh_interval_seconds,
            warmup_workers=warmup_workers,
        )
        self.sync_engine.start()

    def getattr(self, path):
        now = int(time.time())
        entry = self.state.get_entry(path) if self.state else None
        attrs = Stat()

        if path == "/":
            try:
                stats = self.mirror.stat_local(path)
                self._apply_os_stat(attrs, stats)
            except Exception:
                attrs.st_mode = stat.S_IFDIR | 0o755
                attrs.st_nlink = 2
                attrs.st_size = 0
                attrs.st_ctime = now
                attrs.st_mtime = now
                attrs.st_atime = now
                attrs.st_uid = os.getuid()
                attrs.st_gid = os.getgid()
            return attrs

        if self.mirror and self.mirror.exists(path):
            stats = self.mirror.stat_local(path)
            self._apply_os_stat(attrs, stats)
            if entry and entry["type"] == "file" and not entry["hydrated"]:
                attrs.st_size = entry["size"]
                attrs.st_mtime = entry["mtime"]
                attrs.st_ctime = entry["mtime"]
            return attrs

        if entry and not entry["tombstone"]:
            attrs.st_mode = (stat.S_IFDIR | 0o755) if entry["type"] == "folder" else (stat.S_IFREG | 0o644)
            attrs.st_nlink = 2 if entry["type"] == "folder" else 1
            attrs.st_size = entry["size"]
            attrs.st_ctime = entry["mtime"] or now
            attrs.st_mtime = entry["mtime"] or now
            attrs.st_atime = now
            attrs.st_uid = os.getuid()
            attrs.st_gid = os.getgid()
            return attrs

        return -errno.ENOENT

    def readdir(self, path, offset):
        if not self.mirror.exists(path) or not self.mirror.is_dir(path):
            return -errno.ENOENT

        self._log_file_op("readdir", path, level=logging.DEBUG)
        entries = [".", ".."] + sorted(self.mirror.listdir(path))
        for entry in entries:
            yield fuse.Direntry(entry)

    def open(self, path, flags):
        self._log_file_op("open", path, level=logging.DEBUG, flags=flags)
        if not self.state.get_entry(path):
            if flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC):
                self.create(path, 0o644, flags)
                return 0
            return -errno.ENOENT

        entry = self.state.get_entry(path)
        if entry and entry["type"] == "file" and not entry["hydrated"] and not entry["dirty"]:
            try:
                self.sync_engine.ensure_local_file(path)
            except Exception as exc:
                self.logger.error("Failed hydrating on open for %s: %s", path, exc)
                return -errno.EIO
        return 0

    def create(self, path, mode, flags=None):
        try:
            self.mirror.create_file(path)
            stats = self.mirror.stat_local(path)
            self.state.upsert_entry(
                {
                    "path": path,
                    "type": "file",
                    "parent_path": os.path.dirname(path) or "/",
                    "size": 0,
                    "mtime": int(stats.st_mtime),
                    "hydrated": True,
                    "dirty": True,
                    "tombstone": False,
                    "synced_path": None,
                }
            )
            self.state.queue_op("create", path)
            self._log_file_op("create", path, mode=oct(mode), flags=flags)
            return 0
        except Exception as exc:
            self.logger.error("Error creating file %s: %s", path, exc)
            return -errno.EIO

    def read(self, path, size, offset):
        entry = self.state.get_entry(path)
        if not entry or entry["type"] != "file" or entry["tombstone"]:
            return -errno.ENOENT

        try:
            if not entry["hydrated"] and not entry["dirty"]:
                self.sync_engine.ensure_local_file(path)
            self._log_file_op("read", path, level=logging.DEBUG, size=size, offset=offset)
            return self.mirror.read(path, size, offset)
        except Exception as exc:
            self.logger.error("Error reading %s: %s", path, exc)
            return -errno.EIO

    def write(self, path, buf, offset):
        entry = self.state.get_entry(path)
        if entry and not entry["hydrated"] and entry["remote_drivewsid"]:
            try:
                self.sync_engine.ensure_local_file(path)
            except Exception as exc:
                self.logger.error("Failed hydrating before write %s: %s", path, exc)
                return -errno.EIO

        try:
            written = self.mirror.write(path, buf, offset)
            stats = self.mirror.stat_local(path)
            checksum = self.mirror.file_sha256(path)
            if not entry:
                self.state.upsert_entry(
                    {
                        "path": path,
                        "type": "file",
                        "parent_path": os.path.dirname(path) or "/",
                        "size": stats.st_size,
                        "mtime": int(stats.st_mtime),
                        "hydrated": True,
                        "dirty": True,
                        "tombstone": False,
                        "local_sha256": checksum,
                        "synced_path": None,
                    }
                )
            else:
                self.state.mark_dirty(path, stats.st_size, int(stats.st_mtime), 1, checksum)
            self.state.queue_op("update", path)
            self._log_file_op("write", path, size=len(buf), offset=offset, written=written)
            return written
        except Exception as exc:
            self.logger.error("Error writing %s: %s", path, exc)
            return -errno.EIO

    def flush(self, path):
        return 0

    def release(self, path, flags):
        return 0

    def mkdir(self, path, mode):
        try:
            self.mirror.ensure_dir(path)
            stats = self.mirror.stat_local(path)
            self.state.upsert_entry(
                {
                    "path": path,
                    "type": "folder",
                    "parent_path": os.path.dirname(path) or "/",
                    "size": 0,
                    "mtime": int(stats.st_mtime),
                    "hydrated": True,
                    "dirty": True,
                    "tombstone": False,
                    "synced_path": None,
                }
            )
            self.state.queue_op("mkdir", path)
            self._log_file_op("mkdir", path, mode=oct(mode))
            return 0
        except Exception as exc:
            self.logger.error("Error creating directory %s: %s", path, exc)
            return -errno.EIO

    def rmdir(self, path):
        entry = self.state.get_entry(path)
        if not entry:
            return -errno.ENOENT

        try:
            self.mirror.remove_dir(path)
            if entry["remote_drivewsid"]:
                self.state.mark_tombstone(path)
                self.state.queue_op("delete", path)
            else:
                self.state.remove_subtree(path)
            self._log_file_op("rmdir", path)
            return 0
        except OSError as exc:
            if exc.errno:
                return -exc.errno
            self.logger.error("Error removing directory %s: %s", path, exc)
            return -errno.EIO

    def unlink(self, path):
        entry = self.state.get_entry(path)
        if not entry:
            return -errno.ENOENT

        try:
            if self.mirror.exists(path):
                self.mirror.remove_file(path)
            if entry["remote_drivewsid"]:
                self.state.mark_tombstone(path)
                self.state.queue_op("delete", path)
            else:
                self.state.remove_entry(path)
            self._log_file_op("unlink", path)
            return 0
        except OSError as exc:
            if exc.errno:
                return -exc.errno
            self.logger.error("Error unlinking %s: %s", path, exc)
            return -errno.EIO

    def rename(self, oldpath, newpath):
        entry = self.state.get_entry(oldpath)
        if not entry:
            return -errno.ENOENT

        try:
            if self.mirror.exists(newpath):
                self.mirror.remove_tree(newpath)
                existing = self.state.get_entry(newpath)
                if existing:
                    if existing["remote_drivewsid"]:
                        self.state.mark_tombstone(newpath)
                    else:
                        self.state.remove_subtree(newpath)
            self.mirror.rename_path(oldpath, newpath)
            self.state.rename_tree(oldpath, newpath, root_dirty=True)
            self.state.queue_op("rename", oldpath, newpath)
            self._log_file_op("rename", oldpath, target_path=newpath)
            return 0
        except Exception as exc:
            self.logger.error("Error renaming %s to %s: %s", oldpath, newpath, exc)
            return -errno.EIO

    def truncate(self, path, length):
        entry = self.state.get_entry(path)
        if entry and not entry["hydrated"] and entry["remote_drivewsid"]:
            try:
                self.sync_engine.ensure_local_file(path)
            except Exception as exc:
                self.logger.error("Failed hydrating before truncate %s: %s", path, exc)
                return -errno.EIO

        try:
            self.mirror.truncate(path, length)
            stats = self.mirror.stat_local(path)
            checksum = self.mirror.file_sha256(path)
            if not entry:
                self.state.upsert_entry(
                    {
                        "path": path,
                        "type": "file",
                        "parent_path": os.path.dirname(path) or "/",
                        "size": stats.st_size,
                        "mtime": int(stats.st_mtime),
                        "hydrated": True,
                        "dirty": True,
                        "tombstone": False,
                        "local_sha256": checksum,
                        "synced_path": None,
                    }
                )
            else:
                self.state.mark_dirty(path, stats.st_size, int(stats.st_mtime), 1, checksum)
            self.state.queue_op("update", path)
            self._log_file_op("truncate", path, length=length)
            return 0
        except Exception as exc:
            self.logger.error("Error truncating %s: %s", path, exc)
            return -errno.EIO

    def mknod(self, path, mode, dev):
        if not stat.S_ISREG(mode):
            return -errno.ENOSYS
        return self.create(path, mode)

    def utime(self, path, times):
        try:
            if not self.mirror.exists(path):
                return -errno.ENOENT
            atime, mtime = times if times else (time.time(), time.time())
            self.mirror.set_mtime(path, int(mtime))
            stats = self.mirror.stat_local(path)
            if self.state.get_entry(path):
                self.state.mark_dirty(path, stats.st_size, int(stats.st_mtime))
            self._log_file_op("utime", path, atime=int(atime), mtime=int(mtime))
            return 0
        except Exception as exc:
            self.logger.error("Error setting utime for %s: %s", path, exc)
            return -errno.EIO

    def statfs(self):
        stats = self.mirror.statvfs()
        return {
            "f_bsize": stats.f_bsize,
            "f_frsize": stats.f_frsize,
            "f_blocks": stats.f_blocks,
            "f_bfree": stats.f_bfree,
            "f_bavail": stats.f_bavail,
            "f_files": stats.f_files,
            "f_ffree": stats.f_ffree,
            "f_namelen": stats.f_namemax,
        }

    def _apply_os_stat(self, attrs, stats):
        attrs.st_mode = stats.st_mode
        attrs.st_ino = stats.st_ino
        attrs.st_dev = stats.st_dev
        attrs.st_nlink = stats.st_nlink
        attrs.st_uid = stats.st_uid
        attrs.st_gid = stats.st_gid
        attrs.st_size = stats.st_size
        attrs.st_atime = int(stats.st_atime)
        attrs.st_mtime = int(stats.st_mtime)
        attrs.st_ctime = int(stats.st_ctime)


def parse_config(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        return config
    except Exception as exc:
        print(f"Error parsing config file: {exc}")
        sys.exit(1)


def main():
    usage = """
iCloud Linux: Mount iCloud Drive as a FUSE filesystem

%prog [options] mountpoint
"""
    fs = ICloudFS(version="%prog " + fuse.__version__, usage=usage, dash_s_do="setsingle")
    fs.parser.add_option(
        "-c",
        "--config",
        dest="config",
        default=os.path.expanduser("~/.config/icloud-linux/config.yaml"),
        help="Path to config file (default: ~/.config/icloud-linux/config.yaml)",
    )
    fs.parser.add_option("-v", "--debug", dest="debug", action="store_true", help="Enable debug logging")
    fs.parse(errex=1)
    args = fs.cmdline[0]

    log_level = logging.DEBUG if args.debug else logging.INFO
    log_path = os.environ.get("ICLOUD_LOG_PATH", os.path.expanduser("~/.local/state/icloud-linux/icloud.log"))
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=log_level,
        handlers=[logging.StreamHandler(), logging.FileHandler(log_path)],
    )
    logger = logging.getLogger("icloud")
    logging.getLogger("pyicloud.base").addFilter(IgnoreIcdrsWarning())

    config = parse_config(args.config)
    username = config.get("username")
    password = config.get("password")
    if not username or not password:
        logger.error("Username or password not provided in config file")
        sys.exit(1)

    cache_dir = os.path.expanduser(config.get("cache_dir", "~/.cache/icloud-linux"))
    cookie_dir = os.path.expanduser(config.get("cookie_dir", "~/.config/icloud-linux/cookies"))
    warmup_mode = config.get("warmup_mode", "background")
    conflict_mode = config.get("conflict_mode", "copy")
    upload_interval_seconds = int(config.get("upload_interval_seconds", 30))
    remote_refresh_interval_seconds = int(config.get("remote_refresh_interval_seconds", 300))
    warmup_workers = int(config.get("warmup_workers", 1))

    fs.init_icloud(username, password, cache_dir, cookie_dir)
    fs.init_local_cache(
        cache_dir,
        warmup_mode,
        conflict_mode,
        upload_interval_seconds,
        remote_refresh_interval_seconds,
        warmup_workers,
    )

    atexit.register(fs.shutdown)

    def handle_shutdown(signum, frame):
        logger.info("Received signal %s, shutting down background sync", signum)
        fs.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        fs.main()
    finally:
        fs.shutdown()


if __name__ == "__main__":
    main()
