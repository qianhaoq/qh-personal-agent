"""搜索与问答：GET /v1/search、POST /v1/ask。

不依赖请求级数据库会话（P2）：鉴权在独立短会话完成（见 app.deps），
这里只有记忆检索（后端内部还有 LLM/embedding）与 LLM 调用——若挂着
打开的会话/事务，秒级外部调用期间会占住连接池，把并发请求饿死。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.deps import get_current_user_id
from app.memory.base import get_memory_store
from app.schemas import AskRequest, NoNulStr

router = APIRouter(prefix="/v1", tags=["ask"])


@router.get("/search")
async def search(
    q: Annotated[NoNulStr, Query(min_length=1, max_length=500)],
    top_k: int = Query(default=10, ge=1, le=50),
    user_id: str = Depends(get_current_user_id),
):
    store = get_memory_store()
    results = await store.search(user_id, q, top_k=top_k)
    return {
        "items": [
            {"memory_id": m.id, "note_id": m.note_id, "text": m.text, "score": m.score}
            for m in results
        ]
    }


@router.post("/ask")
async def ask(
    body: AskRequest,
    user_id: str = Depends(get_current_user_id),
):
    from app.domain.engine import get_engine
    from app.errors import ApiError
    from app.sources.voice_http import HttpApiSource
    from app.workers.answer import AnswerAskWorker

    engine = get_engine()
    receipt = await HttpApiSource().ask_requested(
        engine, user_id=user_id, payload=body.model_dump(mode="json"))
    if receipt.work_item is None:
        raise ApiError(403, "policy_denied", "策略未授权问答")
    return await AnswerAskWorker(engine).run(receipt.work_item)
