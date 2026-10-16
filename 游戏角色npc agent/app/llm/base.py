"""大模型调用的公共契约（不依赖 agent 层，避免循环导入）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    engine: str = "llm"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_call(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """带分类的模型调用错误，供上层决定是否降级。"""

    def __init__(self, kind: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind          # auth / timeout / network / bad_response / server
        self.status = status


def extract_json(text: str) -> Optional[dict[str, Any]]:
    """从模型输出里稳健地抠出 JSON 对象（容忍代码块包裹与前后废话）。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        # 去掉 ```json ... ``` 围栏
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        pass
    start = s.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(s[start:i + 1])
                        if isinstance(data, dict):
                            return data
                    except Exception:  # noqa: BLE001
                        break
        start = s.find("{", start + 1)
    return None
