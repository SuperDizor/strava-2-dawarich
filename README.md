# strava-2-dawarich

`strava-2-dawarich` downloads GPS activities from Strava, converts them to GPX,
stores a local archive, and imports them into [Dawarich](https://github.com/Freika/dawarich).
It supports scheduled synchronization, resumable historical backfills, and
optional replacement of lower-quality Dawarich points with Strava tracks.

## How it works

For every Strava activity with GPS data, the application:

1. obtains or refreshes the Strava OAuth token;
2. downloads the activity and its available data streams;
3. creates a GPX file containing coordinates, time, elevation, heart rate,
   cadence, power, and temperature when available;
4. stores the GPX in persistent storage;
5. uploads it through the Dawarich imports API;
6. optionally removes explicitly approved legacy points that overlap the new
   track, while always preserving imported GPX points.

Normal sync is incremental. Historical imports and audits are checkpointed.
Destructive deduplication uses an allowlist, limits, reports, and backups.

## Requirements

- Docker with the Compose plugin (recommended), or Python 3.12+
- a Strava account and [Strava API application](https://www.strava.com/settings/api)
- a Dawarich instance and API key if automatic import is wanted

Set the Strava application's authorization callback domain to `localhost`. The
callback port defaults to `8089`.

## Quick start with Docker

```bash
git clone https://github.com/SuperDizor/strava-2-dawarich.git
cd strava-2-dawarich
cp .env.example .env
```

At minimum, edit these values in `.env`:

```dotenv
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=replace_me
DAWARICH_URL=https://dawarich.example.com
DAWARICH_API_KEY=replace_me
APPDATA_PATH=./data
```

Build and start the idle service:

```bash
docker compose up -d --build
docker compose ps
```

The container deliberately stays idle. Sync runs only when invoked manually or
by a scheduler, so a script failure cannot restart or destabilize Dawarich.

## Authorize Strava

```bash
docker compose exec strava-2-dawarich python strava_gpx.py auth
```

Open the printed URL, approve access, and let the browser return to
`http://localhost:8089/callback`. The token is saved in persistent state.

### Remote Docker host

When Docker runs on another machine, create an SSH tunnel from the workstation
where the browser runs:

```bash
ssh -L 8089:localhost:8089 user@docker-host
```

Keep that session open, run `auth` on the Docker host, and open its printed URL
in the workstation browser. The localhost callback travels through the tunnel.

## First validation and normal sync

Check recent activities without writing or uploading anything:

```bash
docker compose exec strava-2-dawarich \
  python strava_gpx.py sync --days 7 --dry-run
```

Perform the real sync:

```bash
docker compose exec strava-2-dawarich python strava_gpx.py sync
```

Other useful forms:

```bash
python strava_gpx.py sync --days 30     # fixed recent period
python strava_gpx.py sync --months 6    # 30 days per month
python strava_gpx.py sync --all         # request all activities
python strava_gpx.py sync --no-push     # create GPX without Dawarich upload
```

Inside Docker, prefix commands with
`docker compose exec strava-2-dawarich`.

### Virtual activities

`VirtualRide` and `VirtualRun` coordinates describe a virtual world and can
create unrealistic teleport distances in Dawarich. The application relocates
their track shape around a real home coordinate while preserving sensor data.

Home is resolved in this order:

1. `HOME_LOCATION=latitude,longitude` from `.env`;
2. a Dawarich place named `Home`;
3. an interactive address prompt when a terminal is available.

For unattended Docker and cron use, configure `HOME_LOCATION` explicitly.

### Upload existing GPX files

```bash
docker compose exec strava-2-dawarich python strava_gpx.py push
docker compose exec strava-2-dawarich \
  python strava_gpx.py push --dir /data/gpx --dry-run
```

Only filenames absent from `dawarich_imports.json` are submitted. Failed
uploads remain pending. Manual `push` never starts automatic deduplication.

## Persistent storage

Compose mounts three directories below `APPDATA_PATH`:

```text
APPDATA_PATH/
├── gpx/                         generated GPX archive
├── logs/                        application logs
└── state/                       tokens, checkpoints and safety reports
    ├── token.json               Strava OAuth token
    ├── state.json               incremental sync state and activity IDs
    ├── dawarich_imports.json    filenames accepted by Dawarich
    ├── auto_dedup_queue.json    unfinished automatic cleanup work
    ├── backfill_state.json      historical backfill checkpoint
    ├── audit_state.json         resumable audit checkpoint
    ├── audit-report.json        aggregate audit result
    ├── dedup-reports/           per-activity candidate reports
    └── dedup-backups/           point backups made before deletion
```

Back up `.env` and all three mounted directories. They contain credentials and
must not be committed to Git.

Console output is appended to `logs/strava-2-dawarich.log` by default. Set
`LOG_TO_FILE=false` to keep terminal output only.

## Internal HTTPS certificates

If Dawarich uses a private homelab CA, put only its public root certificate at
`certs/rootCA.pem`, or change `CUSTOM_CA_PATH`. The entrypoint combines it with
the public CA bundle, preserving trust for both Dawarich and Strava.

Never mount a CA private key or wildcard private key into this container.

## Historical backfill

Use `backfill` rather than `sync --all` for a large history. It checkpoints every
completed activity and reserves capacity in Strava's read-rate limits. The end
date is inclusive.

```bash
docker compose exec -T strava-2-dawarich \
  python strava_gpx.py backfill \
  --from 2020-01-01 \
  --to 2020-12-31 \
  --batch-size 50
```

Resume after a completed batch, interruption, or rate-limit pause:

```bash
docker compose exec -T strava-2-dawarich \
  python strava_gpx.py backfill --resume
```

Activities recorded by a prior sync or already visible in Dawarich are skipped.
Both older `gpx-*` and current `import-<id>-trk-*` Dawarich tracker formats are
recognized. An orphaned local upload marker is cleared only after a complete
Dawarich query confirms that the GPX is absent.

API capacity reserved for normal sync is configurable:

```dotenv
STRAVA_BACKFILL_15MIN_RESERVE=10
STRAVA_BACKFILL_DAILY_RESERVE=100
```

## Dawarich deduplication

Deduplication replaces overlapping points from selected legacy sources—such as
a watch or Google Timeline import—with the cleaner Strava GPX. It uses both the
activity time window and a geographic corridor around the track.

Imported GPX points are always protected. Only exact tracker IDs listed in
`DAWARICH_DEDUP_TRACKER_IDS` can become deletion candidates.

### Why tracker IDs exist

The tracker list is a safety boundary for deletion, not a requirement for normal
sync or audit. A Dawarich time window may contain unrelated data from another
phone, a family member, a manual import, or a future integration. Automatically
treating every non-GPX source as disposable could delete valid history.

The allowlist states exactly which sources Strava is allowed to replace:

```dotenv
DAWARICH_DEDUP_TRACKER_IDS=D5,google-phone-1
```

Leave it empty if no deletion is wanted. It is required only for manual or
automatic deletion. Audits and read-only previews still run and inventory the
tracker sources they discover.

### Read-only preview

```bash
docker compose exec -T strava-2-dawarich \
  python strava_gpx.py sync --days 7 --dedup-dawarich --dry-run
```

Reports are written to
`state/dedup-reports/strava-<activity-id>.json`. This command never deletes.

### Manual cleanup

After reviewing one report:

```bash
docker compose exec strava-2-dawarich \
  python strava_gpx.py dedup \
  --report /data/state/dedup-reports/strava-19732657015.json \
  --confirm-delete 19732657015
```

The command refuses reports without imported GPX points, full original point
data, exact confirmation, or a matching allowlist. It also refuses candidate
counts above `DAWARICH_DEDUP_MAX_POINTS`. The report is copied to
`state/dedup-backups/` before bounded deletion batches begin.

### Automatic post-import cleanup

Automatic cleanup is disabled by default. Enabling it also requires an explicit
tracker allowlist:

```dotenv
DAWARICH_AUTO_DEDUP=true
DAWARICH_DEDUP_TRACKER_IDS=D5,google-phone-1
DAWARICH_AUTO_DEDUP_MAX_POINTS=5000
DAWARICH_AUTO_DEDUP_WAIT_SECONDS=300
DAWARICH_AUTO_DEDUP_POLL_SECONDS=10
```

After upload, the application waits for Dawarich's GPX points, creates a report
and backup, deletes allowed overlap, and verifies that no candidates remain.
Interrupted work stays in `auto_dedup_queue.json` for the next normal sync. A
manual `push` does not trigger automatic cleanup.

## Full read-only audit

Audit every locally archived GPX against Dawarich:

```bash
docker compose exec -T strava-2-dawarich \
  python strava_gpx.py audit --batch-size 20
```

Continue with:

```bash
docker compose exec -T strava-2-dawarich \
  python strava_gpx.py audit --resume
```

The audit never deletes data. It reports local files checked, missing GPX
activities, every tracker source observed, and candidates eligible under the
optional allowlist. Progress is atomic and resumable. Results are stored in
`state/audit-report.json` and `state/dedup-reports/`.

## Scheduling with cron

This example runs every six hours and prevents overlapping executions:

```cron
0 */6 * * * cd /opt/stacks/strava-2-dawarich && /usr/bin/flock -n /tmp/strava-2-dawarich.lock /usr/bin/docker compose exec -T strava-2-dawarich python strava_gpx.py sync >> /tank/appdata/strava-2-dawarich/logs/cron.log 2>&1
```

The working directory matters because Compose must find `compose.yaml`. Use
absolute paths because cron has a minimal environment.

```bash
tail -n 100 /tank/appdata/strava-2-dawarich/logs/cron.log
```

## Updating

```bash
cd /opt/stacks/strava-2-dawarich
git pull --ff-only
docker compose up -d --build --force-recreate
docker compose ps
```

Persistent data remains intact because it is mounted outside the image.

## Troubleshooting

### Compose cannot find its configuration

Run Compose from the repository directory, or pass the file explicitly. This
is especially important for cron:

```bash
cd /opt/stacks/strava-2-dawarich
docker compose ps
```

### OAuth callback does not open

Confirm that port `8089` is published, the Strava callback domain is
`localhost`, and the SSH tunnel is active when the Docker host is remote.

### Upload succeeds but processing times out

Check Dawarich's import worker and import record. A successful HTTP upload only
means Dawarich accepted the file; point creation happens asynchronously. The
application preserves its checkpoint and safely retries or recognizes existing
`import-<id>-trk-*` points on the next run.

### Backfill reports no Compose configuration

The command was likely started outside the stack directory. Change to the
directory containing `compose.yaml` before running it.

## Command reference

| Command | Purpose | Remote effect |
|---|---|---|
| `auth` | Complete Strava OAuth | None |
| `sync` | Download new activities and upload GPX | Adds imports |
| `sync --dry-run` | Preview activities | None |
| `sync --dedup-dawarich --dry-run` | Preview overlap and sources | None |
| `push` | Upload pending or external GPX | Adds imports |
| `backfill` | Checkpointed historical import | Adds imports |
| `audit` | Check all archived GPX activities | None |
| `dedup` | Delete reviewed allowlisted candidates | Deletes points |

Run `python strava_gpx.py <command> --help` for command-specific arguments.

## Important environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `STRAVA_CLIENT_ID` | Yes | — | Strava application ID |
| `STRAVA_CLIENT_SECRET` | Yes | — | Strava application secret |
| `DAWARICH_URL` | Dawarich features | — | Dawarich base URL |
| `DAWARICH_API_KEY` | Dawarich features | — | Dawarich API bearer token |
| `APPDATA_PATH` | Recommended | `./data` | Persistent host directory |
| `HOME_LOCATION` | Virtual activities | automatic/prompt | `latitude,longitude` anchor |
| `CUSTOM_CA_PATH` | Private CA only | `./certs/rootCA.pem` | Public root CA path |
| `LOG_TO_FILE` | No | `true` | Write application log |
| `DAWARICH_DEDUP_TRACKER_IDS` | Deletion only | empty | Exact replaceable sources |
| `DAWARICH_DEDUP_RADIUS_METERS` | No | `200` | Track corridor radius |
| `DAWARICH_DEDUP_MAX_POINTS` | No | `5000` | Manual deletion limit |
| `DAWARICH_AUTO_DEDUP` | No | `false` | Enable post-import cleanup |

See `.env.example` for the complete configuration.

## Local Python installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python strava_gpx.py auth
python strava_gpx.py sync --dry-run --days 7
```

Without Docker, data paths are project-relative unless overridden by
`DATA_DIR`, `STATE_DIR`, `GPX_DIR`, and `LOG_DIR`.

## Safety model

- dry-run and audit operations never delete remote data;
- filenames and processed activity IDs prevent routine duplicates;
- Dawarich is queried before historical re-upload decisions;
- imported GPX tracker formats are always protected;
- deletion requires an exact source allowlist;
- manual deletion requires the exact activity ID;
- reports contain original points and are backed up before deletion;
- safety limits and batches constrain accidental impact;
- backfill and audit state are atomic and resumable.
