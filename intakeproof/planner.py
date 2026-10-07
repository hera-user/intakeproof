"""Bounded planning: propose a recipe, validate it, then require review."""

from __future__ import annotations

import re
import time
from typing import Protocol

from .engine import CONTRACT, FIELDS, OPS, IntakeError, parse_source, validate_recipe

ALIASES = {
    "line_id": ("line_id", "shipment ref", "shipment reference", "reference", "line reference", "order line", "line no", "line number", "shipment id"),
    "sku": ("sku", "product code", "item code", "product sku", "article", "article code", "part number"),
    "quantity": ("quantity", "units", "qty", "order quantity", "unit count", "pieces"),
    "ship_date": ("ship_date", "dispatch date", "shipment date", "shipping date", "date shipped", "delivery date"),
}


class Provider(Protocol):
    mode: str
    def propose(self, source: dict, previous_error: str | None = None) -> tuple[dict, dict]: ...


def header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


class LocalPlanner:
    mode = "local_rules"

    def propose(self, source: dict, previous_error: str | None = None) -> tuple[dict, dict]:
        mapping = {}
        for field in FIELDS:
            aliases = {header_key(alias) for alias in ALIASES[field]}
            matches = [h for h in source["headers"] if header_key(h) in aliases]
            if len(matches) != 1:
                raise IntakeError("mapping_needs_review", f"Cannot uniquely identify {field}. Select its source column manually; no mapping was invented.")
            mapping[field] = {"source": matches[0], "operation": OPS[field], "evidence": f"Header '{matches[0]}' matches a documented {field} alias. Meaning still needs reviewer confirmation."}
        return mapping, {"mode": self.mode, "live_model_call": False, "provider": "Built-in header aliases", "model": None}


def plan(raw: bytes, provider: Provider | None = None, delimiter: str = "auto") -> tuple[dict, dict]:
    source = parse_source(raw, delimiter)
    provider = provider or LocalPlanner()
    error = None
    trace = []
    start = time.perf_counter()
    for attempt in range(1, 3):
        mapping, receipt = provider.propose(source, error)
        recipe = {"version": 1, "contract_id": CONTRACT["id"], "header_sha256": source["header_sha256"], "mapping": mapping}
        try:
            validate_recipe(recipe, source)
        except IntakeError as exc:
            error = str(exc)
            trace.append({"attempt": attempt, "outcome": "rejected", "code": exc.code, "reason": error, "provider_receipt": receipt})
            continue
        trace.append({"attempt": attempt, "outcome": "validated", "mapping_review_required": True, "provider_receipt": receipt})
        return recipe, {**receipt, "attempts": trace, "elapsed_ms": round((time.perf_counter() - start) * 1000, 3)}
    raise IntakeError("planning_failed", f"No safe recipe after two proposals: {error}")


def manual_recipe(source: dict, columns: dict) -> dict:
    if not isinstance(columns, dict) or set(columns) != set(FIELDS):
        raise IntakeError("mapping_fields", "Choose a source column for each target.")
    recipe = {
        "version": 1, "contract_id": CONTRACT["id"], "header_sha256": source["header_sha256"],
        "mapping": {field: {"source": columns[field], "operation": OPS[field], "evidence": "Source column chosen in the local review interface."} for field in FIELDS},
    }
    validate_recipe(recipe, source)
    return recipe
