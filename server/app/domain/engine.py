"""Unique domain write boundary: atomic intake, Assignment CAS, report → Channel.

No external services are called inside these transactions. The Assignment token
(worker_id) must be unique per execution; workers generate it for each run.
"""
from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.common import new_id
from app.config import get_settings
from app.domain.model import Decision, Receipt, Trigger, WorkItem
from app.domain.store import Store, snapshot
from app.models import SaThread, SaTrigger, SaWorkItem
from app.policy import PersonalPolicy
from app.timeutil import utcnow


class Engine:
    def __init__(self, store: Store | None = None, policy: PersonalPolicy | None = None,
                 channel=None):
        self.store = store or Store()
        self.policy = policy or PersonalPolicy()
        if channel is None:
            from app.domain.channel import SyncChannel
            channel = SyncChannel()
        self.channel = channel

    async def receive(self, trigger: Trigger) -> Receipt:
        async with self.store.maker() as session, session.begin():
            insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
            # Lock one thread before dedupe/Eval. No note/job/sequence locks here.
            await session.execute(insert(SaThread).values(
                key=trigger.thread_key, user_id=trigger.user_id, created_at=utcnow()
            ).on_conflict_do_update(index_elements=[SaThread.key],
                                    set_={"key": trigger.thread_key}))
            written = await session.execute(insert(SaTrigger).values(
                id=trigger.id, kind=trigger.kind, source=trigger.source,
                dedupe_key=trigger.dedupe_key, user_id=trigger.user_id,
                payload=trigger.payload, thread_key=trigger.thread_key,
                occurred_at=trigger.occurred_at, decision={},
            ).on_conflict_do_nothing(index_elements=[SaTrigger.dedupe_key]))
            if written.rowcount == 0:
                # INV-12: re-read authoritative trigger; dedupe conflict is the
                # only ignored constraint, hence no guessed failure category.
                original = (await session.scalars(select(SaTrigger).where(
                    SaTrigger.dedupe_key == trigger.dedupe_key))).one()
                item = (await session.scalars(select(SaWorkItem).where(
                    SaWorkItem.trigger_id == original.id))).one_or_none()
                return Receipt(original.id, True, Decision(**original.decision),
                               snapshot(item) if item else None)
            decision = self.policy.evaluate(trigger)
            await session.execute(update(SaTrigger).where(SaTrigger.id == trigger.id)
                                  .values(decision=asdict(decision)))
            if decision.outcome != "accept":
                return Receipt(trigger.id, False, decision, None)
            now = utcnow()
            row = SaWorkItem(
                id=new_id("wki"), trigger_id=trigger.id, thread_key=trigger.thread_key,
                trigger_dedupe_key=trigger.dedupe_key, user_id=trigger.user_id,
                kind={"note_uploaded": "organize_note", "ask_requested": "answer_ask"}[trigger.kind],
                mandate=asdict(self.policy.mandate(trigger)), decision=asdict(decision),
                payload=trigger.payload, status="pending", created_at=now, updated_at=now,
                run_after=now, attempts=0, max_attempts=get_settings().job_max_attempts, note_id=trigger.payload.get("note_id"),
                note_rev=trigger.payload.get("rev"),
            )
            session.add(row)
            await session.flush()
            return Receipt(trigger.id, False, decision, snapshot(row))

    async def claim(self, work_item_id: str, worker_id: str) -> bool:
        async with self.store.maker() as session, session.begin():
            result = await session.execute(update(SaWorkItem).where(
                SaWorkItem.id == work_item_id, SaWorkItem.status == "pending"
            ).values(status="running", worker_id=worker_id,
                     attempts=SaWorkItem.attempts + 1))
            if result.rowcount != 1:
                # Closed boolean CAS: 0 means no transition, never inferred
                # missing/failed/conflict. Callers read Store if they need why.
                return False
            # INV-13: timestamp minted after the claim lock is acquired.
            await session.execute(update(SaWorkItem).where(SaWorkItem.id == work_item_id)
                                  .values(locked_at=utcnow(), updated_at=utcnow()))
            return True

    async def complete(self, work_item_id: str, result: dict | None = None, *,
                       worker_id: str, output=None) -> bool:
        async with self.store.maker() as session, session.begin():
            changed = await session.execute(update(SaWorkItem).where(
                SaWorkItem.id == work_item_id, SaWorkItem.status == "running",
                SaWorkItem.worker_id == worker_id,
            ).values(status="done", result=result, error=None, updated_at=utcnow()))
            if changed.rowcount != 1:
                return False  # boolean CAS only; no guessed error attribution
            row = await session.get(SaWorkItem, work_item_id)
            # Fence and Channel writes share one transaction. Channel may lock
            # note → reminders → INSERT memory_sync → sequences (last lock).
            await self.channel.publish(session, snapshot(row), output)
            return True

    async def fail(self, work_item_id: str, error: BaseException, *, worker_id: str,
                   retry: bool = False, retry_delay_seconds: int = 30) -> bool:
        from app.errors import safe_error_text
        async with self.store.maker() as session, session.begin():
            changed = await session.execute(update(SaWorkItem).where(
                SaWorkItem.id == work_item_id, SaWorkItem.status == "running",
                SaWorkItem.worker_id == worker_id,
            ).values(status="failed", error=safe_error_text(error), updated_at=utcnow()))
            if changed.rowcount != 1:
                return False
            row = await session.get(SaWorkItem, work_item_id)
            if retry and row.attempts < row.max_attempts:
                row.status = "pending"
                row.worker_id = None
                row.run_after = utcnow() + timedelta(seconds=retry_delay_seconds)
                await session.flush()
            else:
                await self.channel.failed(session, snapshot(row))
            return True

    async def recover(self) -> None:
        """Recover expired Assignments one transaction at a time (seq stays last)."""
        # A scan-time cutoff is conservative: waiting may delay recovery,
        # never expire an Assignment earlier than the configured lease.
        cutoff = utcnow() - timedelta(seconds=get_settings().job_lock_timeout_seconds)
        async with self.store.maker() as session:
            ids = (await session.scalars(select(SaWorkItem.id).where(
                SaWorkItem.status == "running", SaWorkItem.locked_at < cutoff
            ).limit(get_settings().worker_batch_size))).all()
        for item_id in ids:
            async with self.store.maker() as session, session.begin():
                claimed = await session.execute(update(SaWorkItem).where(
                    SaWorkItem.id == item_id, SaWorkItem.status == "running",
                    SaWorkItem.locked_at < cutoff,
                ).values(status="pending", worker_id=None, updated_at=utcnow()))
                if claimed.rowcount != 1:
                    continue  # CAS predicate closed; another report/recovery won
                row = await session.get(SaWorkItem, item_id)
                row.run_after = utcnow()
                if row.attempts >= row.max_attempts:
                    row.status = "failed"
                    row.error = "AssignmentExpired"
                    await session.flush()
                    await self.channel.failed(session, snapshot(row))

    async def pending(self, limit: int = 100) -> list[WorkItem]:
        async with self.store.maker() as session:
            rows = (await session.scalars(select(SaWorkItem).where(
                SaWorkItem.status == "pending", SaWorkItem.run_after <= utcnow(),
                SaWorkItem.kind == "organize_note",
            ).order_by(SaWorkItem.created_at, SaWorkItem.id).limit(limit))).all()
            return [snapshot(row) for row in rows]

    async def get_work_item(self, item_id: str) -> WorkItem | None:
        return await self.store.get_work_item(item_id)

    async def get_trigger(self, trigger_id: str) -> Trigger | None:
        return await self.store.get_trigger(trigger_id)

    async def work_items_for_note(self, note_id: str) -> list[WorkItem]:
        return await self.store.work_items_for_note(note_id)


def get_engine() -> Engine:
    return Engine(Store())


def reset_engine() -> None:
    """Compatibility: no process-local state exists to clear."""
