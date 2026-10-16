"""表现标签（文档 §3.8 语音、表情与动作表现）。

文件明确：系统应根据对话意图和情绪生成**表现标签**，
由游戏客户端或引擎选择已审核的动画和音频资源——模型不能直接控制骨骼参数。
因此这里只输出"标签"，不输出任何引擎参数。
"""

from __future__ import annotations

from typing import Optional

from ..config import settings
from ..schemas import CharacterCard, EmotionState, PresentationTags, ToolResult

# 情绪 → 表情 BlendShape 名（客户端按此查表）
EMOTION_EXPRESSION: dict[str, str] = {
    "平静": "neutral", "平和": "neutral", "审慎": "squint",
    "戒备": "squint", "忧虑": "frown", "失望": "frown",
    "不悦": "frown", "愤怒": "glare", "敌意": "glare",
    "喜悦": "smile", "感激": "smile_soft", "满意": "smile",
    "热络": "smile", "欣慰": "smile_soft", "友善": "smile_soft",
}

EMOTION_ACTION: dict[str, str] = {
    "愤怒": "clench_fist", "敌意": "reach_weapon", "喜悦": "nod_lively",
    "热络": "gesture_offer", "感激": "bow_slight", "失望": "shake_head",
    "忧虑": "rub_hands", "戒备": "straighten_up", "平静": "idle",
}


class PresentationBuilder:
    def __init__(self, card: CharacterCard) -> None:
        self.card = card

    def build(
        self,
        *,
        intent: str,
        emotion: Optional[EmotionState],
        tool_result: Optional[ToolResult],
        reply: str,
        tool_name: str = "",
    ) -> PresentationTags:
        label = emotion.label if emotion else "平静"
        intensity = round(emotion.intensity if emotion else 0.0, 2)

        expression = EMOTION_EXPRESSION.get(label, "neutral")
        action = self._action_for(intent, label, tool_result, tool_name)

        # 注视：交易/交付时看向玩家手上的东西，否则看玩家
        gaze = "player_hand" if intent == "trade_request" else "player"
        if label in ("戒备", "敌意"):
            gaze = "scan_perimeter"

        return PresentationTags(
            emotion=label,
            intensity=intensity,
            expression=expression,
            gaze_target=gaze,
            action=action,
            speech_style=self.card.voice.tone,
            subtitle=reply[: settings.max_utterance_chars],
            interruptible=intent not in ("quest_op",),
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _action_for(intent: str, label: str, tool_result: Optional[ToolResult],
                    tool_name: str = "") -> str:
        name = tool_name or (tool_result.tool if tool_result else "")
        if name in ("offer_quest", "advance_quest"):
            return "hand_over_scroll"
        if name == "trade_offer":
            if tool_result is None or tool_result.success:
                return "exchange_goods"
            return "shake_head_refuse"
        if name == "open_gate":
            return "signal_follow"
        if name in ("query_quest", "query_world_state", "reveal_lore"):
            return "gesture_explain"
        if name == "query_inventory":
            return "gesture_offer"
        if name == "query_nearby_entities":
            return "scan_around"
        if name == "give_item":
            return "hand_over_item"
        if tool_result is not None and not tool_result.success:
            return "spread_hands"
        return EMOTION_ACTION.get(label, "idle")
