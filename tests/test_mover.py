import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from qbit_jellyfin_mover.__main__ import JellyfinClient, Mover


class StubHttpClient:
    def __init__(self, sessions: list[dict]) -> None:
        self.sessions = sessions

    def request(self, *args, **kwargs):
        return 200, json.dumps(self.sessions).encode(), {}


class JellyfinPlaybackSessionsTest(unittest.TestCase):
    def active_sessions(self, sessions: list[dict]) -> list[dict]:
        settings = SimpleNamespace(
            jellyfin_url="http://jellyfin",
            jellyfin_api_key="test-key",
            active_within_seconds=600,
        )
        client = JellyfinClient(settings, StubHttpClient(sessions))
        return client.active_playback_sessions()

    def test_logged_in_session_without_playback_is_idle(self) -> None:
        sessions = [{"UserName": "Downstairs TV", "IsActive": True}]

        self.assertEqual(self.active_sessions(sessions), [])

    def test_unpaused_playback_is_active(self) -> None:
        playing = {
            "UserName": "Downstairs TV",
            "IsActive": True,
            "NowPlayingItem": {"Name": "Monsters Inc"},
            "PlayState": {"IsPaused": False},
        }

        self.assertEqual(self.active_sessions([playing]), [playing])

    def test_paused_playback_is_idle(self) -> None:
        paused = {
            "UserName": "Downstairs TV",
            "IsActive": True,
            "NowPlayingItem": {"Name": "Monsters Inc"},
            "PlayState": {"IsPaused": True},
        }

        self.assertEqual(self.active_sessions([paused]), [])


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
