"""Open-Meteo（免费、无需 Key、全球覆盖）实现：地理编码 + 逐日预报 + 空气质量。

- 地理编码：城市 / 行政区 / 知名地标。景点级名称命中率有限，因此坐标优先级为
  高德（有 Key）> 本服务（命中）> 大模型估算（标记 approximate）。
- 预报：官方预报窗口 15 天；超出窗口时回退到「去年同月日」的历史实测数据，
  并在 basis 中标注 archive，避免编造天气。
"""
from __future__ import annotations

import threading
from datetime import date, datetime

from ..http import HttpError, request_json
from ..models import Place, WeatherDay

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

FORECAST_WINDOW_DAYS = 15
ARCHIVE_WINDOW_DAYS = -370      # 超远期（>1 年前）历史数据也不可靠

# WMO weather code → 中文描述
WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "毛毛雨", 53: "小雨", 55: "中雨", 56: "冻毛毛雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}


def _condition(code: int | None) -> str:
    if code is None:
        return "未知"
    return WMO.get(int(code), f"天气代码 {code}")


class OpenMeteoGeocoder:
    name = "open-meteo-geocoding"

    def search(self, query: str, *, count: int = 5) -> list[Place]:
        query = (query or "").strip()
        if not query:
            return []
        try:
            data = request_json(GEOCODE_URL, params={"name": query, "count": count,
                                                     "language": "zh", "format": "json"}, timeout=15)
        except HttpError:
            return []
        out: list[Place] = []
        for item in (data or {}).get("results") or []:
            try:
                lat, lng = float(item["latitude"]), float(item["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append(Place(
                name=item.get("name", query), lat=lat, lng=lng,
                city=item.get("name", ""), admin=item.get("admin1", ""),
                country=item.get("country", ""), precision="exact", source=self.name,
                feature=item.get("feature_code", "") or "",
                population=item.get("population") if isinstance(item.get("population"), int) else None,
            ))
        return out


class OpenMeteoForecast:
    name = "open-meteo"

    def __init__(self) -> None:
        self._cache: dict[str, WeatherDay] = {}
        self._aqi_cache: dict[str, int | None] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def daily(self, location: str, lat: float | None, lng: float | None,
              dates: list[str]) -> list[WeatherDay]:
        if lat is None or lng is None or not dates:
            return [self._unavailable(location, d) for d in dates]

        today = date.today()
        coords = (lat, lng)
        out: list[WeatherDay] = []
        for d in dates:
            key = f"{round(lat, 3)},{round(lng, 3)},{d}"
            with self._lock:
                hit = self._cache.get(key)
            if hit is not None:
                out.append(hit)
                continue
            try:
                day = datetime.fromisoformat(d).date()
            except ValueError:
                out.append(self._unavailable(location, d))
                continue
            offset = (day - today).days
            try:
                if 0 <= offset <= FORECAST_WINDOW_DAYS:
                    wx = self._fetch_forecast(location, d, lat, lng)
                elif ARCHIVE_WINDOW_DAYS <= offset < 0:
                    wx = self._fetch_archive(location, d, lat, lng)
                else:
                    wx = self._climatology(location, d, lat, lng)
            except HttpError:
                wx = self._unavailable(location, d)
            with self._lock:
                self._cache[key] = wx
            out.append(wx)
        return out

    # ------------------------------------------------------------------ #
    def _fetch_forecast(self, location: str, d: str, lat: float, lng: float) -> WeatherDay:
        data = request_json(FORECAST_URL, params={
            "latitude": lat, "longitude": lng,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
            "timezone": "auto", "start_date": d, "end_date": d,
        }, timeout=20)
        daily = (data or {}).get("daily") or {}
        return self._build(location, d, daily, source=self.name, basis="forecast", coords=(lat, lng))

    def _fetch_archive(self, location: str, d: str, lat: float, lng: float) -> WeatherDay:
        """过去日期：取实测归档。"""
        data = request_json(ARCHIVE_URL, params={
            "latitude": lat, "longitude": lng,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_sum,wind_speed_10m_max",
            "timezone": "auto", "start_date": d, "end_date": d,
        }, timeout=25)
        daily = (data or {}).get("daily") or {}
        return self._build(location, d, daily, source=self.name, basis="archive", coords=(lat, lng))

    def _climatology(self, location: str, d: str, lat: float, lng: float) -> WeatherDay:
        """超出预报窗口：取去年同月日的历史实测，给出量级参考而非编造。"""
        day = datetime.fromisoformat(d).date()
        target = day.replace(year=day.year - 1)
        data = request_json(ARCHIVE_URL, params={
            "latitude": lat, "longitude": lng,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_sum,wind_speed_10m_max",
            "timezone": "auto", "start_date": target.isoformat(), "end_date": target.isoformat(),
        }, timeout=25)
        daily = (data or {}).get("daily") or {}
        wx = self._build(location, d, daily, source=self.name, basis="climatology", coords=(lat, lng))
        if wx.condition == "未知":
            return self._unavailable(location, d)
        return wx

    @staticmethod
    def _pick(daily: dict, key: str, index: int = 0):
        values = daily.get(key) or []
        return values[index] if index < len(values) else None

    def _build(self, location: str, d: str, daily: dict, *, source: str,
               basis: str, coords: tuple[float, float]) -> WeatherDay:
        tmax = self._pick(daily, "temperature_2m_max")
        tmin = self._pick(daily, "temperature_2m_min")
        prob = self._pick(daily, "precipitation_probability_max")
        precip = self._pick(daily, "precipitation_sum") or 0.0
        wind = self._pick(daily, "wind_speed_10m_max") or 8.0
        code = self._pick(daily, "weather_code")
        if prob is None:
            prob = 0 if float(precip or 0) <= 0.2 else min(95, int(40 + float(precip) * 12))
        return WeatherDay(
            location=location, date=d, condition=_condition(code),
            temp_low=round(float(tmin), 1) if tmin is not None else 0.0,
            temp_high=round(float(tmax), 1) if tmax is not None else 0.0,
            rain_prob=int(prob), precip_mm=round(float(precip), 1),
            wind_kmh=round(float(wind), 1),
            aqi=self._aqi(coords),
            source=source, basis=basis,
        )

    # ------------------------------------------------------------------ #
    def _aqi(self, coords: tuple[float, float]) -> int | None:
        key = f"{round(coords[0], 2)},{round(coords[1], 2)}"
        with self._lock:
            if key in self._aqi_cache:
                return self._aqi_cache[key]
        value = self._aqi_for(*coords)
        with self._lock:
            self._aqi_cache[key] = value
        return value

    @staticmethod
    def _aqi_for(lat: float, lng: float) -> int | None:
        try:
            data = request_json(AIR_URL, params={
                "latitude": lat, "longitude": lng, "current": "us_aqi", "timezone": "auto",
            }, timeout=12)
        except HttpError:
            return None
        value = ((data or {}).get("current") or {}).get("us_aqi")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _unavailable(location: str, d: str) -> WeatherDay:
        return WeatherDay(location=location, date=d, condition="数据不可用（网络异常）",
                          temp_low=0, temp_high=0, rain_prob=0, wind_kmh=0, basis="unavailable")
