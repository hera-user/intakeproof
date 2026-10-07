from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .engine import IntakeError, execute, import_csv, json_bytes, make_bundle, report_html
from .planner import plan


def provider_options(command):
    command.add_argument("--planner", choices=("local", "openai"), default="local")
    command.add_argument("--executor", choices=("local", "agent37"), default="local")
    command.add_argument("--model", help="Explicit OpenAI API model; alternatively INTAKEPROOF_OPENAI_MODEL")
    command.add_argument("--instance-id", help="Existing authorized Agent37 instance; no instance is provisioned")
    command.add_argument("--free-access-verified", action="store_true", help="Only after verifying free credits, no card and no automatic charges")


def browser_runtime(args):
    from .server import BrowserRuntime
    planner_factory, executor_factory = None, None
    if args.planner == "openai":
        from .providers import OpenAIPlanner
        key = os.environ.get("INTAKEPROOF_OPENAI_API_KEY", "")
        model = args.model or os.environ.get("INTAKEPROOF_OPENAI_MODEL", "")
        def planner_factory():
            return OpenAIPlanner(key, model, free_access_verified=args.free_access_verified)
        planner_factory()  # Validate configuration without any HTTP request.
    if args.executor == "agent37":
        from .providers import Agent37Executor
        agent_key = os.environ.get("INTAKEPROOF_AGENT37_API_KEY", "")
        instance = args.instance_id or os.environ.get("INTAKEPROOF_AGENT37_INSTANCE_ID", "")
        def executor_factory():
            return Agent37Executor(agent_key, instance, free_access_verified=args.free_access_verified)
        executor_factory()
    return BrowserRuntime(planner_factory=planner_factory, executor_factory=executor_factory, max_jobs=args.max_provider_jobs)


def main():
    parser = argparse.ArgumentParser(description="IntakeProof — reviewed supplier imports with row evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="Check an evidence ZIP offline by replaying its recorded decisions")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--expected-source-sha256", help="Hash of a separately retained original file")
    web = sub.add_parser("serve", help="Start the local browser review interface")
    web.add_argument("--port", type=int, default=8765)
    provider_options(web)
    web.add_argument("--max-provider-jobs", type=int, choices=(1, 2, 3), default=3, help="Process-wide limit per provider; failed attempts count")
    for name in ("demo", "run"):
        command = sub.add_parser(name)
        if name == "run":
            command.add_argument("--input", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True, help="New or empty output directory")
        command.add_argument("--recipe", type=Path)
        command.add_argument("--decisions", type=Path)
        command.add_argument("--approve-mapping", action="store_true", help="Record your review of the selected mapping")
        command.add_argument("--delimiter", choices=("auto", "comma", "semicolon", "tab"), default="auto")
        provider_options(command)
        command.add_argument("--allow-external-data", action="store_true", help="Explicitly permit this input file to be sent to the selected live providers")
    args = parser.parse_args()
    try:
        if args.command == "verify":
            from .verify import MAX_BUNDLE_BYTES, verify_bundle
            with args.bundle.open("rb") as stream:
                bundle = stream.read(MAX_BUNDLE_BYTES + 1)
            print(json.dumps(verify_bundle(bundle, expected_source_sha256=args.expected_source_sha256), indent=2))
            return
        live_requested = args.planner == "openai" or args.executor == "agent37"
        if live_requested and not args.free_access_verified:
            raise IntakeError("free_access_unverified", "Verify free service access before enabling live mode. No network call was made.")
        if args.command == "serve":
            from .server import serve
            serve(args.port, runtime=browser_runtime(args))
            return
        if live_requested and args.command != "demo" and not args.allow_external_data:
            raise IntakeError("external_data", "Live mode sends source data to the selected providers. Explicitly permit this input file with --allow-external-data.")
        if args.recipe and args.planner != "local":
            raise IntakeError("planner_conflict", "Choose either a saved recipe or a live planner.")
        if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
            raise IntakeError("output_exists", "Use a new or empty output directory; existing evidence is never overwritten.")
        source_path = args.input if args.command == "run" else Path(__file__).resolve().parents[1] / "examples/supplier-drift.csv"
        raw = source_path.read_bytes()
        delimiter = {"comma": ",", "semicolon": ";", "tab": "\t"}.get(args.delimiter, "auto")
        if args.recipe:
            recipe = json.loads(args.recipe.read_text("utf-8"))
            receipt = {"mode": "saved_recipe", "live_model_call": False}
        else:
            provider = None
            if args.planner == "openai":
                from .providers import OpenAIPlanner
                provider = OpenAIPlanner(os.environ.get("INTAKEPROOF_OPENAI_API_KEY", ""), args.model or os.environ.get("INTAKEPROOF_OPENAI_MODEL", ""), free_access_verified=args.free_access_verified)
            recipe, receipt = plan(raw, provider=provider, delimiter=delimiter)
        decisions = json.loads(args.decisions.read_text("utf-8")) if args.decisions else None
        if args.executor == "agent37":
            from .providers import Agent37Executor
            executor = Agent37Executor(os.environ.get("INTAKEPROOF_AGENT37_API_KEY", ""), args.instance_id or os.environ.get("INTAKEPROOF_AGENT37_INSTANCE_ID", ""), free_access_verified=args.free_access_verified)
            result = executor.run(raw, recipe, delimiter=delimiter, decisions=decisions, mapping_approved=args.approve_mapping, planner_receipt=receipt)
        else:
            result = execute(raw, recipe, delimiter=delimiter, decisions=decisions, mapping_approved=args.approve_mapping, planner_receipt=receipt)
        result["demonstration"] = {"synthetic": args.command == "demo"}
        args.out.mkdir(parents=True, exist_ok=True)
        filename = "reviewed_import.csv" if result["mapping_approved"] else "candidate_import.csv"
        for name, content in {"original.csv": raw, filename: import_csv(result), "audit.json": json_bytes(result), "recipe.json": json_bytes(recipe), "review.json": json_bytes([r for r in result["records"] if r["disposition"] == "review"]), "report.html": report_html(result), "evidence.zip": make_bundle(raw, result)}.items():
            (args.out / name).write_bytes(content)
        print(json.dumps({"run_id": result["run_id"], "out": str(args.out.resolve()), "summary": result["summary"], "planner": result["planner"]["mode"], "executor": result["executor"]["mode"], "mapping_approved": result["mapping_approved"]}, indent=2))
    except (IntakeError, OSError, ValueError) as exc:
        parser.exit(1, f"IntakeProof: {exc}\n")


if __name__ == "__main__":
    main()
