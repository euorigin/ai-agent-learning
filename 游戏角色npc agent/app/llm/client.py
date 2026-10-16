"""OpenAI 兼容协议的模型客户端（文档 §6：Agent 服务 Python + FastAPI）。

只依赖 httpx，不绑定任何 SDK；任何 OpenAI 兼容端点（OpenAI / DeepSeek / 通义 /
本地 vLLM / Ollama 的 /v1）都可以直接接入，只需改 `.env` 三行。
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import settings
from .base import LLMError, LLMReply, ToolCall


class OpenAICompatClient:
    engine = "llm"

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout

    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    # ------------------------------------------------------------------ #
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMReply:
        if not self.available():
            raise LLMError("config", "大模型未配置（缺少 base_url / api_key / model）")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", f"模型请求超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError("network", f"模型网络错误：{exc}") from exc

        if resp.status_code == 401 or resp.status_code == 403:
            raise LLMError("auth", f"鉴权失败（HTTP {resp.status_code}）：请检查 NPC_LLM_API_KEY",
                           resp.status_code)
        if resp.status_code >= 500:
            raise LLMError("server", f"模型服务异常（HTTP {resp.status_code}）", resp.status_code)
        if resp.status_code >= 400:
            raise LLMError("bad_response", f"请求被拒绝（HTTP {resp.status_code}）：{resp.text[:200]}",
                           resp.status_code)

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise LLMError("bad_response", f"响应不是合法 JSON：{exc}") from exc

        return self._parse(data)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse(data: dict[str, Any]) -> LLMReply:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("bad_response", "响应中没有 choices")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                from .base import extract_json
                args = extract_json(args) or {}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args or {}))
        usage = data.get("usage") or {}
        return LLMReply(
            text=text,
            tool_calls=calls,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            engine="llm",
            raw=data,
        )
