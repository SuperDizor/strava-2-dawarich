#!/usr/bin/env python3
"""
Strava-2-Dawarich: Pull Strava activities as GPX and push to Dawarich.
"""

import argparse
import json
import math
import os
import shutil
import sys
import time
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

VERSION = "1.0.0"

SCRIPT_DIR = Path(__file__).parent.resolve()
ENV_FILE = SCRIPT_DIR / ".env"
DATA_DIR = Path(os.environ.get("DATA_DIR", SCRIPT_DIR)).resolve()
STATE_DIR = Path(os.environ.get("STATE_DIR", DATA_DIR)).resolve()
OUTPUT_DIR = Path(os.environ.get("GPX_DIR", DATA_DIR / "gpx_output")).resolve()
LOG_DIR = Path(os.environ.get("LOG_DIR", DATA_DIR / "logs")).resolve()
CONFIG_FILE = STATE_DIR / "config.json"
STATE_FILE = STATE_DIR / "state.json"
TOKEN_FILE = STATE_DIR / "token.json"
IMPORT_STATE_FILE = STATE_DIR / "dawarich_imports.json"
AUTO_DEDUP_QUEUE_FILE = STATE_DIR / "auto_dedup_queue.json"
BACKFILL_STATE_FILE = STATE_DIR / "backfill_state.json"
DEDUP_REPORT_DIR = Path(os.environ.get("DAWARICH_DEDUP_REPORT_DIR", STATE_DIR / "dedup-reports")).resolve()

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"

REDIRECT_PORT = 8089
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
REDIRECT_BIND = os.environ.get("STRAVA_CALLBACK_BIND", "0.0.0.0")

# Activity types that never have GPS data — skip without hitting the streams API
NO_GPS_TYPES = {
    "WeightTraining", "Yoga", "Crossfit", "Elliptical", "StairStepper",
    "Meditation", "Workout", "Sauna",
}

# Virtual activity types — GPS is from a virtual world, not the real location
VIRTUAL_TYPES = {"VirtualRide", "VirtualRun"}


def configure_runtime_paths():
    """Refresh storage paths after values from .env have been loaded."""
    global DATA_DIR, STATE_DIR, OUTPUT_DIR, LOG_DIR
    global CONFIG_FILE, STATE_FILE, TOKEN_FILE, IMPORT_STATE_FILE
    global AUTO_DEDUP_QUEUE_FILE, BACKFILL_STATE_FILE, DEDUP_REPORT_DIR
    DATA_DIR = Path(os.environ.get("DATA_DIR", SCRIPT_DIR)).resolve()
    STATE_DIR = Path(os.environ.get("STATE_DIR", DATA_DIR)).resolve()
    OUTPUT_DIR = Path(os.environ.get("GPX_DIR", DATA_DIR / "gpx_output")).resolve()
    LOG_DIR = Path(os.environ.get("LOG_DIR", DATA_DIR / "logs")).resolve()
    CONFIG_FILE = STATE_DIR / "config.json"
    STATE_FILE = STATE_DIR / "state.json"
    TOKEN_FILE = STATE_DIR / "token.json"
    IMPORT_STATE_FILE = STATE_DIR / "dawarich_imports.json"
    AUTO_DEDUP_QUEUE_FILE = STATE_DIR / "auto_dedup_queue.json"
    BACKFILL_STATE_FILE = STATE_DIR / "backfill_state.json"
    DEDUP_REPORT_DIR = Path(
        os.environ.get("DAWARICH_DEDUP_REPORT_DIR", STATE_DIR / "dedup-reports")
    ).resolve()


class TeeStream:
    """Write console output to both the terminal and a persistent log."""

    def __init__(self, terminal, log_file):
        self.terminal = terminal
        self.log_file = log_file

    def write(self, data):
        self.terminal.write(data)
        self.log_file.write(data)
        self.log_file.flush()

    def flush(self):
        self.terminal.flush()
        self.log_file.flush()


def setup_logging():
    load_dotenv()
    configure_runtime_paths()
    if os.environ.get("LOG_TO_FILE", "true").lower() not in {"1", "true", "yes"}:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_DIR / "strava-2-dawarich.log", "a", encoding="utf-8")
    sys.stdout = TeeStream(sys.stdout, log_file)
    sys.stderr = TeeStream(sys.stderr, log_file)


# ── .env loader (no external deps) ──────────────────────────────────────────

def load_dotenv():
    """Parse .env file into os.environ. Supports KEY=VALUE and KEY="VALUE"."""
    if not ENV_FILE.exists():
        return
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


# ── Config ──────────────────────────────────────────────────────────────────

def load_config():
    """Load config from .env (preferred), then config.json, else exit."""
    load_dotenv()
    configure_runtime_paths()

    # Check env vars first
    client_id = os.environ.get("STRAVA_CLIENT_ID", "")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "")

    if client_id and client_secret:
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "dawarich_url": os.environ.get("DAWARICH_URL", "").rstrip("/"),
            "dawarich_api_key": os.environ.get("DAWARICH_API_KEY", ""),
            "home_location": os.environ.get("HOME_LOCATION", ""),
        }

    # Fall back to config.json
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)

    print("No .env or config.json found.")
    print("Either create a .env file (see .env.example) or run: python strava_gpx.py setup")
    sys.exit(1)


