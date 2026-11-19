from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DiagnosticSource(str, Enum):
    COMPILER = "compiler"
    VALIDATOR = "validator"
    CAPABILITY = "capability"
    EXECUTOR = "executor"


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Diagnostic:
    source: DiagnosticSource
    severity: DiagnosticSeverity
    code: str
    message: str
    node_id: str | None = None
    port_id: str | None = None
    output_id: str | None = None
    parameter_id: str | None = None

    @property
    def level(self) -> str:
        """Compatibility alias for the PoC issue models."""
        return self.severity.value

    @property
    def node(self) -> str | None:
        """Compatibility alias for the PoC capability issue model."""
        return self.node_id

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source": self.source.value,
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "node_id": self.node_id,
            "port_id": self.port_id,
            "output_id": self.output_id,
            "parameter_id": self.parameter_id,
        }
