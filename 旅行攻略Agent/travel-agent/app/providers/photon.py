"""Photon（komoot，基于 OpenStreetMap，免费无需 Key）地理编码。

为什么需要它：Open-Meteo 的中文地名库有同名村庄污染——查「丽江」会返回湖南的
一个同名村落，查「大理」返回四川的同名地。Photon 走 OSM，对中文（尤其中国）
地名覆盖明显更好，且返回地物类型（city/town/lake/station…），可以用来判断
「这个名字到底是城市还是一条河」。

使用注意：Photon 是公益服务，请在低频率场景使用；商用请自建实例。
"""
from __future__ import annotations

from ..http import HttpError, request_json
from ..models import Place

PHOTON_URL = "https://photon.komoot.io/api"

# OSM 地物类型 → 站内可信度（只用于排序，不改变原始数据）
STRONG_FEATURES = {"city", "state", "province", "county", "region", "district", "town",
                   "municipality", "administrative"}


class PhotonGeocoder:
    name = "photon"

    def search(self, query: str, *, count: int = 5) -> list[Place]:
        query = (query or "").strip()
        if not query:
            return []
        try:
            data = request_json(PHOTON_URL, params={"q": query, "limit": max(3, count)},
                                timeout=15)
        except HttpError:
            return []
        out: list[Place] = []
        for feat in (data or {}).get("features") or []:
            props = feat.get("properties") or {}
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            try:
                lng, lat = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                continue
            name = (props.get("name") or "").strip()
            if not name:
                continue
            osm_value = (props.get("osm_value") or props.get("type") or "").lower()
            out.append(Place(
                name=name, lat=lat, lng=lng,
                city=props.get("city") or name,
                admin=props.get("state") or props.get("county") or "",
                country=_cn_country(props.get("country") or ""),
                precision="exact" if osm_value in STRONG_FEATURES else "approximate",
                source=self.name, feature=osm_value,
            ))
        return out


def _cn_country(raw: str) -> str:
    """OSM 里中国的国家名有简繁两种写法，统一一下。"""
    if raw in ("中国", "中華人民共和國", "中华人民共和国", "China", "People's Republic of China"):
        return "中国"
    if raw in ("臺灣", "台湾", "Taiwan"):
        return "中国台湾"
    if raw in ("香港", "Hong Kong"):
        return "中国香港"
    if raw in ("澳門", "澳门", "Macao", "Macau"):
        return "中国澳门"
    return raw