def save_config(cfg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def setup_config():
    """Interactive setup - creates config.json."""
    print("=== Strava-2-Dawarich Setup ===\n")

    client_id = input("Strava Client ID: ").strip()
    client_secret = input("Strava Client Secret: ").strip()

    dawarich_url = input("Dawarich URL (e.g. https://dawarich.example.com) [leave blank to skip]: ").strip()
    dawarich_api_key = ""
    if dawarich_url:
        dawarich_api_key = input("Dawarich API Key: ").strip()

    cfg = {
        "client_id": client_id,
        "client_secret": client_secret,
        "dawarich_url": dawarich_url.rstrip("/"),
        "dawarich_api_key": dawarich_api_key,
    }
    save_config(cfg)
    print(f"\nConfig saved to {CONFIG_FILE}")
    return cfg


# ── State (tracks last sync timestamp) ──────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_import_state():
    """Return filenames already accepted by Dawarich."""
    if IMPORT_STATE_FILE.exists():
        with open(IMPORT_STATE_FILE) as f:
            return set(json.load(f).get("uploaded_files", []))
    return set()


def save_import_state(uploaded_files):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(IMPORT_STATE_FILE, "w") as f:
        json.dump({"uploaded_files": sorted(uploaded_files)}, f, indent=2)


def clear_orphaned_upload_marker(filename):
    """Allow a verified-missing GPX to be uploaded again on backfill resume."""
    uploaded_files = load_import_state()
    uploaded_files.discard(filename)
    save_import_state(uploaded_files)

    queue = load_auto_dedup_queue()
    if filename in queue:
        queue.remove(filename)
        save_auto_dedup_queue(queue)


# ── Dawarich deduplication preview ──────────────────────────────────────────

def haversine_meters(lat1, lon1, lat2, lon2):
    """Return the great-circle distance between two coordinates in meters."""
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    value = (math.sin(d_phi / 2) ** 2
             + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def gpx_coordinates(gpx_xml):
    """Extract track coordinates from generated GPX XML."""
    root = ET.fromstring(gpx_xml)
    coordinates = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "trkpt":
            coordinates.append((float(element.attrib["lat"]), float(element.attrib["lon"])))
    return coordinates


def point_coordinates(point):
    """Normalize Dawarich full or slim point representations."""
    latitude = point.get("lat", point.get("latitude"))
    longitude = point.get("lng", point.get("lon", point.get("longitude")))
    if latitude is None or longitude is None:
        return None
    return float(latitude), float(longitude)


def fetch_dawarich_points(cfg, start_timestamp, end_timestamp):
    """Fetch every Dawarich point in a Unix timestamp window."""
    url = f"{cfg['dawarich_url']}/api/v1/points"
    headers = {"Authorization": f"Bearer {cfg['dawarich_api_key']}"}
    points = []
    page = 1

    while True:
        response = requests.get(
            url,
            headers=headers,
            params={
                "start_at": start_timestamp,
                "end_at": end_timestamp,
                "order": "asc",
                # Full representation is required for tracker_id source filtering.
                "slim": "false",
                "page": page,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            page_points = payload
        else:
            page_points = payload.get("points", payload.get("data", []))
        points.extend(page_points)

        total_pages = int(response.headers.get("X-Total-Pages", "1"))
        if page >= total_pages or not page_points:
            break
        page += 1
    return points


def analyze_dawarich_overlap(
        cfg, activity, gpx_xml, radius_meters=200, allowed_tracker_ids=None):
    """Read-only analysis of Dawarich points overlapping a Strava activity."""
    start = int(datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00")).timestamp())
    end = start + int(activity.get("elapsed_time", 0))
    track = gpx_coordinates(gpx_xml)
    points = fetch_dawarich_points(cfg, start, end)
    if allowed_tracker_ids is None:
        allowed_tracker_ids = {
            value.strip()
            for value in os.environ.get("DAWARICH_DEDUP_TRACKER_IDS", "").split(",")
            if value.strip()
        }
    else:
        allowed_tracker_ids = set(allowed_tracker_ids)
    candidates = []
    invalid = 0
    gpx_preserved = 0
    unapproved_preserved = 0
    tracker_summary = {}

    for point in points:
        tracker_id = point.get("tracker_id") or "<none>"
        tracker_summary[tracker_id] = tracker_summary.get(tracker_id, 0) + 1
        if tracker_id.startswith("gpx-"):
            gpx_preserved += 1
            continue
        if tracker_id not in allowed_tracker_ids:
            unapproved_preserved += 1
            continue
        coordinates = point_coordinates(point)
        if not coordinates or not track:
            invalid += 1
            continue
        latitude, longitude = coordinates
        nearest = min(haversine_meters(latitude, longitude, lat, lon) for lat, lon in track)
        if nearest <= radius_meters:
            candidates.append({
                "id": point.get("id"),
                "tracker_id": tracker_id,
                "distance_m": round(nearest, 1),
                "original_point": point,
            })

    report = {
        "activity_id": activity["id"],
        "activity_name": activity.get("name", "Unknown"),
        "start_timestamp": start,
        "end_timestamp": end,
        "radius_meters": radius_meters,
        "allowed_tracker_ids": sorted(allowed_tracker_ids),
        "tracker_summary": tracker_summary,
        "dawarich_points": len(points),
        "candidate_points": candidates,
        "gpx_points_preserved": gpx_preserved,
        "unapproved_tracker_points_preserved": unapproved_preserved,
        "outside_corridor": (
            len(points) - len(candidates) - invalid - gpx_preserved - unapproved_preserved
        ),
        "invalid_points": invalid,
    }
    print(f"    Dawarich overlap: {len(points)} points in time window")
    print(f"    Tracker sources: {tracker_summary}")
    print(f"    GPX points preserved: {gpx_preserved}")
    print(f"    Unapproved tracker points preserved: {unapproved_preserved}")
    print(f"    Dedup candidates: {len(candidates)} within {radius_meters} m of Strava track")
    print(f"    Preserved: {report['outside_corridor']} outside corridor; {invalid} invalid")
    DEDUP_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = DEDUP_REPORT_DIR / f"strava-{activity['id']}.json"
    with open(report_path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
    print(f"    Report: {report_path}")
    return report


def delete_dawarich_candidates(cfg, report_path, confirmation, max_points=5000, batch_size=500):
    """Delete candidates from a reviewed report after strict safety checks."""
    report_path = Path(report_path).resolve()
    with open(report_path, encoding="utf-8") as report_file:
        report = json.load(report_file)

    activity_id = str(report.get("activity_id", ""))
    if str(confirmation) != activity_id:
        raise ValueError(f"Confirmation must exactly match Strava activity ID {activity_id}")

    candidates = report.get("candidate_points", [])
    if not candidates:
        raise ValueError("Report contains no deletion candidates")
    if len(candidates) > max_points:
        raise ValueError(
            f"Report has {len(candidates)} candidates, exceeding safety limit {max_points}"
        )
    if report.get("gpx_points_preserved", 0) <= 0:
        raise ValueError("No imported GPX points were detected; refusing replacement deletion")
    if any("original_point" not in candidate for candidate in candidates):
        raise ValueError("Report lacks full point backups; regenerate it with --dedup-dawarich --dry-run")

    allowed_trackers = {
        value.strip()
        for value in os.environ.get("DAWARICH_DEDUP_TRACKER_IDS", "").split(",")
        if value.strip()
    }
    candidate_trackers = {candidate.get("tracker_id") for candidate in candidates}
    if not allowed_trackers or not candidate_trackers.issubset(allowed_trackers):
        raise ValueError(
            f"Candidate trackers {sorted(candidate_trackers)} are not covered by current allowlist"
        )

    backup_dir = STATE_DIR / "dedup-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"strava-{activity_id}-{timestamp}.json"
    shutil.copy2(report_path, backup_path)
    print(f"Backup created: {backup_path}")

    url = f"{cfg['dawarich_url']}/api/v1/points/bulk_destroy"
    headers = {"Authorization": f"Bearer {cfg['dawarich_api_key']}"}
    point_ids = [candidate["id"] for candidate in candidates]
    deleted = 0

    for offset in range(0, len(point_ids), batch_size):
        batch = point_ids[offset:offset + batch_size]
        response = requests.delete(
            url,
            headers=headers,
            json={"point_ids": batch},
            timeout=60,
        )
        response.raise_for_status()
        deleted += len(batch)
        print(f"Deleted {deleted}/{len(point_ids)} points")

    report["deletion"] = {
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "deleted_points": deleted,
        "backup_path": str(backup_path),
    }
    with open(report_path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
    return deleted


def activity_from_gpx_file(gpx_path):
    """Reconstruct the activity window needed for post-import deduplication."""
    gpx_path = Path(gpx_path)
    gpx_xml = gpx_path.read_text(encoding="utf-8")
    root = ET.fromstring(gpx_xml)
    name = gpx_path.stem
    metadata_time = None
    track_times = []

    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name == "metadata":
            for child in element:
                child_name = child.tag.rsplit("}", 1)[-1]
                if child_name == "name" and child.text:
                    name = child.text
                elif child_name == "time" and child.text:
                    metadata_time = child.text
        elif local_name == "trkpt":
            for child in element:
                if child.tag.rsplit("}", 1)[-1] == "time" and child.text:
                    track_times.append(child.text)

    parts = gpx_path.stem.split("_", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError(f"Cannot extract Strava activity ID from {gpx_path.name}")
    if not metadata_time and not track_times:
        raise ValueError(f"No timestamps found in {gpx_path.name}")

    start_text = metadata_time or track_times[0]
    start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    end = datetime.fromisoformat((track_times[-1] if track_times else start_text).replace("Z", "+00:00"))
    return {
        "id": int(parts[1]),
        "name": name,
        "start_date": start.isoformat().replace("+00:00", "Z"),
        "elapsed_time": max(0, int((end - start).total_seconds())),
    }, gpx_xml


def wait_for_dawarich_gpx(cfg, activity, timeout_seconds=300, interval_seconds=10):
    """Wait until Dawarich exposes imported GPX points for an activity window."""
    start = int(datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00")).timestamp())
    end = start + int(activity["elapsed_time"])
    deadline = time.monotonic() + timeout_seconds

    while True:
        points = fetch_dawarich_points(cfg, start, end)
        gpx_count = sum(
            1 for point in points if (point.get("tracker_id") or "").startswith("gpx-")
        )
        if gpx_count:
            print(f"    Dawarich GPX processing confirmed: {gpx_count} points")
            return gpx_count
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Dawarich did not expose GPX points for activity {activity['id']} "
                f"within {timeout_seconds} seconds"
            )
        print(f"    Waiting for Dawarich GPX processing ({interval_seconds}s)...")
        time.sleep(interval_seconds)


def auto_dedup_uploaded_files(cfg, gpx_files):
    """Safely deduplicate newly uploaded GPX files when explicitly enabled."""
    radius = int(os.environ.get("DAWARICH_DEDUP_RADIUS_METERS", "200"))
    max_points = int(os.environ.get("DAWARICH_AUTO_DEDUP_MAX_POINTS", "5000"))
    batch_size = int(os.environ.get("DAWARICH_DEDUP_BATCH_SIZE", "500"))
    timeout_seconds = int(os.environ.get("DAWARICH_AUTO_DEDUP_WAIT_SECONDS", "300"))
    interval_seconds = int(os.environ.get("DAWARICH_AUTO_DEDUP_POLL_SECONDS", "10"))

    for gpx_path in gpx_files:
        activity, gpx_xml = activity_from_gpx_file(gpx_path)
        print(f"Auto-dedup activity {activity['id']} ({activity['name']})")
        wait_for_dawarich_gpx(
            cfg, activity, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds
        )
        report = analyze_dawarich_overlap(cfg, activity, gpx_xml, radius)
        candidates = report["candidate_points"]
        if not candidates:
            print("    No legacy points require deletion.")
            continue

        report_path = DEDUP_REPORT_DIR / f"strava-{activity['id']}.json"
        delete_dawarich_candidates(
            cfg,
            report_path,
            confirmation=str(activity["id"]),
            max_points=max_points,
            batch_size=batch_size,
        )
        verification = analyze_dawarich_overlap(cfg, activity, gpx_xml, radius)
        if verification["candidate_points"]:
            raise RuntimeError(
                f"Post-delete verification found remaining candidates for activity {activity['id']}"
            )
        print("    Auto-dedup verification complete: 0 candidates remain.")


def load_auto_dedup_queue():
    if AUTO_DEDUP_QUEUE_FILE.exists():
        with open(AUTO_DEDUP_QUEUE_FILE, encoding="utf-8") as queue_file:
            return list(json.load(queue_file).get("gpx_files", []))
    return []


def save_auto_dedup_queue(filenames):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUTO_DEDUP_QUEUE_FILE, "w", encoding="utf-8") as queue_file:
        json.dump({"gpx_files": filenames}, queue_file, indent=2)


def enqueue_auto_dedup(gpx_files):
    queue = load_auto_dedup_queue()
    for gpx_path in gpx_files:
        if gpx_path.name not in queue:
            queue.append(gpx_path.name)
    save_auto_dedup_queue(queue)


def process_auto_dedup_queue(cfg):
    """Process queued uploads, retaining each item until fully verified."""
    queue = load_auto_dedup_queue()
    for filename in list(queue):
        gpx_path = OUTPUT_DIR / filename
        if not gpx_path.exists():
            raise OSError(f"Queued GPX file is missing: {gpx_path}")
        auto_dedup_uploaded_files(cfg, [gpx_path])
        queue.remove(filename)
        save_auto_dedup_queue(queue)


# ── OAuth ────────────────────────────────────────────────────────────────────

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth redirect from Strava."""
    auth_code = None

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if "code" in query:
            OAuthCallbackHandler.auth_code = query["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Authorization successful!</h2>"
                             b"<p>You can close this tab.</p></body></html>")
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            error = query.get("error", ["unknown"])[0]
            self.wfile.write(f"<html><body><h2>Error: {error}</h2></body></html>".encode())

    def log_message(self, format, *args):
        pass  # suppress request logs


def authorize(cfg):
    """Open browser for Strava OAuth, capture the code, exchange for tokens."""
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "activity:read_all",
        "approval_prompt": "auto",
    }
    auth_url = f"{STRAVA_AUTH_URL}?{urlencode(params)}"

    print(f"Opening browser for Strava authorization...")
    print(f"If it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Bind on all interfaces so Docker's published callback port can reach us.
    server = HTTPServer((REDIRECT_BIND, REDIRECT_PORT), OAuthCallbackHandler)
    server.timeout = 120
    print("Waiting for authorization (timeout: 2 min)...")

    while OAuthCallbackHandler.auth_code is None:
        server.handle_request()

    code = OAuthCallbackHandler.auth_code
    OAuthCallbackHandler.auth_code = None
    server.server_close()

    # Exchange code for tokens
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    token_data = resp.json()

    save_token(token_data)
    print("Authorization complete. Token saved.")
    return token_data


def save_token(token_data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": token_data["expires_at"],
        }, f, indent=2)


def load_token():
    if not TOKEN_FILE.exists():
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)


def refresh_token(cfg, token):
    """Refresh the access token if expired."""
    if token["expires_at"] > time.time() + 60:
        return token  # still valid

    print("Refreshing access token...")
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    })
    resp.raise_for_status()
    token_data = resp.json()
    save_token(token_data)
    return token_data


def get_token(cfg):
    """Load token, refresh if needed, or authorize if no token exists."""
    token = load_token()
    if token is None:
        return authorize(cfg)
    return refresh_token(cfg, token)


# ── HTTP helpers ─────────────────────────────────────────────────────────────

BACKFILL_RATE_GUARD = False
BACKFILL_RATE_PAUSE_REASON = None


class BackfillRateLimit(Exception):
    """Raised before a request that would consume the configured reserve."""


def update_strava_rate_guard(response):
    """Record when the next Strava read should pause during a backfill."""
    global BACKFILL_RATE_PAUSE_REASON
    if not BACKFILL_RATE_GUARD:
        return
    limits = response.headers.get("X-ReadRateLimit-Limit")
    usage = response.headers.get("X-ReadRateLimit-Usage")
    if not limits or not usage:
        return
    try:
        short_limit, daily_limit = (int(value) for value in limits.split(","))
        short_usage, daily_usage = (int(value) for value in usage.split(","))
    except (TypeError, ValueError):
        return

    short_reserve = int(os.environ.get("STRAVA_BACKFILL_15MIN_RESERVE", "10"))
    daily_reserve = int(os.environ.get("STRAVA_BACKFILL_DAILY_RESERVE", "100"))
    if daily_usage >= daily_limit - daily_reserve:
        BACKFILL_RATE_PAUSE_REASON = (
            f"daily read usage {daily_usage}/{daily_limit}; resume after midnight UTC"
        )
    elif short_usage >= short_limit - short_reserve:
        BACKFILL_RATE_PAUSE_REASON = (
            f"15-minute read usage {short_usage}/{short_limit}; resume after the next reset"
        )

def strava_request(method, url, access_token, retries=3, **kwargs):
    """Make an authenticated Strava API request with retry on 429/5xx."""
    if BACKFILL_RATE_GUARD and BACKFILL_RATE_PAUSE_REASON:
        raise BackfillRateLimit(BACKFILL_RATE_PAUSE_REASON)
    headers = {"Authorization": f"Bearer {access_token}"}
    for attempt in range(retries):
        resp = requests.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 429:
            if BACKFILL_RATE_GUARD:
                raise BackfillRateLimit("Strava returned 429 Too Many Requests")
            # Rate limited — check Strava's rate limit reset header or back off
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"    Rate limited. Waiting {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            wait = 2 ** attempt * 5
            print(f"    Server error ({resp.status_code}). Retrying in {wait}s...")
            time.sleep(wait)
            continue

        update_strava_rate_guard(resp)
        return resp

    # Last attempt failed, raise
    resp.raise_for_status()
    return resp


# ── Strava API ───────────────────────────────────────────────────────────────

def get_activities(access_token, after=None, before=None):
    """Fetch all activities within the time range, handling pagination."""
    activities = []
    page = 1
    per_page = 100

    while True:
        params = {"page": page, "per_page": per_page}
        if after is not None:
            params["after"] = int(after)
        if before is not None:
            params["before"] = int(before)

        resp = strava_request(
            "GET",
            f"{STRAVA_API_BASE}/athlete/activities",
            access_token,
            params=params,
        )
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        activities.extend(batch)
        page += 1

        # Rate limiting: Strava allows 100 req/15min, 1000/day
        time.sleep(0.5)

    return activities


def get_activity_streams(access_token, activity_id):
    """Fetch GPS streams for a single activity."""
    keys = "time,latlng,altitude,heartrate,cadence,watts,temp"
    resp = strava_request(
        "GET",
        f"{STRAVA_API_BASE}/activities/{activity_id}/streams",
        access_token,
        params={"keys": keys, "key_type": "time"},
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    streams = {}
    for s in resp.json():
        streams[s["type"]] = s["data"]
    return streams


# ── GPX Generation ───────────────────────────────────────────────────────────

def build_gpx(activity, streams):
    """Build a GPX XML string from activity metadata and streams."""
    gpx = ET.Element("gpx", {
        "version": "1.1",
        "creator": "Strava-2-Dawarich",
        "xmlns": "http://www.topografix.com/GPX/1/1",
        "xmlns:gpxtpx": "http://www.garmin.com/xmlschemas/TrackPointExtension/v1",
    })

    metadata = ET.SubElement(gpx, "metadata")
    ET.SubElement(metadata, "name").text = activity.get("name", "Activity")
    ET.SubElement(metadata, "time").text = activity["start_date"]

    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = activity.get("name", "Activity")
    ET.SubElement(trk, "type").text = activity.get("type", "Unknown")
    trkseg = ET.SubElement(trk, "trkseg")

    latlng = streams.get("latlng", [])
    altitude = streams.get("altitude", [])
    time_offsets = streams.get("time", [])
    heartrate = streams.get("heartrate", [])
    cadence = streams.get("cadence", [])
    watts = streams.get("watts", [])
    temp = streams.get("temp", [])

    start_time = datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))

    for i, (lat, lon) in enumerate(latlng):
        trkpt = ET.SubElement(trkseg, "trkpt", {"lat": str(lat), "lon": str(lon)})

        if i < len(altitude):
            ET.SubElement(trkpt, "ele").text = str(altitude[i])

        if i < len(time_offsets):
            pt_time = start_time + timedelta(seconds=time_offsets[i])
            ET.SubElement(trkpt, "time").text = pt_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Extensions (HR, cadence, power, temp)
        has_ext = (
            (i < len(heartrate)) or
            (i < len(cadence)) or
            (i < len(watts)) or
            (i < len(temp))
        )
        if has_ext:
            extensions = ET.SubElement(trkpt, "extensions")
            tpx = ET.SubElement(extensions, "gpxtpx:TrackPointExtension")
            if i < len(heartrate):
                ET.SubElement(tpx, "gpxtpx:hr").text = str(heartrate[i])
            if i < len(cadence):
                ET.SubElement(tpx, "gpxtpx:cad").text = str(cadence[i])
            if i < len(watts):
                ET.SubElement(tpx, "gpxtpx:power").text = str(watts[i])
            if i < len(temp):
                ET.SubElement(tpx, "gpxtpx:atemp").text = str(temp[i])

    ET.indent(gpx, space="  ")
    return ET.tostring(gpx, encoding="unicode", xml_declaration=True)


def build_gpx_relocated(activity, streams, home_lat, home_lon):
    """Build a GPX with all points shifted to the home location.

    Offsets every trackpoint so the ride's first point lands on home coords.
    Preserves the ride's shape, distance, and all sensor data — just moves it
    from the virtual world to the real home location.
    """
    gpx = ET.Element("gpx", {
        "version": "1.1",
        "creator": "Strava-2-Dawarich",
        "xmlns": "http://www.topografix.com/GPX/1/1",
        "xmlns:gpxtpx": "http://www.garmin.com/xmlschemas/TrackPointExtension/v1",
    })

    metadata = ET.SubElement(gpx, "metadata")
    ET.SubElement(metadata, "name").text = activity.get("name", "Activity")
    ET.SubElement(metadata, "time").text = activity["start_date"]

    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = activity.get("name", "Activity")
    ET.SubElement(trk, "type").text = activity.get("type", "Unknown")
    trkseg = ET.SubElement(trk, "trkseg")

    latlng = streams.get("latlng", [])
    altitude = streams.get("altitude", [])
    time_offsets = streams.get("time", [])
    heartrate = streams.get("heartrate", [])
    cadence = streams.get("cadence", [])
    watts = streams.get("watts", [])
    temp = streams.get("temp", [])

    # Calculate offset from ride's first point to home
    if latlng:
        lat_offset = home_lat - latlng[0][0]
        lon_offset = home_lon - latlng[0][1]
    else:
        lat_offset = 0
        lon_offset = 0

    start_time = datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))

    for i, (lat, lon) in enumerate(latlng):
        trkpt = ET.SubElement(trkseg, "trkpt", {
            "lat": str(lat + lat_offset),
            "lon": str(lon + lon_offset),
        })

        if i < len(altitude):
            ET.SubElement(trkpt, "ele").text = str(altitude[i])

        if i < len(time_offsets):
            pt_time = start_time + timedelta(seconds=time_offsets[i])
            ET.SubElement(trkpt, "time").text = pt_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        has_ext = (
            (i < len(heartrate)) or
            (i < len(cadence)) or
            (i < len(watts)) or
            (i < len(temp))
        )
        if has_ext:
            extensions = ET.SubElement(trkpt, "extensions")
            tpx = ET.SubElement(extensions, "gpxtpx:TrackPointExtension")
            if i < len(heartrate):
                ET.SubElement(tpx, "gpxtpx:hr").text = str(heartrate[i])
            if i < len(cadence):
                ET.SubElement(tpx, "gpxtpx:cad").text = str(cadence[i])
            if i < len(watts):
                ET.SubElement(tpx, "gpxtpx:power").text = str(watts[i])
            if i < len(temp):
                ET.SubElement(tpx, "gpxtpx:atemp").text = str(temp[i])

    ET.indent(gpx, space="  ")
    return ET.tostring(gpx, encoding="unicode", xml_declaration=True)


def save_gpx(activity, gpx_xml):
    """Save GPX file to output directory. Returns the file path."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    date_str = activity["start_date"][:10]
    activity_id = activity["id"]
    name = activity.get("name", "activity").replace("/", "-").replace("\\", "-")
    # Keep filename reasonable
    safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip()[:50]
    filename = f"{date_str}_{activity_id}_{safe_name}.gpx"

    filepath = OUTPUT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(gpx_xml)

    return filepath


# ── Dawarich Helpers ───────────────────────────────────────────────────────

def geocode_location(query):
    """Geocode a location string to (lat, lon) using OpenStreetMap Nominatim."""
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "json", "limit": 1},
        headers={"User-Agent": "Strava-2-Dawarich/1.0"},
    )
    if resp.ok and resp.json():
        result = resp.json()[0]
        return float(result["lat"]), float(result["lon"])
    return None


def save_home_to_env(lat, lon):
    """Append HOME_LOCATION to the .env file."""
    entry = f"\n# Home location for virtual ride anchoring\nHOME_LOCATION={lat},{lon}\n"
    with open(ENV_FILE, "a") as f:
        f.write(entry)
    os.environ["HOME_LOCATION"] = f"{lat},{lon}"


def get_home_location(cfg):
    """Get home coordinates from HOME_LOCATION env var, Dawarich places, or ask the user."""
    # 1. Explicit env var
    home = cfg.get("home_location", "")
    if home:
        lat, lon = [float(x.strip()) for x in home.split(",")]
        return lat, lon

    # 2. Dawarich "Home" place
    if cfg.get("dawarich_url") and cfg.get("dawarich_api_key"):
        places_url = f"{cfg['dawarich_url']}/api/v1/places"
        headers = {"Authorization": f"Bearer {cfg['dawarich_api_key']}"}
        try:
            resp = requests.get(places_url, headers=headers)
            if resp.ok:
                for place in resp.json():
                    if place.get("name", "").lower() == "home":
                        lat, lon = place["latitude"], place["longitude"]
                        print(f"  Using Home from Dawarich: {lat}, {lon}")
                        save_home_to_env(lat, lon)
                        cfg["home_location"] = f"{lat},{lon}"
                        return lat, lon
        except Exception:
            pass

    # 3. Ask the user
    print("\n  Virtual ride detected but no home location configured.")
    print("  Enter your home city/address so virtual rides can be anchored there")
    print("  (this avoids teleport distance in Dawarich).\n")
    while True:
        location = input("  Home location (e.g. 'Chicago, IL'): ").strip()
        if not location:
            print("  Skipping virtual rides for now.")
            return None
        coords = geocode_location(location)
        if coords:
            lat, lon = coords
            print(f"  Found: {lat}, {lon}")
            confirm = input("  Save this to .env? [Y/n]: ").strip().lower()
            if confirm in ("", "y", "yes"):
                save_home_to_env(lat, lon)
                cfg["home_location"] = f"{lat},{lon}"
                print(f"  Saved HOME_LOCATION={lat},{lon} to .env\n")
                return lat, lon
        else:
            print("  Could not find that location. Try again or press Enter to skip.")


# ── Dawarich Push ────────────────────────────────────────────────────────────

def push_to_dawarich(cfg, gpx_files, dry_run=False):
    """Upload GPX files to Dawarich's import endpoint."""
    if not cfg.get("dawarich_url") or not cfg.get("dawarich_api_key"):
        print("Dawarich not configured. Skipping push.")
        return 0, []

    import_url = f"{cfg['dawarich_url']}/api/v1/imports"
    headers = {"Authorization": f"Bearer {cfg['dawarich_api_key']}"}
    uploaded_files = load_import_state()
    failures = 0
    uploaded_now = []

    for gpx_path in gpx_files:
        if gpx_path.name in uploaded_files:
            print(f"  Skipping {gpx_path.name} (already uploaded)")
            continue
        if dry_run:
            print(f"  [dry-run] Would upload {gpx_path.name}")
            continue
        print(f"  Pushing {gpx_path.name}...")
        try:
            with open(gpx_path, "rb") as f:
                resp = requests.post(
                    import_url,
                    headers=headers,
                    files={"file": (gpx_path.name, f, "application/gpx+xml")},
                    timeout=60,
                )
        except requests.RequestException as exc:
            failures += 1
            print(f"    [ERROR] Network error: {exc}")
            continue
        if resp.ok:
            print("    [OK] Uploaded")
            uploaded_files.add(gpx_path.name)
            uploaded_now.append(gpx_path)
            save_import_state(uploaded_files)
        else:
            failures += 1
            print(f"    [ERROR] Failed ({resp.status_code}): {resp.text[:200]}")
    return failures, uploaded_now


def pending_gpx_files(gpx_dir=None):
    """List GPX files not yet recorded as successfully uploaded."""
    directory = gpx_dir or OUTPUT_DIR
    if not directory.exists():
        return []
    uploaded_files = load_import_state()
    return [path for path in sorted(directory.glob("*.gpx")) if path.name not in uploaded_files]


# ── Main sync logic ─────────────────────────────────────────────────────────

def sync(cfg, after_timestamp=None, dry_run=False, dedup_dawarich=False, dedup_radius=200):
    """Fetch new activities, convert to GPX, optionally push to Dawarich."""
    token = get_token(cfg)
    access_token = token["access_token"]

    state = load_state()
    fetched_ids = set(state.get("fetched_ids", []))

    # Determine time range
    if after_timestamp is not None:
        after = after_timestamp
    elif "last_sync" in state:
        after = state["last_sync"]
    else:
        # First run: start from now
        after = int(datetime.now(timezone.utc).timestamp())
        state["last_sync"] = after
        state["fetched_ids"] = []
        if not dry_run:
            save_state(state)
        print(f"First run. Baseline set to {datetime.fromtimestamp(after, tz=timezone.utc).isoformat()}.")
        print("Future syncs will grab activities after this point.")
        print("Use --days, --months, or --all to pull historical data.")
        return []

    print(f"Fetching activities after {datetime.fromtimestamp(after, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}...")
    activities = get_activities(access_token, after=after)
    print(f"Found {len(activities)} activities.")

    # A dedup preview must be able to re-examine activities already synced.
    new_activities = activities if dedup_dawarich else [
        activity for activity in activities if activity["id"] not in fetched_ids
    ]
    if not new_activities:
        print("No new activities to process.")
        return []

    print(f"Processing {len(new_activities)} new activities...")
    gpx_files = []

    for i, activity in enumerate(new_activities, 1):
        name = activity.get("name", "Unknown")
        act_type = activity.get("type", "?")
        date = activity["start_date"][:10]
        print(f"  [{i}/{len(new_activities)}] {date} - {name} ({act_type})")

        # Skip activity types that never have GPS
        if act_type in NO_GPS_TYPES:
            print(f"    Skipped ({act_type} — no GPS)")
            fetched_ids.add(activity["id"])
            continue

        # Virtual activities: anchor at home location to avoid teleport distance
        if act_type in VIRTUAL_TYPES:
            home = get_home_location(cfg)
            if not home:
                print(f"    Skipped ({act_type} — set HOME_LOCATION in .env or mark Home in Dawarich)")
                fetched_ids.add(activity["id"])
                continue
            home_lat, home_lon = home
            streams = get_activity_streams(access_token, activity["id"])
            gpx_xml = build_gpx_relocated(activity, streams or {}, home_lat, home_lon)
            if dedup_dawarich:
                analyze_dawarich_overlap(cfg, activity, gpx_xml, dedup_radius)
            if dry_run:
                filepath = Path(f"{activity['id']}.gpx")
            else:
                filepath = save_gpx(activity, gpx_xml)
                gpx_files.append(filepath)
            fetched_ids.add(activity["id"])
            print(f"    Saved (virtual → home): {filepath.name}")
            time.sleep(0.5)
            continue

        # Skip activities without GPS data
        if not activity.get("start_latlng"):
            print(f"    Skipped (no GPS data)")
            fetched_ids.add(activity["id"])
            continue

        streams = get_activity_streams(access_token, activity["id"])
        if not streams or "latlng" not in streams:
            print(f"    Skipped (no stream data)")
            fetched_ids.add(activity["id"])
            continue

        gpx_xml = build_gpx(activity, streams)
        if dedup_dawarich:
            analyze_dawarich_overlap(cfg, activity, gpx_xml, dedup_radius)
        if dry_run:
            filepath = Path(f"{activity['id']}.gpx")
        else:
            filepath = save_gpx(activity, gpx_xml)
            gpx_files.append(filepath)
        fetched_ids.add(activity["id"])
        print(f"    Saved: {filepath.name}")

        # Rate limit
        time.sleep(0.5)

    # Update state
    state["last_sync"] = int(datetime.now(timezone.utc).timestamp())
    state["fetched_ids"] = list(fetched_ids)
    if not dry_run:
        save_state(state)

    if dry_run:
        print(f"\n[dry-run] {len(new_activities)} activities examined; no GPX or state written.")
    else:
        print(f"\n{len(gpx_files)} GPX files saved to {OUTPUT_DIR}/")
    return gpx_files


# ── Resumable historical backfill ───────────────────────────────────────────

def save_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2)
    os.replace(temporary, path)


def load_backfill_state():
    if not BACKFILL_STATE_FILE.exists():
        return None
    with open(BACKFILL_STATE_FILE, encoding="utf-8") as state_file:
        return json.load(state_file)


def save_backfill_state(state):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json_atomic(BACKFILL_STATE_FILE, state)


def parse_backfill_date(value, end_of_range=False):
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_range:
        parsed += timedelta(days=1)
    return int(parsed.timestamp())


def dawarich_gpx_count(cfg, activity):
    start = int(datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00")).timestamp())
    end = start + int(activity.get("elapsed_time", 0))
    points = fetch_dawarich_points(cfg, start, end)
    return sum(1 for point in points if (point.get("tracker_id") or "").startswith("gpx-"))


def process_backfill_activity(cfg, access_token, activity):
    """Complete one activity through upload and optional deduplication."""
    activity_id = activity["id"]
    activity_type = activity.get("type", "?")
    if activity_type in NO_GPS_TYPES or not activity.get("start_latlng"):
        print(f"    Skipped ({activity_type}: no GPS)")
        return "no_gps"

    existing_gpx = dawarich_gpx_count(cfg, activity)
    if existing_gpx:
        print(f"    Already present in Dawarich ({existing_gpx} GPX points)")
        return "already_present"

    streams = get_activity_streams(access_token, activity_id)
    if not streams or "latlng" not in streams:
        print("    Skipped (no Strava GPS stream)")
        return "no_gps"

    if activity_type in VIRTUAL_TYPES:
        home = get_home_location(cfg)
        if not home:
            raise ValueError("HOME_LOCATION is required for virtual activities")
        gpx_xml = build_gpx_relocated(activity, streams, *home)
    else:
        gpx_xml = build_gpx(activity, streams)
    filepath = save_gpx(activity, gpx_xml)

    # Dawarich was queried above and contains no GPX points. A local upload
    # marker can therefore only represent an interrupted or failed import.
    if filepath.name in load_import_state():
        print(f"    Clearing orphaned upload marker for {filepath.name}")
        clear_orphaned_upload_marker(filepath.name)

    failures, uploaded_now = push_to_dawarich(cfg, [filepath])
    if failures:
        raise RuntimeError(f"Dawarich upload failed for activity {activity_id}")
    if not uploaded_now:
        raise RuntimeError(
            f"Activity {activity_id} is locally marked uploaded but no GPX points exist in Dawarich"
        )

    auto_enabled = (
        os.environ.get("DAWARICH_AUTO_DEDUP", "false").lower() in {"1", "true", "yes"}
    )
    if auto_enabled:
        enqueue_auto_dedup(uploaded_now)
        process_auto_dedup_queue(cfg)
    return "imported"


def run_backfill(cfg, date_from=None, date_to=None, batch_size=50, resume=False):
    """Run a bounded, checkpointed historical import."""
    global BACKFILL_RATE_GUARD, BACKFILL_RATE_PAUSE_REASON
    existing = load_backfill_state()

    if resume:
        if not existing:
            raise ValueError("No backfill state exists to resume")
        state = existing
        date_from = state["date_from"]
        date_to = state["date_to"]
        batch_size = int(state.get("batch_size", batch_size))
    else:
        if not date_from or not date_to:
            raise ValueError("--from and --to are required for a new backfill")
        if existing and existing.get("status") not in {"complete", "cancelled"}:
            raise ValueError("An unfinished backfill exists; use --resume")
        if parse_backfill_date(date_from) >= parse_backfill_date(date_to, end_of_range=True):
            raise ValueError("Backfill start date must be before or equal to end date")
        state = {
            "version": 1,
            "status": "active",
            "date_from": date_from,
            "date_to": date_to,
            "batch_size": batch_size,
            "completed_ids": [],
            "counts": {
                "imported": 0,
                "already_present": 0,
                "already_processed": 0,
                "no_gps": 0,
                "errors": 0,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        save_backfill_state(state)

    BACKFILL_RATE_GUARD = True
    BACKFILL_RATE_PAUSE_REASON = None
    try:
        token = get_token(cfg)
        access_token = token["access_token"]
        after = parse_backfill_date(date_from)
        before = parse_backfill_date(date_to, end_of_range=True)
        activities = get_activities(access_token, after=after, before=before)
        activities.sort(key=lambda activity: activity["start_date"])
        completed = set(state.get("completed_ids", []))
        fetched_ids = set(load_state().get("fetched_ids", []))
        remaining = [activity for activity in activities if activity["id"] not in completed]
        selected = remaining[:batch_size]

        print(
            f"Backfill {date_from} to {date_to}: {len(activities)} activities total, "
            f"{len(remaining)} remaining, processing up to {len(selected)}"
        )
        for index, activity in enumerate(selected, 1):
            activity_id = activity["id"]
            print(
                f"  [{index}/{len(selected)}] {activity['start_date'][:10]} - "
                f"{activity.get('name', 'Unknown')} ({activity.get('type', '?')})"
            )
            if activity_id in fetched_ids:
                result = "already_processed"
                print("    Already processed by a previous sync")
            else:
                result = process_backfill_activity(cfg, access_token, activity)
                fetched_ids.add(activity_id)
                sync_state = load_state()
                sync_state["fetched_ids"] = sorted(fetched_ids)
                save_state(sync_state)

            completed.add(activity_id)
            state["completed_ids"] = sorted(completed)
            state["counts"][result] = state["counts"].get(result, 0) + 1
            state["last_activity_id"] = activity_id
            state["status"] = "active"
            state.pop("last_error", None)
            save_backfill_state(state)

        if len(remaining) <= len(selected):
            state["status"] = "complete"
            save_backfill_state(state)
            print("Backfill complete.")
        else:
            print(f"Batch complete. {len(remaining) - len(selected)} activities remain; use --resume.")
        print(f"Summary: {state['counts']}")
        return state
    except BackfillRateLimit as exc:
        state["status"] = "paused_rate_limit"
        state["last_error"] = str(exc)
        save_backfill_state(state)
        print(f"Backfill paused safely: {exc}")
        return state
    except Exception as exc:
        state["status"] = "error"
        state["counts"]["errors"] = state["counts"].get("errors", 0) + 1
        state["last_error"] = str(exc)
        save_backfill_state(state)
        raise
    finally:
        BACKFILL_RATE_GUARD = False
        BACKFILL_RATE_PAUSE_REASON = None


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Strava-2-Dawarich: Pull Strava activities as GPX and push to Dawarich."
    )
    parser.add_argument("--version", action="version", version=f"strava-2-dawarich {VERSION}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="Configure Strava credentials and Dawarich connection")
    sub.add_parser("auth", help="Authorize with Strava (opens browser)")

    sync_parser = sub.add_parser("sync", help="Sync new activities from Strava")
    sync_parser.add_argument("--days", type=int, help="Pull activities from the last N days")
    sync_parser.add_argument("--months", type=int, help="Pull activities from the last N months")
    sync_parser.add_argument("--all", action="store_true", help="Pull all activities from all time")
    sync_parser.add_argument("--no-push", action="store_true", help="Don't push to Dawarich after sync")
    sync_parser.add_argument("--dry-run", action="store_true", help="Preview without writing GPX/state or pushing")
    sync_parser.add_argument(
        "--dedup-dawarich",
        action="store_true",
        help="Preview Dawarich points overlapping each Strava track (requires --dry-run)",
    )

    push_parser = sub.add_parser("push", help="Push existing GPX files to Dawarich")
    push_parser.add_argument("--dir", type=str, help="Directory of GPX files (default: gpx_output/)")
    push_parser.add_argument("--dry-run", action="store_true", help="Preview uploads without changing Dawarich")

    dedup_parser = sub.add_parser(
        "dedup", help="Delete reviewed Dawarich candidates from one saved report"
    )
    dedup_parser.add_argument("--report", required=True, help="Path to a reviewed dedup report")
    dedup_parser.add_argument(
        "--confirm-delete",
        required=True,
        help="Exact Strava activity ID required to authorize deletion",
    )

    backfill_parser = sub.add_parser(
        "backfill", help="Import a bounded historical period with checkpoints"
    )
    backfill_parser.add_argument("--from", dest="date_from", help="Start date (YYYY-MM-DD)")
    backfill_parser.add_argument("--to", dest="date_to", help="End date, inclusive (YYYY-MM-DD)")
    backfill_parser.add_argument(
        "--batch-size", type=int, default=50, help="Maximum activities per invocation (default: 50)"
    )
    backfill_parser.add_argument(
        "--resume", action="store_true", help="Resume the existing checkpointed backfill"
    )

    return parser.parse_args()


def main():
    setup_logging()
    args = parse_args()

    if args.command == "setup":
        setup_config()
        print("\nNow run: python strava_gpx.py auth")
        return

    if args.command == "auth":
        cfg = load_config()
        authorize(cfg)
        return

    if args.command == "sync":
        cfg = load_config()

        if args.dedup_dawarich and not args.dry_run:
            print("--dedup-dawarich is read-only in this release and requires --dry-run.")
            sys.exit(2)
        if args.dedup_dawarich and not cfg.get("dawarich_url"):
            print("Dawarich must be configured for --dedup-dawarich.")
            sys.exit(2)
        if args.dedup_dawarich and not os.environ.get("DAWARICH_DEDUP_TRACKER_IDS", "").strip():
            print("Set DAWARICH_DEDUP_TRACKER_IDS to the tracker IDs allowed for deduplication.")
            sys.exit(2)

        after = None
        if args.all:
            after = 0
            print("Fetching ALL activities...")
        elif args.months:
            after = int((datetime.now(timezone.utc) - timedelta(days=args.months * 30)).timestamp())
        elif args.days:
            after = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp())

        dedup_radius = int(os.environ.get("DAWARICH_DEDUP_RADIUS_METERS", "200"))
        gpx_files = sync(
            cfg,
            after_timestamp=after,
            dry_run=args.dry_run,
            dedup_dawarich=args.dedup_dawarich,
            dedup_radius=dedup_radius,
        )

        if not args.no_push and not args.dry_run:
            auto_enabled = (
                os.environ.get("DAWARICH_AUTO_DEDUP", "false").lower()
                in {"1", "true", "yes"}
            )
            pending_files = pending_gpx_files()
            if pending_files:
                print(f"Pushing {len(pending_files)} pending GPX files to Dawarich...")
                failures, uploaded_now = push_to_dawarich(cfg, pending_files)
                if auto_enabled and uploaded_now:
                    enqueue_auto_dedup(uploaded_now)
                if failures:
                    print(f"{failures} upload(s) failed and will be retried on the next sync.")
                    sys.exit(1)
            if auto_enabled and load_auto_dedup_queue():
                try:
                    process_auto_dedup_queue(cfg)
                except (OSError, ValueError, RuntimeError, TimeoutError,
                        requests.RequestException) as exc:
                    print(f"Automatic deduplication failed: {exc}")
                    print("The activity remains queued for the next sync.")
                    sys.exit(1)

        return

    if args.command == "push":
        cfg = load_config()
        gpx_dir = Path(args.dir) if args.dir else OUTPUT_DIR
        if not gpx_dir.exists():
            print(f"Directory not found: {gpx_dir}")
            sys.exit(1)
        gpx_files = pending_gpx_files(gpx_dir)
        if not gpx_files:
            print("No pending GPX files found.")
            return
        print(f"Pushing {len(gpx_files)} GPX files to Dawarich...")
        failures, _ = push_to_dawarich(cfg, gpx_files, dry_run=args.dry_run)
        if failures:
            sys.exit(1)
        return

    if args.command == "dedup":
        cfg = load_config()
        max_points = int(os.environ.get("DAWARICH_DEDUP_MAX_POINTS", "5000"))
        batch_size = int(os.environ.get("DAWARICH_DEDUP_BATCH_SIZE", "500"))
        try:
            deleted = delete_dawarich_candidates(
                cfg,
                args.report,
                args.confirm_delete,
                max_points=max_points,
                batch_size=batch_size,
            )
        except (OSError, ValueError, requests.RequestException) as exc:
            print(f"Deduplication aborted: {exc}")
            sys.exit(1)
        print(f"Deduplication complete: {deleted} legacy Dawarich points deleted.")
        return

    if args.command == "backfill":
        cfg = load_config()
        if not cfg.get("dawarich_url") or not cfg.get("dawarich_api_key"):
            print("Dawarich must be configured for backfill.")
            sys.exit(2)
        if args.batch_size < 1:
            print("--batch-size must be at least 1.")
            sys.exit(2)
        try:
            state = run_backfill(
                cfg,
                date_from=args.date_from,
                date_to=args.date_to,
                batch_size=args.batch_size,
                resume=args.resume,
            )
        except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
            print(f"Backfill failed safely: {exc}")
            print(f"Checkpoint: {BACKFILL_STATE_FILE}")
            sys.exit(1)
        if state.get("status") == "paused_rate_limit":
            print("Run the same command with --resume after the indicated reset.")
        return

    # No command given
    print("Usage: python strava_gpx.py {setup|auth|sync|push|dedup|backfill}")
    print("Run with --help for details.")


if __name__ == "__main__":
    main()
