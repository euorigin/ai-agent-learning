"""供应商适配器接口与通用几何计算。

上层只依赖这里的协议；换供应商（Open-Meteo ↔ 高德 ↔ 其他）不影响工具层与编排层。
"""
from __future__ import annotations

import math
from typing import Protocol

from ..models import Place, TransportLeg, WeatherDay

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(max(0.0, min(1.0, a))))


ROAD_FACTOR = {"walk": 1.25, "bike": 1.28, "transit": 1.35, "taxi": 1.32, "drive": 1.30}


def rough_distance_km(a: tuple[float, float], b: tuple[float, float], mode: str = "taxi") -> float:
    return round(haversine_km(a[0], a[1], b[0], b[1]) * ROAD_FACTOR.get(mode, 1.3), 2)


class Geocoder(Protocol):
    name: str

    def search(self, query: str, *, count: int = 5) -> list[Place]:
        ...


class Forecaster(Protocol):
    name: str

    def daily(self, location: str, lat: float | None, lng: float | None, dates: list[str]) -> list[WeatherDay]:
        ...


class Router(Protocol):
    name: str

    def route(self, origin: tuple[float, float], dest: tuple[float, float], mode: str) -> TransportLeg:
        ...
