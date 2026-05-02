# qbit-jellyfin-mover

Dockerized controller for moving completed qBittorrent downloads from local SSD/ZFS storage to an external media/download disk without interrupting Jellyfin users.

It waits until Jellyfin has no authenticated sessions for a grace period, pauses qBittorrent, moves one completed torrent, and updates qBittorrent's torrent location with `setLocation` so Radarr/Sonarr keep seeing the expected final path.

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
- Calls qBittorrent `setLocation` after a successful move.

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
LOG_LEVEL=INFO
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

After the mover finishes a torrent, qBittorrent will be updated to the final destination base path, such as `/data/downloads/movies`.

## Safety

Run with `DRY_RUN=true` until logs show exactly the torrents you expect. The mover skips files that already appear to be under their final destination.
