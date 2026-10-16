"""大模型客户端（OpenAI 兼容 /chat/completions）。

支持三种形态：
- 原生 Tool Calling（tools / tool_calls）
- JSON 模式（response_format=json_object），
- 普通对话。
模型不支持 tools 参数时自动降级为「提示词驱动的 JSON 工具调用」。

另附 ScriptedLLM：把预设的应答脚本喂给上层，用于离线自检与无 Key 演示。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .http import HttpError, request_json
from .settings import SETTINGS, Settings

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

# 思维链片段。部分推理模型（如 MiniMax M2、DeepSeek-R1、QwQ）把推理过程直接写进
# choice.message.content，形如：
#     <think>…推理，里面常有 { } 和数字…</think>{"pois":[...]}
# 必须在取 JSON 之前处理掉，否则大括号切法会切出非法 JSON。
#
# 两种形态都要覆盖：
#   (a) 成对标签块 <think>…</think> / <thinking>…</thinking>
#   (b) 全角竖线分隔符 <｜begin▁of▁thinking｜>…<｜end▁of▁thinking｜>
# 先处理 (b) 的「块」，再处理 (a) 的块 —— 顺序不能反，否则 (b) 里的
# 独立分隔符会被 (a) 漏掉，残留一个处于最前面的 begin 标记，干扰后续取 JSON。
_THINK_PAIR = re.compile(
    r"<\｜(?:begin▁of▁)?thinking\｜>.*?<\｜end▁of▁thinking\｜>"      # (b) 块
    r"|<(think|thinking|reasoning|analysis)\b[^>]*>.*?</\1>",        # (a) 块
    re.S | re.I,
)
# 落单的分隔符（模型只给了一个 begin 或 end）
_THINK_TAG = re.compile(r"<\｜?(?:begin▁of▁|end▁of▁)?thinking\｜?>", re.I)


def _rejected(_m: "re.Match[str]") -> str:
    """替换回调：把思维链整段丢掉，返回空串。"""
    return ""


def _strip_reasoning(text: str) -> str:
    """剥掉思维链片段，只留模型真正要交付的正文。

    注意：只有在**整段正文本身不是合法 JSON** 时才做剥离。
    否则像 {"g":"<think>x</think>"} 这种把标签当普通字符串的合法 JSON，
    会被误伤成 {"g":""}。
    """
    if not text:
        return text
    body = text.strip()
    if body[:1] in "{[":
        try:
            json.loads(body)
        except json.JSONDecodeError:
            pass
        else:
            return body                 # 本身就是干净 JSON，原样返回

    cleaned = _THINK_PAIR.sub(_rejected, text)
    cleaned = _THINK_TAG.sub("", cleaned)
    # 标签被截断（模型没写完 </think>）时，从最后一个未闭合的 <think 起全丢掉。
    cutoff = cleaned.lower().rfind("<think")
    if cutoff >= 0 and "</think" not in cleaned[cutoff:].lower():
        cleaned = cleaned[:cutoff]
    return cleaned.strip()


def strip_reasoning(text: str) -> str:
    """对外暴露的思维链剥离：给「要直接展示给用户的文本」用（narration / QA 回复）。

    extract_json 内部用它来取 JSON；展示路径也得用它，
    否则界面会出现「让我分析一下……」这类模型内心戏。
    """
    return _strip_reasoning(text)


def extract_json(text: str) -> Any:
    """从模型输出里稳健地抠出 JSON。

    处理顺序（每一步都是实测踩过的坑）：
      1. 剥掉 <think> 思维链 —— 它里面常含 { }，会骗过「首尾大括号」切法；
      2. ```json 代码块；
      3. 整段直接 json.loads；
      4. 用 raw_decode 从每个 { / [ 起点试解析，取第一个成功的 ——
         比「首个 { 到最后一个 }」可靠：正文里出现两个对象时不会切出夹层垃圾。
    """
    if not text:
        raise LLMError("模型返回为空，无法解析 JSON")

    candidate = _strip_reasoning(text)
    if not candidate:
        raise LLMError("模型返回只有思维链，没有可解析内容")

    m = _FENCE.search(candidate)
    if m:
        candidate = m.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # raw_decode：从每个可能的起点试解析，返回第一个能解出对象的
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(candidate):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(candidate[idx:])
        except json.JSONDecodeError:
            continue
        return obj

    raise LLMError(
        "模型返回中未找到 JSON 对象"
        f"（已剥思维链，正文前 120 字：{candidate[:120]!r}）"
    )


class LLMError(RuntimeError):
    """大模型调用失败（网络、鉴权、额度、返回格式等）。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class OpenAICompatClient:
    """最小可用的 OpenAI 兼容客户端。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or SETTINGS
        self._native_tools_ok = self.s.llm_tool_mode != "prompt"

    @property
    def available(self) -> bool:
        return self.s.llm_ready

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        if not self.available:
            raise LLMError(
                "大模型未配置：请在项目根目录的 .env 中填写 TRAVEL_LLM_BASE_URL / "
                "TRAVEL_LLM_MODEL / TRAVEL_LLM_API_KEY"
            )
        body: dict[str, Any] = {
            "model": self.s.llm_model,
            "messages": messages,
            "temperature": self.s.llm_temperature if temperature is None else temperature,
        }
        if max_tokens:
            # 推理模型（MiniMax M2 / DeepSeek-R1 等）的 <think> 段也消耗 max_tokens，
            # 按正文长度给预算会被 finish_reason=length 截在半路、正文颗粒无收。
            # 统一放大预算给思考留余量；非推理模型把 TRAVEL_LLM_OUTPUT_RATIO 设为 1 即可。
            ratio = max(1.0, float(getattr(self.s, "llm_output_ratio", 1.0) or 1.0))
            body["max_tokens"] = int(max_tokens * ratio)
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        use_tools = bool(tools) and self._native_tools_ok
        if use_tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        try:
            # 预算放大后单次请求的生成量也变大，超时要同步放宽，否则会
            # 「token 给够了但等不到」→ HTTP 0 超时（实测 want=22 时踩到）。
            base_timeout = timeout or self.s.llm_timeout
            ratio = max(1.0, float(getattr(self.s, "llm_output_ratio", 1.0) or 1.0))
            data = request_json(
                f"{self.s.llm_base_url}/chat/completions",
                method="POST",
                json_body=body,
                headers={"Authorization": f"Bearer {self.s.llm_api_key or 'not-needed'}"},
                timeout=base_timeout * min(ratio, 3.0),
                retries=1,
            )
        except HttpError as exc:
            # 部分模型/网关不认识 tools 参数 → 关闭原生工具，改走提示词 JSON 方案
            if use_tools and exc.status in (400, 404, 422) and "tool" in exc.body.lower():
                self._native_tools_ok = False
                return self.chat(messages, tools=None, json_mode=json_mode,
                                 temperature=temperature, max_tokens=max_tokens)
            if exc.status in (401, 403):
                raise LLMError(f"大模型鉴权失败（HTTP {exc.status}）：请检查 .env 中的 TRAVEL_LLM_API_KEY") from exc
            if exc.status == 429:
                raise LLMError("大模型限流或额度用尽（HTTP 429）") from exc
            raise LLMError(f"大模型调用失败：{exc}") from exc

        return self._parse(data)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse(data: dict) -> LLMResponse:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"大模型返回结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from exc

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall(id=raw.get("id") or f"call_{len(calls)}",
                                  name=fn.get("name", ""), arguments=args or {}))

        content = message.get("content") or ""
        if isinstance(content, list):   # 少数网关返回分片数组
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return LLMResponse(content=content, tool_calls=calls, raw=data)

    @property
    def tool_mode(self) -> str:
        return "native" if self._native_tools_ok else "prompt"


# --------------------------------------------------------------------------- #
# 离线脚本模型：按顺序返回预设应答，用于自检与无 Key 演示
# --------------------------------------------------------------------------- #
class ScriptedLLM:
    """与 OpenAICompatClient 同接口；把脚本喂进去即可完整跑通 Agent 流程。"""

    def __init__(self, script: list[LLMResponse], *, name: str = "scripted") -> None:
        self._script = list(script)
        self.calls: list[list[dict]] = []
        self.name = name
        self.s = SETTINGS

    @property
    def available(self) -> bool:
        return True

    @property
    def tool_mode(self) -> str:
        return "native"

    def chat(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        self.calls.append(messages)
        if not self._script:
            return LLMResponse(content="{}")
        return self._script.pop(0)

    # 便捷构造器
    @staticmethod
    def text(content: str) -> LLMResponse:
        return LLMResponse(content=content)

    @staticmethod
    def json(payload: dict | list) -> LLMResponse:
        return LLMResponse(content=json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def tool(name: str, arguments: dict, call_id: str = "call_1") -> LLMResponse:
        return LLMResponse(content="", tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])
