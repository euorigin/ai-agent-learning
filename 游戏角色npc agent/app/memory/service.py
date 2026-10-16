"""记忆服务（Memory Service）。

文档 §3.4：
- 区分短期上下文 / 事件记忆 / 关系状态 / 任务事实 / 世界知识；
- 所有记忆保留来源、时间、可信度、可删除标记；
- 用记忆写入策略控制哪些内容可以长期保存；
- 关系值与任务状态由规则/任务服务维护，不写入本服务。

文档 §4.1：PostgreSQL + Redis + 向量检索。此处以进程内实现等价替代，
检索用字符二元组重叠 + 时间衰减打分（无外部依赖），换 pgvector 时只需替换 `recall`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from ..config import settings
from ..schemas import MemoryEvent, Turn
from ..store import repository

# 可长期保存的记忆类型白名单（文档 §3.4 记忆写入策略；闲聊不落库）
WRITE_WHITELIST: dict[str, str] = {
    "player_helped": "玩家出手相助",
    "player_cheated": "玩家欺骗",
    "player_promised": "玩家做出承诺",
    "player_threatened": "玩家威胁",
    "conflict": "双方发生冲突",
    "relationship_change": "关系发生变化",
    "quest_fact": "任务相关事实",
    "trade": "交易记录",
    "observation": "NPC 亲眼所见",
    "disclosure": "玩家告知的信息",
}


def _approx_tokens(text: str) -> int:
    """粗略 token 估计：中文按字符、英文按 4 字符切分。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4) if text else 0


def _bigrams(text: str) -> set[str]:
    clean = "".join(ch for ch in text if not ch.isspace())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


@dataclass
class RecallHit:
    event: MemoryEvent
    score: float


class MemoryService:
    def __init__(self) -> None:
        self.rejected_writes: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 短期记忆（会话缓存 + token 预算截断，文档 §3.4）
    # ------------------------------------------------------------------ #
    def history(self, npc_id: str, player_id: str, instance_id: str) -> list[Turn]:
        return list(repository.session(npc_id, player_id, instance_id))

    def record_turn(
        self, npc_id: str, player_id: str, instance_id: str, turn: Turn
    ) -> None:
        turns = repository.session(npc_id, player_id, instance_id)
        turns.append(turn)
        self._truncate(turns)

    def _truncate(self, turns: list[Turn]) -> None:
        # 先按轮数上限
        max_turns = settings.short_term_turns * 2
        if len(turns) > max_turns:
            del turns[: len(turns) - max_turns]
        # 再按 token 预算
        budget = settings.short_term_token_budget
        total = 0
        keep: list[Turn] = []
        for turn in reversed(turns):
            cost = _approx_tokens(turn.text)
            if total + cost > budget and keep:
                break
            total += cost
            keep.append(turn)
        keep.reverse()
        turns[:] = keep

    # ------------------------------------------------------------------ #
    # 事件记忆写入（白名单 + 来源 + 可信度 + 可删除）
    # ------------------------------------------------------------------ #
    def write_event(
        self,
        *,
        kind: str,
        text: str,
        owner: str,
        player_id: str,
        source: str = "agent",
        confidence: float = 0.7,
        subject: str = "",
        tags: Optional[list[str]] = None,
        deletable: bool = True,
    ) -> Optional[MemoryEvent]:
        if kind not in WRITE_WHITELIST:
            self.rejected_writes.append({"kind": kind, "text": text, "reason": "不在写入白名单"})
            return None
        if not text.strip():
            return None
        event = MemoryEvent(
            subject=subject or WRITE_WHITELIST[kind],
            kind=kind,
            text=text.strip(),
            owner=owner,
            player_id=player_id,
            source=source,
            confidence=confidence,
            tags=tags or [],
            deletable=deletable,
        )
        return repository.add_memory(event)

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    def recall(
        self,
        query: str,
        *,
        owner: str = "",
        player_id: str = "",
        top_k: Optional[int] = None,
        now: Optional[float] = None,
    ) -> list[MemoryEvent]:
        top_k = top_k or settings.recall_top_k
        now = now or time.time()
        qgrams = _bigrams(query)
        hits: list[RecallHit] = []
        for ev in repository.memory_events:
            if owner and ev.owner not in (owner, ""):
                continue
            if player_id and ev.player_id not in (player_id, ""):
                continue
            grams = _bigrams(ev.text + ev.subject)
            overlap = len(qgrams & grams)
            union = len(qgrams | grams) or 1
            lexical = overlap / union
            # 时间衰减：越久越淡（半衰期 7 天）
            age_days = max(0.0, (now - ev.timestamp) / 86400.0)
            recency = 0.5 ** (age_days / 7.0)
            score = (lexical * 0.75 + recency * 0.25) * (0.5 + 0.5 * ev.confidence)
            if lexical > 0 or recency > 0.8:
                hits.append(RecallHit(event=ev, score=round(score, 4)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return [h.event for h in hits[:top_k]]

    # ------------------------------------------------------------------ #
    # 摘要（文档 §3.4：通过摘要任务压缩历史）
    # ------------------------------------------------------------------ #
    def summarize(self, npc_id: str, player_id: str, instance_id: str) -> str:
        turns = repository.session(npc_id, player_id, instance_id)
        if not turns:
            return ""
        player_lines = [t.text for t in turns if t.role == "player"][-4:]
        npc_lines = [t.text for t in turns if t.role == "npc"][-4:]
        parts = []
        if player_lines:
            parts.append("玩家近期提到：" + "；".join(_clip(x, 24) for x in player_lines))
        if npc_lines:
            parts.append("你的回应要点：" + "；".join(_clip(x, 24) for x in npc_lines))
        return " ".join(parts)

    # ------------------------------------------------------------------ #
    # 删除（文档 §7 隐私：支持删除）
    # ------------------------------------------------------------------ #
    def forget(self, memory_id: str) -> bool:
        return repository.delete_memory(memory_id)

    def list_events(self, owner: str = "", player_id: str = "") -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in repository.find_memories(owner, player_id)]

    def stats(self) -> dict[str, Any]:
        return {
            "total": len(repository.memory_events),
            "rejected_writes": len(self.rejected_writes),
            "whitelist": sorted(WRITE_WHITELIST),
        }


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


memory_service = MemoryService()
