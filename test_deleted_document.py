import io
import os
import tempfile
import unittest
from unittest.mock import Mock

from driver import ICloudFS, ICloudSyncEngine, LocalMirror, SyncState


class DeletedDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mirror = LocalMirror(self.tmp.name)
        self.state = SyncState(os.path.join(self.tmp.name, 'state.sqlite3'))
        self.engine = ICloudSyncEngine(Mock(), self.mirror, self.state, Mock())
        self.fs = ICloudFS.__new__(ICloudFS)
        self.fs.api = Mock()
        self.fs.logger = Mock()
        self.fs.state = self.state
        self.fs.mirror = self.mirror
        self.fs.sync_engine = self.engine

    def tearDown(self):
        self.engine.shutdown()
        self.state.conn.close()
        self.tmp.cleanup()

    def entry(self, path='/file', data=b'valid data', **kw):
        self.mirror.write_atomic_bytes(path, data)
        item = dict(path=path, type='file', parent_path='/', size=len(data),
                    remote_drivewsid='dead-id', hydrated=False, dirty=False,
                    local_sha256=self.mirror.file_sha256(path), synced_path=path)
        item.update(kw)
        self.state.upsert_entry(item)
        return self.state.get_entry(path)

    def deleted(self):
        self.engine._download_content = Mock(side_effect=RuntimeError('409 NOT_FOUND DocumentDeletedException'))

    def test_read_recovers_verified_local_content_and_detaches_id(self):
        self.entry()
        self.deleted()
        self.assertEqual(self.fs.read('/file', 100, 0), b'valid data')
        entry = self.state.get_entry('/file')
        self.assertTrue(entry['dirty'])
        self.assertTrue(entry['hydrated'])
        self.assertIsNone(entry['remote_drivewsid'])
        self.assertEqual(self.fs.read('/file', 100, 0), b'valid data')
        self.engine._download_content.assert_called_once()

    def test_placeholder_not_promoted_at_startup_or_after_deleted_response(self):
        self.entry(data=b'\0' * 32, local_sha256=None)
        self.engine._reconcile_persistent_cache()
        self.assertFalse(self.state.get_entry('/file')['hydrated'])
        self.assertIsNone(self.state.get_entry('/file')['local_sha256'])
        self.deleted()
        self.engine._refresh_child_meta = Mock(side_effect=KeyError('missing'))
        with self.assertRaises(KeyError):
            self.engine.ensure_local_file('/file')
        self.assertFalse(self.state.get_entry('/file')['dirty'])

    def test_deleted_placeholder_rebinds_to_new_remote_id(self):
        old = self.entry(data=b'\0' * 3, local_sha256=None)
        self.engine._refresh_child_meta = Mock(return_value={**old, 'remote_drivewsid': 'new-id'})
        node = Mock()
        response = Mock(raw=io.BytesIO(b'new'))
        node.open.side_effect = [RuntimeError('404 NOT_FOUND'), response]
        self.engine._node_from_entry = Mock(return_value=node)
        self.assertEqual(self.fs.read('/file', 10, 0), b'new')
        self.assertEqual(self.state.get_entry('/file')['remote_drivewsid'], 'new-id')
        self.assertFalse(self.state.get_entry('/file')['dirty'])

    def test_dirty_content_never_downloaded_over_including_empty_file(self):
        for data in (b'local edits', b''):
            with self.subTest(data=data):
                self.entry(data=data, dirty=True, local_sha256=None)
                self.engine._download_content = Mock()
                self.engine.ensure_local_file('/file')
                self.engine._download_content.assert_not_called()
                self.assertEqual(self.mirror.read('/file', 100, 0), data)

    def test_generic_auth_failure_does_not_detach_identity(self):
        self.entry()
        self.engine._download_content = Mock(side_effect=RuntimeError('401 authentication required'))
        self.assertEqual(self.fs.read('/file', 100, 0), b'valid data')
        self.assertEqual(self.state.get_entry('/file')['remote_drivewsid'], 'dead-id')
        self.assertFalse(self.state.get_entry('/file')['dirty'])

    def test_atomic_replace_preserves_destination_identity_without_sql_collision(self):
        self.entry('/file', b'old', hydrated=True)
        self.entry('/file.tmp', b'new content', hydrated=True, dirty=True, remote_drivewsid=None)
        self.assertEqual(self.fs.rename('/file.tmp', '/file'), 0)
        self.assertEqual(self.fs.read('/file', 100, 0), b'new content')
        self.assertEqual(self.state.get_entry('/file')['remote_drivewsid'], 'dead-id')
        self.assertTrue(self.state.get_entry('/file')['dirty'])
        self.assertIsNone(self.state.get_entry('/file.tmp'))

    def test_uploaded_temp_queued_for_delete_separately(self):
        self.entry('/file', b'old', hydrated=True)
        self.entry('/file.tmp', b'new', hydrated=True, remote_drivewsid='temp-id')
        self.assertEqual(self.fs.rename('/file.tmp', '/file'), 0)
        self.assertTrue(self.state.get_entry('/file.tmp')['tombstone'])
        self.assertFalse(self.state.get_entry('/file')['tombstone'])

    def test_failed_upload_cannot_reuse_deleted_id(self):
        entry = self.entry(hydrated=True, dirty=True)
        parent = Mock()
        parent.upload.side_effect = RuntimeError('network down')
        self.engine._ensure_remote_parent = Mock(return_value=parent)
        self.engine._node_from_entry = Mock()
        self.engine._sync_file(entry)
        self.assertIsNone(self.state.get_entry('/file')['remote_drivewsid'])
        self.assertTrue(self.state.get_entry('/file')['dirty'])
        self.assertEqual(self.fs.read('/file', 100, 0), b'valid data')
