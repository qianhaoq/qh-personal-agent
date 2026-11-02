"""Source 注册表（对标 dsh sources/registry）。"""
from app.sources.base import Source
from app.sources.voice_http import HttpApiSource, VoiceIngress

_SOURCES: dict[str, Source] = {
    HttpApiSource.kind: HttpApiSource(),
    VoiceIngress.kind: VoiceIngress(),
}


def get_source(kind: str) -> Source:
    try:
        return _SOURCES[kind]
    except KeyError:
        known = "、".join(sorted(_SOURCES))
        raise ValueError(f"unknown source kind: {kind}（已知：{known}）") from None
