"""Integration test driver: poll the runtime and its independent Memory port.

Production app.jobs.worker polls these two entry points in the same order.
This helper supplies a finite drain for deterministic assertions, not scheduling.
"""
from app.pipeline.runner import run_pending_jobs as run_memory_jobs
from app.runtime import run_pending_work


async def run_pending_jobs(limit=100, retry_delay_seconds=30, worker_id=None):
    processed = 0
    while processed < limit:
        n = await run_pending_work(limit=1, retry_delay_seconds=retry_delay_seconds)
        if n == 0:
            n = await run_memory_jobs(limit=1, retry_delay_seconds=retry_delay_seconds,
                                      worker_id=worker_id)
        if n == 0:
            break
        processed += n
    return processed


async def seed_legacy_extract(note_id):
    """Explicit setup for legacy queue lease regressions only.

    Upload no longer creates extract jobs. Queue tests must insert the historical
    row they claim/reclaim themselves; this does not change API or test fixtures.
    """
    from app.db import get_sessionmaker
    from app.jobs.queue import enqueue
    from app.models import Note

    async with get_sessionmaker()() as session, session.begin():
        note = await session.get(Note, note_id)
        return await enqueue(session, "extract", {"note_id": note_id, "rev": note.rev},
                             note_id=note_id, note_rev=note.rev)
