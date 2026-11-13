from __future__ import annotations

from dataclasses import dataclass, field
import platform
from typing import Any

from .adapters import AdapterRegistry
from .registry import ModelRegistry
from .schema import FlowDefinition


@dataclass(frozen=True)
class CapabilityIssue:
    level: str
    node: str
    message: str


@dataclass
class CapabilityReport:
    issues: list[CapabilityIssue] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)


class CapabilityChecker:
    def __init__(self, models: ModelRegistry, adapters: AdapterRegistry) -> None:
        self.models = models
        self.adapters = adapters

    def check(self, flow: FlowDefinition) -> CapabilityReport:
        report = CapabilityReport()
        environment = self.detect_environment()
        for node_id, node in flow.nodes.items():
            if node.model not in self.models:
                continue
            model = self.models.get(node.model)
            if model.adapter not in self.adapters:
                report.issues.append(
                    CapabilityIssue(
                        "error",
                        node_id,
                        f"Adapter '{model.adapter}' is not registered",
                    )
                )
                continue
            adapter = self.adapters.get(model.adapter)
            available, reason = adapter.available()
            if not available:
                report.issues.append(
                    CapabilityIssue("error", node_id, reason or "Adapter unavailable")
                )
                continue
            for issue in adapter.preflight(model):
                report.issues.append(
                    CapabilityIssue(issue.level, node_id, issue.message)
                )
            required_os = model.requirements.get("os")
            if required_os and environment["os"] not in required_os:
                report.issues.append(
                    CapabilityIssue(
                        "error",
                        node_id,
                        f"Model supports OS {required_os}, current OS is {environment['os']}",
                    )
                )
            required_arch = model.requirements.get("arch")
            if required_arch and environment["arch"] not in required_arch:
                report.issues.append(
                    CapabilityIssue(
                        "error",
                        node_id,
                        "Model supports architectures "
                        f"{required_arch}, current architecture is {environment['arch']}",
                    )
                )
        return report

    @staticmethod
    def detect_environment() -> dict[str, Any]:
        return {
            "os": platform.system().lower(),
            "arch": platform.machine().lower(),
        }
