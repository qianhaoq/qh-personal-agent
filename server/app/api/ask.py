"""搜索与问答：GET /v1/search、POST /v1/ask。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db_session
from app.deps import get_current_user_id
from app.llm.base import get_llm_client
from app.memory.base import get_memory_store
from app.schemas import AskRequest
from app.timeutil import utcnow

router = APIRouter(prefix="/v1", tags=["ask"])


@router.get("/search")
async def search(
    q: str = Query(min_length=1, max_length=500),
    top_k: int = Query(default=10, ge=1, le=50),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
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
    session: AsyncSession = Depends(get_db_session),
):
    settings = get_settings()
    store = get_memory_store()
    memories = await store.search(user_id, body.text, top_k=settings.ask_top_k)

    if not memories:
        # 无相关记忆：不编造
        return {"answer": "我没有相关记录。", "citations": []}

    context = "\n".join(f"- {m.text}" for m in memories)
    messages = [
        {
            "role": "system",
            "content": (
                "你是用户的个人助理，只依据 <memories> 中给出的记忆回答问题，"
                "不要编造记忆里没有的信息。当前时间：" + utcnow().isoformat()
            ),
        },
        {
            "role": "user",
            "content": f"<memories>\n{context}\n</memories>\n\n问题：{body.text}",
        },
    ]
    llm = await get_llm_client()
    answer = await llm.chat(messages)
    return {
        "answer": answer,
        "citations": [
            {"memory_id": m.id, "note_id": m.note_id, "text": m.text} for m in memories
        ],
    }
