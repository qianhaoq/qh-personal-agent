"""Unified result outlet. Invoked only by Engine inside its fenced transaction."""
from typing import Protocol

from sqlalchemy import update

from app.common import allocate_seq
from app.models import Note
from app.timeutil import utcnow


class Channel(Protocol):
    async def publish(self, session, item, output) -> None: ...
    async def failed(self, session, item) -> None: ...
    def result(self, item) -> dict: ...


class SyncChannel:
    async def publish(self, session, item, output) -> None:
        if item.kind == "organize_note" and output is not None:
            from app.workers.extract_impl import apply_output
            snapshot, extraction = output
            await apply_output(session, item.note_id, item.note_rev, snapshot, extraction)
        # answer_ask's JSON result is persisted by Engine and returned by result().
        # No alarms, pushes or external I/O in this Channel (observe default).

    async def failed(self, session, item) -> None:
        if item.kind != "organize_note":
            return
        changed = await session.execute(update(Note).where(
            Note.id == item.note_id, Note.rev == item.note_rev,
            Note.deleted_at.is_(None), Note.status != "organized",
        ).values(status="failed", updated_at=utcnow()))
        if changed.rowcount != 1:
            return  # closed predicate: no current unorganized revision to mark
        await session.flush()
        seq = await allocate_seq(session)
        await session.execute(update(Note).where(Note.id == item.note_id).values(change_seq=seq))

    def result(self, item) -> dict:
        if item.status != "done" or item.result is None:
            raise RuntimeError("result unavailable")
        return item.result
