"""In-memory background job manager with cancel support."""

from __future__ import annotations

import asyncio
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    kind: str
    status: str = "pending"   # pending | running | cancelling | completed | failed
    progress: float = 0.0
    stage: str = ""
    logs: list[dict] = field(default_factory=list)
    result: dict[str, Any] | None = None
    partial: dict[str, Any] | None = None   # live/incremental results during a run
    error: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def log(self, message: str, progress: float | None = None, stage: str | None = None):
        if progress is not None:
            self.progress = round(progress, 4)
        if stage is not None:
            self.stage = stage
        self.logs.append({"ts": _now(), "message": message, "progress": self.progress})
        self.updated_at = _now()

    def cancel(self):
        if self.status == "running":
            self.cancel_event.set()
            self.status = "cancelling"
            self.log("Stop requested — finishing in-flight documents then building partial graph…",
                     stage="cancelling")
            self.updated_at = _now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "logs": self.logs,
            "result": self.result,
            "partial": self.partial,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobManager:
    """In-memory jobs with write-through persistence to the GraphStore.

    Memory stays the source of truth for the running replica; the store copy
    survives restarts and lets other replicas answer status polls
    (migration step 5). Persistence failures never break a job.
    """

    PERSIST_INTERVAL = 2.0   # seconds between throttled log persists

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._last_persist: dict[str, float] = {}

    def create(self, kind: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def status_dict(self, job_id: str) -> dict | None:
        """Job status for polling — memory first, persisted snapshot as fallback."""
        job = self._jobs.get(job_id)
        if job:
            return job.to_dict()
        try:
            from app.services.graph_store import get_graph_store
            return get_graph_store().get_job(job_id)
        except Exception:
            return None

    def _persist(self, job: Job, force: bool = False) -> None:
        import time as _time
        now = _time.monotonic()
        if not force and now - self._last_persist.get(job.id, 0.0) < self.PERSIST_INTERVAL:
            return
        self._last_persist[job.id] = now
        try:
            from app.services.graph_store import get_graph_store
            get_graph_store().save_job(self._serializable(job.to_dict()))
        except Exception:
            pass  # durability is best-effort — never break the job itself

    @staticmethod
    def _serializable(d: dict) -> dict:
        """Drop non-JSON values — LangGraph results carry emit callables and
        asyncio Events that would make the whole persist fail."""
        import json as _json

        def ok(v) -> bool:
            try:
                _json.dumps(v)
                return True
            except (TypeError, ValueError):
                return False

        if isinstance(d.get("result"), dict):
            d = {**d, "result": {k: v for k, v in d["result"].items() if ok(v)}}
        if isinstance(d.get("partial"), dict):
            d = {**d, "partial": {k: v for k, v in d["partial"].items() if ok(v)}}
        return d if ok(d) else {k: v for k, v in d.items() if ok(v)}

    def run(self, job: Job, coro_factory: Callable[[Callable, asyncio.Event], Awaitable[dict]]):
        def emit(message: str, progress: float | None = None, stage: str | None = None):
            job.log(message, progress=progress, stage=stage)
            self._persist(job)

        def set_partial(data: dict) -> None:
            # Publish incremental results so /status polls stream them live and
            # /download can produce a mid-run partial file.
            job.partial = data
            job.updated_at = _now()
            self._persist(job)

        # Factories that want live results call emit.set_partial(...); the rest
        # ignore it (function attribute keeps the factory signature unchanged).
        emit.set_partial = set_partial

        async def _runner():
            job.status = "running"
            job.log("Job started.", progress=0.0, stage="start")
            self._persist(job, force=True)
            try:
                result = await coro_factory(emit, job.cancel_event)
                job.result = result
                job.status = "completed"
                job.log("Job completed.", progress=1.0, stage="done")
            except Exception as exc:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.log(f"Job failed: {job.error}", stage="error")
                traceback.print_exc()
            finally:
                self._persist(job, force=True)

        asyncio.create_task(_runner())


job_manager = JobManager()
