"""规划辅助工具：开放时间校验、候选打分、预算汇总、行程可信校验。

这些工具把「算数」和「查证」从编排层拆出来，既方便单测，也让每个结论都能
追溯到具体来源（evidence）。
"""
from __future__ import annotations

from typing import Any, Iterable

from ..llm import LLMError
from ..models import (BudgetPlan, DayPlan, OpeningInfo, Poi, ToolResult, Trip, TripRequest)
from ..prompts import OPENING_SYSTEM, opening_user
from .base import ToolContext, call_llm_json, clamp_float, fail, ok


# --------------------------------------------------------------------------- #
def _resolve_pois(ctx: ToolContext, items: Iterable[Any]) -> list[Poi]:
    out: list[Poi] = []
    for item in items or []:
        if isinstance(item, Poi):
            out.append(item)
        elif isinstance(item, dict):
            try:
                out.append(Poi.model_validate(item))
            except Exception:                       # noqa: BLE001
                continue
        elif isinstance(item, str):
            poi = ctx.poi_cache.get(item)
            if poi is None:
                poi = next((p for p in ctx.poi_cache.values() if p.name == item), None)
            if poi is not None:
                out.append(poi)
    return out


def opening_check(ctx: ToolContext, *, pois: list[Any], dates: list[str]) -> ToolResult:
    """POI × 日期的开放与预约校验。先做本地星期闭馆判断，再让大模型补充。"""
    from ..agent.rules import weekday_index

    resolved = _resolve_pois(ctx, pois)
    dates = [d for d in (dates or []) if d]
    if not resolved or not dates:
        return fail("opening_check", "缺少 POI 或日期")

    items: list[OpeningInfo] = []
    remaining: list[Poi] = []
    for poi in resolved:
        local = [d for d in dates if weekday_index(d) in poi.closed_weekdays]
        for d in dates:
            if d in local:
                items.append(OpeningInfo(
                    poi_name=poi.name, date=d, open=False,
                    window=f"{poi.open}-{poi.close}" if poi.open else "",
                    closed_weekdays=poi.closed_weekdays, reservation=poi.reservation,
                    reason=f"该地点每周固定闭馆（{'/'.join(str(c + 1) for c in poi.closed_weekdays)} 不开放）",
                    source="rule", confidence=0.9,
                ))
        if not local:
            remaining.append(poi)

    model_ok = False
    if remaining and ctx.llm_ready:
        payload = {
            "pois": [p.model_dump() for p in remaining],
            "dates": dates,
        }
        try:
            data = call_llm_json(
                ctx, OPENING_SYSTEM,
                opening_user(payload["pois"], dates), max_tokens=2600, temperature=0.2)
            model_ok = True
            for entry in (data.get("items") if isinstance(data, dict) else data) or []:
                if not isinstance(entry, dict):
                    continue
                items.append(OpeningInfo(
                    poi_name=str(entry.get("poi_name") or "")[:40],
                    date=str(entry.get("date") or "")[:10],
                    open=bool(entry.get("open", True)),
                    window=str(entry.get("window") or "")[:24],
                    reservation=bool(entry.get("reservation")),
                    reason=str(entry.get("reason") or "")[:80],
                    source="llm",
                    confidence=clamp_float(entry.get("confidence"), 0.1, 1.0) or 0.5,
                ))
        except LLMError as exc:
            items_errors = [f"大模型开放信息校验失败：{exc}"]
        else:
            items_errors = []
    else:
        items_errors = ["大模型未配置，开放/预约信息仅按本地规则判断，出行前请核实"]

    if not items:
        # 大模型不可用时至少回落到「默认开放 + 需核实」
        for poi in resolved:
            for d in dates:
                items.append(OpeningInfo(poi_name=poi.name, date=d, open=True,
                                         window=f"{poi.open}-{poi.close}" if poi.open else "",
                                         reason="未获取到权威开放信息，默认按开放处理，请出行前核实",
                                         source="default", confidence=0.3))

    source = "llm+rule" if model_ok else "rule"
    result = ok("opening_check", [i.model_dump() for i in items], source=source,
                confidence=0.7 if model_ok else 0.45)
    result.errors.extend(items_errors)
    if any(not i.open for i in items):
        result.needs_confirmation = True
    return result


