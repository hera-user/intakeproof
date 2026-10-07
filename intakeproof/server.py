"""Loopback-only review UI. No input files leave this process in local mode."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .engine import CONTRACT, MAX_BYTES, IntakeError, execute, import_csv, json_bytes, make_bundle, parse_source, report_html
from .planner import manual_recipe, plan

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class AppState:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.sessions = {}
        self.runs = {}
        self.lock = threading.RLock()


def make_server(port: int = 8765) -> ThreadingHTTPServer:
    state = AppState()

    class Handler(BaseHTTPRequestHandler):
        server_version = "IntakeProof/0.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            # Do not put source records, request bodies or credentials in access logs.
            pass

        def valid_host(self):
            return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def send(self, status, content, mime="application/json; charset=utf-8", filename=None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def error(self, status, code, message):
            self.send(status, json_bytes({"error": {"code": code, "message": message}, "conservation_evaluated": False}))

        def do_GET(self):
            if not self.valid_host():
                return self.error(403, "host", "Only this loopback host is permitted.")
            path = urlparse(self.path).path
            if path == "/":
                page = (WEB / "index.html").read_text("utf-8").replace("__CSRF_TOKEN__", state.token)
                return self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if path in ("/app.js", "/style.css"):
                mime = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                return self.send(200, (WEB / path[1:]).read_bytes(), mime)
            if path == "/api/info":
                return self.send(200, json_bytes({"version": "0.1.0", "contract": CONTRACT, "planner": "local_rules", "live_model_call": False, "executor": "local", "agent37_verified": False, "network_transmission": False}))
            if path == "/api/demo":
                return self.send(200, json_bytes({"filename": "supplier-drift.synthetic.csv", "synthetic": True, "source_base64": base64.b64encode((ROOT / "examples/supplier-drift.csv").read_bytes()).decode("ascii")}))
            if path.startswith("/api/download/"):
                parts = path.split("/")
                if len(parts) != 5:
                    return self.error(404, "not_found", "Artifact not found.")
                with state.lock:
                    saved = state.runs.get(parts[3])
                if not saved:
                    return self.error(404, "expired", "This result is not available in the current local session.")
                raw, result = saved
                if parts[4] == "bundle.zip":
                    return self.send(200, make_bundle(raw, result), "application/zip", "intakeproof-evidence.zip")
                if parts[4] == "import.csv":
                    name = "reviewed_import.csv" if result["mapping_approved"] else "candidate_import.csv"
                    return self.send(200, import_csv(result), "text/csv; charset=utf-8", name)
                if parts[4] == "audit.json":
                    return self.send(200, json_bytes(result), filename="audit.json")
                if parts[4] == "report.html":
                    return self.send(200, report_html(result), "text/html; charset=utf-8", "report.html")
            return self.error(404, "not_found", "Route not found.")

        def do_POST(self):
            origins = (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BYTES * 2:
                    raise IntakeError("request_size", "Request body is missing or too large.")
                # Consume the bounded body before replying: closing a Windows socket
                # with unread incoming bytes can reset it and hide the rejection.
                incoming = self.rfile.read(length)
                if len(incoming) != length:
                    raise IntakeError("request_size", "The request body ended early.")
                if not self.valid_host() or self.headers.get("Origin") not in origins or not secrets.compare_digest(self.headers.get("X-IntakeProof-Token", ""), state.token):
                    return self.error(403, "origin", "Use the local IntakeProof review page for this action.")
                if self.headers.get_content_type() != "application/json":
                    raise IntakeError("content_type", "Expected JSON.")
                body = json.loads(incoming)
                if not isinstance(body, dict):
                    raise IntakeError("request_shape", "Expected a JSON object.")
                path = urlparse(self.path).path
                if path == "/api/inspect":
                    encoded = body.get("source_base64")
                    if not isinstance(encoded, str):
                        raise IntakeError("source", "Choose a CSV source.")
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise IntakeError("source_encoding", "Invalid file transfer encoding.") from exc
                    delimiter = body.get("delimiter", "auto")
                    source = parse_source(raw, delimiter)
                    filename = body.get("filename", "source.csv")
                    if not isinstance(filename, str) or len(filename) > 255:
                        raise IntakeError("filename", "Invalid display filename.")
                    recipe, receipt, planning_error, preview = None, None, None, None
                    try:
                        recipe, receipt = plan(raw, delimiter=source["delimiter"])
                        preview = execute(raw, recipe, delimiter=source["delimiter"], planner_receipt=receipt)
                    except IntakeError as exc:
                        planning_error = {"code": exc.code, "message": str(exc)}
                    session_id = str(uuid.uuid4())
                    session = {"raw": raw, "source": source, "recipe": recipe, "receipt": receipt, "last_run_id": None, "run_ids": [], "synthetic": body.get("synthetic") is True}
                    with state.lock:
                        if len(state.sessions) >= 4:
                            oldest = next(iter(state.sessions))
                            for rid in state.sessions.pop(oldest)["run_ids"]:
                                state.runs.pop(rid, None)
                        state.sessions[session_id] = session
                    return self.send(200, json_bytes({"session_id": session_id, "filename": filename, "synthetic": session["synthetic"], "headers": source["headers"], "source_sha256": source["source_sha256"], "source_bytes": source["source_bytes"], "input_records": len(source["records"]), "delimiter": source["delimiter"], "sample": source["records"][:5], "recipe": recipe, "planner": receipt, "planning_error": planning_error, "preview": preview}))
                if path == "/api/run":
                    session_id = body.get("session_id")
                    if not isinstance(session_id, str):
                        raise IntakeError("session", "Inspect a source file first.")
                    with state.lock:
                        session = state.sessions.get(session_id)
                        if not session:
                            raise IntakeError("session", "This source session expired. Load the file again.")
                        if len(session["run_ids"]) >= 12:
                            raise IntakeError("run_limit", "Export the evidence, then reload the source to start a new review session.")
                        columns = body.get("columns")
                        recipe = manual_recipe(session["source"], columns)
                        receipt = session["receipt"] or {"mode": "manual", "live_model_call": False}
                        if session["recipe"] and columns == {f: r["source"] for f, r in session["recipe"]["mapping"].items()}:
                            recipe = session["recipe"]
                        else:
                            receipt = {**receipt, "mapping_changed_by_reviewer": True}
                        result = execute(session["raw"], recipe, delimiter=session["source"]["delimiter"], decisions=body.get("decisions"), mapping_approved=body.get("mapping_approved") is True, planner_receipt=receipt, parent_run_id=session["last_run_id"])
                        result["demonstration"] = {"synthetic": session["synthetic"], "label_source": "User selection or bundled original synthetic fixture"}
                        session["last_run_id"] = result["run_id"]
                        session["run_ids"].append(result["run_id"])
                        state.runs[result["run_id"]] = (session["raw"], result)
                    return self.send(200, json_bytes(result))
                return self.error(404, "not_found", "Route not found.")
            except IntakeError as exc:
                return self.error(400, exc.code, str(exc))
            except (ValueError, TypeError, UnicodeDecodeError):
                return self.error(400, "invalid_request", "The request has invalid values or encoding.")
            except TimeoutError:
                return self.error(408, "request_timeout", "The request body did not arrive in time.")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.app_state = state
    return server


def serve(port=8765):
    server = make_server(port)
    print(f"IntakeProof is running at http://127.0.0.1:{server.server_port}", flush=True)
    print("Local rules and local execution. No live sponsor integration is claimed. Ctrl+C stops the server.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
