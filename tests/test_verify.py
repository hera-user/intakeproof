import io
import json
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from intakeproof.engine import IntakeError, execute, import_csv, json_bytes, make_bundle, report_html, sha256
from intakeproof.planner import plan
from intakeproof.verify import verify_bundle

ROOT = Path(__file__).resolve().parents[1]


def unpack(bundle):
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def pack(files, rehash=True, duplicate=None):
    files = dict(files)
    if rehash:
        files["SHA256SUMS.txt"] = "".join(f"{sha256(value)}  {name}\n" for name, value in files.items() if name != "SHA256SUMS.txt").encode("ascii")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        if duplicate:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(duplicate, files[duplicate])
    return stream.getvalue()


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.raw = (ROOT / "examples/supplier-drift.csv").read_bytes()
        self.recipe, _ = plan(self.raw)
        self.result = execute(self.raw, self.recipe, mapping_approved=True)
        self.bundle = make_bundle(self.raw, self.result)

    def assert_rejected(self, bundle, code, **options):
        with self.assertRaises(IntakeError) as error:
            verify_bundle(bundle, **options)
        self.assertEqual(error.exception.code, code, str(error.exception))

    def test_published_video_and_baseline_evidence_verify_offline(self):
        with patch("socket.socket.connect", side_effect=AssertionError("Verification must stay offline")):
            for name, accepted in (("reviewed-evidence.zip", 5), ("baseline-evidence.zip", 4)):
                with self.subTest(name=name):
                    bundle = (ROOT / "docs/assets" / name).read_bytes()
                    result = verify_bundle(bundle, expected_source_sha256=sha256(self.raw))
                    self.assertEqual(result["source_anchor"], "matched")
                    self.assertEqual(result["summary"]["accepted"], accepted)
                    self.assertEqual(result["summary"]["input"], 10)
                    self.assertFalse(result["limits"]["provider_provenance_verified"])

    def test_changed_import_fails_even_after_recomputing_manifest(self):
        files = unpack(self.bundle)
        files["reviewed_import.csv"] = files["reviewed_import.csv"].replace(b"000101", b"999999", 1)
        self.assert_rejected(pack(files, rehash=False), "bundle_hash")
        self.assert_rejected(pack(files), "bundle_import")

    def test_dropped_record_is_rejected_even_with_consistent_export_hashes(self):
        audit = json.loads(json_bytes(self.result))
        audit["records"].pop()
        audit["summary"]["input"] -= 1
        audit["summary"]["review"] -= 1
        files = unpack(self.bundle)
        files.update({"audit.json": json_bytes(audit), "reviewed_import.csv": import_csv(audit),
                      "review.json": json_bytes([r for r in audit["records"] if r["disposition"] == "review"]),
                      "report.html": report_html(audit)})
        self.assert_rejected(pack(files), "bundle_replay")

    def test_changed_review_reason_and_report_are_rejected(self):
        files = unpack(self.bundle)
        review = json.loads(files["review.json"])
        review[0]["issues"][0]["message"] = "Invented permission to accept"
        files["review.json"] = json_bytes(review)
        self.assert_rejected(pack(files), "bundle_replay")
        files = unpack(self.bundle)
        files["report.html"] = b"<h1>All records approved</h1>"
        self.assert_rejected(pack(files), "bundle_report")

    def test_separate_source_hash_detects_a_self_consistent_replacement(self):
        changed = self.raw.replace(b"000101", b"999999", 1)
        replaced = make_bundle(changed, execute(changed, self.recipe, mapping_approved=True))
        self.assertEqual(verify_bundle(replaced)["source_anchor"], "not_supplied")
        self.assert_rejected(replaced, "source_anchor", expected_source_sha256=sha256(self.raw))

    def test_unknown_paths_duplicate_members_and_incomplete_manifest_fail(self):
        files = unpack(self.bundle)
        self.assert_rejected(pack({**files, "../outside.txt": b"do not extract"}), "bundle_members")
        self.assert_rejected(pack(files, duplicate="original.csv"), "bundle_members")
        files["SHA256SUMS.txt"] = b""
        self.assert_rejected(pack(files, rehash=False), "bundle_manifest")

    def test_duplicate_json_keys_nonfinite_numbers_and_boolean_schema_are_rejected(self):
        files = unpack(self.bundle)
        files["audit.json"] = b'{"schema_version":1,' + files["audit.json"][1:]
        self.assert_rejected(pack(files), "bundle_json")
        rest = {key: value for key, value in self.result.items() if key != "elapsed_ms"}
        files["audit.json"] = b'{"elapsed_ms":1e999,' + json_bytes(rest)[1:]
        self.assert_rejected(pack(files), "bundle_json")
        audit = dict(self.result, schema_version=True)
        files["audit.json"] = json_bytes(audit)
        self.assert_rejected(pack(files), "bundle_schema")

    def test_candidate_import_cannot_be_relabelled_as_reviewed(self):
        candidate = make_bundle(self.raw, execute(self.raw, self.recipe))
        self.assertEqual(verify_bundle(candidate)["import_file"], "candidate_import.csv")
        files = unpack(candidate)
        files["reviewed_import.csv"] = files.pop("candidate_import.csv")
        self.assert_rejected(pack(files), "bundle_import")

    def test_archive_limits_and_invalid_input_are_explicit_failures(self):
        self.assert_rejected(b"not a zip", "bundle_zip")
        self.assert_rejected(self.bundle, "source_hash_format", expected_source_sha256="not a hash")
        with patch("intakeproof.verify.MAX_BUNDLE_BYTES", 100):
            self.assert_rejected(self.bundle, "bundle_size")
        with patch("intakeproof.verify.MAX_UNPACKED_BYTES", 100):
            self.assert_rejected(self.bundle, "bundle_size")


if __name__ == "__main__":
    unittest.main()
