"""确定性规则引擎：时间轴装配、候选打分、预算汇总、冲突检测。

这里是整套系统里**唯一负责"算数"的地方**——几何、时长、金额全部由代码得出，
大模型不参与任何数字的生成，避免幻觉汇率/幻觉里程。
"""
from __future__ import annotations

from datetime import date as _date, datetime
from typing import Callable, Iterable, Optional

from ..models import (BudgetLine, BudgetPlan, DayPlan, ItineraryItem, Poi, TransportLeg,
                      TripRequest, WeatherDay)
from ..settings import (ARRIVAL_SHIFT_MINUTES, BUFFER_RATIO, CATEGORY_GROUPS,
                        DEPARTURE_CUT_MINUTES, DINNER_FROM, LUNCH_FROM, MEAL_MINUTES,
                        PACE_RULES, SCORE_WEIGHTS, WEATHER_RULES)
from ..providers.base import haversine_km

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

RouteFn = Callable[..., TransportLeg]

# 兴趣词 → 命中用的标签/关键词
INTEREST_SYNONYMS: dict[str, tuple[str, ...]] = {
    "自然风光": ("自然", "山", "湖", "海", "雪山", "草原", "湿地", "森林", "峡谷", "风光"),
    "历史人文": ("历史", "古迹", "遗址", "古城", "古镇", "人文", "民俗", "寺庙", "文化"),
    "美食": ("美食", "小吃", "餐饮", "夜市", "米线", "火锅", "菜"),
    "博物馆": ("博物馆", "美术馆", "展览", "科技馆"),
    "摄影": ("观景", "夜景", "日出", "日落", "打卡", "平台", "风光"),
    "亲子": ("亲子", "动物园", "乐园", "海洋", "科普", "适合儿童"),
    "夜生活": ("夜景", "酒吧", "夜市", "灯光", "演出"),
    "徒步": ("徒步", "登山", "步道", "栈道", "古道"),
    "温泉": ("温泉", "汤", "泡汤"),
    "购物": ("购物", "商业街", "百货", "市场", "奥特莱斯"),
    "小众": ("小众", "冷门", "秘境", "人少", "文艺"),
}

CROWD_TAGS = ("热门", "网红", "必去", "地标", "排队")


# --------------------------------------------------------------------------- #
# 时间工具
# --------------------------------------------------------------------------- #
def hhmm_to_min(value: str | None) -> Optional[int]:
    if not value:
        return None
    text = str(value).strip().replace("：", ":")
    if ":" not in text:
        return None
    hh, _, mm = text.partition(":")
    try:
        minutes = int(hh) * 60 + int(mm[:2])
    except ValueError:
        return None
    if minutes < 0 or minutes >= 24 * 60:
        return None
    return minutes


