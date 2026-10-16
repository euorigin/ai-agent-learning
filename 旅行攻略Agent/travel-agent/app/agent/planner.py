"""行程装配：把工具取到的数据拼成一份可交付的行程。

分工明确：
- 数据获取 → 工具层（可能失败，失败就降级并标注）
- 排序与时间轴 → rules.py（纯计算，可单测）
- 本文件只负责「编排数据流 + 记录证据 + 汇总告警」
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from ..models import (CityProfile, DayPlan, ItineraryItem, Poi, ToolResult, ToolTrace,
                      TransportLeg, Trip, TripRequest, WeatherDay)
from ..providers.base import haversine_km
from ..providers.osrm import intercity_leg
from ..tools.base import ToolContext, ToolRegistry
from . import rules

MAX_POI_PER_CITY = 34
# 单次向大模型索要的 POI 上限。实测（MiniMax M2 类推理模型）：
# 一次要 22 个会因 <think> 反复核对而超时/被截断，而 14 个稳定成功。
# 因此按「每次少量、不够再补」的策略取数，而不是一次要一大把。
LLM_POI_BATCH = 12
LLM_POI_MAX_ROUNDS = 3


@dataclass
class PlanDeps:
    """编排依赖：工具注册表 + 调用上下文 + 轨迹收集器。"""
    registry: ToolRegistry
    ctx: ToolContext
    traces: list[ToolTrace] = field(default_factory=list)

    def call(self, tool: str, **kwargs: Any) -> ToolResult:
        result = self.registry.call(tool, **kwargs)
        latency = int(getattr(result, "_latency_ms", 0) or 0)
        self.traces.append(ToolTrace(
            step=len(self.traces) + 1, tool=tool, ok=result.ok,
            source=result.source, confidence=result.confidence,
            summary=_summarize(tool, result), latency_ms=latency,
            degraded=not result.ok or bool(result.errors),
        ))
        return result


def _summarize(name: str, result: ToolResult) -> str:
    if not result.ok:
        return "失败：" + ("；".join(result.errors)[:60] or "未知原因")
    data = result.data
    if isinstance(data, list):
        text = f"返回 {len(data)} 条"
    elif isinstance(data, dict):
        if "total_low" in data:
            text = f"预算 {data['total_low']}~{data['total_high']} 元"
        elif "name" in data:
            text = f"{data['name']} ({data.get('lat')},{data.get('lng')})"
        elif "ok" in data:
            text = "自检通过" if data["ok"] else f"发现 {len(data.get('issues', []))} 个问题"
        else:
            text = "已返回"
    else:
        text = "已返回"
    if result.errors:
        text += "；" + result.errors[0][:40]
    return text


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_trip(request: TripRequest, deps: PlanDeps) -> Trip:
    deps.ctx.reset_caches()
    dates = rules.date_range(request.start_date.isoformat(), request.days)
    allocation = rules.city_day_allocation(request.destinations, request.days) or dates
    cities = list(dict.fromkeys(allocation))
    evidence: list[str] = []
    warnings: list[str] = []

    # ---------- 1. 目的地坐标 ----------
    dest_places: dict[str, dict] = {}
    for city in cities:
        got = deps.call("resolve_place", name=city, city=city)
        if got.ok:
            dest_places[city] = got.data
            evidence.append(f"{city} 坐标 {got.data['lat']:.3f},{got.data['lng']:.3f}"
                            f"（{got.source}/{got.data.get('precision')}）")
            for err in got.errors:
                warnings.append(f"{city}：{err}")
        else:
            warnings.append(f"未能解析「{city}」坐标，该城市天气与路线将不可用")

    # ---------- 2. 城际交通 ----------
    origin_place = None
    if request.origin:
        got = deps.call("resolve_place", name=request.origin, city=request.origin)
        if got.ok:
            origin_place = got.data
        else:
            warnings.append(f"未能解析出发地「{request.origin}」坐标，城际交通改为按距离估算")

    intercity: list[TransportLeg] = []
    if origin_place:
        chain = [request.origin] + cities + [request.origin]
        for a, b in zip(chain, chain[1:]):
            pa = origin_place if a == request.origin else dest_places.get(a)
            pb = origin_place if b == request.origin else dest_places.get(b)
            if not pa or not pb:
                continue
            leg = intercity_leg(a, b, (pa["lat"], pa["lng"]), (pb["lat"], pb["lng"]))
            intercity.append(leg)
            evidence.append(f"城际：{a}→{b} {leg.label} 约 {leg.minutes // 60}小时"
                            f"{leg.minutes % 60}分（{leg.source}）")

    # ---------- 3. 城市画像 ----------
    profiles: dict[str, CityProfile] = {}
    city_dates = {c: [d for d, cc in zip(dates, allocation) if cc == c] for c in cities}
    for city in cities:
        got = deps.call("city_profile", city=city, dates=city_dates[city])
        try:
            profiles[city] = CityProfile.model_validate(got.data)
        except Exception:                            # noqa: BLE001
            profiles[city] = CityProfile(city=city, source="default", confidence=0.3)
        if not got.ok:
            warnings.append(f"{city} 消费水平未取到（已按全国中位估算）："
                            f"{'；'.join(got.errors)[:50]}")
        else:
            evidence.append(f"{city} 价位：住宿 {profiles[city].lodging_low:.0f}~"
                            f"{profiles[city].lodging_high:.0f} 元/晚，"
                            f"正餐 {profiles[city].meal_low:.0f}~{profiles[city].meal_high:.0f} 元"
                            f"（llm 画像，置信 {profiles[city].confidence:.2f}）")
        for note in profiles[city].local_notes:
            warnings.append(f"{city}提示：{note}")

    # ---------- 4. POI 发现 ----------
    city_pois: dict[str, list[Poi]] = {}
    used_names: set[str] = set()
    for city in cities:
        stop_days = len(city_dates[city])
        want = min(MAX_POI_PER_CITY, 8 + stop_days * 5)
        pois: list[Poi] = []
        src: list[str] = []
        # 分批索取 + 失败重试。两条都是实测结论：
        #  · 推理模型一次给太多地点会在 <think> 阶段耗尽预算（超时/被截断），故每轮只要一小批；
        #  · 同一参数的成功率也只有约 4/5（失败那次耗时 ~110s，是超时），故失败必须重试，
        #    靠调小参数是治不好的 —— want=6 同样会随机失败。
        for _round in range(LLM_POI_MAX_ROUNDS):
            need = min(LLM_POI_BATCH, want - len(pois))
            if need <= 0:
                break
            got = deps.call("discover_pois", city=city, interests=request.preferences,
                            days=stop_days, want=need,
                            exclude=[p.name for p in pois])
            batch = _as_pois(got.data)
            fresh = [p for p in batch if p.name not in {q.name for q in pois}]
            if got.source and got.source not in src:
                src.append(got.source)
            if not fresh:
                continue           # 本轮空手而归 → 重试，而不是直接放弃
            pois.extend(fresh)
        if not pois:
            warnings.append(f"{city} 候选地点获取失败：未能获取「{city}」的候选地点")
            city_pois[city] = []
            continue
        city_pois[city] = pois
        used_names |= {p.name for p in pois}
        evidence.append(f"{city} 候选地点 {len(pois)} 个（来源：{' + '.join(src) or '大模型知识库'}）")

    # ---------- 5. 天气 ----------
    weather_map: dict[tuple[str, str], WeatherDay] = {}
    for city in cities:
        place = dest_places.get(city)
        got = deps.call("city_weather", city=city, dates=city_dates[city],
                        lat=(place or {}).get("lat"), lng=(place or {}).get("lng"))
        for raw in got.data if got.ok and isinstance(got.data, list) else []:
            try:
                wx = WeatherDay.model_validate(raw)
            except Exception:                        # noqa: BLE001
                continue
            weather_map[(city, wx.date)] = wx
        if not got.ok:
            warnings.append(f"{city} 天气不可用：{'；'.join(got.errors)[:60]}")
        else:
            bases = sorted({w.basis for (c, _), w in weather_map.items() if c == city})
            evidence.append(f"{city} 天气：{got.source}（{'/'.join(bases)}），"
                            f"共 {len(city_dates[city])} 天")
            for err in got.errors:
                warnings.append(f"{city}：{err}")

    # ---------- 6. 开放与预约校验 ----------
    closed: set[tuple[str, str]] = set()
    reserve_notes: dict[str, list[str]] = {}
    for city in cities:
        pois = city_pois.get(city)
        if not pois:
            continue
        got = deps.call("opening_check", pois=[p.model_dump() for p in pois[:20]],
                        dates=city_dates[city])
        for item in got.data if got.ok and isinstance(got.data, list) else []:
            name, d = item.get("poi_name"), item.get("date")
            if not name or not d:
                continue
            if not item.get("open", True):
                closed.add((name, d))
            elif item.get("reservation"):
                reserve_notes.setdefault(d, []).append(name)
        for err in got.errors:
            warnings.append(f"开放信息：{err}")
        if got.ok:
            evidence.append(f"{city} 开放/预约校验：{got.source}，"
                            f"{len(got.data) if isinstance(got.data, list) else 0} 条")

    # ---------- 7. 逐日装配 ----------
    city_anchor: dict[str, tuple[float, float]] = {}
    stay_area: dict[str, dict] = {}
    for city in cities:
        anchor = _anchor_of(city_pois.get(city, []))
        if anchor is None and city in dest_places:
            anchor = (dest_places[city]["lat"], dest_places[city]["lng"])
        if anchor:
            city_anchor[city] = anchor
            stay_area[city] = {"lat": anchor[0], "lng": anchor[1], "name": _area_name(anchor)}

    day_plans: list[DayPlan] = []
    cursor_coords: dict[str, Optional[tuple[float, float]]] = dict(city_anchor)
    for idx, (d, city) in enumerate(zip(dates, allocation)):
        first = idx == 0
        last = idx == len(dates) - 1 and len(dates) > 1
        day_start = None
        day_end = None
        cfg = rules.pace_cfg(request.pace)
        if first and request.origin:
            base = rules.hhmm_to_min(cfg["day_start"]) or 510
            day_start = min(base + 180, 13 * 60)
        if last and request.origin:
            base = rules.hhmm_to_min(cfg["day_end"]) or 1200
            day_end = max(base - 150, 14 * 60)

        picks = _picks_for_day(city_pois.get(city, []), city_dates[city], d)
        picks = [p for p in picks if (p.name, d) not in closed]
        plan = rules.plan_day_timeline(
            poi_date=d, city=city, pois=picks, pace=request.pace,
            weather=weather_map.get((city, d)),
            anchor=cursor_coords.get(city) or city_anchor.get(city),
            preferences=request.preferences, mobility=request.mobility,
            route_fn=_route_fn(deps),
            start_min=day_start, end_min=day_end,
            ticket_budget=_ticket_budget(request),
        )
        if reserve_notes.get(d):
            plan.notes.append("需提前预约：" + "、".join(sorted(set(reserve_notes[d]))[:5]))
        if plan.items:
            last_item = plan.items[-1]
            if last_item.lat is not None:
                cursor_coords[city] = (last_item.lat, last_item.lng)
        day_plans.append(plan)

    # ---------- 8. 组装 Trip ----------
    trip = Trip(trip_id=_new_id(), request=request, days=day_plans, intercity=intercity,
                stay_area=stay_area, profiles=profiles, evidence=evidence,
                engine="llm" if deps.ctx.llm_ready else "rules", warnings=warnings)

    budget = deps.call("estimate_budget", trip=trip.model_dump(mode="json"))
    if budget.ok:
        from ..models import BudgetPlan
        try:
            trip.budget = BudgetPlan.model_validate(budget.data)
        except Exception:                            # noqa: BLE001
            warnings.append("预算计算结果无法解析")
    else:
        warnings.append("预算计算失败：" + "；".join(budget.errors)[:60])

    trip.warnings = _dedupe(warnings + rules.check_conflicts(request, trip.days, trip.budget))

    check = deps.call("verify_trip", trip=trip.model_dump(mode="json"))
    if check.ok and isinstance(check.data, dict) and not check.data.get("ok"):
        trip.warnings.extend(f"自检：{i}" for i in check.data.get("issues", [])[:6])
    trip.warnings = _dedupe(trip.warnings)

    if not evidence:
        trip.evidence.append("本次规划未取到任何外部数据，结果完全来自规则与默认值，请谨慎参考")
    return trip


# --------------------------------------------------------------------------- #
# 单日重排（供 refine 使用）
# --------------------------------------------------------------------------- #
def relayout_day(trip: Trip, date: str, deps: PlanDeps) -> DayPlan | None:
    day = next((d for d in trip.days if d.date == date), None)
    if day is None or not day.items:
        return None
    # 优先用候选池里的完整信息，缺失时直接从行程项还原（保证重排永远可用）
    pois: list[Poi] = []
    for item in day.items:
        pois.append(ctx_poi_for(deps.ctx, item.name) or poi_from_item(item))
    if not pois:
        return None
    plan = rules.plan_day_timeline(
        poi_date=day.date, city=day.city, pois=pois, pace=trip.request.pace,
        weather=day.weather,
        anchor=_first_coords(day) or None,
        preferences=trip.request.preferences, mobility=trip.request.mobility,
        route_fn=_route_fn(deps), max_pois=len(pois) + 2,
    )
    plan.notes.extend(n for n in day.notes if "预约" in n)
    return plan


def poi_from_item(item: ItineraryItem) -> Poi:
    """从行程项还原一个最小可用的 Poi（用于重排/替换，不依赖候选池）。"""
    return Poi(
        id=item.poi_id, name=item.name, city=item.city, category=item.category,
        lat=item.lat, lng=item.lng, indoor=item.indoor, stay_minutes=item.stay_minutes,
        ticket_low=item.cost_low, ticket_high=item.cost_high,
        precision="exact" if item.lat is not None else "unknown",
        geo_source="itinerary", desc=item.reason[:40], source="itinerary",
        confidence=0.6,
    )


def poi_to_item(poi: Poi, date: str, order: int) -> ItineraryItem:
    """把新加入的 POI 变成行程项（时间轴会在重排时重新计算）。"""
    return ItineraryItem(
        item_id=f"{date}-u{order}", date=date, order=order, poi_id=poi.id, name=poi.name,
        category=poi.category, city=poi.city, lat=poi.lat, lng=poi.lng, indoor=poi.indoor,
        stay_minutes=max(20, poi.stay_minutes),
        cost_low=poi.ticket_low or 0.0,
        cost_high=(poi.ticket_high if poi.ticket_high is not None else (poi.ticket_low or 0.0)),
        score=60.0, reason="用户手动添加",
        evidence=[f"坐标来源：{poi.geo_source or '未知'}（{poi.precision}）",
                  f"信息置信度 {poi.confidence:.2f}"],
    )


def default_day_for(trip: Trip, city: str = "") -> str:
    """挑一个默认操作日：优先当天项目最少、且城市匹配的那天。"""
    pool = [d for d in trip.days if not city or d.city == city] or trip.days
    if not pool:
        return ""
    return min(pool, key=lambda d: len(d.items)).date


def ctx_poi_for(ctx: ToolContext, name: str) -> Optional[Poi]:
    for poi in ctx.poi_cache.values():
        if poi.name == name:
            return poi
    return None


def _first_coords(day: DayPlan) -> Optional[tuple[float, float]]:
    for item in day.items:
        if item.lat is not None and item.lng is not None:
            return (item.lat, item.lng)
    return None


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _as_pois(data: Any) -> list[Poi]:
    out: list[Poi] = []
    for raw in data if isinstance(data, list) else []:
        try:
            out.append(Poi.model_validate(raw))
        except Exception:                            # noqa: BLE001
            continue
    return rules.dedupe_pois(out)


def _picks_for_day(pois: list[Poi], city_days: list[str], this_day: str) -> list[Poi]:
    """把城市候选池切成「每天一批」：景点按分档顺序分配，餐饮每天 2 个。"""
    if not pois:
        return []
    try:
        slot = city_days.index(this_day)
    except ValueError:
        slot = 0
    k = max(1, len(city_days))
    sights, meals = rules.split_by_group(pois)
    per = max(1, (len(sights) + k - 1) // k)
    chunk = sights[slot * per:(slot + 1) * per]
    my_meals = meals[slot * 2:(slot + 1) * 2]
    if not my_meals and meals:
        my_meals = meals[:2]
    return chunk + my_meals


def _anchor_of(pois: list[Poi]) -> Optional[tuple[float, float]]:
    pts = [(p.lat, p.lng) for p in pois if p.lat is not None and p.lng is not None]
    if not pts:
        return None
    return (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))


def _area_name(anchor: tuple[float, float]) -> str:
    return f"{anchor[0]:.4f},{anchor[1]:.4f}（行程几何中心，建议在此 5km 内选住宿）"


def _route_fn(deps: PlanDeps) -> Callable[..., TransportLeg]:
    provider = deps.ctx.providers.router if deps.ctx.providers else None

    def route(origin: tuple[float, float], dest: tuple[float, float], mode: str) -> TransportLeg:
        if provider is None:
            return TransportLeg(mode=mode, label=mode, minutes=15, distance_km=0.0,
                                cost_low=0, cost_high=0, source="none", note="未配置路径服务")
        return provider.route(origin, dest, mode)

    return route


def _ticket_budget(request: TripRequest) -> Optional[float]:
    if not request.budget:
        return None
    return max(50.0, request.budget * 0.25 / max(1, request.days))


def _new_id() -> str:
    return "trip-" + datetime.now().strftime("%y%m%d%H%M%S%f")[:-3]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
