from __future__ import annotations

from .project_repository import ProjectFlowNotFoundError, ProjectNotFoundError
from .project_service import ApplyResult, ProjectService


_FLOW_LIBRARY_SCOPE_ID = "__flow_library__"
_FLOW_LIBRARY_SCOPE_NAME = "Flow Library"


class FlowStudioService:
    """Studio binding facade for ordinary reusable Flows.

    Projects were introduced as an ownership layer around Studio bindings, but
    the product workflow does not require users to manage that extra concept.
    We keep the existing persistence tables for backwards compatibility and GC
    reference tracking, while exposing a project-free Flow -> Run -> Studio API.
    """

    def __init__(self, project_service: ProjectService | None = None) -> None:
        self.project_service = project_service or ProjectService()

    def _ensure_scope(self) -> str:
        repo = self.project_service.project_repository
        try:
            repo.get_project(_FLOW_LIBRARY_SCOPE_ID)
        except ProjectNotFoundError:
            repo.create_project(_FLOW_LIBRARY_SCOPE_NAME, project_id=_FLOW_LIBRARY_SCOPE_ID)
        return _FLOW_LIBRARY_SCOPE_ID

    def _ensure_flow_attached(self, flow_id: str, revision: int | None = None) -> str:
        scope = self._ensure_scope()
        repo = self.project_service.project_repository
        try:
            repo.get_project_flow(scope, flow_id)
        except ProjectFlowNotFoundError:
            repo.attach_project_flow(
                scope,
                flow_id,
                source_flow_id=flow_id,
                source_flow_revision=revision,
            )
        return scope

    def apply_run(self, run_id: str) -> ApplyResult:
        run = self.project_service.run_repository.get_run(run_id)
        if not run.flow_id:
            # ProjectService will produce the canonical domain error too, but
            # this keeps the hidden compatibility scope from being touched for
            # malformed historic runs.
            return self.project_service.apply_run(self._ensure_scope(), run_id)
        scope = self._ensure_flow_attached(run.flow_id, run.flow_revision)
        return self.project_service.apply_run(scope, run_id)

    def list_bindings(self, flow_id: str | None = None):
        scope = self._ensure_scope()
        bindings = self.project_service.list_bindings(scope)
        if flow_id is None:
            return bindings
        return [binding for binding in bindings if binding.flow_id == flow_id]

    def detach_binding(self, binding_id: str):
        scope = self._ensure_scope()
        return self.project_service.detach_binding(scope, binding_id)
