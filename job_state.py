"""Upstash Redis-backed job status, replacing job.json plus the in-memory `_active_jobs`
set that the old threading-based backend relied on.

Everything else that used to be a local JSON file (app_state.json, google_state.json)
still is one: it round-trips through Vercel Blob the same way manifest.csv does, because
nothing but this one piece of state is written by multiple concurrent invocations. The
1-click automation job *is* written concurrently -- one call per article, via QStash --
so its "how many articles are done" counter and "has finalize already run" flag need a
real atomic store. Redis is shared across every serverless instance, so job status also
survives a redeploy or a request landing on a different cold start, unlike the old
in-memory set.

Requires UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN (auto-injected by Vercel's
Upstash integration).
"""
from __future__ import annotations

import json
import time

from upstash_redis import Redis

_client: Redis | None = None


def _redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_env()
    return _client


def _get_json(key: str) -> dict | None:
    raw = _redis().get(key)
    return json.loads(raw) if raw else None


def _set_json(key: str, value: dict) -> None:
    _redis().set(key, json.dumps(value))


# --- 1-click automation job status (replaces job.json + the in-memory _active_jobs set) ---
#
# "completed" is kept as its own atomic counter (not a field inside the JSON job blob)
# because multiple per-article worker calls increment it concurrently; a single
# read-modify-write JSON blob would lose updates under that concurrency.

def start_job(issue_key: str, total: int) -> None:
    _set_json(f"issue:{issue_key}:job", {"status": "running", "started": time.time(), "total": total})
    _redis().set(f"issue:{issue_key}:job_completed", 0)


def get_job(issue_key: str) -> dict | None:
    job = _get_json(f"issue:{issue_key}:job")
    if job is None:
        return None
    completed = _redis().get(f"issue:{issue_key}:job_completed")
    job["completed"] = int(completed) if completed is not None else 0
    return job


def mark_job_article_done(issue_key: str) -> int:
    """Atomically bump the completed count and return the new value."""
    return _redis().incr(f"issue:{issue_key}:job_completed")


def mark_job_failed(issue_key: str, error: str) -> None:
    job = get_job(issue_key) or {}
    job.update(status="error", error=error)
    _set_json(f"issue:{issue_key}:job", job)


def mark_job_done(issue_key: str, result: dict) -> None:
    job = get_job(issue_key) or {}
    job.update(status="done", result=result)
    _set_json(f"issue:{issue_key}:job", job)


def try_acquire_finalize_lock(issue_key: str) -> bool:
    """True for exactly one caller per job: guards the tail step (Excel + Google publish)
    against running twice when two article workers finish at nearly the same instant."""
    return bool(_redis().set(f"issue:{issue_key}:job_finalized", "1", nx=True))
