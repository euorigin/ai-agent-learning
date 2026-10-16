"""REST 接口（文档 §5.2 主要接口）。

| 接口 | 方法 | 用途 |
|---|---|---|
| /api/npcs/{id}/chat | POST | 处理玩家与 NPC 的对话 |
| /api/npcs/{id}/perceive | POST | 提交事件并更新 NPC 感知 |
| /api/npcs/{id}/plan | POST | 生成或刷新行动计划 |
| /api/tools/{name}/execute | POST | 由 Agent 申请受控游戏工具 |
| /api/relationships | GET/PATCH | 查询或由规则服务更新关系 |
| /api/content/npc-profiles | CRUD | 策划配置、审核和发布角色卡 |
| /api/evaluations/replay | POST | 回放轨迹并执行评测 |

另附诊断与运营接口（观测、记忆删除、工具/角色熔断、Markdown 导出）。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from ..agent.orchestrator import orchestrator
from ..config import settings
from ..content import content_service
from ..evaluation import evaluation_service
from ..memory import memory_service
from ..schemas import (
    ChatRequest,
    ChatResponse,
    PerceiveRequest,
    PlanRequest,
    RelationshipPatch,
    ReplayRequest,
    ToolExecuteRequest,
)
from ..store import repository
from ..tools import tool_registry
from ..world import STAGE_THRESHOLDS, world_state

router = APIRouter(prefix="/api", tags=["npc-agent"])


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "ts": time.time(), "version": "1.0.0"}


@router.get("/runtime")
def runtime() -> dict[str, Any]:
    return {
        **orchestrator.runtime_info(),
        "counters": repository.counters(),
        "unknown_note": "tokens 与成本仅在接入真实大模型后统计",
    }


@router.get("/npcs")
def list_npcs() -> dict[str, Any]:
    return {"npcs": content_service.list_public()}


@router.get("/tools")
def list_tools(npc_id: str = Query(default="")) -> dict[str, Any]:
    card = content_service.get(npc_id) if npc_id else None
    return {
        "tools": tool_registry.available_for(card) if card else [
            {"name": s.name, "category": s.category, "read_only": s.read_only,
             "permissions": s.permissions, "requires_confirmation": s.requires_confirmation,
             "description": s.description}
            for s in tool_registry.specs()
        ],
        "openai_schemas": tool_registry.openai_schemas(card),
        "stats": tool_registry.stats(),
    }


@router.get("/world")
def get_world() -> dict[str, Any]:
    return world_state.snapshot()


# --------------------------------------------------------------------------- #
# 对话 / 感知 / 规划（文档 §5.2）
# --------------------------------------------------------------------------- #
@router.post("/npcs/{npc_id}/chat", response_model=ChatResponse)
def chat(npc_id: str, req: ChatRequest) -> ChatResponse:
    req.npc_id = npc_id or req.npc_id
    if not req.npc_id.strip():
        raise HTTPException(status_code=422, detail="npc_id 缺失：路径与请求体至少要提供一处")
    if not req.message.strip():
        raise HTTPException(status_code=422, detail="message 不能为空")
    return orchestrator.chat(req)


@router.post("/npcs/{npc_id}/perceive")
def perceive(npc_id: str, req: PerceiveRequest) -> dict[str, Any]:
    req.npc_id = req.npc_id or npc_id
    req.event.setdefault("npc_id", npc_id)
    return orchestrator.perceive(req)


@router.post("/npcs/{npc_id}/plan")
def plan(npc_id: str, req: PlanRequest) -> dict[str, Any]:
    node = orchestrator.plan(req, npc_id)
    return {"npc_id": npc_id, "plan": node.model_dump(mode="json")}


@router.post("/tools/{name}/execute")
def execute_tool(name: str, req: ToolExecuteRequest) -> dict[str, Any]:
    result = orchestrator.execute_tool(name, req)
    return result.model_dump(mode="json")


@router.get("/tools/{name}/spec")
def tool_spec(name: str) -> dict[str, Any]:
    spec = tool_registry.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知工具：{name}")
    return spec.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# 关系（文档 §5.2：查询或由规则服务更新）
# --------------------------------------------------------------------------- #
@router.get("/relationships")
def list_relationships(player_id: str = Query(default=""), npc_id: str = Query(default="")) -> dict[str, Any]:
    out = []
    for (pid, nid), rel in repository.relationships.items():
        if player_id and pid != player_id:
            continue
        if npc_id and nid != npc_id:
            continue
        out.append(rel.model_dump(mode="json"))
    return {"relationships": out, "stage_thresholds": STAGE_THRESHOLDS}


@router.patch("/relationships")
def patch_relationship(
    player_id: str = Query(...), npc_id: str = Query(...), patch: RelationshipPatch = Body(...)
) -> dict[str, Any]:
    """运营手动调整关系：仍然写入事件记录，保留可追溯的变更原因（文档 §3.6）。"""
    rel = repository.relationship(player_id, npc_id)
    before = rel.model_dump(mode="json")
    for field_name in ("trust", "affinity", "debt", "fear"):
        value = getattr(patch, field_name)
        if value is None:
            continue
        old = float(getattr(rel, field_name))
        new = _clamp(field_name, float(value))
        setattr(rel, field_name, new)
        rel.events.append({"event": "ops_override", "reason": patch.reason,
                           "field": field_name, "before": old, "after": new, "ts": time.time()})
    from ..world.rules import stage_of

    rel.stage = stage_of(rel.trust)
    rel.updated_at = time.time()
    repository.add_audit({"kind": "relationship_patch", "player_id": player_id,
                          "npc_id": npc_id, "reason": patch.reason,
                          "before": before, "after": rel.model_dump(mode="json"),
                          "ts": time.time()})
    return {"ok": True, "relationship": rel.model_dump(mode="json")}


def _clamp(field_name: str, value: float) -> float:
    from ..world.rules import LIMITS

    low, high = LIMITS.get(field_name, (-1e9, 1e9))
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# 内容：角色卡 CRUD / 审核 / 回滚 / 停用（文档 §5.2 + §3.9 运营层）
# --------------------------------------------------------------------------- #
@router.get("/content/npc-profiles")
def list_profiles() -> dict[str, Any]:
    return {"profiles": [c.model_dump(mode="json") for c in content_service.list_cards()],
            "versions": {k: [v.get("version") for v in vs]
                         for k, vs in content_service.versions.items()}}


@router.post("/content/npc-profiles")
def publish_profile(card: dict[str, Any] = Body(...), approver: str = Query(default="ops")) -> dict[str, Any]:
    try:
        published = content_service.publish(card, approver=approver)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"角色卡校验失败：{exc}") from exc
    return {"ok": True, "profile": published.model_dump(mode="json")}


@router.post("/content/npc-profiles/{card_id}/rollback")
def rollback_profile(card_id: str, version: str = Query(default="")) -> dict[str, Any]:
    try:
        card = content_service.rollback(card_id, version or None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "profile": card.model_dump(mode="json")}


@router.post("/content/npc-profiles/{card_id}/disable")
def disable_profile(card_id: str, disabled: bool = Query(default=True)) -> dict[str, Any]:
    try:
        enabled = content_service.disable(card_id, disabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"未知角色卡：{card_id}") from exc
    return {"ok": True, "card_id": card_id, "enabled": enabled}


# --------------------------------------------------------------------------- #
# 运营熔断：禁用工具 / 禁用角色
# --------------------------------------------------------------------------- #
@router.post("/ops/tools/{name}/disable")
def disable_tool(name: str, disabled: bool = Query(default=True)) -> dict[str, Any]:
    if tool_registry.get(name) is None:
        raise HTTPException(status_code=404, detail=f"未知工具：{name}")
    if disabled:
        orchestrator.disabled_tools.add(name)
    else:
        orchestrator.disabled_tools.discard(name)
    repository.add_audit({"kind": "ops_tool_toggle", "tool": name, "disabled": disabled,
                          "ts": time.time()})
    return {"ok": True, "disabled_tools": sorted(orchestrator.disabled_tools)}


@router.post("/ops/npcs/{npc_id}/disable")
def disable_npc(npc_id: str, disabled: bool = Query(default=True)) -> dict[str, Any]:
    if content_service.get(npc_id) is None:
        raise HTTPException(status_code=404, detail=f"未知 NPC：{npc_id}")
    if disabled:
        orchestrator.disabled_npcs.add(npc_id)
    else:
        orchestrator.disabled_npcs.discard(npc_id)
    repository.add_audit({"kind": "ops_npc_toggle", "npc_id": npc_id, "disabled": disabled,
                          "ts": time.time()})
    return {"ok": True, "disabled_npcs": sorted(orchestrator.disabled_npcs)}


# --------------------------------------------------------------------------- #
# 记忆（文档 §7 隐私：支持查询与删除）
# --------------------------------------------------------------------------- #
@router.get("/memories")
def list_memories(owner: str = Query(default=""), player_id: str = Query(default="")) -> dict[str, Any]:
    return {"memories": memory_service.list_events(owner, player_id),
            "stats": memory_service.stats()}


@router.delete("/memories/{memory_id}")
def forget_memory(memory_id: str) -> dict[str, Any]:
    ok = memory_service.forget(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在或不可删除")
    return {"ok": True, "deleted": memory_id}


# --------------------------------------------------------------------------- #
# 观测：轨迹与审计（文档 §7 可观测）
# --------------------------------------------------------------------------- #
@router.get("/traces")
def list_traces(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    traces = repository.traces[-limit:]
    return {"total": len(repository.traces),
            "traces": [t.model_dump(mode="json") for t in reversed(traces)]}


@router.get("/audit")
def list_audit(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    return {"total": len(repository.audit), "entries": repository.audit[-limit:][::-1]}


# --------------------------------------------------------------------------- #
# 评测（文档 §5.2 / §8）
# --------------------------------------------------------------------------- #
@router.get("/evaluations/scenarios")
def list_scenarios() -> dict[str, Any]:
    return {"scenarios": evaluation_service.list_scenarios()}


@router.post("/evaluations/replay")
def replay(req: ReplayRequest | None = Body(default=None)) -> dict[str, Any]:
    req = req or ReplayRequest()
    # 默认固定用演示脑回放，不跟随 .env 引擎：
    # ① 评测要求可复现，真实模型有随机性且 9 个场景串行要跑 60s+，会撞客户端超时；
    # ② 回放本身有 repository/world 快照-还原，不污染线上状态。
    # 需要看真模型表现时，显式传 {"engine": "llm"}。
    return evaluation_service.replay(
        scenario_ids=req.scenario_ids, npc_id=req.npc_id,
        engine=req.engine or "demo",
    )


@router.get("/evaluations/metrics")
def metrics() -> dict[str, Any]:
    return evaluation_service.metrics()


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
@router.get("/export/markdown")
def export_markdown(player_id: str = Query(default="p_001")) -> dict[str, Any]:
    """把当前世界、关系、任务与最近轨迹导成 Markdown（便于复盘与交付）。"""
    world = world_state.snapshot()
    lines: list[str] = ["# 灰港 · NPC Agent 运行快照", ""]
    lines.append(f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 引擎：{orchestrator.engine}（degraded={orchestrator.degraded}）")
    lines.append("")
    lines.append("## 世界")
    for iid, env in world["environment"].items():
        lines.append(f"- {env['name']}（{iid}）：{env['time']} / {env['weather']}")
    for iid, gates in world["gates"].items():
        for gid, g in gates.items():
            lines.append(f"- 门禁 {g.get('name', gid)}：{'开启' if g.get('open') else '闭闸'}（{g.get('note', '')}）")
    lines.append("")
    lines.append("## 玩家")
    p = world["players"].get(player_id, {})
    lines.append(f"- {p.get('name', player_id)}：银币 {p.get('gold')}，背包 {p.get('inventory')}")
    lines.append("")
    lines.append("## 关系")
    for (pid, nid), rel in repository.relationships.items():
        if pid != player_id:
            continue
        lines.append(f"- {nid}：阶段 {rel.stage}，信任 {rel.trust:.0f}，好感 {rel.affinity:.0f}")
    lines.append("")
    lines.append("## 任务")
    for (qid, pid), st in repository.quest_states.items():
        if pid != player_id:
            continue
        qdef = world_state.quest_def(qid)
        stage = qdef.stages[st.stage] if qdef and st.stage < len(qdef.stages) else "-"
        lines.append(f"- {qid} {qdef.title if qdef else ''}：{st.status} / {stage}")
    lines.append("")
    lines.append("## 最近轨迹")
    for t in repository.traces[-10:][::-1]:
        tools = ", ".join(c.get("tool", "") for c in t.tool_calls) or "-"
        lines.append(f"- [{t.intent}] {t.user_message[:24]} → 工具 {tools}"
                     f"（{t.latency_ms:.0f}ms，engine={t.engine}）")
    markdown = "\n".join(lines) + "\n"
    return {"markdown": markdown, "engine": settings.resolved_engine()}
