"""世界状态服务（World State Service）。

文档 §4.1：提供世界和玩家权威状态；§10 核心原则：
    「模型负责理解和提出决策，游戏服务负责确认事实和修改状态。」

因此本模块是全系统**唯一**能改游戏核心数据（金币、物品、任务、门禁、区域）的地方。
工具只能提交"申请"（state_delta 草案），由 `apply_state_delta` 校验后落地，
校验失败一律返回具体错误，绝不静默放行（文档 §3.7）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..schemas import NpcRuntimeState, PlayerState, QuestDef, QuestState
from ..store import repository


@dataclass
class DeltaResult:
    """权威写入结果。"""

    success: bool
    applied: dict[str, Any] = field(default_factory=dict)
    rejected: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class WorldStateService:
    def __init__(self, world: dict[str, Any]) -> None:
        self.instances: dict[str, Any] = world.get("instances", {}) or {}
        self.items: dict[str, Any] = world.get("items", {}) or {}
        self.regions: dict[str, Any] = {}
        self.gates: dict[str, dict[str, Any]] = {}
        for iid, inst in self.instances.items():
            self.regions[iid] = inst.get("regions", {}) or {}
            self.gates[iid] = inst.get("gates", {}) or {}

        self.quest_defs: dict[str, QuestDef] = {
            q["id"]: QuestDef(**q) for q in (world.get("quests", {}) or {}).values()
        }
        self.nearby_map: dict[str, list[str]] = world.get("nearby", {}) or {}
        self.market: dict[str, float] = {}          # 行情系数，默认 1.0
        self.npc_gold: dict[str, int] = {}          # NPC 手头银币

        self._bootstrap(world)

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _bootstrap(self, world: dict[str, Any]) -> None:
        for pid, p in (world.get("players", {}) or {}).items():
            repository.players[pid] = PlayerState(**p)
        for npc_id, n in (world.get("npcs", {}) or {}).items():
            state = NpcRuntimeState(npc_id=npc_id, **n)
            repository.npc_states[npc_id] = state
            self.npc_gold.setdefault(npc_id, 500)

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def player(self, player_id: str) -> PlayerState:
        p = repository.players.get(player_id)
        if p is None:
            # 新玩家自动登记，避免未知身份进入核心流程
            p = PlayerState(player_id=player_id)
            repository.players[player_id] = p
        return p

    def npc_state(self, npc_id: str, instance_id: str = "world_01") -> NpcRuntimeState:
        s = repository.npc_states.get(npc_id)
        if s is None:
            s = NpcRuntimeState(npc_id=npc_id, instance_id=instance_id)
            repository.npc_states[npc_id] = s
        return s

    def environment(self, instance_id: str = "world_01") -> dict[str, Any]:
        inst = self.instances.get(instance_id, {})
        return {
            "instance": instance_id,
            "name": inst.get("name", ""),
            "time": inst.get("time", ""),
            "weather": inst.get("weather", ""),
            "region": None,
        }

    def region_info(self, instance_id: str, region: str) -> Optional[dict[str, Any]]:
        return (self.regions.get(instance_id, {}) or {}).get(region)

    def gate(self, instance_id: str, gate_id: str) -> Optional[dict[str, Any]]:
        return (self.gates.get(instance_id, {}) or {}).get(gate_id)

    def is_gate_open(self, instance_id: str, gate_id: str) -> bool:
        g = self.gate(instance_id, gate_id)
        return bool(g and g.get("open"))

    def nearby(self, npc_id: str, instance_id: str = "world_01") -> list[dict[str, Any]]:
        """局部环境（文档 §3.3）：受距离/视线/阵营/实例限制。

        这里用预先配置的邻接表模拟「同一场景可见」，
        并且严格限定在同一 instance 内。
        """
        out: list[dict[str, Any]] = []
        for other in self.nearby_map.get(npc_id, []) or []:
            st = repository.npc_states.get(other)
            if st is None or st.instance_id != instance_id:
                continue
            out.append({
                "id": other,
                "location": st.location,
                "action_state": st.action_state,
                "visibility": "seen",
                "confidence": 1.0,
            })
        # 玩家也属于局部环境
        for pid, p in repository.players.items():
            if p.location:
                out.append({
                    "id": pid,
                    "location": p.location,
                    "action_state": "present",
                    "visibility": "seen",
                    "confidence": 1.0,
                })
        return out

    def price(self, item_id: str) -> Optional[int]:
        item = self.items.get(item_id)
        if not item:
            return None
        return int(round(item["base_price"] * self.market.get(item_id, 1.0)))

    def item_name(self, item_id: str) -> str:
        return (self.items.get(item_id) or {}).get("name", item_id)

    def has_item(self, item_id: str) -> bool:
        return item_id in self.items

    def inventory(self, player_id: str) -> dict[str, int]:
        return dict(self.player(player_id).inventory)

    def npc_resources(self, npc_id: str) -> dict[str, Any]:
        return dict(self.npc_state(npc_id).resources)

    # ------------------------------------------------------------------ #
    # 任务（文档 §3.4：任务状态是权威事实，由任务服务维护）
    # ------------------------------------------------------------------ #
    def quest_def(self, quest_id: str) -> Optional[QuestDef]:
        return self.quest_defs.get(quest_id)

    def quest_defs_by_giver(self, npc_id: str) -> list[QuestDef]:
        return [q for q in self.quest_defs.values() if q.giver == npc_id]

    def quest_state(self, quest_id: str, player_id: str) -> QuestState:
        key = (quest_id, player_id)
        st = repository.quest_states.get(key)
        if st is None:
            qdef = self.quest_defs.get(quest_id)
            status = "available"
            if qdef is not None:
                rel = repository.relationship(player_id, qdef.giver)
                p = self.player(player_id)
                if rel.trust < qdef.min_trust or p.level < qdef.min_level:
                    status = "locked"
            st = QuestState(quest_id=quest_id, player_id=player_id, status=status)
            repository.quest_states[key] = st
        return st

    def quest_status_for(self, npc_id: str, player_id: str) -> list[dict[str, Any]]:
        out = []
        for qdef in self.quest_defs_by_giver(npc_id):
            st = self.quest_state(qdef.id, player_id)
            out.append({
                "quest_id": qdef.id,
                "title": qdef.title,
                "status": st.status,
                "stage": st.stage,
                "stage_name": qdef.stages[st.stage] if st.stage < len(qdef.stages) else "已完成",
                "summary": qdef.summary,
                "location": qdef.location,
                "rewards": qdef.rewards,
                "min_trust": qdef.min_trust,
            })
        return out

    # ------------------------------------------------------------------ #
    # 权威写入（唯一入口）
    # ------------------------------------------------------------------ #
    def apply_state_delta(
        self,
        delta: dict[str, Any],
        *,
        actor: str,
        source: str,
        instance_id: str = "world_01",
        player_id: str = "p_001",
    ) -> DeltaResult:
        """校验并落地状态变更。

        delta 支持：
            player_id / gold / items / npc_items / quests / gates / events
        """
        pid = delta.get("player_id", player_id)
        player = self.player(pid)
        applied: dict[str, Any] = {}
        rejected: dict[str, Any] = {}
        errors: list[str] = []
        events: list[dict[str, Any]] = []

        # ---- 金币：不允许透支 ----
        if "gold" in delta:
            amount = int(delta["gold"])
            if player.gold + amount < 0:
                errors.append(f"银币不足：现有 {player.gold}，本次需要 {abs(amount)}")
                rejected["gold"] = amount
            else:
                player.gold += amount
                applied["gold"] = amount
                events.append({"type": "gold_changed", "player_id": pid, "delta": amount,
                               "after": player.gold})

        # ---- 玩家背包 ----
        if "items" in delta:
            for item_id, qty in (delta["items"] or {}).items():
                qty = int(qty)
                if not self.has_item(item_id):
                    errors.append(f"未知物品：{item_id}")
                    rejected.setdefault("items", {})[item_id] = qty
                    continue
                cur = player.inventory.get(item_id, 0)
                if cur + qty < 0:
                    errors.append(f"{self.item_name(item_id)} 数量不足：现有 {cur}，需要 {abs(qty)}")
                    rejected.setdefault("items", {})[item_id] = qty
                    continue
                new = cur + qty
                if new == 0:
                    player.inventory.pop(item_id, None)
                else:
                    player.inventory[item_id] = new
                applied.setdefault("items", {})[item_id] = qty

        # ---- NPC 库存 ----
        if "npc_items" in delta:
            for npc_id, changes in (delta["npc_items"] or {}).items():
                st = self.npc_state(npc_id, instance_id)
                for item_id, qty in (changes or {}).items():
                    qty = int(qty)
                    cur = int(st.resources.get(item_id, 0))
                    if cur + qty < 0:
                        errors.append(f"{npc_id} 的 {self.item_name(item_id)} 不足")
                        rejected.setdefault("npc_items", {}).setdefault(npc_id, {})[item_id] = qty
                        continue
                    st.resources[item_id] = cur + qty
                    applied.setdefault("npc_items", {}).setdefault(npc_id, {})[item_id] = qty

        # ---- NPC 手头银币 ----
        if "npc_gold" in delta:
            for npc_id, amount in (delta["npc_gold"] or {}).items():
                amount = int(amount)
                cur = self.npc_gold.get(npc_id, 0)
                if cur + amount < 0:
                    errors.append(f"{npc_id} 的银币不足以支付")
                    rejected.setdefault("npc_gold", {})[npc_id] = amount
                    continue
                self.npc_gold[npc_id] = cur + amount
                applied.setdefault("npc_gold", {})[npc_id] = amount

        # ---- 任务 ----
        if "quests" in delta:
            for change in delta["quests"] or []:
                qid = change.get("quest_id")
                qdef = self.quest_defs.get(qid)
                if qdef is None:
                    errors.append(f"未知任务：{qid}")
                    continue
                st = self.quest_state(qid, pid)
                if st.status == "completed" and change.get("status") != "completed":
                    errors.append(f"任务 {qid} 已完结，不可回退")
                    continue
                if "stage" in change:
                    stage = int(change["stage"])
                    if not (0 <= stage < len(qdef.stages)):
                        errors.append(f"任务 {qid} 阶段越界：{stage}")
                        continue
                    st.stage = stage
                if "status" in change:
                    st.status = change["status"]
                st.version += 1
                st.updated_at = time.time()
                applied.setdefault("quests", []).append({
                    "quest_id": qid, "stage": st.stage, "status": st.status,
                    "stage_name": qdef.stages[st.stage] if st.stage < len(qdef.stages) else "",
                })
                # 任务完成 → 发放奖励（同样走权威写入）
                if change.get("status") == "completed":
                    rewards = qdef.rewards or {}
                    if rewards.get("gold"):
                        player.gold += int(rewards["gold"])
                        applied.setdefault("rewards", {})["gold"] = int(rewards["gold"])
                    for item_id, qty in (rewards.get("items") or {}).items():
                        player.inventory[item_id] = player.inventory.get(item_id, 0) + int(qty)
                        applied.setdefault("rewards", {}).setdefault("items", {})[item_id] = int(qty)
                events.append({"type": "quest_updated", "player_id": pid,
                               "quest_id": qid, "status": st.status, "stage": st.stage})

        # ---- 门禁 / 世界开关 ----
        if "gates" in delta:
            for gate_id, is_open in (delta["gates"] or {}).items():
                g = self.gate(instance_id, gate_id)
                if g is None:
                    errors.append(f"未知门禁：{gate_id}")
                    continue
                g["open"] = bool(is_open)
                applied.setdefault("gates", {})[gate_id] = bool(is_open)
                events.append({"type": "gate_changed", "gate_id": gate_id, "open": bool(is_open)})

        for ev in delta.get("events", []) or []:
            events.append(ev)

        for ev in events:
            ev.setdefault("actor", actor)
            ev.setdefault("source", source)
            ev.setdefault("ts", time.time())
            repository.add_event(ev)

        repository.add_audit({
            "kind": "state_delta",
            "actor": actor,
            "source": source,
            "player_id": pid,
            "applied": applied,
            "rejected": rejected,
            "errors": errors,
            "ts": time.time(),
        })

        return DeltaResult(
            success=not errors,
            applied=applied,
            rejected=rejected,
            errors=errors,
            events=events,
        )

    # ------------------------------------------------------------------ #
    # 世界事件（由游戏服务端推送，文档 §3.3 事件订阅）
    # ------------------------------------------------------------------ #
    def set_environment(self, instance_id: str, **kwargs: Any) -> dict[str, Any]:
        inst = self.instances.setdefault(instance_id, {})
        changed: dict[str, Any] = {}
        for key in ("time", "weather"):
            if key in kwargs and kwargs[key]:
                inst[key] = kwargs[key]
                changed[key] = kwargs[key]
                repository.add_event({"type": "environment_changed", "instance": instance_id,
                                      "field": key, "value": kwargs[key], "ts": time.time(),
                                      "actor": "world_event", "source": "perceive"})
        return changed

    def move_npc(self, npc_id: str, location: str, instance_id: str = "world_01") -> dict[str, Any]:
        if not location:
            return {}
        st = self.npc_state(npc_id, instance_id)
        before = st.location
        st.location = location
        st.action_state = "moving"
        repository.add_event({"type": "npc_moved", "npc_id": npc_id, "from": before,
                              "to": location, "ts": time.time(),
                              "actor": "world_event", "source": "perceive"})
        return {"npc_id": npc_id, "from": before, "to": location}

    # ------------------------------------------------------------------ #
    # 快照 / 恢复（评测回放隔离）
    # ------------------------------------------------------------------ #
    def capture(self) -> dict[str, Any]:
        import copy

        return {
            "instances": copy.deepcopy(self.instances),
            "gates": copy.deepcopy(self.gates),
            "regions": copy.deepcopy(self.regions),
            "market": dict(self.market),
            "npc_gold": dict(self.npc_gold),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.instances = snap["instances"]
        self.gates = snap["gates"]
        self.regions = snap["regions"]
        self.market = dict(snap["market"])
        self.npc_gold = dict(snap["npc_gold"])

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        players = {k: v.model_dump(mode="json") for k, v in repository.players.items()}
        npcs = {k: v.model_dump(mode="json") for k, v in repository.npc_states.items()}
        quests = {}
        for (qid, pid), st in repository.quest_states.items():
            quests[f"{qid}|{pid}"] = st.model_dump(mode="json")
        return {
            "environment": {iid: self.environment(iid) for iid in self.instances},
            "regions": self.regions,
            "gates": self.gates,
            "prices": {iid: self.price(iid) for iid in self.items},
            "npc_gold": self.npc_gold,
            "players": players,
            "npcs": npcs,
            "quests": quests,
            "events": repository.event_log[-30:],
        }
