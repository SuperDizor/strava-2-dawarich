import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


requests_stub = types.ModuleType("requests")
requests_stub.RequestException = Exception
requests_stub.post = lambda *args, **kwargs: None
requests_stub.get = lambda *args, **kwargs: None
requests_stub.delete = lambda *args, **kwargs: None
sys.modules.setdefault("requests", requests_stub)

MODULE_PATH = Path(__file__).parents[1] / "strava_gpx.py"
SPEC = importlib.util.spec_from_file_location("strava_gpx", MODULE_PATH)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class DeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        app.STATE_DIR = self.root / "state"
        app.OUTPUT_DIR = self.root / "gpx"
        app.IMPORT_STATE_FILE = app.STATE_DIR / "dawarich_imports.json"
        app.DEDUP_REPORT_DIR = app.STATE_DIR / "dedup-reports"
        app.OUTPUT_DIR.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pending_files_excludes_successful_uploads(self):
        uploaded = app.OUTPUT_DIR / "uploaded.gpx"
        pending = app.OUTPUT_DIR / "pending.gpx"
        uploaded.write_text("gpx", encoding="utf-8")
        pending.write_text("gpx", encoding="utf-8")
        app.save_import_state({uploaded.name})

        self.assertEqual(app.pending_gpx_files(), [pending])

    def test_failed_upload_is_not_marked_as_uploaded(self):
        pending = app.OUTPUT_DIR / "pending.gpx"
        pending.write_text("gpx", encoding="utf-8")
        cfg = {"dawarich_url": "https://example.test", "dawarich_api_key": "key"}

        with patch.object(app.requests, "post", side_effect=Exception("offline")):
            failures = app.push_to_dawarich(cfg, [pending])

        self.assertEqual(failures, 1)
        self.assertFalse(app.IMPORT_STATE_FILE.exists())

    def test_successful_upload_is_persisted(self):
        pending = app.OUTPUT_DIR / "pending.gpx"
        pending.write_text("gpx", encoding="utf-8")
        response = types.SimpleNamespace(ok=True)
        cfg = {"dawarich_url": "https://example.test", "dawarich_api_key": "key"}

        with patch.object(app.requests, "post", return_value=response):
            failures = app.push_to_dawarich(cfg, [pending])

        state = json.loads(app.IMPORT_STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(failures, 0)
        self.assertEqual(state["uploaded_files"], [pending.name])

    def test_overlap_preview_selects_only_points_inside_corridor(self):
        activity = {
            "id": 42,
            "name": "Test Ride",
            "start_date": "2026-08-14T12:00:00Z",
            "elapsed_time": 3600,
        }
        gpx = (
            '<gpx><trk><trkseg>'
            '<trkpt lat="46.8000" lon="-71.2000" />'
            '<trkpt lat="46.8010" lon="-71.2010" />'
            '</trkseg></trk></gpx>'
        )
        points = [
            {"id": 1, "lat": 46.8001, "lng": -71.2001, "tracker_id": "D5"},
            {"id": 2, "lat": 46.9000, "lng": -71.3000, "tracker_id": "D5"},
            {"id": 3, "lat": 46.8001, "lng": -71.2001, "tracker_id": "gpx-import"},
            {"id": 4, "lat": 46.8001, "lng": -71.2001, "tracker_id": "OTHER"},
        ]

        with patch.object(app, "fetch_dawarich_points", return_value=points):
            report = app.analyze_dawarich_overlap(
                {}, activity, gpx, radius_meters=200, allowed_tracker_ids={"D5"}
            )

        self.assertEqual([point["id"] for point in report["candidate_points"]], [1])
        self.assertEqual(report["outside_corridor"], 1)
        self.assertEqual(report["gpx_points_preserved"], 1)
        self.assertEqual(report["unapproved_tracker_points_preserved"], 1)
        self.assertTrue((app.DEDUP_REPORT_DIR / "strava-42.json").exists())

    def test_dedup_preview_reexamines_already_fetched_activity(self):
        activity = {
            "id": 42,
            "name": "Existing Ride",
            "type": "Ride",
            "start_date": "2026-08-14T12:00:00Z",
            "elapsed_time": 60,
            "start_latlng": [46.8, -71.2],
        }
        app.STATE_FILE = app.STATE_DIR / "state.json"
        app.STATE_DIR.mkdir()
        app.STATE_FILE.write_text(
            json.dumps({"last_sync": 1, "fetched_ids": [42]}), encoding="utf-8"
        )
        streams = {"latlng": [[46.8, -71.2]], "time": [0]}

        with patch.object(app, "get_token", return_value={"access_token": "token"}), \
                patch.object(app, "get_activities", return_value=[activity]), \
                patch.object(app, "get_activity_streams", return_value=streams), \
                patch.object(app, "analyze_dawarich_overlap") as analyze:
            app.sync({}, after_timestamp=0, dry_run=True, dedup_dawarich=True)

        analyze.assert_called_once()

    def test_delete_requires_exact_activity_confirmation(self):
        report_path = self.root / "report.json"
        report_path.write_text(json.dumps({"activity_id": 42}), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "exactly match"):
            app.delete_dawarich_candidates({}, report_path, confirmation="wrong")

    def test_delete_creates_backup_and_batches_allowed_candidates(self):
        report_path = self.root / "report.json"
        report = {
            "activity_id": 42,
            "gpx_points_preserved": 100,
            "candidate_points": [
                {"id": 1, "tracker_id": "D5", "original_point": {"id": 1}},
                {"id": 2, "tracker_id": "D5", "original_point": {"id": 2}},
            ],
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
        cfg = {"dawarich_url": "https://example.test", "dawarich_api_key": "key"}
        response = types.SimpleNamespace(raise_for_status=lambda: None)

        with patch.dict(app.os.environ, {"DAWARICH_DEDUP_TRACKER_IDS": "D5"}), \
                patch.object(app.requests, "delete", return_value=response) as delete:
            deleted = app.delete_dawarich_candidates(
                cfg, report_path, confirmation="42", batch_size=1
            )

        self.assertEqual(deleted, 2)
        self.assertEqual(delete.call_count, 2)
        self.assertEqual(len(list((app.STATE_DIR / "dedup-backups").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
