# qbit-jellyfin-mover

Dockerized controller for moving completed qBittorrent downloads from local SSD/ZFS storage to an external media/download disk without interrupting Jellyfin users.

It waits until Jellyfin has no authenticated sessions for a grace period, pauses qBittorrent, and moves one completed torrent outside qBittorrent with `rsync`.

By default it only calls qBittorrent `setLocation` in `safe` mode: after the external copy has completed, the source has been removed, the destination exists, and qBittorrent's API is responding quickly. This avoids using qBittorrent's own large file move engine, which can make qBittorrent's WebUI/API unavailable and break Radarr/Sonarr communication.

## Intended Flow

Configure qBittorrent to download and complete locally:

```text
/local-downloads/incomplete
/local-downloads/completed/radarr
/local-downloads/completed/sonarr
/local-downloads/completed/autobrr
```

Configure final category destinations to remain the paths your apps already use:

```text
radarr  -> /data/downloads/movies
sonarr  -> /data/downloads/shows
autobrr -> /data/downloads/autobrr
```

Do not mount `/local-downloads` into Radarr/Sonarr. They should import only after the mover places completed files under `/data/downloads/...`.

## Behavior

- Treats any authenticated Jellyfin session as activity, not only playback.
- Checks Jellyfin sessions with `activeWithinSeconds`.
- Requires Jellyfin to be idle for `IDLE_SECONDS` after no recent sessions are returned.
- Checks Jellyfin every `ACTIVE_CHECK_INTERVAL` seconds while moving.
- Stops `rsync` if a Jellyfin user appears.
- Pauses qBittorrent while Jellyfin is active and while moving.
- Health-checks qBittorrent before starting a move.
- Copies/removes files outside qBittorrent with `rsync`.
- Optionally updates qBittorrent location after a successful move, depending on `QBIT_LOCATION_UPDATE_MODE`.
- Supports category-specific minimum ages before moving, for example keeping `autobrr` local for 48 hours.
- Can pause only selected categories, so `autobrr` traffic can continue during Jellyfin activity.

## Jellyfin API Key

Create an API key:

```text
Jellyfin Dashboard -> API Keys -> New API Key
Name: qbit-mover
```

Set it as `JELLYFIN_API_KEY`.

## Docker

Start in dry-run mode first:

```bash
docker compose -f docker-compose.example.yml up
```

Use the GitHub Container Registry image built by this repo:

```text
ghcr.io/OWNER/qbit-jellyfin-mover:latest
```

Replace `OWNER` in `docker-compose.example.yml` with your GitHub username or organization.

## Environment

Required:

```text
JELLYFIN_URL
JELLYFIN_API_KEY
QBIT_URL
CATEGORY_MAP
```

Optional:

```text
QBIT_USERNAME
QBIT_PASSWORD
IDLE_SECONDS=600
ACTIVE_WITHIN_SECONDS=600
ACTIVE_CHECK_INTERVAL=2
SCAN_INTERVAL=30
DRY_RUN=false
PAUSE_QBIT_WHEN_JELLYFIN_ACTIVE=true
PAUSE_QBIT_DURING_MOVE=true
QBIT_HEALTH_REQUIRED=true
QBIT_API_TIMEOUT=8
QBIT_HEALTH_TIMEOUT=3
QBIT_LOCATION_UPDATE_MODE=safe
CATEGORY_MIN_AGE_SECONDS={}
PAUSE_CATEGORIES=["radarr","sonarr","sportarr"]
LOG_LEVEL=INFO
```

`QBIT_LOCATION_UPDATE_MODE` controls whether the mover calls qBittorrent `setLocation`:

```text
safe   Only update after rsync has completed, source is gone, destination exists, and qBittorrent is healthy.
never  Never update qBittorrent location. This avoids qBittorrent move work, but torrents may show missing files after the source is removed.
always Previous behavior: call setLocation after a successful rsync.
```

`CATEGORY_MAP` must be JSON:

```json
{
  "radarr": "/data/downloads/movies",
  "sonarr": "/data/downloads/shows",
  "autobrr": "/data/downloads/autobrr",
  "sportarr": "/data/downloads/sportarr"
}
```

Use `CATEGORY_MIN_AGE_SECONDS` to keep categories local before moving:

```json
{
  "autobrr": 172800
}
```

Use `PAUSE_CATEGORIES` to control which categories may be paused when Jellyfin is active or during a move. To keep `autobrr` downloading/uploading while Jellyfin is active:

```json
["radarr", "sonarr", "sportarr"]
```

## qBittorrent Notes

Set category save paths to local completed folders:

```text
radarr  -> /local-downloads/completed/radarr
sonarr  -> /local-downloads/completed/sonarr
autobrr -> /local-downloads/completed/autobrr
```

Set incomplete downloads to:

```text
/local-downloads/incomplete
```

After the mover finishes a torrent, qBittorrent can be updated to the final destination base path, such as `/data/downloads/movies`. Keep `QBIT_LOCATION_UPDATE_MODE=safe` unless you deliberately want to skip qBittorrent metadata updates.

## Safety

Run with `DRY_RUN=true` until logs show exactly the torrents you expect. The mover skips files that already appear to be under their final destination.
