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


if __name__ == "__main__":
    unittest.main()
