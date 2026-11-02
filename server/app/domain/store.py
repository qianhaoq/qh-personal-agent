"""SQLAlchemy persistence. Engine owns all mutations; reads return detached snapshots."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_sessionmaker
from app.domain.model import Decision, Mandate, Trigger, WorkItem
from app.models import SaTrigger, SaWorkItem
from app.timeutil import as_utc


def snapshot(row: SaWorkItem) -> WorkItem:
    return WorkItem(
        id=row.id, kind=row.kind, thread_key=row.thread_key,
        trigger_dedupe_key=row.trigger_dedupe_key, user_id=row.user_id,
        mandate=Mandate(row.mandate["policy_version"], tuple(row.mandate["scope"])),
        decision=Decision(**row.decision), status=row.status,
        created_at=as_utc(row.created_at), updated_at=as_utc(row.updated_at),
        note_id=row.note_id, note_rev=row.note_rev, worker_id=row.worker_id,
        result=row.result, error=row.error, payload=row.payload,
        attempts=row.attempts, max_attempts=row.max_attempts, locked_at=as_utc(row.locked_at),
    )


class Store:
    def __init__(self, maker: async_sessionmaker[AsyncSession] | None = None):
        self._maker = maker

    @property
    def maker(self):
        # Resolve lazily: pytest and service shutdown may replace DB pools.
        return self._maker or get_sessionmaker()

    async def get_work_item(self, item_id: str) -> WorkItem | None:
        async with self.maker() as session:
            row = await session.get(SaWorkItem, item_id)
            return snapshot(row) if row else None

    async def work_items_for_note(self, note_id: str) -> list[WorkItem]:
        async with self.maker() as session:
            rows = (await session.scalars(select(SaWorkItem).where(
                SaWorkItem.note_id == note_id
            ).order_by(SaWorkItem.created_at, SaWorkItem.id))).all()
            return [snapshot(row) for row in rows]

    async def get_trigger(self, trigger_id: str) -> Trigger | None:
        async with self.maker() as session:
            row = await session.get(SaTrigger, trigger_id)
            if row is None:
                return None
            return Trigger(id=row.id, kind=row.kind, source=row.source,
                           dedupe_key=row.dedupe_key, user_id=row.user_id,
                           payload=row.payload, occurred_at=as_utc(row.occurred_at),
                           thread_key=row.thread_key)
