"""Deterministic contract enforcement. Source strings never become code."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import time
import uuid
import zipfile
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any

MAX_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 20000
FIELDS = ("line_id", "sku", "quantity", "ship_date")
OPS = {"line_id": "identity", "sku": "identity", "quantity": "integer", "ship_date": "date"}
CONTRACT = {
    "id": "shipment.v1",
    "fields": list(FIELDS),
    "identifiers": "1–64 ASCII letters, digits, dot, underscore, slash or hyphen; first character alphanumeric; whitespace is not silently stripped",
    "quantity": "Unsigned base-10 integer from 0 to 1000000; surrounding whitespace may be trimmed",
    "ship_date": "Valid calendar date, emitted as YYYY-MM-DD; ambiguous dates require an explicit review decision",
    "line_id_unique": True,
    "encoding": "UTF-8, optional BOM",
    "max_bytes": MAX_BYTES,
    "max_records": MAX_RECORDS,
}


class IntakeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_source(raw: bytes, delimiter: str = "auto") -> dict:
    if not isinstance(raw, bytes) or not raw:
        raise IntakeError("empty_file", "Choose a non-empty UTF-8 CSV file.")
    if len(raw) > MAX_BYTES:
        raise IntakeError("file_too_large", "This version accepts files up to 2 MiB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IntakeError("encoding", "The file must be UTF-8. Original bytes have not been changed.") from exc
    if "\x00" in text:
        raise IntakeError("nul_character", "NUL bytes are not supported in CSV input.")
    if delimiter == "auto":
        try:
            delimiter = csv.Sniffer().sniff(text[:65536], delimiters=",;\t").delimiter
        except csv.Error as exc:
            raise IntakeError("dialect", "Could not determine the separator. Choose comma, semicolon or tab explicitly.") from exc
    if delimiter not in (",", ";", "\t"):
        raise IntakeError("dialect", "Unsupported separator.")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        headers = next(reader)
        normalized = [h.strip().casefold() for h in headers]
        if not headers or any(not h for h in normalized):
            raise IntakeError("empty_header", "Every source column needs a non-empty header.")
        if len(set(normalized)) != len(normalized):
            raise IntakeError("duplicate_header", "Duplicate or indistinguishable headers could overwrite data; the file was rejected.")
        if len(headers) > 100:
            raise IntakeError("too_many_columns", "This version accepts up to 100 source columns.")
        if any(len(header) > 128 for header in headers):
            raise IntakeError("header_too_long", "Source headers must be at most 128 characters.")
        records = []
        last_line = reader.line_num
        for index, values in enumerate(reader, 1):
            first_line, last_line = last_line + 1, reader.line_num
            if index > MAX_RECORDS:
                raise IntakeError("too_many_records", f"This version accepts up to {MAX_RECORDS} records.")
            if len(values) != len(headers):
                raise IntakeError("row_width", f"Record {index}, physical lines {first_line}–{last_line}, has {len(values)} values for {len(headers)} headers. No rows were silently skipped.")
            original = dict(zip(headers, values))
            records.append({
                "record_id": f"r{index:06d}", "ordinal": index,
                "physical_lines": [first_line, last_line], "original": original,
                "record_sha256": sha256(json_bytes(original)),
            })
    except StopIteration as exc:
        raise IntakeError("empty_file", "The CSV has no header.") from exc
    except csv.Error as exc:
        raise IntakeError("csv_syntax", f"Malformed CSV near physical line {reader.line_num}: {exc}") from exc
    if not records:
        raise IntakeError("no_records", "The CSV contains a header but no records.")
    return {
        "headers": headers, "header_sha256": sha256(json_bytes(headers)),
        "source_sha256": sha256(raw), "source_bytes": len(raw),
        "delimiter": delimiter, "records": records,
    }


def validate_recipe(recipe: dict, source: dict) -> None:
    if not isinstance(recipe, dict) or set(recipe) != {"version", "contract_id", "header_sha256", "mapping"}:
        raise IntakeError("recipe_shape", "Recipe must contain only version, contract_id, header_sha256 and mapping.")
    if type(recipe["version"]) is not int or recipe["version"] != 1 or recipe["contract_id"] != CONTRACT["id"]:
        raise IntakeError("recipe_contract", "Recipe version or contract does not match shipment.v1.")
    if recipe["header_sha256"] != source["header_sha256"]:
        raise IntakeError("header_changed", "This saved recipe belongs to a different source header. Review a fresh mapping.")
    mapping = recipe["mapping"]
    if not isinstance(mapping, dict) or set(mapping) != set(FIELDS):
        raise IntakeError("mapping_fields", "Map each of the four required target fields exactly once.")
    used = []
    for target in FIELDS:
        rule = mapping[target]
        if not isinstance(rule, dict) or set(rule) != {"source", "operation", "evidence"}:
            raise IntakeError("mapping_shape", f"Unsupported rule for {target}.")
        if not isinstance(rule["source"], str) or rule["source"] not in source["headers"]:
            raise IntakeError("unknown_column", f"The source column for {target} does not exist.")
        if rule["operation"] != OPS[target]:
            raise IntakeError("unsafe_operation", f"Only {OPS[target]} is allowed for {target}.")
        if not isinstance(rule["evidence"], str) or not 1 <= len(rule["evidence"]) <= 1000:
            raise IntakeError("mapping_evidence", "Each mapping needs a short explanation for review.")
        used.append(rule["source"])
    if len(used) != len(set(used)):
        raise IntakeError("reused_column", "Each target needs its own source column. A source column cannot be reused.")


def normalize_date(value: str, date_order: str | None) -> tuple[str, str]:
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value).isoformat(), "ISO date in source"
        except ValueError as exc:
            raise IntakeError("invalid_date", "The date does not exist in the calendar.") from exc
    match = re.fullmatch(r"([0-9]{1,2})([/.-])([0-9]{1,2})\2([0-9]{4})", value)
    if not match:
        raise IntakeError("date_format", "Use YYYY-MM-DD or a numeric day/month/year date with a four-digit year.")
    first, second, year = int(match[1]), int(match[3]), int(match[4])
    choices = {}
    for order, month, day in (("DMY", second, first), ("MDY", first, second)):
        try:
            choices[order] = date(year, month, day).isoformat()
        except ValueError:
            pass
    if date_order:
        if date_order not in choices:
            raise IntakeError("invalid_date", f"The date is invalid under the reviewed {date_order} order.")
        return choices[date_order], f"Explicit reviewer date order: {date_order}"
    if not choices:
        raise IntakeError("invalid_date", "The date does not exist under either supported date order.")
    if len(set(choices.values())) == 1:
        return next(iter(choices.values())), "Only one valid calendar interpretation for this value"
    raise IntakeError("ambiguous_date", f"Could mean {choices['DMY']} or {choices['MDY']}; a reviewer must choose.")


def normalize(field: str, value: str, date_order: str | None) -> tuple[str, str]:
    if not value or not value.strip():
        raise IntakeError("required", "A required value is missing.")
    if field in ("line_id", "sku"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}", value):
            raise IntakeError("invalid_identifier", "Identifier fails the narrow import contract; formula-like or whitespace values are not silently altered.")
        return value, "Identifier preserved exactly as a string"
    if field == "quantity":
        cleaned = value.strip()
        if not re.fullmatch(r"[0-9]{1,10}", cleaned) or int(cleaned) > 1000000:
            raise IntakeError("invalid_quantity", "Quantity must be an unsigned integer between 0 and 1000000; no grouping or decimals are inferred.")
        return str(int(cleaned)), "Exact base-10 integer; surrounding whitespace removed"
    return normalize_date(value.strip(), date_order)


def validate_decisions(source: dict, decisions: dict | None) -> dict:
    decisions = decisions or {}
    if not isinstance(decisions, dict) or set(decisions) - {"date_order", "date_order_reason", "corrections"}:
        raise IntakeError("decision_shape", "Unsupported review decision.")
    order = decisions.get("date_order") or None
    reason = decisions.get("date_order_reason", "")
    if order not in (None, "DMY", "MDY"):
        raise IntakeError("date_order", "Choose DMY, MDY or leave date order unconfirmed.")
    if order and (not isinstance(reason, str) or not reason.strip() or len(reason) > 1000):
        raise IntakeError("decision_reason", "Explain the evidence for the chosen date order.")
    corrections = decisions.get("corrections", [])
    if not isinstance(corrections, list) or len(corrections) > MAX_RECORDS:
        raise IntakeError("corrections", "Corrections must be a bounded list.")
    known_ids = {r["record_id"] for r in source["records"]}
    seen = set()
    for c in corrections:
        if not isinstance(c, dict) or set(c) != {"record_id", "values", "reason"}:
            raise IntakeError("correction_shape", "Each correction needs record_id, values and reason.")
        rid = c["record_id"]
        if not isinstance(rid, str) or rid not in known_ids or rid in seen:
            raise IntakeError("correction_record", "Corrections must identify distinct records in this source file.")
        if not isinstance(c["values"], dict) or not c["values"] or set(c["values"]) - set(FIELDS):
            raise IntakeError("correction_fields", "Corrections can only change target-contract fields.")
        if any(not isinstance(v, str) or len(v) > 1000 for v in c["values"].values()):
            raise IntakeError("correction_value", "Replacement values must be strings of at most 1000 characters.")
        if not isinstance(c["reason"], str) or not c["reason"].strip() or len(c["reason"]) > 1000:
            raise IntakeError("correction_reason", "Each correction needs an evidence-based reason.")
        seen.add(rid)
    return {"date_order": order, "date_order_reason": reason if order else "", "corrections": corrections}


def execute(raw: bytes, recipe: dict, *, delimiter: str = "auto", decisions: dict | None = None,
            mapping_approved: bool = False, planner_receipt: dict | None = None,
            executor_receipt: dict | None = None, parent_run_id: str | None = None) -> dict:
    started = time.perf_counter()
    source = parse_source(raw, delimiter)
    validate_recipe(recipe, source)
    decisions = validate_decisions(source, decisions)
    corrections = {c["record_id"]: c for c in decisions["corrections"]}
    rows = []
    for record in source["records"]:
        row = {**record, "values": {}, "issues": [], "changes": [], "field_evidence": {}}
        correction = corrections.get(record["record_id"])
        for field in FIELDS:
            rule = recipe["mapping"][field]
            original = record["original"][rule["source"]]
            value = original
            if correction and field in correction["values"]:
                value = correction["values"][field]
                row["changes"].append({"field": field, "kind": "reviewer_correction", "before": original, "after": value, "reason": correction["reason"], "source": "local_review_decision"})
            try:
                normalized, evidence = normalize(field, value, decisions["date_order"])
                row["values"][field] = normalized
                row["field_evidence"][field] = {"source_column": rule["source"], "operation": rule["operation"], "evidence": evidence}
                if normalized != value:
                    row["changes"].append({"field": field, "kind": "normalization", "before": value, "after": normalized, "reason": evidence})
            except IntakeError as exc:
                row["values"][field] = value
                row["issues"].append({"field": field, "code": exc.code, "message": str(exc)})
        rows.append(row)
    counts = Counter(row["values"]["line_id"] for row in rows if row["values"]["line_id"])
    for row in rows:
        if counts[row["values"]["line_id"]] > 1:
            row["issues"].append({"field": "line_id", "code": "duplicate_line_id", "message": "Every record with this duplicate reference is held for review."})
        row["disposition"] = "review" if row["issues"] else "accepted"
    accepted = [row for row in rows if row["disposition"] == "accepted"]
    review = [row for row in rows if row["disposition"] == "review"]
    input_ids = {r["record_id"] for r in rows}
    accepted_ids, review_ids = {r["record_id"] for r in accepted}, {r["record_id"] for r in review}
    conservation = (not (accepted_ids & review_ids) and accepted_ids | review_ids == input_ids and len(accepted) + len(review) == len(rows))
    if not conservation:
        raise RuntimeError("Internal row-accounting invariant failed; no import is released.")
    identifier_checks = []
    for row in accepted:
        for field in ("line_id", "sku"):
            before = row["original"][recipe["mapping"][field]["source"]]
            after = row["values"][field]
            identifier_checks.append({"record_id": row["record_id"], "field": field, "unchanged": before == after, "reviewed_correction": before != after and row["record_id"] in corrections})
    executor = executor_receipt or {"mode": "local", "verified": True, "implementation": "Python standard-library deterministic executor", "cloud_call": False}
    return {
        "schema_version": 1, "run_id": str(uuid.uuid4()), "created_at_utc": utcnow(),
        "parent_run_id": parent_run_id, "contract": CONTRACT, "recipe": recipe,
        "decisions": decisions, "mapping_approved": mapping_approved is True,
        "source": {k: v for k, v in source.items() if k != "records"},
        "summary": {"input": len(rows), "accepted": len(accepted), "review": len(review), "conservation_passed": conservation,
                    "identifier_checks": len(identifier_checks), "identifiers_unchanged": sum(c["unchanged"] for c in identifier_checks),
                    "reviewed_identifier_corrections": sum(c["reviewed_correction"] for c in identifier_checks)},
        "identifier_evidence": identifier_checks, "records": rows,
        "planner": planner_receipt or {"mode": "manual", "live_model_call": False},
        "executor": executor, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "claims": {"semantic_mapping_correctness_proven": False, "original_bytes_preserved_in_bundle": True,
                   "unresolved_records_excluded_from_import": True, "human_identity_authenticated": False,
                   "timing_scope": "Parsing, validation and deterministic transformation only; excludes setup and model planning"},
    }


def import_csv(result: dict) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for row in result["records"]:
        if row["disposition"] == "accepted":
            writer.writerow(row["values"])
    return stream.getvalue().encode("utf-8")


def report_html(result: dict) -> bytes:
    esc = lambda x: html.escape(str(x), quote=True)
    s = result["summary"]
    rows = []
    for row in result["records"]:
        values = " · ".join(f"{field}: {row['values'][field]}" for field in FIELDS)
        issues = "; ".join(f"{i['field']}: {i['message']}" for i in row["issues"]) or "Contract checks passed"
        rows.append(f"<tr><td>{esc(row['record_id'])}</td><td>{esc(row['physical_lines'])}</td><td>{esc(row['disposition'])}</td><td>{esc(values)}</td><td>{esc(issues)}</td></tr>")
    approval = "Reviewed mapping" if result["mapping_approved"] else "Draft — mapping needs review"
    output = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>IntakeProof evidence report</title>
<style>body{{font:16px system-ui;background:#f4f2ec;color:#182f28;max-width:1120px;margin:48px auto;padding:0 24px}}h1{{font-size:44px;letter-spacing:-2px}}.card{{background:white;border:1px solid #d5ddd4;border-radius:14px;padding:24px;margin:24px 0}}.stats{{font-size:26px;font-weight:700}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #d5ddd4;vertical-align:top;overflow-wrap:anywhere}}code{{overflow-wrap:anywhere}}.muted{{color:#53685c}}@media print{{body{{margin:10px}}}}</style>
<p>INTAKEPROOF / EVIDENCE REPORT</p><h1>Every record accounted for.</h1><p>{esc(approval)} · {esc(result['created_at_utc'])}</p>
<div class="card stats">{s['input']} input = {s['accepted']} accepted + {s['review']} in review</div>
<p>Row conservation: {'PASS' if s['conservation_passed'] else 'FAIL'}. Unresolved rows are excluded from the import and retained in review.json. Row accounting does not prove the semantic correctness of the chosen mapping.</p>
<div class="card"><p>Run: <code>{esc(result['run_id'])}</code></p><p>Original source SHA-256: <code>{esc(result['source']['source_sha256'])}</code></p><p>Planner: {esc(result['planner'].get('mode'))} · Executor: {esc(result['executor'].get('mode'))}</p><p>{s['identifiers_unchanged']} of {s['identifier_checks']} accepted identifier values preserved exactly; {s['reviewed_identifier_corrections']} explicitly corrected.</p><p class="muted">Original bytes, complete field evidence, review reasons and decisions are in the accompanying audit bundle. Local reviewer identity is not authenticated.</p></div>
<table><thead><tr><th>Record</th><th>Physical lines</th><th>Decision</th><th>Values</th><th>Evidence / unresolved case</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p class="muted">{esc(result['claims']['timing_scope'])}. Elapsed: {result['elapsed_ms']} ms. No time-saved or customer-use claim is made.</p></html>"""
    return output.encode("utf-8")


def make_bundle(raw: bytes, result: dict) -> bytes:
    if sha256(raw) != result["source"]["source_sha256"]:
        raise IntakeError("source_mismatch", "This result does not match the original source bytes.")
    filename = "reviewed_import.csv" if result["mapping_approved"] else "candidate_import.csv"
    files = {
        "original.csv": raw, filename: import_csv(result), "audit.json": json_bytes(result),
        "review.json": json_bytes([r for r in result["records"] if r["disposition"] == "review"]),
        "recipe.json": json_bytes(result["recipe"]), "contract.json": json_bytes(CONTRACT),
        "report.html": report_html(result),
    }
    files["SHA256SUMS.txt"] = "".join(f"{sha256(content)}  {name}\n" for name, content in files.items()).encode("ascii")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()
