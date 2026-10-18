"""OpenAI 兼容接口客户端（httpx 直连，避免 SDK 依赖）。"""
import json
import logging

import httpx

logger = logging.getLogger(__name__)

# 进程内记住「该 (base_url, model) 不支持 json_schema、已回落 json_object」
# 的决定（auto 模式）：首个 400 判定后，同进程后续请求直接走 json_object，
# 不再先撞一次 400。key 不含 api_key（不落日志、不进异常消息，INV-14）。
_json_object_forced: dict[tuple[str, str], bool] = {}

# json_schema 变体被拒的错误体判定字样（小写匹配；DeepSeek 实际报
# "This response_format type is unavailable now"）
_JSON_SCHEMA_UNSUPPORTED_MARKERS = ("unsupported", "not support", "unavailable", "invalid")

# json_object 模式没有 schema 通道，把 schema 说明追加进 system 消息。
# 文案必须含 "JSON"（DeepSeek 的 json_object 要求 prompt 中出现 json 字样）
_JSON_OBJECT_NOTE_TMPL = (
    "\n\n只输出一个 JSON 对象，不要输出代码围栏或任何其他文本。"
    "输出的 JSON 必须符合以下 JSON Schema：\n{schema_json}"
)


def _reset_response_format_cache() -> None:
    """测试用：清空 auto 模式的 json_object 回落缓存。"""
    _json_object_forced.clear()


def _response_format_unsupported(resp: httpx.Response) -> bool:
    """400 响应体是否表明供应商不支持当前 response_format（json_schema 变体）。

    只读错误体 JSON 的 error.message/code/type（解析不了或没有则用原始
    文本），小写化后判定：提到 response_format，或提到 json_schema 且带
    unsupported/not support/unavailable/invalid 字样。判定只用于决定是否
    回落，错误体内容不落日志、不进异常消息（INV-14）。
    """
    parts: list[str] = []
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            for key in ("message", "code", "type"):
                value = err.get(key)
                if isinstance(value, str):
                    parts.append(value)
        elif isinstance(err, str):
            parts.append(err)
    text = " ".join(parts).lower() or resp.text.lower()
    if "response_format" in text:
        return True
    return "json_schema" in text and any(m in text for m in _JSON_SCHEMA_UNSUPPORTED_MARKERS)


def _messages_with_schema_note(messages: list[dict], schema: dict) -> list[dict]:
    """json_object 模式的请求消息：schema 说明写进 system 消息。

    有 system 消息就追加在其内容末尾，没有就在最前面插一条。拷贝传入的
    messages（每个 dict 浅拷贝），不修改调用方数据。
    """
    note = _JSON_OBJECT_NOTE_TMPL.format(schema_json=json.dumps(schema, ensure_ascii=False))
    msgs = [dict(m) for m in messages]
    for m in msgs:
        if m.get("role") == "system":
            m["content"] = f"{m.get('content') or ''}{note}"
            return msgs
    return [{"role": "system", "content": note.strip()}, *msgs]


def _chat_content(resp: httpx.Response) -> str:
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


class OpenAICompatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 15.0,
        response_format_mode: str = "auto",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.response_format_mode = response_format_mode

    async def _post(self, body: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )

    async def chat(self, messages: list[dict], response_format: dict | None = None) -> str:
        # 问答（无 schema）：不发 response_format，行为与回落机制无关
        if response_format is None:
            body: dict = {"model": self.model, "messages": messages, "temperature": 0.2}
            return _chat_content(await self._post(body))

        prefer_json_object = (
            self.response_format_mode == "json_object"
            or (
                self.response_format_mode == "auto"
                and (self.base_url, self.model) in _json_object_forced
            )
        )
        if prefer_json_object:
            return _chat_content(await self._post(self._json_object_body(messages, response_format)))

        # OpenAI 兼容的 JSON 模式：给出 schema 的 json_schema 变体
        resp = await self._post(
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0.2,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "extraction", "schema": response_format, "strict": False},
                },
            }
        )
        if (
            self.response_format_mode == "auto"
            and resp.status_code == 400
            and _response_format_unsupported(resp)
        ):
            # 供应商（如 DeepSeek）不支持 json_schema 变体：改用 json_object
            # 重试一次，并把决定缓存在进程内（之后不再先撞 400）。判定只读
            # 响应体，不落日志/异常；日志只记固定文案（INV-14）
            _json_object_forced[(self.base_url, self.model)] = True
            logger.warning(
                "llm provider rejected response_format=json_schema, falling back to json_object"
            )
            resp = await self._post(self._json_object_body(messages, response_format))
        return _chat_content(resp)

    def _json_object_body(self, messages: list[dict], response_format: dict) -> dict:
        """json_object 变体：schema 说明进 system 消息（见 _messages_with_schema_note）。"""
        return {
            "model": self.model,
            "messages": _messages_with_schema_note(messages, response_format),
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
