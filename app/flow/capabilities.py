from __future__ import annotations

from dataclasses import dataclass, field
import platform
from typing import Any

from .adapters import AdapterRegistry
from .diagnostics import Diagnostic, DiagnosticSeverity, DiagnosticSource
from .registry import ModelRegistry
from .schema import FlowDefinition, NodeDefinition


class CapabilityIssue(Diagnostic):
    """Capability diagnostic with the legacy constructor kept for PoC callers."""

    def __init__(
        self,
        level: str,
        node: str | None,
        message: str,
        code: str = "capability_error",
    ) -> None:
        super().__init__(
            source=DiagnosticSource.CAPABILITY,
            severity=DiagnosticSeverity(level),
            code=code,
            message=message,
            node_id=node,
        )


@dataclass
class DefinitionCapabilityReport:
    issues: list[CapabilityIssue] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)


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

    def check_definition(
        self,
        definition: NodeDefinition,
        *,
        node_id: str | None = None,
    ) -> DefinitionCapabilityReport:
        report = DefinitionCapabilityReport()
        environment = self.detect_environment()

        if definition.adapter not in self.adapters:
            report.issues.append(
                CapabilityIssue(
                    "error",
                    node_id,
                    f"Adapter '{definition.adapter}' is not registered",
                    code="adapter_not_registered",
                )
            )
            return report

        adapter = self.adapters.get(definition.adapter)
        available, reason = adapter.available()
        if not available:
            report.issues.append(
                CapabilityIssue(
                    "error",
                    node_id,
                    reason or "Adapter unavailable",
                    code="adapter_unavailable",
                )
            )
            return report

        for issue in adapter.preflight(definition):
            report.issues.append(
                CapabilityIssue(
                    issue.level,
                    node_id,
                    issue.message,
                    code="adapter_preflight",
                )
            )

        required_os = definition.requirements.get("os")
        if required_os and environment["os"] not in required_os:
            report.issues.append(
                CapabilityIssue(
                    "error",
                    node_id,
                    f"Node supports OS {required_os}, current OS is {environment['os']}",
                    code="unsupported_os",
                )
            )

        required_arch = definition.requirements.get("arch")
        if required_arch and environment["arch"] not in required_arch:
            report.issues.append(
                CapabilityIssue(
                    "error",
                    node_id,
                    "Node supports architectures "
                    f"{required_arch}, current architecture is {environment['arch']}",
                    code="unsupported_architecture",
                )
            )
        return report

    def check(self, flow: FlowDefinition) -> CapabilityReport:
        report = CapabilityReport()
        for node_id, node in flow.nodes.items():
            if node.model not in self.models:
                continue
            definition_report = self.check_definition(
                self.models.get(node.model),
                node_id=node_id,
            )
            report.issues.extend(definition_report.issues)
        return report

    @staticmethod
    def detect_environment() -> dict[str, Any]:
        return {
            "os": platform.system().lower(),
            "arch": platform.machine().lower(),
        }
