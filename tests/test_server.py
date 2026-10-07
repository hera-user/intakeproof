import base64
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from intakeproof.server import make_server

ROOT = Path(__file__).resolve().parents[1]


class HTTPClientMixin:
    def request(self, path, body=None, extra_headers=None):
        headers = {"Origin": self.base, "X-IntakeProof-Token": self.server.app_state.token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def load_demo(self):
        demo = json.loads(self.request("/api/demo")[2])
        status, _, body = self.request("/api/inspect", demo)
        self.assertEqual(status, 200)
        return demo, json.loads(body)

    def run_demo(self, inspection, decisions=None):
        columns = {field: rule["source"] for field, rule in inspection["recipe"]["mapping"].items()}
        status, _, body = self.request("/api/run", {"session_id": inspection["session_id"], "columns": columns, "mapping_approved": True, "decisions": decisions or {}})
        self.assertEqual(status, 200, body)
        return json.loads(body)


class ServerTests(HTTPClientMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def test_browser_workflow_exports_matching_source_and_reviewed_import(self):
        demo, inspection = self.load_demo()
        self.assertFalse(inspection["preview"]["mapping_approved"])
        result = self.run_demo(inspection)
        status, headers, body = self.request(f"/api/download/{result['run_id']}/bundle.zip")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(body))
        self.assertEqual(archive.read("original.csv"), base64.b64decode(demo["source_base64"]))
        self.assertEqual(len(json.loads(archive.read("review.json"))), 6)
        self.assertIn("reviewed_import.csv", archive.namelist())

    def test_correction_creates_new_result_and_preserves_previous_download(self):
        _, inspection = self.load_demo()
        first = self.run_demo(inspection)
        second = self.run_demo(inspection, {"corrections": [{"record_id": "r000003", "values": {"ship_date": "2026-04-03"}, "reason": "Synthetic reviewer confirms 3 April."}]})
        self.assertEqual(second["summary"]["accepted"], 5)
        self.assertEqual(second["parent_run_id"], first["run_id"])
        unchanged = json.loads(self.request(f"/api/download/{first['run_id']}/audit.json")[2])
        self.assertEqual(unchanged["summary"]["accepted"], 4)
        self.assertEqual(unchanged["decisions"]["corrections"], [])

    def test_cross_origin_and_missing_token_requests_are_denied(self):
        for headers in ({"Origin": "https://example.com"}, {"X-IntakeProof-Token": ""}, {"Host": "attacker.example"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/inspect", {}, headers)[0], 403)

    def test_malformed_file_does_not_release_partial_results(self):
        status, _, body = self.request("/api/inspect", {"source_base64": base64.b64encode(b"a,b\n1,2\n3\n").decode(), "delimiter": ","})
        self.assertEqual(status, 400)
        error = json.loads(body)
        self.assertEqual(error["error"]["code"], "row_width")
        self.assertFalse(error["conservation_evaluated"])

    def test_unrecognized_headers_require_manual_review(self):
        raw = b"alpha,beta,gamma,delta\n0001,0007,2,2026-10-09\n"
        status, _, body = self.request("/api/inspect", {"source_base64": base64.b64encode(raw).decode()})
        inspection = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIsNone(inspection["recipe"])
        self.assertEqual(inspection["planning_error"]["code"], "mapping_needs_review")
        status, _, body = self.request("/api/run", {"session_id": inspection["session_id"], "columns": {"line_id": "alpha", "sku": "beta", "quantity": "gamma", "ship_date": "delta"}, "mapping_approved": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["summary"]["accepted"], 1)

    def test_ui_assets_do_not_allow_arbitrary_file_reads(self):
        self.assertEqual(self.request("/../intakeproof/engine.py")[0], 404)
        self.assertEqual(self.request("/api/download/no-such-result/audit.json")[0], 404)
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(b"__CSRF_TOKEN__", body)


if __name__ == "__main__":
    unittest.main()
