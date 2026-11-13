from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from app.core.models import Job

from .cache import ArtifactCache
from .defaults import build_default_adapter_registry, build_default_model_registry
from .executor import FlowExecutor
from .io import load_flow, load_model_definition
from .planner import FlowPlanner
from .registry import ModelRegistry
from .validation import FlowValidator


def _bindings(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        name, sep, raw_path = value.partition("=")
        if not sep or not name or not raw_path:
            raise ValueError(f"Invalid --input '{value}', expected NAME=PATH")
        result[name] = Path(raw_path)
    return result


def _add_external_models(registry: ModelRegistry, directories: list[Path]) -> None:
    for directory in directories:
        for path in sorted(directory.glob("*.yaml")):
            registry.register(load_model_definition(path))


def _validate(flow_path: Path, model_dirs: list[Path]) -> int:
    flow = load_flow(flow_path)
    models = build_default_model_registry()
    _add_external_models(models, model_dirs)
    adapters = build_default_adapter_registry()
    result = FlowValidator(models).validate(flow)
    for issue in result.issues:
        print(f"{issue.level.upper()}: {issue.code}: {issue.message}")
    if not result.valid:
        return 1

    plan = FlowPlanner().plan(flow)
    print("Flow is valid")
    for stage in plan.stages:
        print(f"stage {stage.index}: {', '.join(stage.nodes)}")
    for node_id, node in flow.nodes.items():
        model = models.get(node.model)
        adapter = adapters.get(model.adapter)
        available, reason = adapter.available()
        status = "available" if available else f"unavailable: {reason}"
        print(f"{node_id}: {model.id} [{adapter.id}] - {status}")
    return 0


def _run(args: argparse.Namespace) -> int:
    flow = load_flow(args.flow)
    models = build_default_model_registry()
    _add_external_models(models, args.model_dir)
    adapters = build_default_adapter_registry()
    executor = FlowExecutor(
        model_registry=models,
        adapter_registry=adapters,
        cache=ArtifactCache(args.cache_dir),
        runs_root=args.runs_dir,
        progress_listener=lambda node, fraction, message: print(
            f"[{node}] {fraction if fraction is not None else '-'} {message}"
        ),
    )
    job = Job(id=uuid.uuid4().hex[:12])
    result = executor.execute(flow, _bindings(args.input), context={"job": job})
    print(json.dumps({name: str(a.path) for name, a in result.outputs.items()}, indent=2))
    print(f"run manifest: {result.run_dir / 'run.json'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Experimental StemDeck audio flow engine")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a flow and show its execution plan")
    validate.add_argument("flow", type=Path)
    validate.add_argument("--model-dir", action="append", type=Path, default=[])

    run = sub.add_parser("run", help="execute a flow")
    run.add_argument("flow", type=Path)
    run.add_argument("--input", action="append", default=[], help="flow binding NAME=PATH")
    run.add_argument("--model-dir", action="append", type=Path, default=[])
    run.add_argument("--runs-dir", type=Path, default=Path("flow-runs"))
    run.add_argument("--cache-dir", type=Path, default=Path("flow-cache"))

    args = parser.parse_args()
    if args.command == "validate":
        return _validate(args.flow, args.model_dir)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
