"""Task queue abstraction.

The MVP uses an in-process asyncio queue (`InProcessAsyncQueue`); the interface stays small so a
distributed backend such as Celery / RQ / Arq can be added later without touching callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class JobQueue(ABC):
    @abstractmethod
    async def enqueue(self, job_id: str) -> None:
        """Put an already persisted Job on the queue to await execution."""

    async def start(self) -> None:  # noqa: B027 - optional lifecycle hook
        """Start the background workers (if any)."""

    async def stop(self) -> None:  # noqa: B027
        """Stop the workers gracefully."""
