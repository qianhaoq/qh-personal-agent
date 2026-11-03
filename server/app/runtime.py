"""Durable scheduling: repair (lease recovery + intake gap rescan) and Ticket execution.

No task/dict is authoritative. Multiple processes scan the same Store; CAS picks
one executor. `repair` is the reusable infrastructure half: the standalone
Watchdog Control Loop (`app.jobs.watchdog`) polls it alone, while the Worker
poll (`app.jobs.worker`) runs it before claiming Tickets so the single-process
path keeps repairing too.
"""
from sqlalchemy import String, cast, exists, literal, select

from app.domain.engine import get_engine
from app.models import Note, SaTrigger
from app.sources.base import build_note_uploaded_trigger
from app.workers.organize import OrganizeNoteWorker


async def repair(limit: int = 100) -> int:
    """Recover expired Assignments + rescan uploaded notes missing Trigger.

    Infrastructure repair only — no Ticket execution, no memory queue. Same
    idempotency, lock order and INV semantics as before the Watchdog split:
    Engine.recover keeps its per-item CAS reopening, intake keeps the stable
    `note_uploaded:{id}:{rev}` dedupe key. Returns the number of intake
    repairs attempted (recover re-openings are not counted).
    """
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
    return len(missing)


async def run_pending_work(limit: int = 100, retry_delay_seconds: int = 30) -> int:
    engine = get_engine()
    await repair(limit=limit)
    processed = 0
    for item in await engine.pending(limit):
        result = await OrganizeNoteWorker(engine).run(item, retry_delay_seconds=retry_delay_seconds)
        if result["status"] != "unclaimed":
            processed += 1
    return processed
