"""Source 插件基类（对标 dsh SourceKind 的最小面）。

职责：把入口事件翻译成 Trigger（含幂等键）并交给 engine.receive。
v0 无读位点维护 / 订阅直通 / 自消息过滤——那些随 PR-3 Subscription 落。
"""
from app.common import new_id
from app.domain.engine import Engine
from app.domain.model import TRIGGER_NOTE_UPLOADED, Receipt, Trigger
from app.timeutil import utcnow


def build_note_uploaded_trigger(
    source: str, *, user_id: str, note_id: str, rev: int
) -> Trigger:
    """NoteUploaded 事件的统一构造：幂等键 = note_id + rev（同 note 同 rev
    的重放只记一次）；Thread 取「一条笔记」粒度（整理+后续确认同串）。"""
    return Trigger(
        id=new_id("trg"),
        kind=TRIGGER_NOTE_UPLOADED,
        source=source,
        dedupe_key=f"note_uploaded:{note_id}:{rev}",
        user_id=user_id,
        payload={"note_id": note_id, "rev": rev},
        occurred_at=utcnow(),
        thread_key=f"note:{note_id}",
    )


class Source:
    """Source 插件基类：子类声明 kind，提供各自事件的 Trigger 构造。"""

    kind: str = ""

    async def note_uploaded(
        self, engine: Engine, *, user_id: str, note_id: str, rev: int
    ) -> Receipt:
        """笔记上传事件 → engine.receive（VoiceIngress/HttpApiSource 共用）。"""
        return await engine.receive(
            build_note_uploaded_trigger(self.kind, user_id=user_id, note_id=note_id, rev=rev)
        )

    async def ask_requested(self, engine: Engine, *, user_id: str, payload: dict) -> Receipt:
        request_id = new_id("ask")
        return await engine.receive(Trigger(
            id=new_id("trg"), kind="ask_requested", source=self.kind,
            dedupe_key=f"ask:{user_id}:{request_id}", user_id=user_id,
            payload=payload, occurred_at=utcnow(), thread_key=f"ask:{user_id}:{request_id}",
        ))
