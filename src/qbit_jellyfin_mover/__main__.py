from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LOG = logging.getLogger("qbit-jellyfin-mover")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    jellyfin_url: str
    jellyfin_api_key: str
    qbit_url: str
    qbit_username: str | None
    qbit_password: str | None
    category_map: dict[str, str]
    idle_seconds: int
    active_within_seconds: int
    active_check_interval: int
    scan_interval: int
    dry_run: bool
    pause_qbit_when_jellyfin_active: bool
    pause_qbit_during_move: bool
    qbit_api_timeout: int
    qbit_health_timeout: int
    qbit_location_update_mode: str
    qbit_health_required: bool

    @classmethod
    def from_env(cls) -> "Settings":
        category_map_raw = os.getenv(
            "CATEGORY_MAP",
            json.dumps(
                {
                    "radarr": "/data/downloads/movies",
                    "sonarr": "/data/downloads/shows",
                    "autobrr": "/data/downloads/autobrr",
                    "sportarr": "/data/downloads/sportarr",
                }
            ),
        )
        try:
            category_map = json.loads(category_map_raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"CATEGORY_MAP is not valid JSON: {exc}") from exc
        if not isinstance(category_map, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in category_map.items()
        ):
            raise SystemExit("CATEGORY_MAP must be a JSON object of category to destination path")

        jellyfin_url = os.getenv("JELLYFIN_URL", "").rstrip("/")
        jellyfin_api_key = os.getenv("JELLYFIN_API_KEY", "")
        qbit_url = os.getenv("QBIT_URL", "http://127.0.0.1:8085").rstrip("/")
        if not jellyfin_url:
            raise SystemExit("JELLYFIN_URL is required")
        if not jellyfin_api_key:
            raise SystemExit("JELLYFIN_API_KEY is required")

        qbit_location_update_mode = os.getenv("QBIT_LOCATION_UPDATE_MODE", "safe").lower()
        if qbit_location_update_mode not in {"never", "safe", "always"}:
            raise SystemExit("QBIT_LOCATION_UPDATE_MODE must be one of: never, safe, always")

        return cls(
            jellyfin_url=jellyfin_url,
            jellyfin_api_key=jellyfin_api_key,
            qbit_url=qbit_url,
            qbit_username=os.getenv("QBIT_USERNAME") or None,
            qbit_password=os.getenv("QBIT_PASSWORD") or None,
            category_map=category_map,
            idle_seconds=int(os.getenv("IDLE_SECONDS", "600")),
            active_within_seconds=int(os.getenv("ACTIVE_WITHIN_SECONDS", "600")),
            active_check_interval=int(os.getenv("ACTIVE_CHECK_INTERVAL", "2")),
            scan_interval=int(os.getenv("SCAN_INTERVAL", "30")),
            dry_run=env_bool("DRY_RUN", False),
            pause_qbit_when_jellyfin_active=env_bool("PAUSE_QBIT_WHEN_JELLYFIN_ACTIVE", True),
            pause_qbit_during_move=env_bool("PAUSE_QBIT_DURING_MOVE", True),
            qbit_api_timeout=int(os.getenv("QBIT_API_TIMEOUT", "8")),
            qbit_health_timeout=int(os.getenv("QBIT_HEALTH_TIMEOUT", "3")),
            qbit_location_update_mode=qbit_location_update_mode,
            qbit_health_required=env_bool("QBIT_HEALTH_REQUIRED", True),
        )


class HttpClient:
    def __init__(self) -> None:
        self.cookie_jar: dict[str, str] = {}

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: int = 15,
    ) -> tuple[int, bytes, dict[str, str]]:
        request_headers = dict(headers or {})
        if self.cookie_jar:
            request_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookie_jar.items())
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self._save_cookies(resp.headers)
                return resp.status, resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self._save_cookies(exc.headers)
            return exc.code, body, dict(exc.headers.items())

    def _save_cookies(self, headers: Any) -> None:
        cookies = headers.get_all("Set-Cookie") if hasattr(headers, "get_all") else None
        for cookie in cookies or []:
            first = cookie.split(";", 1)[0]
            if "=" in first:
                key, value = first.split("=", 1)
                self.cookie_jar[key] = value


