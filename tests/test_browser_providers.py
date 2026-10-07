"""HTTP review workflow with explicit provider doubles; no external requests."""

import base64
import io
import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from intakeproof.planner import plan
from intakeproof.providers import Agent37Executor, OpenAIPlanner
from intakeproof.server import BrowserRuntime, make_server
from test_providers import ControlDouble, InstanceDouble, ModelDouble
from test_server import HTTPClientMixin, ROOT


class BrowserProviderTests(HTTPClientMixin, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="intakeproof-browser-test-")
        self.raw = (ROOT / "examples/supplier-drift.csv").read_bytes()
        self.recipe, _ = plan(self.raw)
        self.models, self.executors = [], []
        self.fail_model, self.fail_worker = False, False

        def planner_factory():
            response = {"id": "resp_browser_test_only", "model": "test-model", "status": "incomplete" if self.fail_model else "completed",
                        "output": [{"content": [{"type": "output_text", "text": json.dumps({"mapping": self.recipe["mapping"]})}]}]}
            transport = ModelDouble(response)
            self.models.append(transport)
            return OpenAIPlanner("test-key-only", "test-model", free_access_verified=True, client=transport)

        def executor_factory():
            directory = Path(tempfile.mkdtemp(dir=self.temp.name))
            control = ControlDouble(directory, failed=self.fail_worker)
            instance = InstanceDouble(directory)
            self.executors.append((control, instance))
            return Agent37Executor("test-key-only", "testinstance", free_access_verified=True, control_client=control, instance_client=instance)

        self.runtime = BrowserRuntime(planner_factory=planner_factory, executor_factory=executor_factory, max_jobs=2)
        self.server = make_server(0, runtime=self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def run_body(self, inspection, approved=True):
        return {"session_id": inspection["session_id"], "columns": {f: r["source"] for f, r in self.recipe["mapping"].items()}, "mapping_approved": approved}

    def test_review_correction_and_export_use_adapters_without_claiming_live_calls(self):
        info = json.loads(self.request("/api/info")[2])
        self.assertTrue(info["synthetic_only"])
        self.assertFalse(info["live_model_call"])
        self.assertFalse(info["agent37_verified"])
        self.assertEqual(self.models + self.executors, [])
        demo, inspection = self.load_demo()
        self.assertEqual(inspection["planner"]["mode"], "openai_test_double")
        self.assertEqual(inspection["preview"]["executor"]["mode"], "local")
        self.assertEqual(self.executors, [])  # Inspect never launches remote execution.
        first = self.run_demo(inspection)
        second = self.run_demo(inspection, {"corrections": [{"record_id": "r000003", "values": {"ship_date": "2026-04-03"}, "reason": "Synthetic example decision."}]})
        self.assertEqual((first["summary"]["accepted"], second["summary"]["accepted"]), (4, 5))
        self.assertEqual(second["parent_run_id"], first["run_id"])
        self.assertEqual(second["executor"]["mode"], "agent37_test_double")
        self.assertFalse(second["executor"]["verified"])
        self.assertFalse(second["planner"]["live_model_call"])
        archive = zipfile.ZipFile(io.BytesIO(self.request(f"/api/download/{second['run_id']}/bundle.zip")[2]))
        self.assertEqual(archive.read("original.csv"), base64.b64decode(demo["source_base64"]))
        self.assertEqual(len(json.loads(archive.read("review.json"))), 5)
        self.assertNotIn(b"test-key-only", archive.read("audit.json"))
        status, _, body = self.request("/api/run", self.run_body(inspection))
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "provider_job_limit")
        self.assertEqual(len(self.executors), 2)
        self.assertEqual(json.loads(self.request(f"/api/download/{first['run_id']}/audit.json")[2])["summary"]["accepted"], 4)

    def test_claiming_synthetic_does_not_allow_other_source_bytes_to_leave(self):
        changed = self.raw.replace(b"Shipment ref", b"Private ref")
        self.assertNotEqual(changed, self.raw)
        status, _, body = self.request("/api/inspect", {"synthetic": True, "source_base64": base64.b64encode(changed).decode()})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "synthetic_only")
        self.assertEqual(self.models + self.executors, [])
        self.assertEqual(self.runtime.attempts, {"planner": 0, "executor": 0})

    def test_failed_model_is_retained_as_failure_when_manual_mapping_is_reviewed(self):
        self.fail_model = True
        _, inspection = self.load_demo()
        self.assertIsNone(inspection["recipe"])
        self.assertIsNone(inspection["preview"])
        self.assertEqual(inspection["planning_error"]["code"], "model_incomplete")
        status, _, body = self.request("/api/run", self.run_body(inspection))
        result = json.loads(body)
        self.assertEqual(status, 200, body)
        self.assertEqual(result["planner"]["mode"], "manual")
        self.assertEqual(result["planner"]["failed_proposal"]["code"], "model_incomplete")
        self.assertIsNone(result["planner"]["live_model_call"])
        self.assertIn("Unverified", result["planner"]["call_outcome"])
        self.assertEqual(result["summary"]["input"], 10)

    def test_failed_remote_attempts_consume_limit_and_never_save_fallback_results(self):
        self.fail_worker = True
        _, inspection = self.load_demo()
        for code in ("remote_execution_failed", "remote_execution_failed", "provider_job_limit"):
            status, _, body = self.request("/api/run", self.run_body(inspection))
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"]["code"], code)
            self.assertEqual(self.server.app_state.runs, {})
        self.assertEqual(len(self.executors), 2)

    def test_concurrent_new_sessions_cannot_reset_or_exceed_process_planner_cap(self):
        demo = json.loads(self.request("/api/demo")[2])
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(lambda _: self.request("/api/inspect", demo), range(3)))
        results = [json.loads(body) for _, _, body in responses]
        self.assertEqual(sum(r["recipe"] is not None for r in results), 2)
        self.assertEqual([r["planning_error"]["code"] for r in results if r["planning_error"]], ["provider_job_limit"])
        self.assertEqual(len(self.models), 2)
        self.assertEqual(self.runtime.attempts["planner"], 2)
        self.assertEqual(len(self.server.app_state.sessions), 3)

    def test_remote_execution_requires_explicit_mapping_approval(self):
        _, inspection = self.load_demo()
        status, _, body = self.request("/api/run", self.run_body(inspection, approved=False))
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "mapping_review")
        self.assertEqual(self.executors, [])
        self.assertEqual(self.runtime.attempts["executor"], 0)


if __name__ == "__main__":
    unittest.main()
