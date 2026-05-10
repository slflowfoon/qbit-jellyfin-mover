from pathlib import Path
import tempfile
import unittest

from qbit_jellyfin_mover.__main__ import Mover


class RsyncSourceArgTest(unittest.TestCase):
    def test_directory_source_copies_contents_into_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Release"
            source.mkdir()

            self.assertEqual(Mover._rsync_source_arg(source), str(source) + "/")

    def test_file_source_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Release.mkv"
            source.touch()

            self.assertEqual(Mover._rsync_source_arg(source), str(source))


if __name__ == "__main__":
    unittest.main()
