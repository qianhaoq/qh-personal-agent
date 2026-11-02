"""HTTP Source and future voice ingress. Note intake runs after API commit."""
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


async def submit_note_uploaded(*, user_id: str, note_id: str, rev: int,
                               source_kind: str = "http_api") -> Receipt | None:
    try:
        from app.sources.registry import get_source
        return await get_source(source_kind).note_uploaded(
            get_engine(), user_id=user_id, note_id=note_id, rev=rev)
    except Exception as exc:  # noqa: BLE001 safe failure boundary
        # Committed upload remains 202; Runtime reconciles uploaded notes if
        # this process dies or DB intake temporarily fails after note commit.
        logger.warning("note intake failed: %s", safe_error_text(exc))
        return None