class JellyfinClient:
    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.settings = settings
        self.http = http

    def active_user_sessions(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"activeWithinSeconds": str(self.settings.active_within_seconds)}
        )
        url = f"{self.settings.jellyfin_url}/Sessions?{query}"
        status, body, _ = self.http.request(
            "GET",
            url,
            headers={"X-Emby-Token": self.settings.jellyfin_api_key},
            timeout=10,
        )
        if status != 200:
            raise RuntimeError(f"Jellyfin /Sessions returned HTTP {status}: {body[:200]!r}")
        sessions = json.loads(body.decode("utf-8"))
        active = []
        for session in sessions:
            # Treat any authenticated user session as activity, not only playback.
            if session.get("IsActive", True) and (session.get("UserId") or session.get("UserName")):
                active.append(session)
        return active


class QBittorrentClient:
    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.settings = settings
        self.http = http
        self.logged_in = False

    def _url(self, path: str) -> str:
        return f"{self.settings.qbit_url}{path}"

    def login(self) -> None:
        if self.logged_in or not self.settings.qbit_username:
            self.logged_in = True
            return
        payload = urllib.parse.urlencode(
            {"username": self.settings.qbit_username, "password": self.settings.qbit_password or ""}
        ).encode()
        status, body, _ = self.http.request(
            "POST",
            self._url("/api/v2/auth/login"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
            timeout=self.settings.qbit_api_timeout,
        )
        if status != 200 or body.strip() != b"Ok.":
            raise RuntimeError(f"qBittorrent login failed: HTTP {status} {body[:200]!r}")
        self.logged_in = True

    def api_get(self, path: str) -> Any:
        self.login()
        status, body, _ = self.http.request(
            "GET", self._url(path), timeout=self.settings.qbit_api_timeout
        )
        if status != 200:
            raise RuntimeError(f"qBittorrent GET {path} returned HTTP {status}: {body[:200]!r}")
        return json.loads(body.decode("utf-8"))

    def api_post(self, path: str, fields: dict[str, str]) -> None:
        self.login()
        data = urllib.parse.urlencode(fields).encode()
        status, body, _ = self.http.request(
            "POST",
            self._url(path),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data,
            timeout=self.settings.qbit_api_timeout,
        )
        if status not in {200, 204}:
            raise RuntimeError(f"qBittorrent POST {path} returned HTTP {status}: {body[:200]!r}")

    def torrents(self) -> list[dict[str, Any]]:
        return self.api_get("/api/v2/torrents/info")

    def healthy(self) -> bool:
        try:
            self.login()
            status, body, _ = self.http.request(
                "GET",
                self._url("/api/v2/app/version"),
                timeout=self.settings.qbit_health_timeout,
            )
            if status == 200 and body.strip():
                return True
            LOG.warning("qBittorrent health check failed: HTTP %s %r", status, body[:80])
            return False
        except Exception as exc:
            LOG.warning("qBittorrent health check failed: %s", exc)
            return False

    def pause_all(self) -> None:
        LOG.info("Pausing all qBittorrent torrents")
        try:
            self.api_post("/api/v2/torrents/stop", {"hashes": "all"})
        except RuntimeError:
            self.api_post("/api/v2/torrents/pause", {"hashes": "all"})

    def resume_all(self) -> None:
        LOG.info("Resuming all qBittorrent torrents")
        try:
            self.api_post("/api/v2/torrents/start", {"hashes": "all"})
        except RuntimeError:
            self.api_post("/api/v2/torrents/resume", {"hashes": "all"})

    def pause_hashes(self, hashes: list[str]) -> None:
        if not hashes:
            return
        LOG.info("Pausing %s managed qBittorrent torrents", len(hashes))
        joined = "|".join(hashes)
        try:
            self.api_post("/api/v2/torrents/stop", {"hashes": joined})
        except RuntimeError:
            self.api_post("/api/v2/torrents/pause", {"hashes": joined})

    def resume_hashes(self, hashes: list[str]) -> None:
        if not hashes:
            return
        LOG.info("Resuming %s managed qBittorrent torrents", len(hashes))
        joined = "|".join(hashes)
        try:
            self.api_post("/api/v2/torrents/start", {"hashes": joined})
        except RuntimeError:
            self.api_post("/api/v2/torrents/resume", {"hashes": joined})

    def set_location(self, torrent_hash: str, location: str) -> None:
        LOG.info("Updating qBittorrent location for %s to %s", torrent_hash, location)
        self.api_post("/api/v2/torrents/setLocation", {"hashes": torrent_hash, "location": location})


class Mover:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = HttpClient()
        self.jellyfin = JellyfinClient(settings, self.http)
        self.qbit = QBittorrentClient(settings, self.http)
        self.stop_requested = False
        self.qbit_paused_by_us = False
        self.qbit_paused_hashes: list[str] = []
        self.pending_location_updates: list[dict[str, str]] = []
        self.skipped_hashes: set[str] = set()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        LOG.info("Starting qBittorrent/Jellyfin mover dry_run=%s", self.settings.dry_run)
        while not self.stop_requested:
            self._jellyfin_guard()
            if self.settings.qbit_health_required and not self.qbit.healthy():
                LOG.warning(
                    "qBittorrent API is not healthy; not starting any file move this cycle"
                )
                self._sleep(self.settings.scan_interval)
                continue
            self._flush_pending_location_updates()
            candidate = self._next_candidate()
            if candidate is None:
                self._resume_if_paused()
                self._sleep(self.settings.scan_interval)
                continue
            self._move_candidate(candidate)

    def _stop(self, *_: Any) -> None:
        self.stop_requested = True

    def _sleep(self, seconds: int) -> None:
        end = time.monotonic() + seconds
        while not self.stop_requested and time.monotonic() < end:
            time.sleep(min(1, end - time.monotonic()))

    def _jellyfin_guard(self) -> None:
        idle_since: float | None = None
        while not self.stop_requested:
            active = self.jellyfin.active_user_sessions()
            if active:
                idle_since = None
                names = ", ".join(str(s.get("UserName") or s.get("UserId")) for s in active)
                LOG.info("Jellyfin has active sessions (%s); waiting", names)
                if self.settings.pause_qbit_when_jellyfin_active:
                    self._pause_if_needed()
                self._sleep(self.settings.active_check_interval)
                continue
            if idle_since is None:
                idle_since = time.monotonic()
                LOG.info("Jellyfin has no active sessions; starting idle timer")
            remaining = self.settings.idle_seconds - int(time.monotonic() - idle_since)
            if remaining <= 0:
                return
            LOG.info("Waiting for Jellyfin idle grace: %ss remaining", remaining)
            self._sleep(min(self.settings.active_check_interval, max(1, remaining)))

    def _pause_if_needed(self) -> None:
        if self.settings.dry_run or self.qbit_paused_by_us:
            return
        hashes = self._managed_torrent_hashes()
        if not hashes:
            LOG.info("No managed qBittorrent torrents to pause")
            return
        self.qbit.pause_hashes(hashes)
        self.qbit_paused_hashes = hashes
        self.qbit_paused_by_us = True

    def _resume_if_paused(self) -> None:
        if self.settings.dry_run or not self.qbit_paused_by_us:
            return
        self.qbit.resume_hashes(self.qbit_paused_hashes)
        self.qbit_paused_hashes = []
        self.qbit_paused_by_us = False

    def _managed_torrent_hashes(self) -> list[str]:
        managed_categories = set(self.settings.category_map)
        hashes = []
        for torrent in self.qbit.torrents():
            if (torrent.get("category") or "") in managed_categories and torrent.get("hash"):
                hashes.append(torrent["hash"])
        return hashes

    def _next_candidate(self) -> dict[str, Any] | None:
        for torrent in self.qbit.torrents():
            category = torrent.get("category") or ""
            if category not in self.settings.category_map:
                continue
            if torrent.get("hash") in self.skipped_hashes:
                continue
            if float(torrent.get("progress") or 0) < 1:
                continue
            if torrent.get("state") in {"moving", "checkingDL", "checkingUP", "checkingResumeData"}:
                continue
            root_path = torrent.get("root_path") or torrent.get("content_path") or ""
            if not root_path:
                continue
            destination_base = self.settings.category_map[category].rstrip("/")
            if root_path == destination_base or root_path.startswith(destination_base + "/"):
                continue
            return torrent
        return None

    def _move_candidate(self, torrent: dict[str, Any]) -> None:
        name = torrent.get("name", "<unknown>")
        torrent_hash = torrent["hash"]
        category = torrent.get("category") or ""
        source = Path(torrent.get("root_path") or torrent.get("content_path"))
        destination_base = Path(self.settings.category_map[category])
        destination = destination_base / source.name
        LOG.info("Candidate complete torrent: %s", name)
        LOG.info("Move plan: %s -> %s", source, destination)

        if self.settings.dry_run:
            LOG.info("Dry run: not moving %s", name)
            self._sleep(self.settings.scan_interval)
            return

        if not source.exists():
            LOG.warning("Source path no longer exists: %s", source)
            self.skipped_hashes.add(torrent_hash)
            self._sleep(self.settings.scan_interval)
            return

        destination_base.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            LOG.warning("Destination already exists; skipping to avoid overwrite: %s", destination)
            self.skipped_hashes.add(torrent_hash)
            self._sleep(self.settings.scan_interval)
            return

        if self.settings.pause_qbit_during_move:
            self._pause_if_needed()

        while not self.stop_requested:
            active = self.jellyfin.active_user_sessions()
            if active:
                LOG.info("Jellyfin session appeared before move; waiting again")
                self._jellyfin_guard()
                continue
            ok = self._rsync_interruptible(source, destination)
            if ok:
                self._remove_empty_parents(source)
                if not self._update_qbit_location(
                    torrent_hash, name, source, destination, destination_base
                ):
                    self._queue_location_update(
                        torrent_hash, name, destination, destination_base
                    )
                LOG.info("Move completed for %s", name)
                return
            LOG.info("Move interrupted; waiting for Jellyfin idle grace before resuming")
            self._jellyfin_guard()

    def _update_qbit_location(
        self,
        torrent_hash: str,
        name: str,
        source: Path,
        destination: Path,
        destination_base: Path,
    ) -> bool:
        mode = self.settings.qbit_location_update_mode
        if mode == "never":
            LOG.info("Skipping qBittorrent location update for %s: mode=never", name)
            return True

        if mode == "safe":
            if source.exists():
                LOG.warning(
                    "Skipping qBittorrent location update for %s: source still exists after rsync",
                    name,
                )
                return False
            if not destination.exists():
                LOG.warning(
                    "Skipping qBittorrent location update for %s: destination is missing: %s",
                    name,
                    destination,
                )
                return False
            if not self.qbit.healthy():
                LOG.warning(
                    "Skipping qBittorrent location update for %s: API is not healthy", name
                )
                return False

        try:
            self.qbit.set_location(torrent_hash, str(destination_base))
        except Exception as exc:
            LOG.warning("qBittorrent location update failed for %s: %s", name, exc)
            return False

        if mode == "safe" and not self.qbit.healthy():
            LOG.warning(
                "qBittorrent API became unhealthy after location update for %s; "
                "future moves will wait for recovery",
                name,
            )
            return False
        return True

    def _queue_location_update(
        self, torrent_hash: str, name: str, destination: Path, destination_base: Path
    ) -> None:
        if self.settings.qbit_location_update_mode == "never":
            return
        if any(item["hash"] == torrent_hash for item in self.pending_location_updates):
            return
        self.pending_location_updates.append(
            {
                "hash": torrent_hash,
                "name": name,
                "destination": str(destination),
                "destination_base": str(destination_base),
            }
        )
        LOG.warning("Queued qBittorrent location update retry for %s", name)

    def _flush_pending_location_updates(self) -> None:
        if not self.pending_location_updates:
            return
        if self.settings.qbit_location_update_mode == "never":
            self.pending_location_updates.clear()
            return
        if not self.qbit.healthy():
            LOG.info(
                "qBittorrent API is not healthy; keeping %s location updates pending",
                len(self.pending_location_updates),
            )
            return
        remaining = []
        for index, item in enumerate(self.pending_location_updates):
            ok = self._update_qbit_location(
                item["hash"],
                item["name"],
                Path(item["destination"]).parent / ".qbit-mover-source-already-moved",
                Path(item["destination"]),
                Path(item["destination_base"]),
            )
            if not ok:
                remaining = self.pending_location_updates[index:]
                break
        self.pending_location_updates = remaining

    def _rsync_interruptible(self, source: Path, destination: Path) -> bool:
        source_arg = str(source)
        destination_arg = str(destination)
        cmd = [
            "rsync",
            "-a",
            "--partial",
            "--append-verify",
            "--remove-source-files",
            source_arg,
            destination_arg,
        ]
        LOG.info("Running: %s", " ".join(cmd))
        proc = subprocess.Popen(cmd)
        try:
            while proc.poll() is None:
                active = self.jellyfin.active_user_sessions()
                if active or self.stop_requested:
                    LOG.info("Stopping rsync because Jellyfin became active or shutdown was requested")
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=10)
                    return False
                time.sleep(self.settings.active_check_interval)
            if proc.returncode == 0:
                return True
            LOG.warning("rsync exited with code %s", proc.returncode)
            return False
        finally:
            if proc.poll() is None:
                proc.kill()

    def _remove_empty_parents(self, source: Path) -> None:
        if source.is_dir():
            shutil.rmtree(source, ignore_errors=True)
            return
        parent = source.parent
        try:
            source.unlink(missing_ok=True)
        except OSError:
            pass
        while parent.name and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    settings = Settings.from_env()
    Mover(settings).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
