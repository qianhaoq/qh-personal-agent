"""Durable scheduling: repair post-commit intake gaps, recover leases, run Tickets.

No task/dict is authoritative. Multiple processes scan the same Store; CAS picks
one executor. Run this from the service worker poll, independently of memory jobs.
"""
from sqlalchemy import String, cast, exists, literal, select

from app.domain.engine import get_engine
from app.models import Note, SaTrigger
from app.sources.base import build_note_uploaded_trigger
from app.workers.organize import OrganizeNoteWorker


async def run_pending_work(limit: int = 100, retry_delay_seconds: int = 30) -> int:
    engine = get_engine()
    await engine.recover()
    # Trigger presence, rather than Ticket presence, respects policy ignore.
    # Every unorganized revision has its own stable dedupe key. The query is
    # bounded so one permanently failing intake cannot exhaust connection pools.
    async with engine.store.maker() as session:
        dedupe = literal("note_uploaded:") + Note.id + literal(":") + cast(Note.rev, String)
        missing = (await session.execute(select(Note.id, Note.user_id, Note.rev).where(
            Note.status == "uploaded", Note.deleted_at.is_(None),
            ~exists().where(SaTrigger.dedupe_key == dedupe),
        ).order_by(Note.change_seq).limit(limit))).all()
    for note_id, user_id, rev in missing:
        await engine.receive(build_note_uploaded_trigger(
            "http_api", user_id=user_id, note_id=note_id, rev=rev))
    processed = 0
    for item in await engine.pending(limit):
        result = await OrganizeNoteWorker(engine).run(item, retry_delay_seconds=retry_delay_seconds)
        if result["status"] != "unclaimed":
            processed += 1
    return processed
