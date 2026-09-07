import errno
import io
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import Mock

from driver import ICloudFS, ICloudSyncEngine, LocalMirror, SyncState
from pyicloud.exceptions import PyiCloudAPIResponseException, PyiCloudFailedLoginException


class NoUnboundedReadStream(io.BytesIO):
    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("stream was read without a chunk size")
        return super().read(size)


class DriverStateTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="icloud-linux-test-")
        self.mirror = LocalMirror(self.root)
        self.state = SyncState(os.path.join(self.root, "state.sqlite3"))

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_mirror_read_write_truncate(self):
        self.mirror.create_file("/docs/a.txt")
        self.mirror.write("/docs/a.txt", b"hello", 0)
        self.assertEqual(self.mirror.read("/docs/a.txt", 5, 0), b"hello")

        self.mirror.truncate("/docs/a.txt", 2)
        self.assertEqual(self.mirror.read("/docs/a.txt", 10, 0), b"he")

    def test_ensure_dir_replaces_file_placeholder(self):
        self.mirror.create_file("/Obsidian")

        self.mirror.ensure_dir("/Obsidian")

        self.assertTrue(self.mirror.is_dir("/Obsidian"))

    def test_write_atomic_stream_copies_in_chunks(self):
        stream = NoUnboundedReadStream(b"streamed content")

        self.mirror.write_atomic_stream("/docs/a.txt", stream)

        self.assertEqual(self.mirror.read("/docs/a.txt", 100, 0), b"streamed content")

    def test_rename_tree_preserves_old_synced_paths_for_local_rename(self):
        self.state.upsert_entry(
            {
                "path": "/docs",
                "type": "folder",
                "parent_path": "/",
                "hydrated": True,
                "dirty": False,
                "tombstone": False,
                "synced_path": "/docs",
            }
        )
        self.state.upsert_entry(
            {
                "path": "/docs/a.txt",
                "type": "file",
                "parent_path": "/docs",
                "hydrated": True,
                "dirty": False,
                "tombstone": False,
                "synced_path": "/docs/a.txt",
            }
        )

        self.state.rename_tree("/docs", "/archive", root_dirty=True)

        folder = self.state.get_entry("/archive")
        child = self.state.get_entry("/archive/a.txt")
        self.assertEqual(folder["synced_path"], "/docs")
        self.assertEqual(child["synced_path"], "/docs/a.txt")
        self.assertEqual(folder["dirty"], 1)
        self.assertEqual(child["dirty"], 0)

    def test_rename_tree_updates_synced_paths_for_remote_rename(self):
        self.state.upsert_entry(
            {
                "path": "/docs",
                "type": "folder",
                "parent_path": "/",
                "hydrated": True,
                "dirty": False,
                "tombstone": False,
                "synced_path": "/docs",
            }
        )
        self.state.upsert_entry(
            {
                "path": "/docs/a.txt",
                "type": "file",
                "parent_path": "/docs",
                "hydrated": True,
                "dirty": False,
                "tombstone": False,
                "synced_path": "/docs/a.txt",
            }
        )

        self.state.rename_tree("/docs", "/remote-docs", root_dirty=False, update_synced=True)

        folder = self.state.get_entry("/remote-docs")
        child = self.state.get_entry("/remote-docs/a.txt")
        self.assertEqual(folder["synced_path"], "/remote-docs")
        self.assertEqual(child["synced_path"], "/remote-docs/a.txt")

    def test_detach_subtree_as_conflict_clears_remote_identity(self):
        self.state.upsert_entry(
            {
                "path": "/docs",
                "type": "folder",
                "parent_path": "/",
                "remote_drivewsid": "folder-1",
                "hydrated": True,
                "dirty": True,
                "tombstone": False,
                "synced_path": "/docs",
            }
        )
        self.state.upsert_entry(
            {
                "path": "/docs/a.txt",
                "type": "file",
                "parent_path": "/docs",
                "remote_drivewsid": "file-1",
                "remote_docwsid": "doc-1",
                "remote_etag": "etag-1",
                "remote_zone": "zone",
                "hydrated": True,
                "dirty": True,
                "tombstone": False,
                "synced_path": "/docs/a.txt",
            }
        )

        self.state.detach_subtree_as_conflict("/docs", "/docs.local-conflict-123")

        folder = self.state.get_entry("/docs.local-conflict-123")
        child = self.state.get_entry("/docs.local-conflict-123/a.txt")
        self.assertIsNone(folder["remote_drivewsid"])
        self.assertIsNone(child["remote_docwsid"])
        self.assertEqual(folder["dirty"], 1)
        self.assertEqual(child["dirty"], 1)

    def test_reconcile_persistent_cache_keeps_placeholder_unhydrated(self):
        self.state.upsert_entry(
            {
                "path": "/docs/a.txt",
                "type": "file",
                "parent_path": "/docs",
                "remote_drivewsid": "file-1",
                "size": 128,
                "mtime": 123,
                "hydrated": False,
                "dirty": False,
                "tombstone": False,
                "synced_path": "/docs/a.txt",
            }
        )
        self.mirror.ensure_dir("/docs")
        self.mirror.materialize_placeholder("/docs/a.txt", 128, 123)

        api = Mock()
        api.drive.root = Mock()
        engine = ICloudSyncEngine(api, self.mirror, self.state, Mock())
        engine._reconcile_persistent_cache()

        entry = self.state.get_entry("/docs/a.txt")
        self.assertEqual(entry["hydrated"], 0)

    def test_reconcile_persistent_cache_replaces_app_library_placeholder(self):
        self.state.upsert_entry(
            {
                "path": "/Obsidian",
                "type": "app_library",
                "parent_path": "/",
                "remote_drivewsid": "folder-1",
                "hydrated": True,
                "dirty": False,
                "tombstone": False,
                "synced_path": "/Obsidian",
            }
        )
        self.mirror.create_file("/Obsidian")

        api = Mock()
        api.drive.root = Mock()
        engine = ICloudSyncEngine(api, self.mirror, self.state, Mock())

        engine._reconcile_persistent_cache()

        self.assertTrue(self.mirror.is_dir("/Obsidian"))

    def test_remote_shareid_round_trips_through_state(self):
        self.state.upsert_entry(
            {
                "path": "/shared/a.txt",
                "type": "file",
                "parent_path": "/shared",
                "remote_drivewsid": "file-1",
                "remote_docwsid": "doc-1",
                "remote_etag": "etag-1",
                "remote_zone": "zone-1",
                "remote_shareid": {"share-zone": "abc"},
                "hydrated": False,
                "dirty": False,
                "tombstone": False,
                "synced_path": "/shared/a.txt",
            }
        )

        entry = self.state.get_entry("/shared/a.txt")

        self.assertEqual(entry["remote_shareid"], {"share-zone": "abc"})

    def test_existing_state_db_is_migrated_for_remote_shareid(self):
        legacy_db = os.path.join(self.root, "legacy.sqlite3")
        conn = sqlite3.connect(legacy_db)
        conn.execute(
            """
            CREATE TABLE entries (
                path TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                parent_path TEXT NOT NULL,
                remote_drivewsid TEXT,
                remote_docwsid TEXT,
                remote_etag TEXT,
                remote_zone TEXT,
                size INTEGER NOT NULL DEFAULT 0,
                mtime INTEGER NOT NULL DEFAULT 0,
                hydrated INTEGER NOT NULL DEFAULT 0,
                dirty INTEGER NOT NULL DEFAULT 0,
                tombstone INTEGER NOT NULL DEFAULT 0,
                local_sha256 TEXT,
                last_synced_at INTEGER,
                synced_path TEXT
            )
            """
        )
        conn.commit()
        conn.close()

        migrated = SyncState(legacy_db)
        columns = migrated.conn.execute("PRAGMA table_info(entries)").fetchall()

        self.assertIn("remote_shareid", {column["name"] for column in columns})


class SyncEngineStartupTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="icloud-linux-test-")
        self.mirror = LocalMirror(self.root)
        self.state = SyncState(os.path.join(self.root, "state.sqlite3"))
        self.logger = Mock()
        api = Mock()
        api.drive.root = Mock()
        self.engine = ICloudSyncEngine(api, self.mirror, self.state, self.logger)
        self.engine._start_background_threads = Mock()
        self.engine._schedule_all_unhydrated = Mock()
        self.engine.initial_scan = Mock()
        self.engine._reconcile_persistent_cache = Mock()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_start_uses_persistent_cache_without_initial_scan(self):
        self.state.upsert_entry(
            {
                "path": "/docs",
                "type": "folder",
                "parent_path": "/",
                "hydrated": True,
                "dirty": False,
                "tombstone": False,
                "synced_path": "/docs",
            }
        )

        self.engine.start()

        self.engine.initial_scan.assert_not_called()
        self.engine._reconcile_persistent_cache.assert_called_once()
        self.engine._schedule_all_unhydrated.assert_called_once()
        self.engine._start_background_threads.assert_called_once()

    def test_start_performs_initial_scan_on_first_run(self):
        self.engine.start()

        self.engine.initial_scan.assert_called_once()
        self.engine._reconcile_persistent_cache.assert_not_called()
        self.engine._schedule_all_unhydrated.assert_called_once()
        self.engine._start_background_threads.assert_called_once()

    def test_failed_download_is_retried_with_backoff(self):
        self.engine.ensure_local_file = Mock(side_effect=RuntimeError("500"))
        self.engine._schedule_download_with_delay = Mock()
        self.engine.scheduled_downloads.add("/docs/a.txt")

        self.engine._download_job("/docs/a.txt")

        self.engine._schedule_download_with_delay.assert_called_once()
        args = self.engine._schedule_download_with_delay.call_args[0]
        self.assertEqual(args[0], "/docs/a.txt")
        self.assertGreater(args[1], 0)

    def test_auth_failure_is_not_retried(self):
        self.engine.ensure_local_file = Mock(
            side_effect=PyiCloudFailedLoginException("bad session")
        )
        self.engine._schedule_download_with_delay = Mock()
        self.engine.scheduled_downloads.add("/docs/a.txt")

        self.engine._download_job("/docs/a.txt")

        self.engine._schedule_download_with_delay.assert_not_called()

    def test_generic_500_auth_message_is_still_retried(self):
        self.engine.ensure_local_file = Mock(
            side_effect=PyiCloudAPIResponseException(
                "Authentication required for Account.",
                500,
            )
        )
        self.engine._schedule_download_with_delay = Mock()
        self.engine.scheduled_downloads.add("/docs/a.txt")

        self.engine._download_job("/docs/a.txt")

        self.engine._schedule_download_with_delay.assert_called_once()

    def test_schedule_download_ignores_executor_shutdown_race(self):
        self.engine.executor.submit = Mock(side_effect=RuntimeError("cannot schedule new futures after interpreter shutdown"))

        self.engine._schedule_download_with_delay("/docs/a.txt", 0)

        self.assertNotIn("/docs/a.txt", self.engine.scheduled_downloads)

    def test_request_remote_refresh_sets_wakeup_event(self):
        self.assertFalse(self.engine.refresh_now_event.is_set())

        self.engine.request_remote_refresh()

        self.assertTrue(self.engine.refresh_now_event.is_set())

    def test_node_from_entry_reuses_persisted_file_metadata(self):
        shareid = {"share-zone": "abc"}
        node = self.engine._node_from_entry(
            {
                "path": "/docs/a.txt",
                "type": "file",
                "remote_drivewsid": "file-1",
                "remote_docwsid": "doc-1",
                "remote_etag": "etag-1",
                "remote_zone": "zone-1",
                "remote_shareid": shareid,
                "size": 5,
            }
        )

        self.engine.api.drive.get_node_data.assert_not_called()
        self.assertEqual(node.data["docwsid"], "doc-1")
        self.assertEqual(node.data["shareID"], shareid)
        self.assertEqual(node.data["size"], 5)

    def test_crawl_descends_into_app_library_nodes(self):
        note = Mock()
        note.name = "vault.md"
        note.data = {
            "type": "FILE",
            "drivewsid": "file-1",
            "docwsid": "doc-1",
            "etag": "etag-1",
            "zone": "zone-1",
            "size": 12,
            "dateModified": "2026-04-06T00:00:00Z",
        }
        obsidian = Mock()
        obsidian.name = "Obsidian"
        obsidian.data = {
            "type": "APP_LIBRARY",
            "drivewsid": "folder-1",
            "docwsid": "documents",
            "etag": "etag-folder",
            "zone": "zone-1",
            "dateModified": "2026-04-06T00:00:00Z",
        }
        obsidian.get_children.return_value = [note]
        root = Mock()
        root.get_children.return_value = [obsidian]
        self.engine.api.drive.root = root

        snapshot = self.engine._crawl_remote_snapshot()

        self.assertIn("folder-1", snapshot)
        self.assertIn("file-1", snapshot)
        self.assertEqual(snapshot["folder-1"]["path"], "/Obsidian")
        self.assertEqual(snapshot["folder-1"]["type"], "app_library")
        self.assertEqual(snapshot["file-1"]["path"], "/Obsidian/vault.md")

    def test_materialize_remote_entry_treats_app_library_as_directory(self):
        self.engine._materialize_remote_entry(
            {
                "path": "/Obsidian",
                "type": "app_library",
                "parent_path": "/",
                "remote_drivewsid": "folder-1",
                "remote_docwsid": "documents",
                "remote_etag": "etag-folder",
                "remote_zone": "zone-1",
                "size": 0,
                "mtime": 123,
            }
        )

        self.assertTrue(self.mirror.is_dir("/Obsidian"))
        entry = self.state.get_entry("/Obsidian")
        self.assertEqual(entry["hydrated"], 1)

    def test_ensure_local_file_streams_remote_content_in_chunks(self):
        self.state.upsert_entry(
            {
                "path": "/docs/a.txt",
                "type": "file",
                "parent_path": "/docs",
                "remote_drivewsid": "file-1",
                "remote_docwsid": "doc-1",
                "remote_zone": "zone-1",
                "size": 16,
                "mtime": 123,
                "hydrated": False,
                "dirty": False,
                "tombstone": False,
                "synced_path": "/docs/a.txt",
            }
        )
        self.mirror.ensure_dir("/docs")
        response = Mock()
        response.raw = NoUnboundedReadStream(b"chunked download")
        response.close = Mock()
        node = Mock()
        node.open.return_value = response
        self.engine._node_from_entry = Mock(return_value=node)

        self.engine.ensure_local_file("/docs/a.txt")

        self.assertEqual(self.mirror.read("/docs/a.txt", 100, 0), b"chunked download")
        response.close.assert_called_once()

    def test_sync_file_uploads_stream_without_buffering_entire_file(self):
        self.mirror.create_file("/docs/a.txt")
        self.mirror.write("/docs/a.txt", b"hello world", 0)
        self.state.upsert_entry(
            {
                "path": "/docs/a.txt",
                "type": "file",
                "parent_path": "/docs",
                "remote_drivewsid": None,
                "hydrated": True,
                "dirty": True,
                "tombstone": False,
                "synced_path": "/docs/a.txt",
            }
        )
        upload_state = {}

        def capture_upload(stream):
            upload_state["class_name"] = stream.__class__.__name__
            upload_state["name"] = stream.name
            upload_state["prefix"] = stream.read(5)

        parent_node = Mock()
        parent_node.upload.side_effect = capture_upload
        self.engine._ensure_remote_parent = Mock(return_value=parent_node)
        self.engine.ensure_local_file = Mock()
        self.engine._refresh_child_meta = Mock(
            return_value={
                "path": "/docs/a.txt",
                "type": "file",
                "parent_path": "/docs",
                "remote_drivewsid": "file-1",
                "remote_docwsid": "doc-1",
                "remote_etag": "etag-1",
                "remote_zone": "zone-1",
                "size": 11,
                "mtime": 123,
            }
        )

        self.engine._sync_file(self.state.get_entry("/docs/a.txt"))

        self.assertEqual(upload_state["class_name"], "NamedFileStream")
        self.assertEqual(upload_state["name"], "a.txt")
        self.assertEqual(upload_state["prefix"], b"hello")


class ICloudFSPathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="icloud-linux-test-")
        self.mirror = LocalMirror(self.root)
        self.state = SyncState(os.path.join(self.root, "state.sqlite3"))
        self.api = Mock()
        self.api.drive.root = Mock()
        self.engine = ICloudSyncEngine(
            self.api,
            self.mirror,
            self.state,
            Mock(),
            sync_paths=["/allowed/"],
            exclude_paths=["/allowed/excluded/"],
        )
        self.fs = ICloudFS.__new__(ICloudFS)
        self.fs.logger = Mock()
        self.fs.api = self.api
        self.fs.mirror = self.mirror
        self.fs.state = self.state
        self.fs.sync_engine = self.engine

    def tearDown(self):
        self.engine.shutdown()
        shutil.rmtree(self.root)

    def _add_entry(self, path, entry_type="file", content=b"existing"):
        if entry_type == "folder":
            self.mirror.ensure_dir(path)
        else:
            self.mirror.write(path, content, 0)
        stats = self.mirror.stat_local(path)
        self.state.upsert_entry(
            {
                "path": path,
                "type": entry_type,
                "parent_path": os.path.dirname(path) or "/",
                "remote_drivewsid": f"remote-{path}",
                "size": stats.st_size,
                "mtime": int(stats.st_mtime),
                "hydrated": True,
                "dirty": False,
                "tombstone": False,
                "synced_path": path,
            }
        )

    def _pending_op_count(self):
        return self.state.conn.execute("SELECT COUNT(*) FROM pending_ops").fetchone()[0]

    def test_allowed_mutations_are_accepted(self):
        self.assertEqual(self.fs.create("/allowed/queued.txt", 0o644), 0)
        self.assertEqual(self.fs.mkdir("/allowed/newdir", 0o755), 0)
        self.assertEqual(self.fs.create("/allowed/newdir/file.txt", 0o644), 0)
        self.assertEqual(self.fs.write("/allowed/newdir/file.txt", b"hello", 0), 5)
        self.assertEqual(self.fs.truncate("/allowed/newdir/file.txt", 2), 0)
        self.assertEqual(
            self.fs.rename("/allowed/newdir/file.txt", "/allowed/newdir/renamed.txt"),
            0,
        )
        self.assertEqual(self.fs.unlink("/allowed/newdir/renamed.txt"), 0)
        self.assertEqual(self.fs.rmdir("/allowed/newdir"), 0)
        self.assertGreater(self._pending_op_count(), 0)

    def test_excluded_and_out_of_scope_mutations_are_rejected_without_queueing(self):
        self._add_entry("/allowed/source.txt")
        self._add_entry("/allowed/excluded/file.txt")
        self._add_entry("/outside/file.txt")
        self._add_entry("/outside/dir", entry_type="folder")

        entry_paths_before = [entry["path"] for entry in self.state.list_entries()]

        attempts = [
            self.fs.create("/allowed/excluded/new.txt", 0o644),
            self.fs.write("/outside/file.txt", b"changed", 0),
            self.fs.truncate("/allowed/excluded/file.txt", 0),
            self.fs.mkdir("/outside/newdir", 0o755),
            self.fs.unlink("/allowed/excluded/file.txt"),
            self.fs.rmdir("/outside/dir"),
            self.fs.rename("/allowed/source.txt", "/outside/destination.txt"),
            self.fs.rename("/outside/file.txt", "/allowed/destination.txt"),
            self.fs.open("/outside/file.txt", os.O_WRONLY),
            self.fs.mknod("/outside/node.txt", stat.S_IFREG | 0o644, 0),
            self.fs.utime("/outside/file.txt", None),
        ]

        self.assertEqual(attempts, [-errno.EACCES] * len(attempts))
        self.assertEqual(self._pending_op_count(), 0)
        self.assertEqual(
            [entry["path"] for entry in self.state.list_entries()],
            entry_paths_before,
        )
        self.assertEqual(self.mirror.read("/outside/file.txt", 100, 0), b"existing")
        self.assertTrue(self.mirror.exists("/allowed/source.txt"))
        self.assertFalse(self.mirror.exists("/outside/destination.txt"))

    def test_uploader_skips_disallowed_dirty_entries(self):
        self._add_entry("/allowed/file.txt")
        self._add_entry("/allowed/excluded/file.txt")
        self._add_entry("/outside/file.txt")
        self._add_entry("/allowed/moved.txt")
        self.state.mark_dirty("/allowed/file.txt")
        self.state.mark_dirty("/allowed/excluded/file.txt")
        self.state.mark_dirty("/outside/file.txt")
        self.state.upsert_entry(
            {
                **self.state.get_entry("/allowed/moved.txt"),
                "dirty": True,
                "synced_path": "/outside/original.txt",
            }
        )
        self.engine._sync_file = Mock()

        self.engine.sync_dirty_entries()

        self.engine._sync_file.assert_called_once_with(
            self.state.get_entry("/allowed/file.txt")
        )

    def test_rename_rejects_allowed_directory_with_excluded_descendant(self):
        self.engine.exclude_paths = ["/allowed/parent/excluded"]
        self._add_entry("/allowed/parent", entry_type="folder")
        self._add_entry("/allowed/parent/excluded", entry_type="folder")
        self._add_entry("/allowed/parent/excluded/file.txt")

        result = self.fs.rename("/allowed/parent", "/allowed/newparent")

        self.assertEqual(result, -errno.EACCES)
        self.assertTrue(self.mirror.exists("/allowed/parent/excluded/file.txt"))
        self.assertFalse(self.mirror.exists("/allowed/newparent"))
        self.assertIsNotNone(self.state.get_entry("/allowed/parent/excluded/file.txt"))
        self.assertIsNone(self.state.get_entry("/allowed/newparent"))

    def test_empty_sync_paths_preserve_unrestricted_behavior(self):
        engine = ICloudSyncEngine(
            self.api,
            self.mirror,
            self.state,
            Mock(),
            sync_paths=[],
        )
        try:
            self.assertTrue(engine._path_allowed("/outside/file.txt"))
        finally:
            engine.shutdown()


if __name__ == "__main__":
    unittest.main()
