"""API 层：REST 接口。所有对外结构都显式 model_dump(mode="json")，避免前端猜字段。"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException

from ..agent.orchestrator import Orchestrator, trip_text
from ..llm import LLMError
from ..models import ChatRequest, TripRequest
from ..settings import SETTINGS

router = APIRouter(prefix="/api", tags=["travel"])
_lock = threading.Lock()
_engine: Orchestrator | None = None


def get_engine() -> Orchestrator:
    global _engine
    with _lock:
        if _engine is None:
            _engine = Orchestrator()
        return _engine


def reset_engine() -> None:
    """配置变更后重建（供 /api/reload 使用）。"""
    global _engine
    with _lock:
        _engine = None


# --------------------------------------------------------------------------- #
@router.get("/status")
def status() -> dict:
    return get_engine().status()


@router.post("/reload")
def reload_config() -> dict:
    reset_engine()
    eng = get_engine()
    return {"ok": True, "status": eng.status()}


@router.get("/tools")
def tools() -> dict:
    eng = get_engine()
    return {"count": len(eng.registry.specs()), "tools": eng.registry.specs()}


@router.get("/examples")
def examples() -> dict:
    today = date.today()
    start = (today + timedelta(days=7)).isoformat()
    return {"examples": [
        "从北京出发，下周五去成都玩4天，预算6000，喜欢美食和自然风光",
        f"{start} 去西安 3 天，带父母，节奏轻松一点",
        "去杭州玩5天，想逛博物馆和老街，人均预算4000",
        "我想去大理和丽江，7天，喜欢摄影和小众的地方",
        "帮我规划一趟青海湖4日自驾，预算8000",
        "去东京玩5天，喜欢美食和购物，预算12000",
    ]}


@router.post("/plan")
def plan(payload: dict[str, Any]) -> dict:
    """结构化生成行程（前端表单走这个接口，跳过 NLU）。"""
    try:
        request = TripRequest.model_validate(payload)
    except Exception as exc:                        # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"参数不合法：{exc}") from exc
    eng = get_engine()
    try:
        trip, traces = eng.plan(request)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"大模型调用失败：{exc}") from exc
    except Exception as exc:                        # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"规划失败：{type(exc).__name__}: {exc}") from exc
    return {"trip": trip.model_dump(mode="json"), "trace": [t.model_dump() for t in traces],
            "overview": eng.overview(trip)}


@router.post("/chat")
def chat(payload: dict[str, Any]) -> dict:
    try:
        req = ChatRequest.model_validate(payload)
    except Exception as exc:                        # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"参数不合法：{exc}") from exc
    eng = get_engine()
    try:
        resp = eng.chat(req)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"大模型调用失败：{exc}") from exc
    except Exception as exc:                        # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"对话失败：{type(exc).__name__}: {exc}") from exc
    data = resp.model_dump(mode="json")
    return data


@router.get("/trips")
def list_trips() -> dict:
    return {"trips": get_engine().store.list()}


@router.get("/trips/{trip_id}")
def get_trip(trip_id: str) -> dict:
    trip = get_engine().store.get(trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="行程不存在")
    return {"trip": trip.model_dump(mode="json"), "text": trip_text(trip)}


@router.delete("/trips/{trip_id}")
def delete_trip(trip_id: str) -> dict:
    ok = get_engine().store.delete(trip_id)
    if not ok:
        raise HTTPException(status_code=404, detail="行程不存在")
    return {"ok": True}


@router.get("/trips/{trip_id}/export")
def export_trip(trip_id: str) -> dict:
    """导出为可粘贴的纯文本攻略。"""
    trip = get_engine().store.get(trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="行程不存在")
    return {"trip_id": trip_id, "markdown": render_markdown(trip, get_engine())}


# --------------------------------------------------------------------------- #
def render_markdown(trip, eng: Orchestrator) -> str:
    req = trip.request
    lines = [f"# {('、'.join(req.destinations))} {len(trip.days)} 日行程",
             "",
             f"- 出发地：{req.origin or '未填写'}",
             f"- 日期：{req.start_date} ~ {req.end_date}",
             f"- 人数：{req.travelers} 人 / {req.rooms} 间房",
             f"- 节奏：{req.pace}　偏好：{'、'.join(req.preferences) or '未指定'}",
             f"- 生成引擎：{trip.engine}",
             ""]
    if trip.intercity:
        lines.append("## 城际交通")
        for leg in trip.intercity:
            lines.append(f"- {leg.label}：约 {leg.minutes} 分钟，"
                         f"¥{leg.cost_low:.0f}~{leg.cost_high:.0f}（{leg.note}）")
        lines.append("")
    for day in trip.days:
        wx = day.weather
        lines.append(f"## {day.date}（{day.weekday}）{day.city}")
        if wx:
            lines.append(f"天气：{wx.condition} {wx.temp_low:.0f}~{wx.temp_high:.0f}℃  "
                         f"降雨概率 {wx.rain_prob}%　来源：{wx.source}/{wx.basis}")
        for item in day.items:
            trans = (f"｜{item.transport.label} {item.transport.minutes} 分钟"
                     f"（{item.transport.distance_km}km，来源 {item.transport.source}）"
                     if item.transport else "")
            lines.append(f"- {item.arrive}-{item.leave}　**{item.name}**（{item.category}，"
                         f"{item.stay_minutes} 分钟）{trans}")
            if item.reason:
                lines.append(f"    - 推荐理由：{item.reason}")
            if item.risk:
                lines.append(f"    - 风险提示：{item.risk}")
        if day.notes:
            lines.append(f"- 备注：{'；'.join(day.notes)}")
        if day.walking_km:
            lines.append(f"- 步行约 {day.walking_km}km")
        lines.append("")
    if trip.budget:
        lines.append("## 预算估算（元）")
        lines.append("| 项目 | 下限 | 上限 | 依据 |")
        lines.append("| --- | ---: | ---: | --- |")
        for line in trip.budget.lines:
            lines.append(f"| {line.category} | {line.low:.0f} | {line.high:.0f} | {line.basis} |")
        lines.append(f"| **合计** | **{trip.budget.total_low:.0f}** | "
                     f"**{trip.budget.total_high:.0f}** | {trip.budget.verdict} |")
        for advice in trip.budget.advice:
            lines.append(f"- {advice}")
        lines.append("")
    if trip.evidence:
        lines.append("## 数据来源")
        for ev in trip.evidence:
            lines.append(f"- {ev}")
        lines.append("")
    if trip.warnings:
        lines.append("## 提示与风险")
        for w in trip.warnings:
            lines.append(f"- {w}")
        lines.append("")
    lines.append("> 本行程由「旅行攻略 Agent 助手」生成；价格与开放信息为区间估算，"
                 "出行前请以官方渠道为准。")
    return "\n".join(lines)
