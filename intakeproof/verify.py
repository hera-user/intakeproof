"""Offline verification of exported evidence, without extraction or provider access."""

from __future__ import annotations

import io
import json
import math
import re
import uuid
import zipfile
import zlib

from .engine import MAX_BYTES, IntakeError, execute, import_csv, json_bytes, report_html, sha256

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
BASE_FILES = {"original.csv", "audit.json", "review.json", "recipe.json", "contract.json", "report.html", "SHA256SUMS.txt"}


def read_json(raw: bytes, name: str):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IntakeError("bundle_json", f"{name} is not valid, unambiguous UTF-8 JSON.") from exc


def same_json(left, right):
    # Canonical serialization also distinguishes JSON true from the integer 1.
    return json_bytes(left) == json_bytes(right)


def verify_bundle(bundle: bytes, *, expected_source_sha256: str | None = None) -> dict:
    """Replay the recorded decisions, not the planner or claimed cloud execution.

    A successful replay establishes internal consistency with this trusted engine.
    The optional source hash must come from a separately retained original.
    Neither hashes nor a self-contained bundle authenticate its author or claims.
    """
    if expected_source_sha256 is not None:
        if not isinstance(expected_source_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_source_sha256):
            raise IntakeError("source_hash_format", "Expected source hash must contain exactly 64 hexadecimal characters.")
        expected_source_sha256 = expected_source_sha256.lower()
    if not isinstance(bundle, bytes) or not bundle or len(bundle) > MAX_BUNDLE_BYTES:
        raise IntakeError("bundle_size", "Choose an evidence ZIP of at most 32 MiB.")
    try:
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            import_names = set(names) & {"candidate_import.csv", "reviewed_import.csv"}
            if len(names) != 8 or len(set(names)) != 8 or len(import_names) != 1 or set(names) != BASE_FILES | import_names:
                raise IntakeError("bundle_members", "The evidence ZIP must contain exactly the eight expected files, with one import and no duplicate names or paths.")
            if sum(entry.file_size for entry in entries) > MAX_UNPACKED_BYTES:
                raise IntakeError("bundle_size", "The evidence ZIP exceeds the 128 MiB unpacked limit.")
            files = {}
            for entry in entries:
                limit = MAX_BYTES if entry.filename == "original.csv" else MAX_UNPACKED_BYTES
                if entry.filename == "SHA256SUMS.txt":
                    limit = 8192
                if entry.file_size > limit:
                    raise IntakeError("bundle_size", f"{entry.filename} exceeds its verification size limit.")
                if entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise IntakeError("bundle_encoding", "Encrypted or unsupported ZIP entries cannot be verified.")
                with archive.open(entry) as stream:
                    content = stream.read(limit + 1)
                if len(content) != entry.file_size or len(content) > limit:
                    raise IntakeError("bundle_size", f"{entry.filename} does not match its declared size.")
                files[entry.filename] = content
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error) as exc:
        raise IntakeError("bundle_zip", "The evidence ZIP is damaged or unsupported.") from exc

    try:
        manifest = {}
        for line in files["SHA256SUMS.txt"].decode("ascii").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.]+)", line)
            if not match or match[2] in manifest:
                raise ValueError("Invalid or duplicate manifest entry")
            manifest[match[2]] = match[1]
        if set(manifest) != set(files) - {"SHA256SUMS.txt"}:
            raise ValueError("Incomplete manifest")
    except (ValueError, UnicodeError) as exc:
        raise IntakeError("bundle_manifest", "The hash manifest must identify each of the seven payload files exactly once.") from exc
    for name, digest in manifest.items():
        if sha256(files[name]) != digest:
            raise IntakeError("bundle_hash", f"Hash mismatch: {name}.")

    source_hash = sha256(files["original.csv"])
    if expected_source_sha256 is not None and source_hash != expected_source_sha256:
        raise IntakeError("source_anchor", "The bundled source differs from the independently supplied original-source hash.")
    audit = read_json(files["audit.json"], "audit.json")
    if not isinstance(audit, dict) or type(audit.get("schema_version")) is not int or audit["schema_version"] != 1:
        raise IntakeError("bundle_schema", "Expected an IntakeProof audit with schema_version 1.")
    if type(audit.get("mapping_approved")) is not bool or not isinstance(audit.get("source"), dict):
        raise IntakeError("bundle_schema", "The audit lacks explicit mapping approval and source metadata.")
    delimiter = audit["source"].get("delimiter")
    if not isinstance(delimiter, str) or delimiter not in (",", ";", "\t"):
        raise IntakeError("bundle_schema", "The audit must record its actual CSV delimiter.")
    if not isinstance(audit.get("recipe"), dict) or not isinstance(audit.get("decisions"), dict):
        raise IntakeError("bundle_schema", "The audit must contain its mapping and recorded review decisions.")
    try:
        uuid.UUID(audit["run_id"])
        if audit.get("parent_run_id") is not None:
            uuid.UUID(audit["parent_run_id"])
        if not isinstance(audit["created_at_utc"], str) or type(audit["elapsed_ms"]) not in (int, float) or audit["elapsed_ms"] < 0:
            raise ValueError("Invalid run metadata")
        if any(not isinstance(audit[key], dict) for key in ("planner", "executor")):
            raise ValueError("Invalid claimed provider metadata")
        replay = execute(files["original.csv"], audit["recipe"], delimiter=delimiter,
                         decisions=audit["decisions"], mapping_approved=audit["mapping_approved"])
        for field in ("records", "source", "summary", "identifier_evidence", "recipe", "contract", "decisions", "mapping_approved", "claims"):
            if not same_json(audit.get(field), replay[field]):
                raise IntakeError("bundle_replay", f"Audit {field} differs from a fresh deterministic replay of the original and recorded decisions.")
        for name, value in (("recipe.json", replay["recipe"]), ("contract.json", replay["contract"]),
                            ("review.json", [row for row in replay["records"] if row["disposition"] == "review"] )):
            if not same_json(read_json(files[name], name), value):
                raise IntakeError("bundle_replay", f"{name} differs from the verified replay.")
        import_name = "reviewed_import.csv" if audit["mapping_approved"] else "candidate_import.csv"
        if import_name not in files or files[import_name] != import_csv(replay):
            raise IntakeError("bundle_import", "The import filename or exact CSV bytes differ from the verified accepted records.")
        if files["report.html"] != report_html(audit):
            raise IntakeError("bundle_report", "The readable report differs from the audited result.")
    except IntakeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
        raise IntakeError("bundle_schema", "The audit has missing or invalid fields; no verification result was issued.") from exc

    return {
        "status": "verified", "verification_mode": "offline_deterministic_replay",
        "bundle_sha256": sha256(bundle), "source_sha256": source_hash,
        "source_anchor": "matched" if expected_source_sha256 else "not_supplied",
        "run_id": audit["run_id"], "recorded_mapping_approval": audit["mapping_approved"],
        "summary": replay["summary"], "import_file": import_name,
        "checks": ["seven_file_hashes", "source_bytes_and_lineage", "row_accounting", "identifier_evidence",
                   "recorded_decisions_replayed", "exact_import_bytes", "complete_review_queue", "report_matches_audit"],
        "limits": {"provider_provenance_verified": False, "reviewer_identity_verified": False,
                   "semantic_mapping_correctness_proven": False,
                   "explanation": "Internal consistency with this engine is verified. Author, provider usage and review authority are not authenticated; a separately retained original hash is needed to anchor source identity."},
    }
