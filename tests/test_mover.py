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


class StubQbitClient:
    def __init__(self, torrents: list[dict] | None = None) -> None:
        self.torrent_data = torrents or []
        self.resumed_hashes: list[str] = []

    def torrents(self) -> list[dict]:
        return self.torrent_data

    def resume_hashes(self, hashes: list[str]) -> None:
        self.resumed_hashes.extend(hashes)


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


class TorrentPauseLifecycleTest(unittest.TestCase):
    def test_only_running_torrents_in_pause_categories_are_selected(self) -> None:
        qbit = StubQbitClient(
            [
                {"hash": "running-radarr", "category": "radarr", "state": "stalledUP"},
                {"hash": "stopped-radarr", "category": "radarr", "state": "stoppedUP"},
                {"hash": "paused-sonarr", "category": "sonarr", "state": "pausedDL"},
                {"hash": "running-autobrr", "category": "autobrr", "state": "uploading"},
            ]
        )
        mover = Mover.__new__(Mover)
        mover.qbit = qbit
        mover.settings = SimpleNamespace(pause_categories={"radarr", "sonarr"})

        self.assertEqual(mover._managed_torrent_hashes(), ["running-radarr"])

    def test_graceful_shutdown_resumes_torrents_paused_by_mover(self) -> None:
        qbit = StubQbitClient()
        mover = Mover.__new__(Mover)
        mover.qbit = qbit
        mover.settings = SimpleNamespace(dry_run=False)
        mover.stop_requested = False
        mover.qbit_paused_by_us = True
        mover.qbit_paused_hashes = ["paused-by-mover"]

        def request_stop() -> None:
            mover.stop_requested = True

        mover._jellyfin_guard = request_stop
        mover.run()

        self.assertEqual(qbit.resumed_hashes, ["paused-by-mover"])
        self.assertFalse(mover.qbit_paused_by_us)


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
