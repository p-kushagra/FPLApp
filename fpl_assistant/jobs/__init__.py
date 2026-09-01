"""Background job layer. See ADR-002 for why the default runner is threads."""
from __future__ import annotations

from .base import JobRunner, JobState, JobStatus, pending, read_status, reap_stale
from .runner_local import LocalThreadRunner, get_executor, shutdown_executor
from .tasks import REGISTRY

__all__ = [
    "REGISTRY",
    "JobRunner",
    "JobState",
    "JobStatus",
    "LocalThreadRunner",
    "get_executor",
    "install_cache_revalidator",
    "make_runner",
    "pending",
    "read_status",
    "reap_stale",
    "shutdown_executor",
]


def make_runner(db_path, synchronous: bool = False, **kw) -> LocalThreadRunner:
    """Build the default runner wired to the full task catalogue."""
    return LocalThreadRunner(db_path, registry=REGISTRY, synchronous=synchronous, **kw)


def install_cache_revalidator(runner, mapping: dict[str, str] | None = None) -> None:
    """Wire SWR background refresh into the job queue.

    Phase 1 shipped the cache with a pluggable hook and no runner; this is the
    one call that closes the loop. A STALE read now enqueues a real refresh
    instead of just serving the stale value.
    """
    from .. import cache

    mapping = mapping or {
        "fpl_static": "refresh_reference",
        "fpl_fixtures": "refresh_reference",
        "fpl_prices": "snapshot_prices",
        "understat_league": "ingest_understat_league",
    }

    def revalidate(key: str, tier: str) -> None:
        job = mapping.get(tier)
        if job:
            runner.submit(job, priority=3)

    cache.set_revalidator(revalidate)
