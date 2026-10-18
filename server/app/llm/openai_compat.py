"""OpenAI 兼容接口客户端（httpx 直连，避免 SDK 依赖）。"""
import httpx


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def chat(self, messages: list[dict], response_format: dict | None = None) -> str:
        body: dict = {"model": self.model, "messages": messages, "temperature": 0.2}
        if response_format is not None:
            # OpenAI 兼容的 JSON 模式：给出 schema 的 json_schema 变体，不支持的供应商退回 json_object
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": response_format, "strict": False},
            }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"]
