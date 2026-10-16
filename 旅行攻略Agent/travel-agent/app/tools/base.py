"""工具层基础设施：工具注册表 + 调用上下文。

每个工具都是一次「带来源与置信度」的取数或计算，返回值统一为 ToolResult，
上层（编排层）据此决定是否降级、是否需要在回复里提示用户。
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from ..llm import OpenAICompatClient
from ..models import ToolResult
from ..providers import Providers
from ..settings import SETTINGS, Settings


@dataclass
class ToolContext:
    settings: Settings = field(default_factory=lambda: SETTINGS)
    providers: Providers | None = None
    llm: Any = None                      # OpenAICompatClient | ScriptedLLM | None
    store: Any = None                    # store.Store
    poi_cache: dict[str, Any] = field(default_factory=dict)   # poi_id -> Poi（单次规划的候选池）
    place_cache: dict[str, Any] = field(default_factory=dict)  # 名称 -> Place

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm and getattr(self.llm, "available", False))

    def reset_caches(self) -> None:
        self.poi_cache.clear()
        self.place_cache.clear()


def call_llm_json(ctx: ToolContext, system: str, user: str, *, max_tokens: int = 2200,
                  temperature: float = 0.3) -> Any:
    """让大模型返回 JSON 并稳健解析；失败抛 LLMError 由调用方处理。"""
    from ..llm import LLMError, extract_json
    if not ctx.llm_ready:
        raise LLMError("大模型未配置")
    resp = ctx.llm.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        json_mode=True, temperature=temperature, max_tokens=max_tokens,
    )
    if not resp.content and resp.tool_calls:
        raise LLMError("模型返回了工具调用而非 JSON")
    return extract_json(resp.content)


def clamp_float(value: Any, low: float, high: float) -> float | None:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return None


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., ToolResult]
    tags: list[str] = field(default_factory=list)

    def schema(self) -> dict:
        """OpenAI 兼容的 function 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def spec_view(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters, "tags": self.tags}


class ToolRegistry:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._tools: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ #
    def register(self, spec: ToolSpec) -> ToolSpec:
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def specs(self) -> list[dict]:
        return [t.spec_view() for t in self._tools.values()]

    # ------------------------------------------------------------------ #
    def call(self, tool: str, **kwargs: Any) -> ToolResult:
        spec = self._tools.get(tool)
        if spec is None:
            return ToolResult(tool=tool, ok=False, errors=[f"未注册的工具：{tool}"])
        started = time.perf_counter()
        try:
            result = spec.handler(**kwargs)
        except TypeError as exc:
            result = ToolResult(tool=tool, ok=False, errors=[f"参数不匹配：{exc}"])
        except Exception as exc:                      # noqa: BLE001 —— 工具必须永不抛出
            result = ToolResult(tool=tool, ok=False,
                                errors=[f"{type(exc).__name__}: {exc}"],
                                data={"trace": traceback.format_exc(limit=2)})
        result.tool = tool
        if not result.source:
            result.source = ""
        elapsed = int((time.perf_counter() - started) * 1000)
        result.updated_at = result.updated_at or ""
        setattr(result, "_latency_ms", elapsed)
        return result


def ok(name: str, data: Any, *, source: str = "", confidence: float = 0.8,
       **extra: Any) -> ToolResult:
    return ToolResult(tool=name, ok=True, data=data, source=source,
                      confidence=confidence, **extra)


def fail(name: str, message: str, *, source: str = "", data: Any = None) -> ToolResult:
    return ToolResult(tool=name, ok=False, data=data, source=source,
                      confidence=0.0, errors=[message])