# --------------------------------------------------------------------------- #
def score_candidates(ctx: ToolContext, *, city: str = "", preferences: list[str] | None = None,
                     anchor: list[float] | None = None, weather: dict | None = None,
                     limit: int = 20) -> ToolResult:
    """对候选池打分排序（偏好/评分/距离/预算/天气/拥挤度 六维加权）。"""
    from ..agent.rules import score_poi
    from ..models import WeatherDay

    pool = [p for p in ctx.poi_cache.values() if not city or p.city == city or not p.city]
    if not pool:
        return fail("score_candidates", "候选池为空，请先调用 discover_pois")

    wx = None
    if isinstance(weather, dict):
        try:
            wx = WeatherDay.model_validate(weather)
        except Exception:                           # noqa: BLE001
            wx = None
    anchor_t = tuple(anchor) if anchor and len(anchor) == 2 else None

    ranked = []
    for poi in pool:
        info = score_poi(poi, preferences=preferences or [], weather=wx, anchor=anchor_t)
        ranked.append({"id": poi.id, "name": poi.name, "category": poi.category,
                       "city": poi.city, "score": info["score"], "parts": info["parts"],
                       "reason": info["reason"], "indoor": poi.indoor,
                       "precision": poi.precision})
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ok("score_candidates", ranked[:max(1, min(60, limit))], source="rule-engine",
              confidence=0.85)


# --------------------------------------------------------------------------- #
def estimate_budget(ctx: ToolContext, *, trip: dict) -> ToolResult:
    """按行程明细汇总预算（住宿/餐饮/门票/交通/机动）。"""
    from ..agent.rules import estimate_budget as _calc

    try:
        t = Trip.model_validate(trip)
    except Exception as exc:                        # noqa: BLE001
        return fail("estimate_budget", f"行程结构不合法：{exc}")
    try:
        plan: BudgetPlan = _calc(t.request, t.days, t.profiles, t.intercity)
    except Exception as exc:                        # noqa: BLE001
        return fail("estimate_budget", f"预算计算失败：{exc}")

    prof_conf = [p.confidence for p in t.profiles.values()] or [0.3]
    confidence = 0.5 + 0.35 * (sum(prof_conf) / len(prof_conf))
    return ok("estimate_budget", plan.model_dump(), source="rule-engine",
              confidence=round(min(0.9, confidence), 2))


# --------------------------------------------------------------------------- #
def verify_trip(ctx: ToolContext, *, trip: dict) -> ToolResult:
    """交付前的自检：结构一致性、时间冲突、证据完整性、坐标精度、天气覆盖。"""
    try:
        t = Trip.model_validate(trip)
    except Exception as exc:                        # noqa: BLE001
        return fail("verify_trip", f"行程结构不合法：{exc}")

    issues: list[str] = []
    stats = {"days": len(t.days), "items": 0, "with_evidence": 0,
             "approximate_coords": 0, "with_weather": 0, "total_km_walk": 0.0}

    if len(t.days) != t.request.days:
        issues.append(f"天数不一致：需求 {t.request.days} 天，实际 {len(t.days)} 天")

    for day in t.days:
        prev_leave = None
        for item in day.items:
            stats["items"] += 1
            if item.evidence:
                stats["with_evidence"] += 1
            if prev_leave is not None and item.arrive and item.arrive < prev_leave:
                issues.append(f"{day.date} 时间冲突：{item.name} 于 {item.arrive} 开始，早于上一点结束 {prev_leave}")
            prev_leave = item.leave or prev_leave
        stats["total_km_walk"] += day.walking_km
        if day.weather and day.weather.basis != "unavailable":
            stats["with_weather"] += 1
        if not day.items:
            issues.append(f"{day.date} 无任何安排")
        cities = {i.city for i in day.items if i.city}
        if len(cities) > 1:
            issues.append(f"{day.date} 跨城市安排（{'、'.join(sorted(cities))}），建议避免")

    approx = sum(1 for d in t.days for i in d.items
                 if i.lat is not None and i.evidence and any("approximate" in e for e in i.evidence))
    stats["approximate_coords"] = approx

    if t.request.mobility == "reduced" and stats["total_km_walk"] / max(1, len(t.days)) > 6:
        issues.append("行动不便者每日步行距离偏大，建议增加代步")

    if not t.budget:
        issues.append("缺少预算估算")
    if not t.evidence:
        issues.append("缺少数据来源说明")

    ok_flag = not issues
    result = ok("verify_trip", {"ok": ok_flag, "issues": issues, "stats": stats},
                source="rule-engine", confidence=0.9 if ok_flag else 0.6)
    if issues:
        result.needs_confirmation = True
    return result
