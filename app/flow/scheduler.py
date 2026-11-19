from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ResourceClaim:
    """A concurrency claim for one execution node.

    Claims intentionally describe runtime contention rather than graph semantics.
    Nodes in the same DAG stage may execute concurrently while a shared claim
    (for example the legacy Demucs worker) remains serialized.
    """

    key: str = "default"
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("ResourceClaim.max_concurrency must be >= 1")


class ResourceScheduler:
    """Small in-process resource-aware scheduler for one Flow Engine process."""

    def __init__(self, max_workers: int = 4) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.max_workers = max_workers
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="flow-node")
        self._lock = Lock()
        self._limits: dict[str, int] = {}
        self._semaphores: dict[str, BoundedSemaphore] = {}

    def _semaphore(self, claim: ResourceClaim) -> BoundedSemaphore:
        with self._lock:
            existing_limit = self._limits.get(claim.key)
            if existing_limit is not None and existing_limit != claim.max_concurrency:
                raise ValueError(
                    f"Conflicting concurrency limits for resource '{claim.key}': "
                    f"{existing_limit} vs {claim.max_concurrency}"
                )
            if claim.key not in self._semaphores:
                self._limits[claim.key] = claim.max_concurrency
                self._semaphores[claim.key] = BoundedSemaphore(claim.max_concurrency)
            return self._semaphores[claim.key]

    def submit(self, claim: ResourceClaim, fn: Callable[[], T]) -> Future[T]:
        semaphore = self._semaphore(claim)

        def guarded() -> T:
            with semaphore:
                return fn()

        return self._pool.submit(guarded)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)

    def __enter__(self) -> "ResourceScheduler":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
