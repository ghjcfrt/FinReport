from __future__ import annotations

from collections.abc import Callable
from typing import Any


class WorkflowRuntime:
    """Shared application workflow entrypoint used by API and Worker composition roots.

    Adapters are supplied as callables; this package does not import FastAPI, Celery,
    SQLAlchemy, or provider SDKs.
    """

    def __init__(self, extract: Callable[[int], None], analyze: Callable[[int], None]):
        self._extract = extract
        self._analyze = analyze

    def extract(self, job_id: int) -> None:
        self._extract(job_id)

    def analyze(self, run_id: int) -> None:
        self._analyze(run_id)

    def dispatch_extract(self, job_id: int, background: Any) -> None:
        background.add_task(self.extract, job_id)

    def dispatch_analyze(self, run_id: int, background: Any) -> None:
        background.add_task(self.analyze, run_id)
