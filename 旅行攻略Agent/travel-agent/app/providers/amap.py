"""高德地图 Web 服务适配（可选，填了 AMAP_KEY 才启用）。

提供三件套：地点检索（POI + 地理编码）、路径规划（驾车/步行/公交）。
优势是**景点级 POI 与真实路网**，可把大模型估算的坐标换成实测坐标。

坐标系提醒：高德返回 GCJ-02（火星坐标），与 Open-Meteo/OSRM 的 WGS-84 存在
数十米到数百米偏移。本项目只用于「同一城市内相对距离」，误差可忽略；若将来
要做跨坐标系精确叠加，需先做 GCJ-02 → WGS-84 纠偏。
"""
from __future__ import annotations

from typing import Any

from ..http import HttpError, request_json
from ..models import Place, Poi, TransportLeg
from ..providers.base import haversine_km

BASE = "https://restapi.amap.com/v3"

# 高德 type 字符串包含的关键词 → 站内分类
_CATEGORY_KEYWORDS = [
    (("餐饮", "餐厅", "中餐厅", "外国餐厅", "快餐厅", "美食"), "餐饮"),
    (("小吃", "糕饼", "甜品"), "小吃"),
    (("咖啡", "茶艺", "茶座", "冷饮"), "咖啡"),
    (("商场", "购物", "超市", "市场"), "购物"),
    (("风景名胜", "公园", "广场", "寺庙", "教堂", "纪念馆", "博物馆",
      "展览馆", "动物园", "植物园", "水族馆", "游乐园", "景点"), "景点"),
    (("住宿", "宾馆", "酒店", "旅馆"), "住宿"),
    (("运动", "健身", "娱乐", "影剧院", "ktv"), "娱乐"),
]


def _category(type_str: str, name: str) -> str:
    blob = f"{type_str};{name}"
    for keys, label in _CATEGORY_KEYWORDS:
        if any(k in blob for k in keys):
            return label
    return "景点"


def _indoor(type_str: str, name: str) -> bool:
    blob = f"{type_str};{name}"
    return any(k in blob for k in ("博物馆", "展览馆", "美术馆", "纪念馆", "影剧院",
                                   "水族馆", "商场", "购物", "茶馆", "咖啡", "室内"))


