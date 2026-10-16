"""供应商工厂：按配置装配「地理编码 / 天气 / 路径规划 / POI 检索」。

设计要点：
- 有 AMAP_KEY → 高德负责地理编码、POI 检索、路径规划（真实路网 + 景点级坐标）
- 无 AMAP_KEY → Photon + Open-Meteo 双源合并地理编码，OSRM/启发式负责路径
- 大模型负责所有「非结构化知识」（POI 清单、城市画像、开放时间），并在
  坐标来源冲突时做交叉验证；缺坐标的 POI 标记 precision=approximate
"""
from __future__ import annotations

from dataclasses import dataclass

from ..settings import SETTINGS, Settings
from .amap import AmapClient
from .base import Geocoder, Forecaster, Router, haversine_km, rough_distance_km
from .composite import CompositeGeocoder
from .open_meteo import OpenMeteoForecast, OpenMeteoGeocoder
from .osrm import HybridRouter, OsrmRouter, intercity_leg
from .photon import PhotonGeocoder


@dataclass
class Providers:
    geocoder: Geocoder
    forecaster: Forecaster
    router: Router
    amap: AmapClient | None = None

    @property
    def has_amap(self) -> bool:
        return self.amap is not None

    def poi_source(self) -> str:
        """结构化 POI 的真实来源名（用于 evidence 展示）。"""
        return "amap" if self.amap else "llm"

    def geo_note(self) -> str:
        if self.amap:
            return "高德地图（景点级坐标，GCJ-02）"
        return "Photon + Open-Meteo 双源地理编码（城市级，WGS-84），景点坐标由大模型给出"


def build_providers(settings: Settings | None = None) -> Providers:
    s = settings or SETTINGS
    amap = AmapClient(s.amap_key) if s.amap_key else None
    if amap is not None:
        geocoder: Geocoder = amap
    else:
        geocoder = CompositeGeocoder([PhotonGeocoder(), OpenMeteoGeocoder()])
    router: Router = amap if amap else HybridRouter(OsrmRouter(s.osrm_url))
    return Providers(geocoder=geocoder, forecaster=OpenMeteoForecast(), router=router, amap=amap)


__all__ = [
    "Providers", "build_providers", "AmapClient", "OpenMeteoForecast",
    "OpenMeteoGeocoder", "PhotonGeocoder", "CompositeGeocoder",
    "HybridRouter", "OsrmRouter", "haversine_km", "rough_distance_km", "intercity_leg",
]