def min_to_hhmm(minutes: int) -> str:
    minutes = max(0, int(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def weekday_of(d: str) -> str:
    try:
        return WEEKDAYS[datetime.fromisoformat(d).weekday()]
    except ValueError:
        return ""


def pace_cfg(pace: str) -> dict:
    return PACE_RULES.get(pace, PACE_RULES["normal"])


def mode_for(distance_km: float, mobility: str = "normal") -> str:
    if distance_km <= 1.4:
        return "walk"
    if distance_km <= 4.0:
        return "taxi" if mobility == "reduced" else "bike"
    if distance_km <= 22:
        return "taxi"
    return "transit"


# --------------------------------------------------------------------------- #
# 打分（§3.1 偏好契合 / 评分 / 距离 / 预算 / 天气 / 拥挤度）
# --------------------------------------------------------------------------- #
def _interest_score(poi: Poi, preferences: Iterable[str]) -> tuple[float, list[str]]:
    prefs = [p for p in preferences if p]
    if not prefs:
        return 0.6, []
    blob = " ".join([poi.name, poi.category, *poi.tags, poi.desc])
    hits: list[str] = []
    for pref in prefs:
        keys = INTEREST_SYNONYMS.get(pref, (pref,))
        if any(k in blob for k in keys):
            hits.append(pref)
    if not hits:
        return 0.25, []
    return min(1.0, 0.55 + 0.45 * len(hits) / len(prefs)), hits


def _weather_score(poi: Poi, weather: Optional[WeatherDay]) -> tuple[float, list[str]]:
    notes: list[str] = []
    if weather is None or weather.basis == "unavailable":
        return 0.75, notes
    score = 0.9
    if weather.rain_prob >= WEATHER_RULES["rain_prob"]:
        if poi.indoor:
            score = 1.0
            notes.append(f"{weather.condition}（降雨概率{weather.rain_prob}%），室内项目不受影响")
        else:
            score = 0.35
            notes.append(f"{weather.condition}（降雨概率{weather.rain_prob}%），露天项目体验受限")
    if weather.temp_high >= WEATHER_RULES["heat_temp"] and not poi.indoor:
        score = min(score, 0.4)
        notes.append(f"高温 {weather.temp_high}℃，建议避开午后户外")
    if weather.temp_low <= -8 and not poi.indoor:
        score = min(score, 0.5)
        notes.append(f"低温 {weather.temp_low}℃，注意保暖")
    if weather.wind_kmh >= WEATHER_RULES["wind_kmh"] and not poi.indoor:
        score = min(score, 0.5)
        notes.append(f"大风 {weather.wind_kmh}km/h，索道/游船可能停运")
    return score, notes


def _crowd_score(poi: Poi) -> float:
    blob = f"{poi.name}{''.join(poi.tags)}"
    return 0.45 if any(t in blob for t in CROWD_TAGS) else 0.85


def score_poi(poi: Poi, *, preferences: Iterable[str] = (), weather: Optional[WeatherDay] = None,
              anchor: Optional[tuple[float, float]] = None,
              ticket_budget: Optional[float] = None) -> dict:
    """返回 0~100 的综合分与各分项依据。"""
    interest, hits = _interest_score(poi, preferences)
    rating = (poi.rating / 5.0) if poi.rating else 0.8

    if anchor and poi.lat is not None and poi.lng is not None:
        km = haversine_km(anchor[0], anchor[1], poi.lat, poi.lng)
        distance = max(0.15, 1.0 - min(1.0, km / 25.0))
    else:
        km = None
        distance = 0.7

    mid = None
    if poi.ticket_low is not None:
        mid = (poi.ticket_low + (poi.ticket_high if poi.ticket_high is not None else poi.ticket_low)) / 2
    if mid is None:
        budget = 0.8
    elif ticket_budget and ticket_budget > 0:
        budget = 1.0 if mid <= ticket_budget else max(0.2, ticket_budget / max(mid, 1.0))
    else:
        budget = 0.9

    weather_score, weather_notes = _weather_score(poi, weather)
    crowd = _crowd_score(poi)

    parts = {
        "interest": interest, "rating": rating, "distance": distance,
        "budget": budget, "weather": weather_score, "crowd": crowd,
    }
    total = sum(parts[k] * SCORE_WEIGHTS[k] for k in SCORE_WEIGHTS)
    total *= 0.6 + 0.4 * max(0.3, min(1.0, poi.confidence))    # 置信度折价

    reasons: list[str] = []
    if hits:
        reasons.append("契合你的「" + "、".join(hits[:3]) + "」偏好")
    if poi.rating:
        reasons.append(f"评分 {poi.rating}")
    if km is not None and km <= 15:
        reasons.append(f"距住宿锚点约 {km:.1f}km")
    reasons.extend(weather_notes[:2])

    return {
        "score": round(total * 100, 1),
        "parts": parts,
        "reason": "；".join(reasons) or "常规推荐",
        "weather_notes": weather_notes,
        "distance_km": round(km, 2) if km is not None else None,
    }


# --------------------------------------------------------------------------- #
# 时间轴装配
# --------------------------------------------------------------------------- #
def _coords(poi: Poi) -> Optional[tuple[float, float]]:
    if poi.lat is None or poi.lng is None:
        return None
    return (poi.lat, poi.lng)


def order_sights(sights: list[Poi], anchor: Optional[tuple[float, float]]) -> list[Poi]:
    """从锚点出发的最近邻排序；无坐标的放最后。"""
    remaining = [p for p in sights if _coords(p)]
    tail = [p for p in sights if not _coords(p)]
    ordered: list[Poi] = []
    cursor = anchor
    while remaining:
        if cursor is None:
            ordered.extend(remaining)
            break
        remaining.sort(key=lambda p: haversine_km(cursor[0], cursor[1], p.lat, p.lng))
        nxt = remaining.pop(0)
        ordered.append(nxt)
        cursor = _coords(nxt)
    return ordered + tail


def plan_day_timeline(
    *,
    poi_date: str,
    city: str,
    pois: list[Poi],
    pace: str,
    weather: Optional[WeatherDay] = None,
    anchor: Optional[tuple[float, float]] = None,
    preferences: Iterable[str] = (),
    mobility: str = "normal",
    route_fn: Optional[RouteFn] = None,
    start_min: Optional[int] = None,
    end_min: Optional[int] = None,
    ticket_budget: Optional[float] = None,
    max_pois: Optional[int] = None,
    require_indoor: bool = False,
) -> DayPlan:
    cfg = pace_cfg(pace)
    day_start = start_min if start_min is not None else hhmm_to_min(cfg["day_start"]) or 8 * 60 + 30
    day_end = end_min if end_min is not None else hhmm_to_min(cfg["day_end"]) or 20 * 60
    limit = max_pois or cfg["max_pois"]

    scored = []
    for poi in pois:
        s = score_poi(poi, preferences=preferences, weather=weather,
                      anchor=anchor, ticket_budget=ticket_budget)
        scored.append((poi, s))
    scored.sort(key=lambda pair: pair[1]["score"], reverse=True)

    if require_indoor:
        indoor = [p for p, s in scored if p.indoor]
        others = [p for p, s in scored if not p.indoor]
        ranked = indoor + others
    else:
        ranked = [p for p, _ in scored]

    sights = [p for p in ranked if p.is_sight][:limit]
    meals = [p for p in ranked if p.is_meal]
    score_of = {s[0].id: s[1] for s in scored}

    # 若某天整体不适合户外，优先把室内项目排进来
    if weather is not None and weather.rain_prob >= WEATHER_RULES["rain_prob"]:
        indoor = [p for p in sights if p.indoor]
        if indoor:
            sights = indoor + [p for p in sights if not p.indoor]
        require_indoor = True

    ordered = order_sights(sights, anchor)

    items: list[ItineraryItem] = []
    cursor_min = day_start
    cursor_coords = anchor
    cursor_name = "住宿地"
    lunch_done = dinner_done = False
    walking_km = 0.0
    active = 0
    notes: list[str] = []
    risk_notes: list[str] = []
    # 因「开放时段对不上」或「当天塞不下」而放弃的点，最后汇报出来，
    # 免得用户以为这些点被静默丢掉了。
    skipped_hours: list[str] = []

    queue: list[tuple[Poi, str]] = [(p, "sight") for p in ordered]

    def push(poi: Poi, kind: str) -> None:
        nonlocal cursor_min, cursor_coords, cursor_name, walking_km, active
        stay = MEAL_MINUTES[kind] if kind in ("lunch", "dinner") else max(20, poi.stay_minutes)
        leg: Optional[TransportLeg] = None
        coords = _coords(poi)
        if cursor_coords and coords and route_fn:
            straight = haversine_km(cursor_coords[0], cursor_coords[1], coords[0], coords[1])
            mode = mode_for(straight, mobility)
            try:
                leg = route_fn(cursor_coords, coords, mode)
            except Exception:                      # noqa: BLE001
                leg = None
            if leg is not None:
                if mode == "walk":
                    walking_km += leg.distance_km
                cursor_min += leg.minutes
        elif cursor_coords and coords:
            dist = haversine_km(cursor_coords[0], cursor_coords[1], coords[0], coords[1]) * 1.3
            cursor_min += max(5, round(dist / 20 * 60))
            if dist <= 1.4:
                walking_km += dist
        elif cursor_coords:
            # 坐标缺失（常见于大模型未给出经纬度）：给一个标称同城移动时间，明确标注为估算
            leg = TransportLeg(mode="taxi", label="同城移动", minutes=20, distance_km=0.0,
                               cost_low=15, cost_high=35, source="estimate",
                               note="地点坐标未知，按同城移动约 20 分钟估算")
            cursor_min += leg.minutes

        arrive = max(cursor_min, day_start)
        # 尊重 POI 的开放时段：候选点用 open/close 描述「几点能进、几点关门」。
        # 不看这个字段就会出现「锦江夜游排在 08:37」这类荒谬安排（实测）。
        opening = hhmm_to_min(poi.open)
        closing = hhmm_to_min(poi.close)
        if opening is not None and arrive < opening:
            arrive = opening
        if closing is not None:
            if arrive >= closing:
                # 当日已过开放时段 → 本点排不进去
                skipped_hours.append(poi.name)
                return
            stay = min(stay, closing - arrive)
        leave = arrive + stay
        if leave > day_end:
            skipped_hours.append(poi.name)
            return
        info = score_of.get(poi.id, {"score": 50, "reason": "常规推荐", "weather_notes": []})
        evidence = []
        if poi.geo_source:
            evidence.append(f"坐标来源：{poi.geo_source}（{poi.precision}）")
        if weather is not None and weather.basis != "unavailable":
            evidence.append(f"当日天气：{weather.condition} {weather.temp_low}~{weather.temp_high}℃"
                            f"（{weather.source}/{weather.basis}）")
        if leg is not None:
            evidence.append(f"交通：{leg.label} {leg.minutes} 分钟，来源 {leg.source}")

        items.append(ItineraryItem(
            item_id=f"{poi_date}-{len(items) + 1}",
            date=poi_date, order=len(items) + 1, poi_id=poi.id, name=poi.name,
            category=poi.category, city=city,
            lat=poi.lat, lng=poi.lng, indoor=poi.indoor,
            arrive=min_to_hhmm(arrive), leave=min_to_hhmm(leave), stay_minutes=stay,
            transport=leg,
            cost_low=poi.ticket_low or 0.0,
            cost_high=(poi.ticket_high if poi.ticket_high is not None else (poi.ticket_low or 0.0)),
            score=info["score"], reason=info["reason"],
            evidence=evidence,
        ))
        if poi.risk_tags:
            risk_notes.append(f"{poi.name}：" + "、".join(poi.risk_tags))
            items[-1].risk = "、".join(poi.risk_tags)
        if poi.altitude and poi.altitude >= 3000:
            risk_notes.append(f"{poi.name} 海拔约 {int(poi.altitude)}m，注意高原反应")
        cursor_min = leave
        cursor_coords = coords or cursor_coords
        cursor_name = poi.name
        active += stay + (leg.minutes if leg else 0)

    for idx, (poi, _kind) in enumerate(queue):
        if not lunch_done and cursor_min >= LUNCH_FROM and meals:
            lunch = meals.pop(0)
            push(lunch, "lunch")
            lunch_done = True
        if not dinner_done and cursor_min >= DINNER_FROM and meals:
            dinner = meals.pop(0)
            push(dinner, "dinner")
            dinner_done = True
        if cursor_min >= day_end:
            notes.append(f"当日时间已满，{len(queue) - idx} 个候选点未安排，可调整节奏或延长天数")
            break
        # 容量检查必须带上「本项目预计停留时长」再判：只比 cursor_min 是不够的，
        # 一次 push 会额外吃掉交通 + 游玩 2~3 小时，不加这道判断就会排出
        # 27:40、28:50 这种跨过午夜的时刻（实测真模型给出 5 个点时会触发）。
        if cursor_min + max(20, poi.stay_minutes) > day_end:
            notes.append(f"当日时间已满，{len(queue) - idx} 个候选点未安排，可调整节奏或延长天数")
            break
        push(poi, "sight")

    # 收尾：晚餐必须在离开前吃上
    if not dinner_done and meals and cursor_min <= day_end - 45:
        push(meals.pop(0), "dinner")

    # 步行量约束（§3.4 适老/适幼）
    walk_limit = 8.0 if mobility == "reduced" else {"relaxed": 9.0, "normal": 13.0, "intensive": 17.0}[pace]
    if walking_km > walk_limit:
        risk_notes.append(f"当日步行约 {walking_km:.1f}km，超过建议上限 {walk_limit:.0f}km，建议打车替代部分路段")

    # hhmm_to_min 是 Optional[int]：leave 字段为空时返回 None，
    # 直接相减会抛 TypeError（真模型给出的 POI 不带时间时就会走到这里）。
    last_min = hhmm_to_min(items[-1].leave) if items else None
    total_min = (last_min - day_start) if last_min is not None else 0
    if skipped_hours:
        notes.append("因开放时段或当日容量不足未安排：" + "、".join(skipped_hours[:5]))
    return DayPlan(date=poi_date, weekday=weekday_of(poi_date), city=city, weather=weather,
                   items=items, walking_km=round(walking_km, 1),
                   active_minutes=max(0, int(total_min or 0)), risk_notes=risk_notes,
                   notes=notes)


# --------------------------------------------------------------------------- #
# 预算
# --------------------------------------------------------------------------- #
def estimate_budget(request: TripRequest, days: list[DayPlan],
                    profiles: dict, intercity: list[TransportLeg]) -> BudgetPlan:
    travelers = max(1, request.travelers)
    rooms = max(1, min(request.rooms, travelers))
    nights = max(0, len(days) - 1)

    lines: list[BudgetLine] = []

    # 大交通
    if intercity:
        lo = sum(l.cost_low for l in intercity) * travelers
        hi = sum(l.cost_high for l in intercity) * travelers
        lines.append(BudgetLine(category="城际交通", low=round(lo), high=round(hi),
                                basis="出发地↔目的地往返，按舱位/席别区间估算"))

    # 住宿
    lodging_low = lodging_high = 0.0
    counted = 0
    for city, prof in profiles.items():
        if prof.lodging_low is None:
            continue
        share = sum(1 for d in days if d.city == city) or 0
        share = max(1, share - (1 if counted == 0 else 0))   # 首尾衔接不强求多订
        lodging_low += prof.lodging_low * share * rooms
        lodging_high += (prof.lodging_high or prof.lodging_low) * share * rooms
        counted += 1
    if nights > 0 and lodging_low <= 0:
        lodging_low, lodging_high = 260 * nights * rooms, 520 * nights * rooms
    lines.append(BudgetLine(category="住宿", low=round(lodging_low), high=round(lodging_high),
                            basis=f"{nights} 晚 × {rooms} 间（酒店价位来自大模型城市画像，区间估算）"))

    # 餐饮
    meal_low = meal_high = 0.0
    for city, prof in profiles.items():
        share = sum(1 for d in days if d.city == city) or 1
        ml = prof.meal_low if prof.meal_low is not None else 50
        mh = prof.meal_high if prof.meal_high is not None else 120
        meal_low += ml * 3 * share * travelers
        meal_high += mh * 3 * share * travelers
    if meal_low <= 0:
        meal_low, meal_high = 120 * len(days) * travelers, 300 * len(days) * travelers
    lines.append(BudgetLine(category="餐饮", low=round(meal_low), high=round(meal_high),
                            basis="按城市消费水平 × 3 餐 × 人数"))

    # 门票
    t_lo = sum(i.cost_low for d in days for i in d.items)
    t_hi = sum(i.cost_high for d in days for i in d.items)
    lines.append(BudgetLine(category="门票/体验", low=round(t_lo * travelers),
                            high=round(t_hi * travelers),
                            basis="行程内各点票价区间求和（缺失票价按 0 计）"))

    # 市内交通
    local_lo = sum((i.transport.cost_low if i.transport else 0) for d in days for i in d.items)
    local_hi = sum((i.transport.cost_high if i.transport else 0) for d in days for i in d.items)
    lines.append(BudgetLine(category="市内交通", low=round(local_lo * travelers),
                            high=round(local_hi * travelers),
                            basis="行程内各段交通费用求和"))

    subtotal_lo = sum(l.low for l in lines)
    subtotal_hi = sum(l.high for l in lines)
    lines.append(BudgetLine(category="机动/杂项", low=round(subtotal_lo * BUFFER_RATIO),
                            high=round(subtotal_hi * BUFFER_RATIO),
                            basis=f"按总额 {int(BUFFER_RATIO * 100)}% 预留"))

    lo = round(sum(l.low for l in lines))
    hi = round(sum(l.high for l in lines))
    verdict, advice = _budget_verdict(request.budget, lo, hi, travelers, len(days))
    return BudgetPlan(currency="CNY", lines=lines, total_low=lo, total_high=hi,
                      user_budget=request.budget, verdict=verdict, advice=advice,
                      basis="区间估算；住宿与餐饮价位来自大模型城市画像，交通与门票来自实测/已知区间")


def _budget_verdict(budget: Optional[float], low: float, high: float,
                    travelers: int, days_count: int) -> tuple[str, list[str]]:
    advice: list[str] = []
    if not budget:
        return "未设置预算，以下为参考区间", advice
    if budget >= high:
        advice.append(f"预算可覆盖偏高方案，人均约 ¥{round(budget / travelers)}")
        return f"预算充足（{low:.0f}~{high:.0f} 元 vs 预算 {budget:.0f} 元）", advice
    if budget >= low:
        advice.append("建议选择经济型住宿、减少跨城移动，并优先免费景点")
        return f"预算基本可行（{low:.0f}~{high:.0f} 元 vs 预算 {budget:.0f} 元）", advice
    gap = round(low - budget)
    advice.append(f"缺口约 ¥{gap}：可压缩住宿等级、缩短 1 天、或改用高铁替代飞机")
    if days_count > 3:
        advice.append(f"若去掉 1 天，约可节省 ¥{round(low / days_count)}")
    return f"预算偏紧（下限 {low:.0f} 元 已超预算 {budget:.0f} 元）", advice


# --------------------------------------------------------------------------- #
# 冲突与风险检测（§3.4）
# --------------------------------------------------------------------------- #
def check_conflicts(request: TripRequest, days: list[DayPlan],
                    budget: Optional[BudgetPlan]) -> list[str]:
    warnings: list[str] = []
    for d in days:
        if d.active_minutes >= 11 * 60:
            warnings.append(f"{d.date} 安排偏满（约 {d.active_minutes // 60} 小时），建议减少 1 个点或延长 1 天")
        if request.mobility == "reduced" and d.walking_km > 6:
            warnings.append(f"{d.date} 步行 {d.walking_km}km，对行动不便者偏多，建议安排代步")
        if not d.items:
            warnings.append(f"{d.date} 暂无可用安排（可能因为候选点不足或天气受限）")
    alts = [i for d in days for i in d.items if i.poi_id.startswith("altitude")]
    if alts:
        warnings.append("行程含高海拔区域，首日建议降低强度并预留适应时间")
    if budget and budget.user_budget and budget.total_low > budget.user_budget:
        warnings.append(f"预算下限 {budget.total_low:.0f} 元已超过设定预算，请参考预算建议调整")
    seen: set[str] = set()
    out: list[str] = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def city_day_allocation(destinations: list[str], day_count: int) -> list[str]:
    """把总天数尽量均分给各目的地，余数优先给靠前的城市。"""
    cities = [c for c in destinations if c]
    if not cities:
        return []
    if len(cities) == 1:
        return [cities[0]] * day_count
    base, extra = divmod(day_count, len(cities))
    plan: list[str] = []
    for idx, city in enumerate(cities):
        plan.extend([city] * (base + (1 if idx < extra else 0)))
    return plan[:day_count]


def date_range(start: str, days: int) -> list[str]:
    first = datetime.fromisoformat(start).date()
    return [(first.fromordinal(first.toordinal() + i)).isoformat() for i in range(days)]


def nights_per_city(allocation: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for city in allocation[:-1]:                 # 最后一晚通常已返程
        out[city] = out.get(city, 0) + 1
    for city in allocation:                      # 至少记录 1 晚，便于取价位
        out.setdefault(city, 0)
    return out


def dedupe_pois(pois: Iterable[Poi]) -> list[Poi]:
    seen: set[str] = set()
    out: list[Poi] = []
    for p in pois:
        key = p.name.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def split_by_group(pois: Iterable[Poi]) -> tuple[list[Poi], list[Poi]]:
    sights, meals = [], []
    for p in pois:
        (meals if p.category in CATEGORY_GROUPS["meal"] else sights).append(p)
    return sights, meals


def weekday_index(d: str) -> int:
    try:
        return _date.fromisoformat(d).weekday()
    except ValueError:
        return -1
