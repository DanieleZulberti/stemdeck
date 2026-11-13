from __future__ import annotations

from dataclasses import dataclass

from .schema import FlowDefinition


@dataclass(frozen=True)
class ExecutionStage:
    index: int
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionPlan:
    stages: tuple[ExecutionStage, ...]


class FlowPlanner:
    def plan(self, flow: FlowDefinition) -> ExecutionPlan:
        deps: dict[str, set[str]] = {name: set() for name in flow.nodes}
        for node_name, node in flow.nodes.items():
            for source in node.inputs.values():
                owner = source.split(".", 1)[0]
                if owner in flow.nodes:
                    deps[node_name].add(owner)

        completed: set[str] = set()
        remaining = set(flow.nodes)
        stages: list[ExecutionStage] = []
        while remaining:
            ready = sorted(name for name in remaining if deps[name] <= completed)
            if not ready:
                raise ValueError("Cannot build execution plan: cyclic or unresolved dependencies")
            stages.append(ExecutionStage(index=len(stages), nodes=tuple(ready)))
            completed.update(ready)
            remaining.difference_update(ready)
        return ExecutionPlan(stages=tuple(stages))
