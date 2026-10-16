"""离线演示脑（离线演示 + 测试替身 + 降级后端）。

用途（文档 §8.2 验收：关闭模型或外部服务时 NPC 仍能降级运行）：
1. 不填任何 API Key 也能完整跑通「理解 → 决策 → 工具 → 表现」全链路；
2. 作为 `tests/selfcheck.py` 的测试替身，断言完全不依赖网络与真实模型。

实现方式：按系统提示里的任务标记路由——
- 含 `[[TASK:NLU]]`   → 复用规则解析器产出结构化意图（与 LLM 路径输出同构）
- 传入 tools 且含 `[[TASK:ACT]]` → 按关键词确定性地挑一个受控工具
- 含 `DEMO_CTX` 标记  → 用 `template_reply` 生成角色化文本
"""

from __future__ import annotations

from typing import Any, Optional

from .base import LLMReply, ToolCall
from ..agent.responder import ReplyContext, parse_marker, template_reply

TASK_NLU = "[[TASK:NLU]]"
TASK_ACT = "[[TASK:ACT]]"
TASK_REPLY = "[[TASK:REPLY]]"


class DemoBrain:
    """确定性、零依赖、可离线运行的"模型"。"""

    engine = "demo"
    degraded = True

    def available(self) -> bool:
        return True

    # ------------------------------------------------------------------ #
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMReply:
        system = messages[0].get("content", "") if messages else ""
        user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user = msg.get("content", "")
                break

        # 1) 意图识别：与规则路径同源，保证行为一致
        if TASK_NLU in system:
            from ..agent.nlu import rule_parse

            card_id = self._extract(system, "CARD_ID") or ""
            understanding = rule_parse(user, card_id=card_id)
            return LLMReply(text=_json({
                "intent": understanding.intent,
                "slots": understanding.slots,
                "confidence": understanding.confidence,
                "need_clarify": understanding.need_clarify,
                "clarify_question": understanding.clarify_question,
                "reason": understanding.reason,
                "candidate_tool": understanding.candidate_tool,
            }), engine="demo")

        # 2) 工具选择
        if tools and (TASK_ACT in system or not json_mode):
            call = self._choose_tool(user, tools)
            if call is not None:
                return LLMReply(tool_calls=[call], engine="demo")

        # 3) 回复生成
        rc = parse_marker(system)
        if rc is not None:
            return LLMReply(text=template_reply(rc), engine="demo")

        return LLMReply(text="嗯，我在听。", engine="demo")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract(system: str, key: str) -> Optional[str]:
        token = f"[[{key}:"
        i = system.find(token)
        if i == -1:
            return None
        j = system.find("]]", i)
        return system[i + len(token):j] if j != -1 else None

    @staticmethod
    def _choose_tool(user: str, tools: list[dict[str, Any]]) -> Optional[ToolCall]:
        names = {t.get("function", {}).get("name") for t in tools}
        text = user

        def has(*kw: str) -> bool:
            return any(k in text for k in kw)

        if has("钥匙", "布防", "军情", "内应的名字", "他叫什么") and "reveal_lore" in names:
            return ToolCall("reveal_lore", {"topic": "治安"})
        if has("传闻", "听说", "消息", "知道什么", "打听") and "reveal_lore" in names:
            return ToolCall("reveal_lore", {"topic": ""})
        if has("附近", "周围", "有没有人", "谁在") and "query_nearby_entities" in names:
            return ToolCall("query_nearby_entities", {})
        if has("任务", "委托", "接下来要做什么", "进度") and "query_quest" in names:
            return ToolCall("query_quest", {})
        if has("几点", "什么时候", "天气", "城门", "宵禁") and "query_world_state" in names:
            return ToolCall("query_world_state", {})
        if has("多少钱", "价格", "库存", "有什么货") and "query_inventory" in names:
            return ToolCall("query_inventory", {"who": "npc"})
        if has("买", "来一份", "给我来", "要一份") and "trade_offer" in names:
            return ToolCall("trade_offer", {"item_id": "yin_leaf", "quantity": 1, "side": "buy"})
        if has("卖给你", "收购") and "trade_offer" in names:
            return ToolCall("trade_offer", {"item_id": "yin_leaf", "quantity": 1, "side": "sell"})
        if has("开城门", "放我过去", "开门") and "open_gate" in names:
            return ToolCall("open_gate", {"gate_id": "north_gate", "open": True})
        return None


def _json(data: dict[str, Any]) -> str:
    import json
    return json.dumps(data, ensure_ascii=False)


def build_demo_context(**kwargs: Any) -> ReplyContext:
    """便捷构造器（给编排层用）。"""
    return ReplyContext(**kwargs)
