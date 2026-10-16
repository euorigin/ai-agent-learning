"""模型层导出：按引擎策略选一个"大脑"（真实模型 / 离线演示脑）。"""

from __future__ import annotations

from ..config import settings
from .base import LLMError, LLMReply, ToolCall, extract_json
from .client import OpenAICompatClient
from .demo_brain import TASK_ACT, TASK_NLU, TASK_REPLY, DemoBrain


def get_brain(engine: str | None = None):
    """engine: llm / demo / rules / None(=按 .env 自动)。

    - `llm` 且凭据齐全 → OpenAI 兼容客户端；
    - 其余情况（含未配置、显式 demo/rules）→ 离线演示脑，保证系统仍可降级运行。
    """
    resolved = (engine or settings.resolved_engine()).lower()
    if resolved == "llm":
        client = OpenAICompatClient()
        if client.available():
            return client
    return DemoBrain()


__all__ = [
    "LLMError",
    "LLMReply",
    "ToolCall",
    "extract_json",
    "OpenAICompatClient",
    "DemoBrain",
    "get_brain",
    "TASK_NLU",
    "TASK_ACT",
    "TASK_REPLY",
]
