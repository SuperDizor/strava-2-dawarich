#!/usr/bin/env python3
"""
Strava-2-Dawarich: Pull Strava activities as GPX and push to Dawarich.
"""

import argparse
import json
import math
import os
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
    global CONFIG_FILE, STATE_FILE, TOKEN_FILE, IMPORT_STATE_FILE, DEDUP_REPORT_DIR
    DATA_DIR = Path(os.environ.get("DATA_DIR", SCRIPT_DIR)).resolve()
    STATE_DIR = Path(os.environ.get("STATE_DIR", DATA_DIR)).resolve()
    OUTPUT_DIR = Path(os.environ.get("GPX_DIR", DATA_DIR / "gpx_output")).resolve()
    LOG_DIR = Path(os.environ.get("LOG_DIR", DATA_DIR / "logs")).resolve()
    CONFIG_FILE = STATE_DIR / "config.json"
    STATE_FILE = STATE_DIR / "state.json"
    TOKEN_FILE = STATE_DIR / "token.json"
    IMPORT_STATE_FILE = STATE_DIR / "dawarich_imports.json"
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
                "slim": "true",
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


def analyze_dawarich_overlap(cfg, activity, gpx_xml, radius_meters=200):
    """Read-only analysis of Dawarich points overlapping a Strava activity."""
    start = int(datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00")).timestamp())
    end = start + int(activity.get("elapsed_time", 0))
    track = gpx_coordinates(gpx_xml)
    points = fetch_dawarich_points(cfg, start, end)
    candidates = []
    invalid = 0

    for point in points:
        coordinates = point_coordinates(point)
        if not coordinates or not track:
            invalid += 1
            continue
        latitude, longitude = coordinates
        nearest = min(haversine_meters(latitude, longitude, lat, lon) for lat, lon in track)
        if nearest <= radius_meters:
            candidates.append({"id": point.get("id"), "distance_m": round(nearest, 1)})

    report = {
        "activity_id": activity["id"],
        "activity_name": activity.get("name", "Unknown"),
        "start_timestamp": start,
        "end_timestamp": end,
        "radius_meters": radius_meters,
        "dawarich_points": len(points),
        "candidate_points": candidates,
        "outside_corridor": len(points) - len(candidates) - invalid,
        "invalid_points": invalid,
    }
    print(f"    Dawarich overlap: {len(points)} points in time window")
    print(f"    Dedup candidates: {len(candidates)} within {radius_meters} m of Strava track")
    print(f"    Preserved: {report['outside_corridor']} outside corridor; {invalid} invalid")
    DEDUP_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = DEDUP_REPORT_DIR / f"strava-{activity['id']}.json"
    with open(report_path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
    print(f"    Report: {report_path}")
    return report


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

def strava_request(method, url, access_token, retries=3, **kwargs):
    """Make an authenticated Strava API request with retry on 429/5xx."""
    headers = {"Authorization": f"Bearer {access_token}"}
    for attempt in range(retries):
        resp = requests.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 429:
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
        return 0

    import_url = f"{cfg['dawarich_url']}/api/v1/imports"
    headers = {"Authorization": f"Bearer {cfg['dawarich_api_key']}"}
    uploaded_files = load_import_state()
    failures = 0

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
            save_import_state(uploaded_files)
        else:
            failures += 1
            print(f"    [ERROR] Failed ({resp.status_code}): {resp.text[:200]}")
    return failures


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
            pending_files = pending_gpx_files()
            if pending_files:
                print(f"Pushing {len(pending_files)} pending GPX files to Dawarich...")
                failures = push_to_dawarich(cfg, pending_files)
                if failures:
                    print(f"{failures} upload(s) failed and will be retried on the next sync.")
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
        failures = push_to_dawarich(cfg, gpx_files, dry_run=args.dry_run)
        if failures:
            sys.exit(1)
        return

    # No command given
    print("Usage: python strava_gpx.py {setup|auth|sync|push}")
    print("Run with --help for details.")


if __name__ == "__main__":
    main()
