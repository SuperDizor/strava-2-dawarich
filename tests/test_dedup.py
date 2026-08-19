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
        app.STATE_FILE = app.STATE_DIR / "state.json"
        app.OUTPUT_DIR = self.root / "gpx"
        app.IMPORT_STATE_FILE = app.STATE_DIR / "dawarich_imports.json"
        app.AUTO_DEDUP_QUEUE_FILE = app.STATE_DIR / "auto_dedup_queue.json"
        app.BACKFILL_STATE_FILE = app.STATE_DIR / "backfill_state.json"
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
            failures, uploaded = app.push_to_dawarich(cfg, [pending])

        self.assertEqual(failures, 1)
        self.assertEqual(uploaded, [])
        self.assertFalse(app.IMPORT_STATE_FILE.exists())

    def test_successful_upload_is_persisted(self):
        pending = app.OUTPUT_DIR / "pending.gpx"
        pending.write_text("gpx", encoding="utf-8")
        response = types.SimpleNamespace(ok=True)
        cfg = {"dawarich_url": "https://example.test", "dawarich_api_key": "key"}

        with patch.object(app.requests, "post", return_value=response):
            failures, uploaded = app.push_to_dawarich(cfg, [pending])

        state = json.loads(app.IMPORT_STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(failures, 0)
        self.assertEqual(uploaded, [pending])
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
            {"id": 4, "lat": 46.8001, "lng": -71.2001,
             "tracker_id": "import-619-trk-0-seg-0"},
            {"id": 5, "lat": 46.8001, "lng": -71.2001, "tracker_id": "OTHER"},
        ]

        with patch.object(app, "fetch_dawarich_points", return_value=points):
            report = app.analyze_dawarich_overlap(
                {}, activity, gpx, radius_meters=200, allowed_tracker_ids={"D5"}
            )

        self.assertEqual([point["id"] for point in report["candidate_points"]], [1])
        self.assertEqual(report["outside_corridor"], 1)
        self.assertEqual(report["gpx_points_preserved"], 2)
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

    def test_activity_is_reconstructed_from_saved_gpx(self):
        gpx_path = self.root / "2026-08-14_19732657015_Test_Run.gpx"
        gpx_path.write_text(
            '<gpx><metadata><name>Test Run</name><time>2026-08-14T12:00:00Z</time></metadata>'
            '<trk><trkseg><trkpt lat="46.8" lon="-71.2">'
            '<time>2026-08-14T12:05:00Z</time></trkpt></trkseg></trk></gpx>',
            encoding="utf-8",
        )

        activity, _ = app.activity_from_gpx_file(gpx_path)

        self.assertEqual(activity["id"], 19732657015)
        self.assertEqual(activity["name"], "Test Run")
        self.assertEqual(activity["elapsed_time"], 300)

    def test_wait_requires_visible_gpx_points(self):
        activity = {
            "id": 42,
            "start_date": "2026-08-14T12:00:00Z",
            "elapsed_time": 60,
        }
        points = [
            {"tracker_id": "import-619-trk-0-seg-0"},
            {"tracker_id": "D5"},
        ]

        with patch.object(app, "fetch_dawarich_points", return_value=points):
            count = app.wait_for_dawarich_gpx({}, activity, timeout_seconds=0)

        self.assertEqual(count, 1)

    def test_only_dawarich_import_trackers_are_treated_as_gpx(self):
        self.assertTrue(app.is_dawarich_gpx_tracker("gpx-abc-trk-0-seg-0"))
        self.assertTrue(app.is_dawarich_gpx_tracker("import-619-trk-0-seg-0"))
        self.assertFalse(app.is_dawarich_gpx_tracker("google-phone-1"))
        self.assertFalse(app.is_dawarich_gpx_tracker("imported-phone"))

    def test_auto_dedup_verifies_after_deletion(self):
        gpx_path = self.root / "2026-08-14_42_Test.gpx"
        gpx_path.write_text("<gpx />", encoding="utf-8")
        activity = {
            "id": 42,
            "name": "Test",
            "start_date": "2026-08-14T12:00:00Z",
            "elapsed_time": 60,
        }
        candidate = {"id": 1, "tracker_id": "D5", "original_point": {"id": 1}}

        with patch.object(app, "activity_from_gpx_file", return_value=(activity, "<gpx />")), \
                patch.object(app, "wait_for_dawarich_gpx"), \
                patch.object(
                    app,
                    "analyze_dawarich_overlap",
                    side_effect=[{"candidate_points": [candidate]}, {"candidate_points": []}],
                ) as analyze, \
                patch.object(app, "delete_dawarich_candidates") as delete:
            app.auto_dedup_uploaded_files({}, [gpx_path])

        delete.assert_called_once()
        self.assertEqual(analyze.call_count, 2)

    def test_auto_dedup_queue_is_removed_only_after_success(self):
        gpx_path = app.OUTPUT_DIR / "2026-08-14_42_Test.gpx"
        gpx_path.write_text("<gpx />", encoding="utf-8")
        app.enqueue_auto_dedup([gpx_path])

        with patch.object(app, "auto_dedup_uploaded_files", side_effect=TimeoutError("slow")):
            with self.assertRaises(TimeoutError):
                app.process_auto_dedup_queue({})
        self.assertEqual(app.load_auto_dedup_queue(), [gpx_path.name])

        with patch.object(app, "auto_dedup_uploaded_files"):
            app.process_auto_dedup_queue({})
        self.assertEqual(app.load_auto_dedup_queue(), [])

    def test_backfill_checkpoints_each_activity_and_resumes(self):
        activities = [
            {"id": value, "start_date": f"2024-01-0{value}T12:00:00Z", "name": str(value)}
            for value in (1, 2, 3)
        ]
        app.save_state({"fetched_ids": [1]})

        with patch.object(app, "get_token", return_value={"access_token": "token"}), \
                patch.object(app, "get_activities", return_value=activities), \
                patch.object(app, "process_backfill_activity", return_value="imported") as process:
            first = app.run_backfill(
                {}, date_from="2024-01-01", date_to="2024-01-31", batch_size=2
            )
            second = app.run_backfill({}, resume=True)

        self.assertEqual(first["status"], "active")
        self.assertEqual(second["status"], "complete")
        self.assertEqual(second["completed_ids"], [1, 2, 3])
        self.assertEqual(process.call_count, 2)
        self.assertEqual(app.load_state()["fetched_ids"], [1, 2, 3])

    def test_rate_guard_preserves_configured_reserve(self):
        response = types.SimpleNamespace(
            headers={
                "X-ReadRateLimit-Limit": "100,1000",
                "X-ReadRateLimit-Usage": "90,500",
            }
        )
        app.BACKFILL_RATE_GUARD = True
        app.BACKFILL_RATE_PAUSE_REASON = None
        try:
            app.update_strava_rate_guard(response)
            with self.assertRaises(app.BackfillRateLimit):
                app.strava_request("GET", "https://example.test", "token")
        finally:
            app.BACKFILL_RATE_GUARD = False
            app.BACKFILL_RATE_PAUSE_REASON = None

    def test_backfill_reuploads_when_local_marker_has_no_dawarich_points(self):
        activity = {
            "id": 5270485334,
            "name": "Lunch Hike",
            "type": "Hike",
            "start_date": "2021-05-09T12:00:00Z",
            "elapsed_time": 60,
            "start_latlng": [46.8, -71.2],
        }
        streams = {"latlng": [[46.8, -71.2]], "time": [0]}
        filename = "2021-05-09_5270485334_Lunch Hike.gpx"
        app.save_import_state({filename})
        app.save_auto_dedup_queue([filename])

        def successful_reupload(cfg, paths):
            self.assertNotIn(filename, app.load_import_state())
            self.assertNotIn(filename, app.load_auto_dedup_queue())
            return 0, paths

        with patch.object(app, "dawarich_gpx_count", return_value=0), \
                patch.object(app, "get_activity_streams", return_value=streams), \
                patch.object(app, "push_to_dawarich", side_effect=successful_reupload), \
                patch.dict(app.os.environ, {"DAWARICH_AUTO_DEDUP": "false"}):
            result = app.process_backfill_activity({}, "token", activity)

        self.assertEqual(result, "imported")


if __name__ == "__main__":
    unittest.main()
