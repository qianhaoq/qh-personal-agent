"""Organize Worker: claim → tools outside transactions → report to Engine."""
from app.common import new_id
from app.domain.engine import Engine
from app.domain.model import WorkItem
from app.errors import safe_error_text


class OrganizeNoteWorker:
    def __init__(self, engine: Engine):
        self.engine = engine

    async def run(self, work_item: WorkItem, *, retry_delay_seconds: int = 30) -> dict:
        worker_id = new_id("execution")  # unique per Assignment, never reused
        if not await self.engine.claim(work_item.id, worker_id):
            return {"status": "unclaimed"}
        try:
            from app.workers.extract_impl import prepare_output
            output = await prepare_output(work_item.note_id, work_item.note_rev,
                                          self.engine.store.maker)
            accepted = await self.engine.complete(
                work_item.id, {"note_id": work_item.note_id},
                worker_id=worker_id, output=output)
            return {"status": "done" if accepted else "superseded"}
        except Exception as exc:  # noqa: BLE001 safe failure boundary
            await self.engine.fail(work_item.id, exc, worker_id=worker_id, retry=True,
                                   retry_delay_seconds=retry_delay_seconds)
            return {"status": "failed", "error": safe_error_text(exc)}
