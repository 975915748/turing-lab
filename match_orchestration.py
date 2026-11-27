"""Single-process executor for SQLite-backed round workflows.

The database remains the source of truth. This module only owns the worker
lifecycle; claim, fencing, recovery and business transitions stay in app.py.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any


class PersistentWorkflowWorker:
    """Poll durable workflow records and execute one claimed workflow at a time."""

    def __init__(
        self,
        claim_next: Callable[[], dict[str, Any] | None],
        process: Callable[[dict[str, Any]], Awaitable[None]],
        on_error: Callable[[dict[str, Any], BaseException], Awaitable[None]],
        *,
        poll_interval: float = 0.5,
    ) -> None:
        self._claim_next = claim_next
        self._process = process
        self._on_error = on_error
        self._poll_interval = max(0.05, float(poll_interval))
        self._task: asyncio.Task | None = None
        self._wake_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_users = 0
        self._lock = threading.Lock()
        self._stopping = False

    @property
    def running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._task is not None and not self._task.done():
                # Starlette TestClient can mount the same app in nested lifespan
                # contexts. Share one worker instead of binding a second worker
                # or its asyncio.Event to another event loop.
                self._lifecycle_users += 1
                return
            self._stopping = False
            self._loop = loop
            self._wake_event = asyncio.Event()
            self._lifecycle_users = 1
            self._task = loop.create_task(self._run(), name="round-workflow-worker")

    def wake(self) -> None:
        with self._lock:
            loop = self._loop
            event = self._wake_event
        if loop is not None and event is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._lifecycle_users > 0:
                self._lifecycle_users -= 1
            if self._lifecycle_users > 0:
                return
            owner_loop = self._loop
            task = self._task
            if owner_loop is None or task is None:
                self._task = None
                self._wake_event = None
                self._loop = None
                return
            self._stopping = True
            event = self._wake_event
            if event is not None and owner_loop.is_running():
                owner_loop.call_soon_threadsafe(event.set)

        if owner_loop is loop:
            await self._cancel_on_owner_loop(task)
            return
        if owner_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._cancel_on_owner_loop(task), owner_loop
            )
            await asyncio.wrap_future(future)
        else:
            # The owning TestClient loop has already closed. Its task cannot
            # still be executing; discard the stale references before restart.
            with self._lock:
                self._task = None
                self._wake_event = None
                self._loop = None

    async def _cancel_on_owner_loop(self, task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        with self._lock:
            if self._task is task:
                self._task = None
                self._wake_event = None
                self._loop = None

    async def _run(self) -> None:
        while not self._stopping:
            workflow = self._claim_next()
            if workflow is not None:
                try:
                    await self._process(workflow)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._on_error(workflow, exc)
                continue

            event = self._wake_event
            if event is None:
                await asyncio.sleep(self._poll_interval)
                continue
            try:
                await asyncio.wait_for(event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass
            finally:
                event.clear()
