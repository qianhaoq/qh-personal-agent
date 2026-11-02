"""入口 Source 包：把各入口事件翻译成 Trigger 交给 engine（映射 §3）。

v0 两个占位：HttpApiSource（已转写文本经 POST /v1/notes 进入）与
VoiceIngress（Android PTT/ASR，S1 仍借道 HTTP，直连留后续刀）。
"""
from app.sources.registry import get_source
from app.sources.voice_http import HttpApiSource, VoiceIngress, submit_note_uploaded

__all__ = ["HttpApiSource", "VoiceIngress", "get_source", "submit_note_uploaded"]
