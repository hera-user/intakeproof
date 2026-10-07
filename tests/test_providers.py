"""No live requests: HTTP doubles plus the real worker in a temporary directory."""

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from intakeproof.engine import IntakeError, json_bytes, parse_source
from intakeproof.planner import plan
from intakeproof.providers import Agent37Executor, HTTPSClient, OpenAIPlanner

ROOT = Path(__file__).resolve().parents[1]


class ModelDouble:
    is_live = False
    def __init__(self, response):
        self.response, self.requests = response, []
    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return json_bytes(self.response), {"http_status": 200, "test_double": True}


class InstanceDouble:
    is_live = False
    def __init__(self, directory, corrupt=False):
        self.directory, self.corrupt, self.requests = directory, corrupt, []
    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        remote_path = parse_qs(urlparse(url).query)["path"][0]
        name = Path(remote_path).name
        local_path = self.directory / name
        if method == "PUT":
            local_path.write_bytes(kwargs["body"])
            return json_bytes({"path": remote_path}), {"http_status": 200, "test_double": True}
        content = local_path.read_bytes()
        if self.corrupt:
            result = json.loads(content)
            result["source"]["source_sha256"] = "fabricated"
            content = json_bytes(result)
        return content, {"http_status": 200, "test_double": True}


class ControlDouble:
    is_live = False
    def __init__(self, directory, failed=False):
        self.directory, self.failed, self.requests = directory, failed, []
    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if method == "GET":
            return json_bytes({"id": "testinstance", "status": "running", "past_due": False}), {"http_status": 200, "test_double": True}
        command = json.loads(kwargs["body"])["command"]
        if not command.startswith("python3 /tmp/intakeproof-") or not command.endswith("/worker.py"):
            raise AssertionError("Unexpected generated worker command")
        if self.failed:
            return json_bytes({"exit_code": 1, "stdout": "", "stderr": "Synthetic failure", "truncated": False}), {"http_status": 200, "test_double": True}
        completed = subprocess.run([sys.executable, str(self.directory / "worker.py")], capture_output=True, text=True, timeout=5)
        return json_bytes({"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr, "truncated": False}), {"http_status": 200, "test_double": True}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.raw = (ROOT / "examples/supplier-drift.csv").read_bytes()
        self.recipe, self.receipt = plan(self.raw)

    def response(self):
        return {"id": "resp_test_only", "status": "completed", "model": "test-model", "usage": {"input_tokens": 1, "output_tokens": 1}, "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"mapping": self.recipe["mapping"]})}]}]}

    def test_live_adapters_refuse_unverified_free_access(self):
        with self.assertRaises(IntakeError) as ctx:
            OpenAIPlanner("test-key", "test-model")
        self.assertEqual(ctx.exception.code, "free_access_unverified")
        with self.assertRaises(IntakeError):
            Agent37Executor("test-key", "testinstance")

    def test_host_boundary_rejects_before_network(self):
        client = HTTPSClient("api.openai.com", 1)
        for url in ("http://api.openai.com/v1/responses", "https://attacker.example/v1/responses", "https://api.openai.com@attacker.example/", "https://api.openai.com:444/"):
            with self.subTest(url=url), self.assertRaises(IntakeError):
                client.request("POST", url)
        self.assertEqual(client.calls, 0)

    def test_openai_schema_request_and_receipt_are_bounded_and_truthful(self):
        transport = ModelDouble(self.response())
        provider = OpenAIPlanner("test-key", "test-model", free_access_verified=True, client=transport)
        recipe, receipt = plan(self.raw, provider)
        self.assertEqual(recipe, self.recipe)
        self.assertFalse(receipt["live_model_call"])
        self.assertEqual(receipt["mode"], "openai_test_double")
        method, url, kwargs = transport.requests[0]
        payload = json.loads(kwargs["body"])
        self.assertEqual(url, "https://api.openai.com/v1/responses")
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["store"])
        self.assertNotIn("tools", payload)
        self.assertEqual(len(json.loads(payload["input"][1]["content"])["samples"]), 2)
        self.assertNotIn("test-key", json.dumps(receipt))

    def test_model_refusal_incomplete_and_missing_output_fail_explicitly(self):
        for change, expected in (({"status": "incomplete"}, "model_incomplete"), ({"output": []}, "model_receipt"), ({"output": [{"content": [{"type": "refusal", "refusal": "test"}]}]}, "model_refusal"), ({"output": None}, "model_output")):
            response = self.response()
            response.update(change)
            provider = OpenAIPlanner("test-key", "test-model", free_access_verified=True, client=ModelDouble(response))
            with self.subTest(change=change), self.assertRaises(IntakeError) as ctx:
                plan(self.raw, provider)
            self.assertEqual(ctx.exception.code, expected)

    def remote(self, directory, *, failed=False, corrupt=False):
        control = ControlDouble(directory, failed)
        instance = InstanceDouble(directory, corrupt)
        return Agent37Executor("test-key", "testinstance", free_access_verified=True, control_client=control, instance_client=instance), control, instance

    def test_real_worker_roundtrip_over_mocked_http_preserves_receipts(self):
        with tempfile.TemporaryDirectory(prefix="intakeproof-test-") as temp:
            executor, control, instance = self.remote(Path(temp))
            result = executor.run(self.raw, self.recipe, mapping_approved=True, planner_receipt=self.receipt)
            self.assertEqual(result["summary"]["accepted"], 4)
            self.assertEqual(result["summary"]["review"], 6)
            self.assertEqual(result["executor"]["mode"], "agent37_test_double")
            self.assertFalse(result["executor"]["cloud_call"])
            self.assertFalse(result["executor"]["verified"])
            self.assertTrue(result["executor"]["deterministic_replay_matches"])
            self.assertEqual(len(instance.requests), 4)
            self.assertEqual(len(control.requests), 2)
            self.assertNotIn("test-key", json.dumps(result))

    def test_remote_nonzero_exit_cannot_be_misreported_as_success(self):
        with tempfile.TemporaryDirectory(prefix="intakeproof-test-") as temp:
            executor, control, instance = self.remote(Path(temp), failed=True)
            with self.assertRaises(IntakeError) as ctx:
                executor.run(self.raw, self.recipe)
            self.assertEqual(ctx.exception.code, "remote_execution_failed")
            self.assertEqual(len(instance.requests), 3)
            self.assertEqual(sum(method == "POST" for method, *_ in control.requests), 1)

    def test_remote_corrupted_result_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="intakeproof-test-") as temp:
            executor, _, _ = self.remote(Path(temp), corrupt=True)
            with self.assertRaises(IntakeError) as ctx:
                executor.run(self.raw, self.recipe)
            self.assertEqual(ctx.exception.code, "remote_integrity")


if __name__ == "__main__":
    unittest.main()
