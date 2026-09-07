import unittest

from hydrate import _normalise, path_allowed


class HydratePathPolicyTests(unittest.TestCase):
    def test_trailing_slashes_are_normalized_for_allow_and_exclude_paths(self):
        sync_paths = _normalise(["/allowed/"])
        exclude_paths = _normalise(["/allowed/excluded/"])

        self.assertTrue(path_allowed("/allowed/file.txt", sync_paths, exclude_paths))
        self.assertFalse(
            path_allowed("/allowed/excluded/file.txt", sync_paths, exclude_paths)
        )

    def test_dot_segments_cannot_escape_an_allowed_path(self):
        sync_paths = _normalise(["/allowed"])

        self.assertFalse(path_allowed("/allowed/../outside.txt", sync_paths, []))


if __name__ == "__main__":
    unittest.main()
