"""多源地理编码合并：把多个免费数据源的候选放在一起打分排序。

动机：单一免费源都不可靠——
- Open-Meteo 的中文库有同名村庄污染（「丽江」→ 湖南村落）
- Photon 走 OSM，中国地名准，但中文查境外城市（东京/巴黎）会退化成国内同名词
所以两个源一起查、按「名称匹配 + 地物类型 + 人口 + 国别」打分，取最高分。
分数过低时把 precision 降为 unknown，让上层明确告知「这个坐标不可靠」。
"""
from __future__ import annotations

import math
import re
from typing import Iterable

from ..models import Place

# 地物类型 → 基础分
TYPE_SCORE = {
    "city": 5.0, "municipality": 5.0, "state": 4.5, "province": 4.5, "region": 4.0,
    "county": 4.0, "administrative": 4.0, "district": 3.0, "town": 3.0, "suburb": 2.0,
    "village": 1.5, "hamlet": 1.5, "peak": 2.6, "mountain": 2.6, "lake": 2.6,
    "water": 2.2, "nature_reserve": 2.4, "island": 2.0, "attraction": 2.4,
    "station": 0.5, "stop": 0.3, "aerodrome": 0.5, "apartments": 0.4, "clothes": 0.2,
    # GeoNames/Open-Meteo 的 feature_code
    "PPLC": 5.0, "PPLA": 4.5, "PPLA2": 4.0, "PPLA3": 3.5, "PPLA4": 3.0,
    "PPL": 1.5, "PPLL": 1.2, "AIRP": 0.5, "ADM1": 4.5, "ADM2": 4.0, "ADM3": 3.5,
}

WEAK_FEATURES = {"village", "hamlet", "stop", "station", "apartments", "clothes", "PPL", "PPLL"}
CONFIDENT_SCORE = 6.0           # 低于此分 → 坐标不可靠
SUFFIX = re.compile(r"(市|省|自治区|特别行政区|自治州|地区|盟|县|区|镇|街道)$")
CN_CJK = re.compile(r"[\u4e00-\u9fa5]")


def _norm(name: str) -> str:
    text = (name or "").strip()
    text = SUFFIX.sub("", text)
    return text


def score_place(place: Place, query: str) -> float:
    q, n = _norm(query), _norm(place.name)
    score = 0.0
    if n and n == q:
        score += 3.0
    elif n and (q in n or n in q):
        score += 1.5

    feature = (place.feature or "").lower()
    type_score = TYPE_SCORE.get(feature, TYPE_SCORE.get(place.feature or "", 1.0))
    score += type_score

    pop = place.population or 0
    if pop > 1000:
        score += max(0.0, min(3.0, math.log10(pop) - 3.0))

    is_cjk = bool(CN_CJK.search(query))
    if is_cjk and place.country == "中国":
        score += 1.5
    elif is_cjk and place.country and place.country != "中国" and type_score < 3.0:
        score -= 1.0                      # 中文查询命中境外弱地物，多半是同名误匹配
    if feature in WEAK_FEATURES:
        score -= 0.5
    return round(score, 3)


class CompositeGeocoder:
    """把多个地理编码源合并成一个（按分数排序）。"""

    def __init__(self, sources: Iterable[object]) -> None:
        self.sources = [s for s in sources if s is not None]
        self.name = "+".join(getattr(s, "name", "?") for s in self.sources) or "none"

    def search(self, query: str, *, count: int = 5) -> list[Place]:
        query = (query or "").strip()
        if not query:
            return []
        pool: list[Place] = []
        for src in self.sources:
            try:
                pool.extend(src.search(query, count=count))
            except Exception:                     # noqa: BLE001 —— 单源失败不影响整体
                continue
        if not pool:
            return []

        ranked = sorted(pool, key=lambda p: score_place(p, query), reverse=True)

        out: list[Place] = []
        seen: set[tuple[int, int]] = set()
        for place in ranked:
            key = (round(place.lat * 20), round(place.lng * 20))     # ~5km 去重
            if key in seen:
                continue
            seen.add(key)
            if score_place(place, query) < CONFIDENT_SCORE:
                place.precision = "unknown"
            out.append(place)
            if len(out) >= count:
                break
        return out

    def best(self, query: str) -> Place | None:
        hits = self.search(query, count=3)
        return hits[0] if hits else None
