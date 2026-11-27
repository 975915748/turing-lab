"""Single-process executor for SQLite-backed round workflows.

The database remains the source of truth. This module only owns the worker
lifecycle; claim, fencing, recovery and business transitions stay in app.py.
"""
from __future__ import annotations

import asyncio
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
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        self._wake_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="round-workflow-worker")

    def wake(self) -> None:
        event = self._wake_event
        if event is not None:
            event.set()

    async def stop(self) -> None:
        self._stopping = True
        self.wake()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._wake_event = None

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
