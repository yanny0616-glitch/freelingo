"""Long AI generations that outlive a single HTTP request.

Lesson generation can take minutes, while proxies in front of the API cut
requests after about 100 seconds. Jobs run as tasks in the API process; a
request waits briefly for its job and otherwise reports that it is still
running, so the client can ask again. A running job is shared by every
request with the same key, so retries never start duplicate generations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

WAIT_SECONDS = 20.0
KEEP_FINISHED_SECONDS = 600.0

_jobs: dict[Hashable, asyncio.Task[Any]] = {}


def get(key: Hashable) -> asyncio.Task[Any] | None:
    """Return the running or recently finished job for key."""
    return _jobs.get(key)


def start(key: Hashable, factory: Callable[[], Awaitable[Any]]) -> asyncio.Task[Any]:
    """Return the running job for key, or start a new one."""
    task = _jobs.get(key)
    if task is not None and not task.done():
        return task
    task = asyncio.ensure_future(factory())
    _jobs[key] = task

    def finished(done: asyncio.Task[Any]) -> None:
        if not done.cancelled():
            done.exception()  # mark the error as retrieved; callers inspect it

        def forget() -> None:
            if _jobs.get(key) is done:
                del _jobs[key]

        asyncio.get_running_loop().call_later(KEEP_FINISHED_SECONDS, forget)

    task.add_done_callback(finished)
    return task


async def wait(task: asyncio.Task[Any], timeout: float | None = None) -> bool:
    """Wait up to timeout seconds; return whether the job has finished."""
    if timeout is None:
        timeout = WAIT_SECONDS
    if not task.done() and timeout > 0:
        await asyncio.wait({task}, timeout=timeout)
    return task.done()


def clear() -> None:
    """Forget all jobs (for tests)."""
    _jobs.clear()
