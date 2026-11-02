"""HTTP/语音入口适配（第一刀的 notes API 钩子用）。

- HttpApiSource：已转写文本经 POST /v1/notes 进入的事件流；
- VoiceIngress：Android PTT → ASR 的语音入口占位——S1 端侧转写后仍走
  HTTP 上行（即与 HttpApiSource 同一条 POST /v1/notes 链路），Source
  语义上先占位，端侧直连（不经 notes API）留待后续刀；
- submit_note_uploaded：API 路径的适配入口，绝不上抛——领域旁路不许
  影响 /v1 契约（异常只记安全摘要，INV-14）。
"""
import logging

from app.domain.engine import get_engine
from app.domain.model import Receipt
from app.errors import safe_error_text
from app.sources.base import Source

logger = logging.getLogger(__name__)


class HttpApiSource(Source):
    kind = "http_api"


class VoiceIngress(Source):
    kind = "voice_ingress"


def submit_note_uploaded(
    *, user_id: str, note_id: str, rev: int, source_kind: str = "http_api"
) -> Receipt | None:
    """API 成功路径的领域旁路钩子：NoteUploaded → engine.receive。

    纯内存同步调用，不跑 LLM/worker，也不重复 enqueue extract（现网
    兼容路径见 app/api/notes.py 的 enqueue）。任何异常吞掉并记安全摘要，
    返回 None——绝不把 202 变 500。
    """
    # 延迟导入：registry 反向引用本模块的 Source 类，模块级互 import 成环
    from app.sources.registry import get_source

    source = get_source(source_kind)
    try:
        return source.note_uploaded(get_engine(), user_id=user_id, note_id=note_id, rev=rev)
    except Exception as exc:  # noqa: BLE001 旁路不得影响主契约
        logger.warning("engine.receive(note_uploaded) 失败: %s", safe_error_text(exc))
        return None
