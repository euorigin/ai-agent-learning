"""仓储层（Repository 模式）。

文档 §5.1 的核心数据表在此落地为进程内仓储，并可快照到 `data/world.json`：
- npc_profiles        → content 层角色卡（由 ContentService 管理，带版本）
- npc_runtime_states  → repository.npc_states
- player_relationships→ repository.relationships
- memory_events       → repository.memory_events
- quest_states        → repository.quest_states（权威写入由 WorldStateService 完成）
- agent_traces        → repository.traces

换 PostgreSQL/Redis/pgvector 只需替换本文件实现，上层无感（文档 §6 存储层）。
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from .schemas import (
    AgentTrace,
    MemoryEvent,
    NpcRuntimeState,
    PlayerState,
    QuestState,
    Relationship,
    Turn,
)

SessionKey = tuple[str, str, str]  # (npc_id, player_id, instance_id)


class Repository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.npc_states: dict[str, NpcRuntimeState] = {}
        self.players: dict[str, PlayerState] = {}
        self.relationships: dict[tuple[str, str], Relationship] = {}
        self.quest_states: dict[tuple[str, str], QuestState] = {}
        self.memory_events: list[MemoryEvent] = []
        self.traces: list[AgentTrace] = []
        self.sessions: dict[SessionKey, list[Turn]] = defaultdict(list)
        # 工具限流窗口：key=(npc_id,player_id,tool) → 时间戳列表
        self.rate_windows: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        # 审计日志（文档 §3.7 审计 / §3.9 运营层）
        self.audit: list[dict[str, Any]] = []
        # 目录事件流（文档 §3.3 事件订阅）
        self.event_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 关系
    # ------------------------------------------------------------------ #
    def relationship(self, player_id: str, npc_id: str) -> Relationship:
        key = (player_id, npc_id)
        with self._lock:
            rel = self.relationships.get(key)
            if rel is None:
                rel = Relationship(player_id=player_id, npc_id=npc_id)
                self.relationships[key] = rel
            return rel

    def set_relationship(self, rel: Relationship) -> None:
        with self._lock:
            self.relationships[(rel.player_id, rel.npc_id)] = rel

    # ------------------------------------------------------------------ #
    # 会话短期记忆
    # ------------------------------------------------------------------ #
    def session(self, npc_id: str, player_id: str, instance_id: str) -> list[Turn]:
        return self.sessions[(npc_id, player_id, instance_id)]

    # ------------------------------------------------------------------ #
    # 记忆
    # ------------------------------------------------------------------ #
    def add_memory(self, event: MemoryEvent) -> MemoryEvent:
        with self._lock:
            self.memory_events.append(event)
        return event

    def find_memories(self, owner: str = "", player_id: str = "") -> list[MemoryEvent]:
        out = []
        for ev in self.memory_events:
            if owner and ev.owner not in (owner, ""):
                continue
            if player_id and ev.player_id not in (player_id, ""):
                continue
            out.append(ev)
        return out

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock:
            before = len(self.memory_events)
            self.memory_events = [
                ev for ev in self.memory_events
                if not (ev.id == memory_id and ev.deletable)
            ]
            return len(self.memory_events) < before

    # ------------------------------------------------------------------ #
    # 轨迹与审计
    # ------------------------------------------------------------------ #
    def add_trace(self, trace: AgentTrace) -> AgentTrace:
        with self._lock:
            self.traces.append(trace)
        return trace

    def add_audit(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.audit.append(entry)

    def add_event(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.event_log.append(entry)
        # 事件流只保留最近 200 条
        if len(self.event_log) > 200:
            del self.event_log[:-200]

    # ------------------------------------------------------------------ #
    # 限流
    # ------------------------------------------------------------------ #
    def rate_check(self, npc_id: str, player_id: str, tool: str,
                   limit: int, window: float, now: float) -> tuple[bool, int]:
        key = (npc_id, player_id, tool)
        stamps = [t for t in self.rate_windows[key] if now - t < window]
        if len(stamps) >= limit:
            self.rate_windows[key] = stamps
            return False, len(stamps)
        stamps.append(now)
        self.rate_windows[key] = stamps
        return True, len(stamps)

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        return {
            "players": {k: v.model_dump(mode="json") for k, v in self.players.items()},
            "npc_states": {k: v.model_dump(mode="json") for k, v in self.npc_states.items()},
            "relationships": {
                f"{p}|{n}": v.model_dump(mode="json")
                for (p, n), v in self.relationships.items()
            },
            "quest_states": {
                f"{q}|{p}": v.model_dump(mode="json")
                for (q, p), v in self.quest_states.items()
            },
            "memory_events": [m.model_dump(mode="json") for m in self.memory_events],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def restore_players(self, players: Iterable[PlayerState]) -> None:
        for p in players:
            self.players[p.player_id] = p

    def counters(self) -> dict[str, int]:
        return {
            "players": len(self.players),
            "npc_states": len(self.npc_states),
            "relationships": len(self.relationships),
            "quest_states": len(self.quest_states),
            "memory_events": len(self.memory_events),
            "traces": len(self.traces),
            "sessions": len(self.sessions),
            "audit": len(self.audit),
            "events": len(self.event_log),
        }

    # ------------------------------------------------------------------ #
    # 快照 / 恢复（评测回放需要隔离运行，不污染线上状态）
    # ------------------------------------------------------------------ #
    def capture(self) -> dict[str, Any]:
        import copy

        with self._lock:
            return {
                "npc_states": copy.deepcopy(self.npc_states),
                "players": copy.deepcopy(self.players),
                "relationships": copy.deepcopy(self.relationships),
                "quest_states": copy.deepcopy(self.quest_states),
                "memory_events": copy.deepcopy(self.memory_events),
                "sessions": copy.deepcopy(dict(self.sessions)),
                "rate_windows": copy.deepcopy(dict(self.rate_windows)),
                "audit_len": len(self.audit),
                "event_len": len(self.event_log),
                "traces_len": len(self.traces),
            }

    def restore(self, snap: dict[str, Any]) -> None:
        with self._lock:
            self.npc_states = snap["npc_states"]
            self.players = snap["players"]
            self.relationships = snap["relationships"]
            self.quest_states = snap["quest_states"]
            self.memory_events = snap["memory_events"]
            self.sessions = defaultdict(list, snap["sessions"])
            self.rate_windows = defaultdict(list, snap["rate_windows"])
            del self.audit[snap["audit_len"]:]
            del self.event_log[snap["event_len"]:]
            del self.traces[snap["traces_len"]:]


repository = Repository()
