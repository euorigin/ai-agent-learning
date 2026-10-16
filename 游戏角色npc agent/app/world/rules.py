"""规则引擎：关系、情绪、人格的一致性计算。

文档 §3.6：
- 即时情绪由事件与规则即时更新，可自然衰减；
- 关系数值由事件规则计算，有上限和变化原因；
- 人格特征由策划配置，原则上不可被单次对话改变；
- 关系阶段按阈值与关键事件迁移。

文档 §3.4/§3.6 明确：「关系值和任务状态必须由规则服务确认，不能由大模型直接修改」。
因此本模块的输出才是关系的真实结果，模型只能"提出变化建议"。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..schemas import CharacterCard, EmotionState, NpcRuntimeState, RelationChange, Relationship

# 关系数值边界（有上限）
LIMITS: dict[str, tuple[float, float]] = {
    "trust": (0.0, 100.0),
    "affinity": (0.0, 100.0),
    "fear": (0.0, 100.0),
    "debt": (-999.0, 999.0),
}

# 关系阶段阈值（按 trust 升序，取最后一个满足项）
STAGE_THRESHOLDS: list[tuple[float, str]] = [
    (0.0, "敌对"),
    (10.0, "陌生"),
    (40.0, "熟悉"),
    (70.0, "盟友"),
]

# 事件 → 关系变化权重（带变化原因）
EVENT_EFFECTS: dict[str, dict[str, Any]] = {
    "helped": {"trust": +8, "affinity": +6, "reason": "玩家出手相助"},
    "gift": {"trust": +4, "affinity": +8, "reason": "玩家赠送物品"},
    "promise_kept": {"trust": +10, "affinity": +4, "reason": "玩家兑现了承诺"},
    "promise_broken": {"trust": -14, "affinity": -8, "reason": "玩家失信"},
    "cheated": {"trust": -18, "affinity": -12, "reason": "玩家欺骗"},
    "threatened": {"trust": -10, "fear": +15, "reason": "玩家威胁"},
    "insulted": {"affinity": -8, "trust": -4, "reason": "玩家出言不逊"},
    "quest_completed": {"trust": +12, "affinity": +8, "reason": "玩家完成了委托"},
    "quest_failed": {"trust": -8, "reason": "玩家未能完成任务"},
    "trade_fair": {"affinity": +2, "reason": "公平交易"},
    "trade_haggled_hard": {"affinity": -1, "reason": "玩家压价过狠"},
    "greeted_politely": {"affinity": +1, "reason": "礼貌问候"},
}

# 事件 → 即时情绪（标签, 强度）
EVENT_EMOTION: dict[str, tuple[str, float]] = {
    "helped": ("感激", 0.60),
    "gift": ("喜悦", 0.50),
    "promise_kept": ("欣慰", 0.55),
    "promise_broken": ("失望", 0.60),
    "cheated": ("愤怒", 0.80),
    "threatened": ("愤怒", 0.70),
    "insulted": ("不悦", 0.50),
    "quest_completed": ("欣慰", 0.60),
    "quest_failed": ("失望", 0.50),
    "trade_fair": ("满意", 0.35),
    "trade_haggled_hard": ("不悦", 0.35),
    "greeted_politely": ("平和", 0.20),
}

# 从文本快速识别事件（规则兜底；LLM 路由下由 NLU 产出）
EVENT_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("谢谢", "多谢", "感谢", "帮了", "救了我", "多谢了"), "helped"),
    (("送给你", "送你", "这个给你", "拿去", "留给你"), "gift"),
    (("我保证", "我发誓", "我一定", "我答应", "说到做到"), "promise_kept"),
    (("我骗", "骗你", "耍你", "忽悠你"), "cheated"),
    (("杀了你", "弄死你", "打死你", "动手", "找死"), "threatened"),
    (("蠢货", "废物", "滚开", "闭嘴", "垃圾"), "insulted"),
    (("完成了", "办好了", "搞定了", "做完了"), "quest_completed"),
    (("你好", "您好", "早安", "晚上好", "久仰"), "greeted_politely"),
]

EMOTION_DECAY_TAU = 240.0  # 秒；强度按指数衰减
EMOTION_FLOOR = 0.06


@dataclass
class RuleOutcome:
    relation_changes: list[RelationChange] = field(default_factory=list)
    emotion: Optional[EmotionState] = None
    stage_before: str = ""
    stage_after: str = ""
    notes: list[str] = field(default_factory=list)


def detect_event(text: str, intent: str = "") -> Optional[str]:
    """从玩家话语里识别关系事件（规则层，确定性）。"""
    if intent == "out_of_scope":
        return None
    for keywords, kind in EVENT_HINTS:
        if any(k in text for k in keywords):
            return kind
    return None


def stage_of(trust: float) -> str:
    result = STAGE_THRESHOLDS[0][1]
    for low, name in STAGE_THRESHOLDS:
        if trust >= low:
            result = name
    return result


def personality_modifier(card: Optional[CharacterCard], field_name: str, delta: float) -> float:
    """人格只影响"变化的幅度"，不改变数值本身（文档 §3.6 人格稳定边界）。"""
    if card is None or delta == 0:
        return delta
    traits = set(card.personality)
    factor = 1.0
    if field_name == "trust":
        if "多疑" in traits or "不轻信陌生人" in traits:
            factor *= 0.75 if delta > 0 else 1.2
        if "忠诚" in traits or "务实" in traits:
            factor *= 1.1 if delta > 0 else 1.0
    if field_name == "affinity":
        if "贪婪" in traits:
            factor *= 1.25 if delta > 0 else 0.9
        if "同情弱者" in traits and delta > 0:
            factor *= 1.15
        if "严厉" in traits and delta < 0:
            factor *= 1.15
    if field_name == "fear":
        if "严厉" in traits and delta > 0:
            factor *= 0.7
    return delta * factor


def clamp(field_name: str, value: float) -> float:
    low, high = LIMITS.get(field_name, (-1e9, 1e9))
    return max(low, min(high, value))


def apply_event(
    rel: Relationship,
    npc_state: Optional[NpcRuntimeState],
    card: Optional[CharacterCard],
    event_kind: str,
    weight: float = 1.0,
    note: str = "",
) -> RuleOutcome:
    """把一个关系事件落到关系与情绪上，返回带原因的变更记录。"""
    outcome = RuleOutcome(stage_before=rel.stage)
    effect = EVENT_EFFECTS.get(event_kind)
    if effect is None:
        outcome.stage_after = rel.stage
        return outcome

    for field_name in ("trust", "affinity", "fear", "debt"):
        raw = effect.get(field_name)
        if not raw:
            continue
        delta = personality_modifier(card, field_name, float(raw) * weight)
        before = float(getattr(rel, field_name))
        after = clamp(field_name, before + delta)
        actual = round(after - before, 2)
        if abs(actual) < 1e-6:
            continue
        setattr(rel, field_name, after)
        outcome.relation_changes.append(RelationChange(
            field=field_name,
            delta=actual,
            reason=effect.get("reason", event_kind) + (f"（{note}）" if note else ""),
            before=round(before, 2),
            after=round(after, 2),
        ))

    rel.updated_at = time.time()
    rel.events.append({
        "event": event_kind,
        "reason": effect.get("reason", ""),
        "delta": {c.field: c.delta for c in outcome.relation_changes},
        "ts": rel.updated_at,
    })
    if len(rel.events) > 100:
        del rel.events[:-100]

    rel.stage = stage_of(rel.trust)
    outcome.stage_after = rel.stage
    if outcome.stage_before != outcome.stage_after:
        outcome.notes.append(f"关系阶段：{outcome.stage_before} → {outcome.stage_after}")

    emo = EVENT_EMOTION.get(event_kind)
    if emo and npc_state is not None:
        label, intensity = emo
        outcome.emotion = EmotionState(
            label=label,
            intensity=round(min(1.0, intensity * max(0.6, weight)), 2),
            updated_at=time.time(),
        )
    return outcome


def decay_emotion(npc_state: NpcRuntimeState, now: Optional[float] = None) -> bool:
    """情绪自然衰减（文档 §3.6：可自然衰减）。返回是否发生变化。"""
    now = now or time.time()
    emo = npc_state.emotion
    if emo is None or emo.intensity <= 0:
        return False
    dt = max(0.0, now - emo.updated_at)
    if dt <= 0:
        return False
    new_intensity = emo.intensity * math.exp(-dt / EMOTION_DECAY_TAU)
    if new_intensity < EMOTION_FLOOR:
        changed = emo.label != "平静" or emo.intensity != 0.0
        emo.label, emo.intensity, emo.updated_at = "平静", 0.0, now
        return changed
    if abs(new_intensity - emo.intensity) < 1e-3:
        return False
    emo.intensity = round(new_intensity, 3)
    emo.updated_at = now
    return True


def baseline_emotion(card: Optional[CharacterCard], stage: str) -> EmotionState:
    """按人格与关系阶段给一个初始/回落情绪。"""
    label, intensity = "平静", 0.0
    if card:
        traits = set(card.personality)
        if "多疑" in traits or "严厉" in traits:
            label, intensity = "戒备", 0.4
        elif "热络" in traits or "圆滑" in traits:
            label, intensity = "热络", 0.4
        elif "谨慎" in traits:
            label, intensity = "审慎", 0.3
    if stage == "敌对":
        label, intensity = "敌意", max(intensity, 0.6)
    elif stage == "盟友":
        label, intensity = "友善", max(intensity, 0.4)
    return EmotionState(label=label, intensity=intensity)