class AmapClient:
    name = "amap"

    def __init__(self, key: str, *, timeout: float = 20.0) -> None:
        self.key = key
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: dict[str, Any]) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["key"] = self.key
        data = request_json(f"{BASE}{path}", params=params, timeout=self.timeout, retries=1)
        if str((data or {}).get("status")) != "1":
            raise HttpError(200, str(data.get("info") or "高德接口返回失败"), "")
        return data

    # ------------------------------------------------------------------ #
    def geocode(self, address: str, city: str = "") -> list[Place]:
        try:
            data = self._get("/geocode/geo", {"address": address, "city": city})
        except HttpError:
            return []
        out: list[Place] = []
        for item in data.get("geocodes") or []:
            lng, _, lat = (item.get("location") or "").partition(",")
            if not lat:
                continue
            out.append(Place(
                name=item.get("formatted_address") or address,
                lat=float(lat), lng=float(lng),
                city=item.get("city") or city, admin=item.get("province", ""),
                country="中国", precision="exact", source=self.name,
            ))
        return out

    def search(self, query: str, *, count: int = 5) -> list[Place]:
        return self.geocode(query)[:count]

    # ------------------------------------------------------------------ #
    def poi_search(self, keywords: str, city: str = "", *, count: int = 20,
                   types: str = "") -> list[Poi]:
        out: list[Poi] = []
        page = 1
        while len(out) < count and page <= 3:
            try:
                data = self._get("/place/text", {
                    "keywords": keywords, "city": city, "citylimit": "true" if city else "",
                    "types": types, "offset": min(25, count - len(out)), "page": page,
                    "extensions": "all",
                })
            except HttpError:
                break
            pois = data.get("pois") or []
            if not pois:
                break
            for item in pois:
                lng, _, lat = (item.get("location") or "").partition(",")
                if not lat:
                    continue
                biz = item.get("biz_ext") or {}
                if isinstance(biz, list):
                    biz = biz[0] if biz else {}
                out.append(Poi(
                    id=f"amap:{item.get('id') or len(out)}",
                    name=item.get("name") or "",
                    city=item.get("cityname") or city,
                    category=_category(item.get("type", ""), item.get("name", "")),
                    tags=[t for t in (item.get("type", "") or "").split(";") if t][:4],
                    lat=float(lat), lng=float(lng),
                    precision="exact", geo_source=self.name,
                    rating=_num(biz.get("rating")),
                    ticket_low=_num(biz.get("cost")),
                    ticket_high=_num(biz.get("cost")),
                    indoor=_indoor(item.get("type", ""), item.get("name", "")),
                    desc=item.get("address") or "",
                    source="amap", confidence=0.95,
                ))
            page += 1
        return out[:count]

    # ------------------------------------------------------------------ #
    def route(self, origin: tuple[float, float], dest: tuple[float, float],
              mode: str = "taxi", *, city: str = "") -> TransportLeg:
        o = f"{origin[1]:.6f},{origin[0]:.6f}"       # 高德用 lng,lat
        d = f"{dest[1]:.6f},{dest[0]:.6f}"
        straight = haversine_km(origin[0], origin[1], dest[0], dest[1])
        path = {"driving": "/direction/driving", "taxi": "/direction/driving",
                "walk": "/direction/walking", "bike": "/direction/bicycling"}.get(mode, "/direction/driving")
        try:
            data = self._get(path, {"origin": o, "destination": d, "extensions": "base"})
        except HttpError:
            return _fallback_leg(origin, dest, mode, straight, "高德路径规划不可用")

        route = data.get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            return _fallback_leg(origin, dest, mode, straight, "高德未返回可行路径")
        p = paths[0]
        dist_km = round(float(p.get("distance") or 0) / 1000, 2)
        minutes = max(1, round(float(p.get("duration") or 0) / 60))
        tolls = float(p.get("tolls") or 0)
        if mode == "walk":
            low = high = 0.0
        elif mode == "bike":
            low, high = 1.0, 3.0
        else:
            low = round(min(tolls + 13 + max(0.0, dist_km - 3) * 2.3, 200), 1)
            high = round(low * 1.35 + tolls * 0.3, 1)
        return TransportLeg(mode=mode, label=_mode_label(mode), minutes=minutes,
                            distance_km=dist_km, cost_low=low, cost_high=high,
                            source=self.name, note=f"高德实测路网（红绿灯 {p.get('traffic_lights', '-')} 个）")

    # ------------------------------------------------------------------ #
    def transit(self, origin: tuple[float, float], dest: tuple[float, float],
                city: str, *, city_d: str = "") -> TransportLeg:
        o = f"{origin[1]:.6f},{origin[0]:.6f}"
        d = f"{dest[1]:.6f},{dest[0]:.6f}"
        straight = haversine_km(origin[0], origin[1], dest[0], dest[1])
        try:
            data = self._get("/direction/transit/integrated", {
                "origin": o, "destination": d, "city": city, "cityd": city_d or city,
            })
        except HttpError:
            return _fallback_leg(origin, dest, "transit", straight, "高德公交规划不可用")
        transits = ((data.get("route") or {}).get("transits")) or []
        if not transits:
            return _fallback_leg(origin, dest, "transit", straight, "高德未返回公交方案")
        t = transits[0]
        minutes = max(1, round(float(t.get("duration") or 0) / 60))
        cost = float(t.get("cost") or 0)
        return TransportLeg(mode="transit", label="公交/地铁", minutes=minutes,
                            distance_km=round(float(t.get("walking_distance") or straight * 1000) / 1000, 2),
                            cost_low=round(cost, 1), cost_high=round(cost + 2, 1),
                            transfers=max(0, len(t.get("segments") or []) - 2),
                            source=self.name, note="高德公交方案（含步行接驳）")


def _num(value: Any) -> float | None:
    try:
        if value in (None, "", "[]"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _mode_label(mode: str) -> str:
    return {"walk": "步行", "bike": "骑行", "transit": "公共交通",
            "drive": "自驾", "taxi": "打车"}.get(mode, mode)


def _fallback_leg(origin: tuple[float, float], dest: tuple[float, float], mode: str,
                  straight_km: float, note: str) -> TransportLeg:
    """高德不可用时的直线降级（标记为估算）。"""
    from ..providers.base import ROAD_FACTOR
    dist = round(straight_km * ROAD_FACTOR.get(mode, 1.3), 2)
    speed = {"walk": 4.5, "bike": 14, "transit": 22, "taxi": 30, "drive": 35}.get(mode, 28)
    minutes = max(3, round(dist / speed * 60 + 6))
    if mode == "walk":
        low = high = 0.0
    elif mode == "bike":
        low, high = 1.0, 3.0
    elif mode == "transit":
        low, high = 2.0, 12.0
    else:
        low, high = round(min(13 + max(0.0, dist - 3) * 2.3, 200), 1), round(min(18 + dist * 3.2, 280), 1)
    return TransportLeg(mode=mode, label=_mode_label(mode), minutes=minutes,
                        distance_km=dist, cost_low=low, cost_high=high,
                        source="estimate", note=note + "，按直线距离与常见路况估算")
