# Strava-2-Dawarich

Pull Strava activities as GPX files and push them into [Dawarich](https://github.com/Freika/dawarich) for location tracking.

## How It Works

1. Authenticates with Strava via OAuth (opens your browser)
2. Fetches activities and their GPS streams from the Strava API
3. Converts each activity to a GPX file (includes HR, cadence, power, temp if available)
4. Optionally pushes GPX files to Dawarich's import API

On first run, a baseline date is set to **now** — only future activities are synced. Use `--days`, `--months`, or `--all` to pull historical data.

## Prerequisites

- Python 3.8+
- A [Strava API Application](https://www.strava.com/settings/api) with:
  - **Authorization Callback Domain**: `localhost`
- (Optional) A running Dawarich instance with an API key

## Setup

```bash
# Clone and install
git clone https://github.com/JOHNKIMBLE/strava-2-darawich.git
cd strava-2-darawich
pip install -r requirements.txt
```

### Option A: `.env` file (quickest)

```bash
cp .env.example .env
# Edit .env with your values
```

### Option B: Interactive setup

```bash
python strava_gpx.py setup
```

Both methods work — `.env` takes priority if both exist.

### Virtual Rides (Zwift, etc.)

Virtual rides report GPS from the virtual world (New Caledonia, London, etc.), which creates huge "teleport" distances in Dawarich between your real location and the virtual one. The script auto-detects virtual activities (`VirtualRide`, `VirtualRun`) and relocates their GPS to your home coordinates — preserving the ride's shape, distance, and sensor data without the teleport.

Home location is resolved automatically in this order:

1. `HOME_LOCATION` in `.env` (e.g. `HOME_LOCATION=41.8781,-87.6298`)
2. A place named "Home" in your Dawarich instance (via the Places API)
3. Interactive prompt — the script asks for your city/address, geocodes it, and saves to `.env`

Once set, it's cached in `.env` and never asked again.

Then authorize with Strava:

```bash
python strava_gpx.py auth
```

## Usage

### Sync new activities

```bash
# Sync activities since last run
python strava_gpx.py sync

# Sync but don't push to Dawarich
python strava_gpx.py sync --no-push
```

### Pull historical data

```bash
# Last 30 days
python strava_gpx.py sync --days 30

# Last 6 months
python strava_gpx.py sync --months 6

# Everything
python strava_gpx.py sync --all
```

### Push existing GPX files to Dawarich

```bash
# Push all files from gpx_output/
python strava_gpx.py push

# Push from a custom directory
python strava_gpx.py push --dir /path/to/gpx/files
```

## Docker

Copy the example environment and choose a persistent host directory:

```bash
cp .env.example .env
# For the homelab, set APPDATA_PATH=/tank/appdata/strava-2-dawarich
docker compose up -d --build
```

The container stays idle until a command is requested, which keeps failures isolated
from Dawarich and makes development predictable:

```bash
docker compose exec strava-2-dawarich python strava_gpx.py auth
docker compose exec strava-2-dawarich python strava_gpx.py sync --dry-run --days 7
docker compose exec strava-2-dawarich python strava_gpx.py sync
```

Persistent files are separated under `state/`, `gpx/`, and `logs/`. Successful
Dawarich uploads are recorded in `state/dawarich_imports.json`; later pushes skip
the same filename. This is the first deduplication layer and does not yet query
Dawarich's own records.

`--dry-run` fetches and evaluates activities but does not write GPX/state files or
push anything to Dawarich. It is also available on the `push` command.

Console output is also appended to `logs/strava-2-dawarich.log` by default.
Set `LOG_TO_FILE=false` to disable file logging.

### Internal HTTPS certificates

For a Dawarich endpoint signed by a homelab CA, copy only the public root
certificate to `certs/rootCA.pem`, or set `CUSTOM_CA_PATH` to its host path.
The container combines it with the public certifi roots, preserving TLS trust
for both Dawarich and Strava. Never copy the CA private key into the project.

Failed Dawarich uploads remain pending and are retried automatically by the
next `sync`. A failed upload also causes a non-zero exit code for monitoring.

### Dawarich overlap preview

The first deduplication phase is deliberately read-only. It compares Dawarich
points in each activity's `start_date` to `start_date + elapsed_time` window
against the generated Strava track:

```bash
docker compose exec strava-2-dawarich \
  python strava_gpx.py sync --days 7 --dedup-dawarich --dry-run
```

Points within `DAWARICH_DEDUP_RADIUS_METERS` (200 meters by default) are listed
as deletion candidates only when their `tracker_id` is explicitly listed in
`DAWARICH_DEDUP_TRACKER_IDS` (comma-separated). Imported `gpx-*` points are
always preserved. JSON reports are written to
`state/dedup-reports/strava-<activity-id>.json`. This release never sends a
DELETE request; `--dedup-dawarich` is rejected unless `--dry-run` is also set.

After manually reviewing a newly generated report, one activity can be cleaned
with an explicit activity-ID confirmation:

```bash
docker compose exec strava-2-dawarich python strava_gpx.py dedup \
  --report /data/state/dedup-reports/strava-19732657015.json \
  --confirm-delete 19732657015
```

The command refuses reports without full point backups, without existing GPX
points, above `DAWARICH_DEDUP_MAX_POINTS`, or containing trackers outside the
current allowlist. It copies the reviewed report to `state/dedup-backups/`
before deleting in bounded batches. This manual command is never invoked unless
explicitly requested; automatic behavior requires the separate opt-in below.

### Automatic post-import deduplication

Automatic cleanup is disabled by default. When `DAWARICH_AUTO_DEDUP=true`, a
normal `sync` performs cleanup only for GPX files successfully uploaded during
that same run. It waits until Dawarich exposes `gpx-*` points, generates the
same full backup report, applies the tracker allowlist and automatic safety
limit, deletes in batches, and verifies that zero candidates remain.

```dotenv
DAWARICH_AUTO_DEDUP=true
DAWARICH_AUTO_DEDUP_MAX_POINTS=5000
DAWARICH_AUTO_DEDUP_WAIT_SECONDS=300
DAWARICH_AUTO_DEDUP_POLL_SECONDS=10
```

If import processing times out, candidate count exceeds the limit, deletion
fails, or post-delete verification finds remaining candidates, `sync` exits
non-zero so cron records a failure. The GPX remains in
`state/auto_dedup_queue.json` and is retried on the next sync; it is removed
from the queue only after successful post-delete verification. Manual `push`
never triggers auto-dedup.

### Resumable historical backfill

Historical imports run in bounded batches and checkpoint after every completed
activity. The end date is inclusive:

```bash
docker compose exec strava-2-dawarich python strava_gpx.py backfill \
  --from 2024-01-01 --to 2024-12-31 --batch-size 50
```

Continue the same period with:

```bash
docker compose exec strava-2-dawarich \
  python strava_gpx.py backfill --resume
```

State is stored atomically in `state/backfill_state.json`. Each activity is
fully processed through GPX creation, Dawarich upload, optional automatic
deduplication, and verification before its checkpoint is committed. Existing
`fetched_ids` and GPX points already visible in Dawarich are skipped safely.

During backfill, Strava's `X-ReadRateLimit-*` headers are monitored. Processing
pauses before consuming `STRAVA_BACKFILL_15MIN_RESERVE` or
`STRAVA_BACKFILL_DAILY_RESERVE`; resume after the indicated reset. This avoids
using the request capacity needed by normal scheduled syncs.

If an interrupted import left a filename in the local upload registry but
Dawarich exposes no GPX points in that activity window, `--resume` clears the
orphaned marker and re-uploads the file. This recovery happens only after a
complete Dawarich point query confirms the GPX is absent.

### Read-only historical audit

Audit saved GPX activities in bounded batches after a large backfill:

```bash
docker compose exec -T strava-2-dawarich \
  python strava_gpx.py audit --batch-size 20
```

Continue with `python strava_gpx.py audit --resume`. The audit never deletes
remote data. It verifies that Dawarich exposes imported GPX points and reports
legacy points from `DAWARICH_DEDUP_TRACKER_IDS` within the configured corridor.
Progress is stored in `state/audit_state.json`, the aggregate result in
`state/audit-report.json`, and reviewed per-activity reports remain in
`state/dedup-reports/` for the separately confirmed `dedup` command.

## Automating with Cron (Unraid User Scripts)

Add a User Script on Unraid to sync on a schedule:

```bash
#!/bin/bash
cd /path/to/Strava-2-Darawich
python3 strava_gpx.py sync
```

Set the schedule (e.g., every 6 hours) in the User Scripts plugin.

On a regular Linux Docker host, the equivalent user crontab entry is:

```cron
0 */6 * * * cd /opt/stacks/strava-2-dawarich && /usr/bin/flock -n /tmp/strava-2-dawarich.lock /usr/bin/docker compose exec -T strava-2-dawarich python strava_gpx.py sync >> /tank/appdata/strava-2-dawarich/logs/cron.log 2>&1
```

## Files

| File | Purpose |
|------|---------|
| `strava_gpx.py` | Main script |
| `.env` | Strava/Dawarich credentials (git-ignored) |
| `.env.example` | Template for `.env` |
| `config.json` | Alternative to `.env` — created by `setup` (git-ignored) |
| `token.json` | OAuth tokens (git-ignored) |
| `state.json` | Tracks last sync time and fetched activity IDs (git-ignored) |
| `gpx_output/` | Generated GPX files (git-ignored) |

## Rate Limits

Strava API allows 100 requests per 15 minutes and 1,000 per day. Each activity requires 2 API calls (list + streams). The script includes built-in rate limiting delays.
