import os
import shutil
import tempfile
import unittest
from unittest.mock import Mock

from driver import ICloudSyncEngine, LocalMirror, SyncState


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


if __name__ == "__main__":
    unittest.main()
