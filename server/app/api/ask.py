"""搜索与问答：GET /v1/search、POST /v1/ask。

不依赖请求级数据库会话（P2）：鉴权在独立短会话完成（见 app.deps），
这里只有记忆检索（mem0 内部还有 LLM/embedding）与 LLM 调用——若挂着
打开的会话/事务，秒级外部调用期间会占住连接池，把并发请求饿死。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.config import get_settings
from app.deps import get_current_user_id
from app.llm.base import get_llm_client
from app.memory.base import get_memory_store
from app.schemas import AskRequest, NoNulStr
from app.timeutil import to_utc, utcnow

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
    settings = get_settings()
    store = get_memory_store()
    memories = await store.search(user_id, body.text, top_k=settings.ask_top_k)

    if not memories:
        # 无相关记忆：不编造
        return {"answer": "我没有相关记录。", "citations": []}

    context = "\n".join(f"- {m.text}" for m in memories)
    tzname = body.timezone or settings.default_timezone
    # 参照时间：客户端时间（语音提问的“今天/明天”以它为准），缺省退回服务器时钟。
    # naive 的 client_time 按请求时区（缺省 default_timezone）本地化，不当成 UTC
    ref_time = to_utc(body.client_time, tzname) or utcnow()
    messages = [
        {
            "role": "system",
            "content": (
                "你是用户的个人助理，只依据 <memories> 中给出的记忆回答问题，"
                "不要编造记忆里没有的信息。"
                f"当前时间：{ref_time.isoformat()}（时区：{tzname}）"
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
