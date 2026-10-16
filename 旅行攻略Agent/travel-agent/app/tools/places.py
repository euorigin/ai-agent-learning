"""地点类工具：地名解析、POI 发现、城市画像。

「任意目的地」这条需求就靠这三个工具撑着：
- 城市坐标走免费地理编码（也可换高德）
- POI 清单在没有高德 Key 时由大模型提供（坐标标记 approximate）
- 有高德 Key 时优先用高德真实 POI，不足部分再让大模型补齐
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from ..llm import LLMError
from ..models import CityProfile, Place, Poi, ToolResult
from ..providers.base import haversine_km
from ..prompts import POI_SYSTEM, PROFILE_SYSTEM, poi_user, profile_user
from .base import ToolContext, call_llm_json, clamp_float, clamp_int, fail, ok

VALID_CATEGORIES = {"景点", "自然", "古镇", "文化", "博物馆", "观景", "户外", "亲子",
                    "室内", "餐饮", "小吃", "咖啡", "购物", "娱乐", "住宿"}

# 兴趣 → 高德检索关键词
AMAP_KEYWORDS = {
    "自然风光": "风景区|公园", "历史人文": "文物古迹|博物馆", "美食": "小吃|美食街",
    "博物馆": "博物馆|美术馆", "摄影": "观景台|风景名胜", "亲子": "游乐园|动物园",
    "夜生活": "夜市|酒吧街", "徒步": "登山|步道", "温泉": "温泉", "购物": "商业街|购物中心",
}

DEFAULT_PROFILE = CityProfile(city="", lodging_low=260, lodging_high=520, meal_low=45, meal_high=120,
                              local_notes=["住宿/餐饮价位未获取到大模型画像，按全国中位水平估算"],
                              source="default", confidence=0.3)


CROSS_CHECK_KM = 120.0      # 两个来源分歧超过这个距离，认为地理编码不可信


# --------------------------------------------------------------------------- #
def resolve_place(ctx: ToolContext, name: str, *, city: str = "") -> ToolResult:
    """把地名解析成带坐标的 Place。

    两条来源交叉验证：
    1. 地理编码（Photon + Open-Meteo，或高德）
    2. 大模型的地理知识
    两者分歧超过 120km 时以大模型为准并降级精度标注——因为中文/境外城市名
    在免费地理编码里最容易被同名村庄污染。
    """
    name = (name or "").strip()
    if name:
        name = _clean_query(name)
    if not name:
        return fail("resolve_place", "地名为空")

    cached = ctx.place_cache.get(name)
    if cached is not None:
        return ok("resolve_place", cached.model_dump(), source=cached.source, confidence=0.9)

    geo = ctx.providers.geocoder if ctx.providers else None
    geo_hit: Place | None = None
    geo_error = ""
    if geo is not None:
        try:
            hits = geo.search(name, count=5)
            geo_hit = hits[0] if hits else None
        except Exception as exc:                    # noqa: BLE001
            geo_error = f"{type(exc).__name__}: {exc}"

    llm_hit = _llm_place(ctx, name, city)

    if geo_hit is None and llm_hit is None:
        detail = f"（{geo_error}）" if geo_error else ""
        return fail("resolve_place",
                    f"未能解析「{name}」的坐标：地理编码无命中且大模型未配置{detail}")

    if geo_hit is None:
        ctx.place_cache[name] = llm_hit
        return ok("resolve_place", llm_hit.model_dump(), source=llm_hit.source,
                  confidence=0.5)

    if llm_hit is None:
        ctx.place_cache[name] = geo_hit
        return ok("resolve_place", geo_hit.model_dump(), source=geo_hit.source,
                  confidence=0.75 if geo_hit.precision == "exact" else 0.5)

    drift = haversine_km(geo_hit.lat, geo_hit.lng, llm_hit.lat, llm_hit.lng)
    limit = ctx.settings.geocode_crosscheck_km if ctx.settings else CROSS_CHECK_KM
    if drift <= limit:
        geo_hit.source = f"{geo_hit.source}+llm核对"
        geo_hit.precision = "exact" if geo_hit.precision == "exact" else "approximate"
        ctx.place_cache[name] = geo_hit
        return ok("resolve_place", geo_hit.model_dump(), source=geo_hit.source, confidence=0.85)

    # 分歧过大：地理编码多半命中了同名地，采信大模型
    llm_hit.source = f"llm(地理编码分歧 {drift:.0f}km)"
    llm_hit.precision = "approximate"
    ctx.place_cache[name] = llm_hit
    result = ok("resolve_place", llm_hit.model_dump(), source=llm_hit.source, confidence=0.55)
    result.errors.append(f"地理编码与模型给出的「{name}」坐标相差 {drift:.0f}km，"
                         f"已采信模型结果（精度 approximate），建议核对")
    result.needs_confirmation = True
    return result


def _clean_query(name: str) -> str:
    """去掉「市/省」等后缀与多余修饰，提高命中率。"""
    text = re.sub(r"(的旅行|旅游|自由行|攻略)$", "", name.strip())
    return text.strip() or name.strip()


def _llm_place(ctx: ToolContext, name: str, city: str) -> Place | None:
    if not ctx.llm_ready:
        return None
    try:
        payload = call_llm_json(
            ctx,
            "你是地理信息助手。输出 JSON：{\"name\":\"\",\"lat\":0.0,\"lng\":0.0,"
            "\"admin\":\"\",\"country\":\"\",\"confidence\":0.0}。"
            "lat/lng 为十进制度（WGS-84），给城市/景区中心点即可。"
            "若你确实不知道这个地方，把 lat/lng 设为 null。只输出 JSON。",
            f"请给出「{name}」的坐标。" + (f"它位于 {city} 附近。" if city else ""),
            max_tokens=300, temperature=0.1,
        )
    except LLMError:
        return None
    lat = clamp_float(payload.get("lat"), -90, 90)
    lng = clamp_float(payload.get("lng"), -180, 180)
    if lat is None or lng is None:
        return None
    return Place(name=payload.get("name") or name, lat=lat, lng=lng,
                 city=city or payload.get("name") or name, admin=payload.get("admin", ""),
                 country=payload.get("country", ""), precision="approximate",
                 source="llm-estimate",
                 population=None)


# --------------------------------------------------------------------------- #
def discover_pois(ctx: ToolContext, city: str, *, interests: list[str] | None = None,
                  days: int = 3, want: int = 18, exclude: list[str] | None = None) -> ToolResult:
    """发现某城市的候选 POI。"""
    city = (city or "").strip()
    if not city:
        return fail("discover_pois", "城市名为空")
    interests = interests or []
    exclude = exclude or []
    want = max(6, min(40, want))
    sources: list[str] = []

    pois: list[Poi] = []
    amap = ctx.providers.amap if ctx.providers else None
    if amap is not None:
        keywords = set()
        for pref in interests or ["景点"]:
            for kw in AMAP_KEYWORDS.get(pref, "风景名胜").split("|"):
                keywords.add(kw)
        keywords.add("风景名胜")
        seen_ids: set[str] = set()
        for kw in list(keywords)[:5]:
            for poi in amap.poi_search(kw, city, count=max(8, want // 2)):
                if poi.id in seen_ids or poi.name in exclude:
                    continue
                seen_ids.add(poi.id)
                pois.append(poi)
        for poi in amap.poi_search("小吃|特色餐厅", city, count=6):
            poi.category = poi.category if poi.category in ("餐饮", "小吃") else "餐饮"
            if poi.id not in seen_ids:
                seen_ids.add(poi.id)
                pois.append(poi)
        if pois:
            sources.append(f"高德地图（{len(pois)} 个实测 POI）")

    if len(pois) < want:
        llm_part = _llm_pois(ctx, city, interests=interests, days=days,
                             want=want - len(pois) + 4, exclude=exclude + [p.name for p in pois])
        if llm_part:
            merge_pois(pois, llm_part, ctx)
            sources.append(f"大模型知识库（{len(llm_part)} 个，坐标可能为估算）")

    if not pois:
        return fail("discover_pois",
                    f"未能获取「{city}」的候选地点：高德未配置且大模型不可用或返回异常")

    for poi in pois:
        ctx.poi_cache[poi.id] = poi

    return ok("discover_pois", [p.model_dump() for p in pois],
              source=" + ".join(sources), confidence=0.85 if amap else 0.6)


def merge_pois(base: list[Poi], extra: list[Poi], ctx: ToolContext,
               *, drift_km: float | None = None) -> None:
    """把大模型给的 POI 并入已有池；同名项用已有坐标校准，并保留大模型的软信息。"""
    limit = drift_km if drift_km is not None else (ctx.settings.poi_max_drift_km if ctx.settings else 200.0)
    index = {p.name: p for p in base}
    for poi in extra:
        exist = index.get(poi.name)
        if exist is None:
            # 名称近似（含子串）也视作同一地点
            for name, cand in index.items():
                if name and (name in poi.name or poi.name in name) and abs(len(name) - len(poi.name)) <= 6:
                    exist = cand
                    break
        if exist is None:
            base.append(poi)
            index[poi.name] = poi
            continue
        # 已有坐标为准（更可信），补充标签/描述/室内等软信息
        if exist.lat is not None and poi.lat is not None:
            drift = haversine_km(exist.lat, exist.lng, poi.lat, poi.lng)
            if drift > limit:
                exist.risk_tags = sorted(set(exist.risk_tags) | {"坐标待核实"})
        exist.tags = sorted(set(exist.tags) | set(poi.tags))
        exist.desc = exist.desc or poi.desc
        exist.stay_minutes = exist.stay_minutes or poi.stay_minutes
        exist.indoor = exist.indoor or poi.indoor
        if exist.rating is None:
            exist.rating = poi.rating
        if exist.ticket_low is None and poi.ticket_low is not None:
            exist.ticket_low, exist.ticket_high = poi.ticket_low, poi.ticket_high


def _llm_pois(ctx: ToolContext, city: str, *, interests: list[str], days: int,
              want: int, exclude: list[str]) -> list[Poi]:
    if not ctx.llm_ready:
        return []
    try:
        payload = call_llm_json(ctx, POI_SYSTEM,
                                poi_user(city, interests=interests, want=want, days=days,
                                         exclude=exclude),
                                max_tokens=3500, temperature=0.4)
    except LLMError:
        return []
    raw = payload.get("pois") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    out: list[Poi] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        poi = _to_poi(item, city, idx)
        if poi is not None and poi.name not in exclude:
            out.append(poi)
    return out


def _to_poi(item: dict, city: str, idx: int) -> Poi | None:
    name = str(item.get("name", "")).strip()
    if not name:
        return None
    lat = clamp_float(item.get("lat"), -90, 90)
    lng = clamp_float(item.get("lng"), -180, 180)
    category = str(item.get("category") or "景点")
    if category not in VALID_CATEGORIES:
        category = "景点"
    tags = [str(t)[:12] for t in (item.get("tags") or []) if str(t).strip()][:6]
    closed = [int(c) for c in (item.get("closed_weekdays") or [])
              if isinstance(c, (int, float)) and 0 <= int(c) <= 6]
    pid = "llm-" + hashlib.md5(f"{city}|{name}".encode("utf-8")).hexdigest()[:10]
    return Poi(
        id=pid, name=name, city=city, category=category, tags=tags,
        lat=lat, lng=lng,
        precision="approximate" if lat is not None else "unknown",
        geo_source="llm-estimate" if lat is not None else "",
        stay_minutes=clamp_int(item.get("stay_minutes"), 20, 480, 100),
        open=str(item.get("open") or "")[:5], close=str(item.get("close") or "")[:5],
        closed_weekdays=sorted(set(closed)),
        reservation=bool(item.get("reservation")),
        indoor=bool(item.get("indoor")),
        altitude=clamp_float(item.get("altitude"), 0, 9000),
        risk_tags=[str(t)[:16] for t in (item.get("risk_tags") or [])][:4],
        desc=str(item.get("desc") or "")[:60],
        source="llm", confidence=clamp_float(item.get("confidence"), 0.1, 1.0) or 0.6,
    )


# --------------------------------------------------------------------------- #
def city_profile(ctx: ToolContext, city: str, *, dates: list[str] | None = None) -> ToolResult:
    """城市消费水平与在地提示（决定预算是否可信）。"""
    city = (city or "").strip()
    if not city:
        return fail("city_profile", "城市名为空")
    dates = dates or []
    if not ctx.llm_ready:
        prof = DEFAULT_PROFILE.model_copy(update={"city": city})
        return ok("city_profile", prof.model_dump(), source="default", confidence=0.3)

    try:
        payload = call_llm_json(ctx, PROFILE_SYSTEM, profile_user(city, dates),
                                max_tokens=800, temperature=0.2)
    except LLMError as exc:
        prof = DEFAULT_PROFILE.model_copy(update={"city": city})
        return fail("city_profile", f"大模型城市画像失败：{exc}", data=prof.model_dump())

    lo = clamp_float(payload.get("lodging_low"), 40, 20000)
    hi = clamp_float(payload.get("lodging_high"), 40, 30000)
    ml = clamp_float(payload.get("meal_low"), 5, 3000)
    mh = clamp_float(payload.get("meal_high"), 5, 5000)
    if lo is None or hi is None:
        lo, hi = DEFAULT_PROFILE.lodging_low, DEFAULT_PROFILE.lodging_high
    if hi < lo:
        lo, hi = hi, lo
    if ml is None or mh is None:
        ml, mh = DEFAULT_PROFILE.meal_low, DEFAULT_PROFILE.meal_high
    if mh < ml:
        ml, mh = mh, ml
    prof = CityProfile(
        city=city, lodging_low=lo, lodging_high=hi, meal_low=ml, meal_high=mh,
        local_notes=[str(n)[:40] for n in (payload.get("local_notes") or [])][:5],
        source="llm", confidence=clamp_float(payload.get("confidence"), 0.1, 1.0) or 0.55,
    )
    return ok("city_profile", prof.model_dump(), source="llm", confidence=prof.confidence)
