import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def call(self, *arguments):
        return subprocess.run([sys.executable, "-m", "intakeproof", *arguments], cwd=ROOT, text=True, capture_output=True, timeout=8)

    def test_portable_demo_command_writes_real_evidence(self):
        with tempfile.TemporaryDirectory(prefix="intakeproof-cli-") as temp:
            result = self.call("demo", "--out", temp, "--approve-mapping")
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["summary"]["accepted"], 4)
            self.assertEqual(summary["planner"], "local_rules")
            self.assertEqual(summary["executor"], "local")
            self.assertTrue((Path(temp) / "evidence.zip").is_file())

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="intakeproof-cli-") as temp:
            sentinel = Path(temp) / "original.csv"
            sentinel.write_text("user-owned sentinel", encoding="utf-8")
            result = self.call("demo", "--out", temp)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text("utf-8"), "user-owned sentinel")

    def test_live_request_without_verified_free_access_stops_before_credentials(self):
        with tempfile.TemporaryDirectory(prefix="intakeproof-cli-") as temp:
            for option in ("--planner", "--executor"):
                provider = "openai" if option == "--planner" else "agent37"
                result = self.call("demo", "--out", temp, option, provider)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("No network call was made", result.stderr)
                self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
