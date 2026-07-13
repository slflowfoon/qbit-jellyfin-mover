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
        self.locations: list[tuple[str, str]] = []

    def torrents(self) -> list[dict]:
        return self.torrent_data

    def resume_hashes(self, hashes: list[str]) -> None:
        self.resumed_hashes.extend(hashes)

    def healthy(self) -> bool:
        return True

    def set_location(self, torrent_hash: str, location: str) -> None:
        self.locations.append((torrent_hash, location))


class StubJellyfinClient:
    def active_playback_sessions(self) -> list[dict]:
        return []


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
    def test_running_pause_categories_and_external_torrents_are_selected(self) -> None:
        qbit = StubQbitClient(
            [
                {"hash": "running-radarr", "category": "radarr", "state": "stalledUP"},
                {"hash": "stopped-radarr", "category": "radarr", "state": "stoppedUP"},
                {"hash": "paused-sonarr", "category": "sonarr", "state": "pausedDL"},
                {
                    "hash": "local-autobrr",
                    "category": "autobrr",
                    "state": "uploading",
                    "save_path": "/local-downloads/completed",
                },
                {
                    "hash": "external-autobrr",
                    "category": "autobrr",
                    "state": "stalledUP",
                    "save_path": "/data/downloads/autobrr",
                },
            ]
        )
        mover = Mover.__new__(Mover)
        mover.qbit = qbit
        mover.settings = SimpleNamespace(
            pause_categories={"radarr", "sonarr"},
            category_map={
                "radarr": "/data/downloads/movies",
                "sonarr": "/data/downloads/shows",
                "autobrr": "/data/downloads/autobrr",
            },
        )

        self.assertEqual(
            mover._managed_torrent_hashes(),
            ["running-radarr", "external-autobrr"],
        )

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


class PartialMoveRecoveryTest(unittest.TestCase):
    def make_mover(self, destination_base: Path, mode: str = "never") -> Mover:
        mover = Mover.__new__(Mover)
        mover.settings = SimpleNamespace(
            category_map={"autobrr": str(destination_base)},
            dry_run=False,
            scan_interval=0,
            pause_qbit_during_move=False,
            qbit_location_update_mode=mode,
        )
        mover.qbit = StubQbitClient()
        mover.jellyfin = StubJellyfinClient()
        mover.stop_requested = False
        mover.skipped_hashes = set()
        mover.pending_location_updates = []
        mover._sleep = lambda seconds: None
        return mover

    def test_unmarked_existing_destination_is_not_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "local" / "Release"
            destination_base = Path(tmp) / "external"
            destination = destination_base / source.name
            source.mkdir(parents=True)
            destination.mkdir(parents=True)
            mover = self.make_mover(destination_base)
            mover._rsync_interruptible = lambda *args: self.fail("rsync should not run")

            mover._move_candidate(
                {"hash": "abc", "name": "Release", "category": "autobrr", "root_path": str(source)}
            )

            self.assertEqual(mover.skipped_hashes, {"abc"})

    def test_marked_existing_destination_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "local" / "Release"
            destination_base = Path(tmp) / "external"
            destination = destination_base / source.name
            source.mkdir(parents=True)
            destination.mkdir(parents=True)
            (source / "remaining.bin").write_bytes(b"remaining")
            mover = self.make_mover(destination_base)
            marker = mover._partial_marker_path(destination_base, "abc")
            mover._write_partial_marker(marker, "abc", destination)

            def finish_rsync(source_path: Path, destination_path: Path) -> bool:
                source_file = source_path / "remaining.bin"
                (destination_path / "remaining.bin").write_bytes(source_file.read_bytes())
                source_file.unlink()
                return True

            mover._rsync_interruptible = finish_rsync

            mover._move_candidate(
                {"hash": "abc", "name": "Release", "category": "autobrr", "root_path": str(source)}
            )

            self.assertFalse(source.exists())
            self.assertEqual((destination / "remaining.bin").read_bytes(), b"remaining")
            self.assertFalse(marker.exists())

    def test_completed_copy_after_restart_updates_qbit_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "local" / "Release"
            destination_base = Path(tmp) / "external"
            destination = destination_base / source.name
            destination.mkdir(parents=True)
            mover = self.make_mover(destination_base, mode="safe")
            marker = mover._partial_marker_path(destination_base, "abc")
            mover._write_partial_marker(marker, "abc", destination)

            mover._move_candidate(
                {"hash": "abc", "name": "Release", "category": "autobrr", "root_path": str(source)}
            )

            self.assertEqual(mover.qbit.locations, [("abc", str(destination_base))])
            self.assertFalse(marker.exists())


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
