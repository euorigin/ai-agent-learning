"""OSRM（公开路网，无需 Key）+ 启发式降级路由器。

公开的 router.project-osrm.org 只开放 driving profile，因此：
- 驾车 / 打车 → 调 OSRM 取真实路网距离与时长
- 步行 / 骑行 / 公交 → 用球面距离 × 道路系数 + 城市场景速度估算（标注 estimate）
"""
from __future__ import annotations

from ..http import HttpError, request_json
from ..models import TransportLeg
from .base import ROAD_FACTOR, haversine_km

# 各方式的城市内平均速度（km/h），含停靠与等待
SPEED_KMH = {"walk": 4.5, "bike": 13.5, "transit": 20.0, "taxi": 28.0, "drive": 32.0}
MODE_LABEL = {"walk": "步行", "bike": "骑行", "transit": "公共交通",
              "drive": "自驾", "taxi": "打车"}


class OsrmRouter:
    name = "osrm"

    def __init__(self, base_url: str = "https://router.project-osrm.org") -> None:
        self.base_url = base_url.rstrip("/")

    def _osrm(self, origin: tuple[float, float], dest: tuple[float, float]) -> tuple[float, float] | None:
        coords = f"{origin[1]:.6f},{origin[0]:.6f};{dest[1]:.6f},{dest[0]:.6f}"
        try:
            data = request_json(f"{self.base_url}/route/v1/driving/{coords}",
                                params={"overview": "false", "alternatives": "false"}, timeout=15)
        except HttpError:
            return None
        routes = (data or {}).get("routes") or []
        if not routes:
            return None
        return float(routes[0].get("distance") or 0) / 1000, float(routes[0].get("duration") or 0) / 60

    def route(self, origin: tuple[float, float], dest: tuple[float, float],
              mode: str = "taxi", **_: object) -> TransportLeg:
        straight = haversine_km(origin[0], origin[1], dest[0], dest[1])
        if mode in ("drive", "taxi"):
            got = self._osrm(origin, dest)
            if got is not None:
                dist, minutes = got
                if mode == "drive":
                    low = round(dist * 0.75, 1)          # 自驾按油费+过路费粗估
                    high = round(dist * 1.15 + 25, 1)
                else:
                    low = round(min(13 + max(0.0, dist - 3) * 2.3, 200), 1)
                    high = round(min(18 + dist * 3.0, 280), 1)
                return TransportLeg(mode=mode, label=MODE_LABEL[mode],
                                    minutes=max(1, round(minutes)), distance_km=round(dist, 2),
                                    cost_low=low, cost_high=high, source=self.name,
                                    note="OSRM 真实路网（驾车路径代理）")
        return heuristic_leg(origin, dest, mode, straight)


def heuristic_leg(origin: tuple[float, float], dest: tuple[float, float], mode: str,
                  straight_km: float | None = None, note: str = "") -> TransportLeg:
    straight = haversine_km(origin[0], origin[1], dest[0], dest[1]) if straight_km is None else straight_km
    dist = round(straight * ROAD_FACTOR.get(mode, 1.3), 2)
    speed = SPEED_KMH.get(mode, 25.0)
    minutes = max(3, round(dist / speed * 60 + (6 if mode in ("transit", "taxi") else 0)))
    if mode == "walk":
        low = high = 0.0
    elif mode == "bike":
        low, high = 1.0, 3.0
    elif mode == "transit":
        low, high = 2.0, 12.0
    elif mode == "drive":
        low, high = round(dist * 0.75, 1), round(dist * 1.15 + 25, 1)
    else:
        low = round(min(13 + max(0.0, dist - 3) * 2.3, 200), 1)
        high = round(min(18 + dist * 3.0, 280), 1)
    return TransportLeg(mode=mode, label=MODE_LABEL.get(mode, mode), minutes=minutes,
                        distance_km=dist, cost_low=low, cost_high=high, source="estimate",
                        note=(note or "按直线距离与城市平均速度估算"))


class HybridRouter:
    """优先真实路网，失败或非驾车场景走启发式。"""

    name = "hybrid"

    def __init__(self, osrm: OsrmRouter | None = None) -> None:
        self.osrm = osrm or OsrmRouter()

    def route(self, origin: tuple[float, float], dest: tuple[float, float],
              mode: str = "taxi", **kw: object) -> TransportLeg:
        return self.osrm.route(origin, dest, mode, **kw)


def intercity_leg(origin_name: str, dest_name: str,
                  origin: tuple[float, float], dest: tuple[float, float]) -> TransportLeg:
    """城市间交通：按直线距离选择高铁 / 飞机 / 长途大巴，并给出时长与票价区间。"""
    km = haversine_km(origin[0], origin[1], dest[0], dest[1])
    if km < 120:
        return TransportLeg(mode="drive", label="自驾/大巴", minutes=max(60, round(km / 70 * 60)),
                            distance_km=round(km, 1), cost_low=round(km * 0.5, 1),
                            cost_high=round(km * 0.9, 1), source="estimate",
                            note=f"{origin_name}→{dest_name} 短途，建议自驾或大巴")
    if km < 1200:
        minutes = round(km / 200 * 60 + 70)        # 高铁均速约 200km/h + 进出站
        return TransportLeg(mode="rail", label="高铁/动车", minutes=minutes,
                            distance_km=round(km, 1), cost_low=round(km * 0.42, 0),
                            cost_high=round(km * 0.55, 0), source="estimate",
                            note=f"{origin_name}→{dest_name} 高铁约 {minutes // 60} 小时")
    minutes = round(km / 750 * 60 + 180)           # 飞行 + 机场流程
    return TransportLeg(mode="flight", label="飞机", minutes=minutes,
                        distance_km=round(km, 1), cost_low=round(min(300 + km * 0.45, 1600), 0),
                        cost_high=round(min(500 + km * 0.75, 2600), 0), source="estimate",
                        note=f"{origin_name}→{dest_name} 建议飞行")
