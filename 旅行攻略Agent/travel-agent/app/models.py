"""核心数据模型。

约定：任何来自外部的字段都带来源与精度标记（source / precision / confidence），
因为本系统对「可信」的要求是：**不确定就明说**，不允许伪精确。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

Pace = Literal["relaxed", "normal", "intensive"]
Precision = Literal["exact", "approximate", "unknown"]


# --------------------------------------------------------------------------- #
# 需求（§3.1）
# --------------------------------------------------------------------------- #
class TripRequest(BaseModel):
    origin: str = Field(description="出发地")
    destinations: list[str] = Field(default_factory=list, description="目的地，支持多城市")
    start_date: date
    end_date: date
    budget: Optional[float] = None
    preferences: list[str] = Field(default_factory=list)
    pace: Pace = "normal"
    travelers: int = Field(default=1, ge=1, le=30)
    rooms: int = Field(default=1, ge=1, le=10)
    mobility: Literal["normal", "reduced"] = "normal"
    avoid_repeat_hotel: bool = True

    @model_validator(mode="after")
    def _check(self) -> "TripRequest":
        if self.end_date < self.start_date:
            raise ValueError("结束日期不得早于开始日期")
        if (self.end_date - self.start_date).days > 60:
            raise ValueError("单次行程最长支持 60 天")
        if not self.destinations:
            raise ValueError("目的地不能为空")
        return self

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1


# --------------------------------------------------------------------------- #
# 外部事实
# --------------------------------------------------------------------------- #
class Place(BaseModel):
    name: str
    lat: float
    lng: float
    city: str = ""
    admin: str = ""
    country: str = ""
    precision: Precision = "exact"
    source: str = ""
    feature: str = ""                    # 地物类型（city/town/lake/PPLA…），用于可信度排序
    population: Optional[int] = None


class Poi(BaseModel):
    id: str
    name: str
    city: str
    category: str = "景点"
    tags: list[str] = Field(default_factory=list)
    lat: Optional[float] = None
    lng: Optional[float] = None
    precision: Precision = "unknown"
    geo_source: str = ""
    rating: Optional[float] = None
    ticket_low: Optional[float] = None
    ticket_high: Optional[float] = None
    stay_minutes: int = 100
    open: str = ""
    close: str = ""
    closed_weekdays: list[int] = Field(default_factory=list)   # 0=周一
    reservation: bool = False
    indoor: bool = False
    altitude: Optional[float] = None
    risk_tags: list[str] = Field(default_factory=list)
    desc: str = ""
    source: str = "llm"
    confidence: float = 0.6

    @property
    def is_sight(self) -> bool:
        return self.category not in {"餐饮", "小吃", "咖啡", "茶馆", "购物"}

    @property
    def is_meal(self) -> bool:
        return self.category in {"餐饮", "小吃"}


class WeatherDay(BaseModel):
    location: str
    date: str
    condition: str
    temp_low: float
    temp_high: float
    rain_prob: int
    precip_mm: float = 0.0
    wind_kmh: float = 10.0
    visibility_km: Optional[float] = None
    aqi: Optional[int] = None
    alerts: list[str] = Field(default_factory=list)
    source: str = "open-meteo"
    basis: str = "forecast"      # forecast | archive | unavailable

    @property
    def is_rainy(self) -> bool:
        return self.rain_prob > 60


class TransportLeg(BaseModel):
    mode: str
    label: str
    minutes: int
    distance_km: float
    cost_low: float
    cost_high: float
    transfers: int = 0
    source: str = ""
    note: str = ""


class OpeningInfo(BaseModel):
    poi_name: str
    date: str
    open: bool = True
    window: str = ""
    closed_weekdays: list[int] = Field(default_factory=list)
    reservation: bool = False
    reason: str = ""
    source: str = "llm"
    confidence: float = 0.5


# --------------------------------------------------------------------------- #
# 行程
# --------------------------------------------------------------------------- #
class ItineraryItem(BaseModel):
    item_id: str
    date: str
    order: int
    poi_id: str
    name: str
    category: str
    city: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    indoor: bool = False
    arrive: str = ""
    leave: str = ""
    stay_minutes: int = 0
    transport: Optional[TransportLeg] = None
    cost_low: float = 0
    cost_high: float = 0
    score: float = 0.0
    reason: str = ""
    risk: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)   # 数据来源与依据
    locked: bool = False
    done: bool = False


class DayPlan(BaseModel):
    date: str
    weekday: str
    city: str
    weather: Optional[WeatherDay] = None
    items: list[ItineraryItem] = Field(default_factory=list)
    walking_km: float = 0.0
    active_minutes: int = 0
    risk_notes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class BudgetLine(BaseModel):
    category: str
    low: float
    high: float
    basis: str = ""


class BudgetPlan(BaseModel):
    currency: str = "CNY"
    lines: list[BudgetLine] = Field(default_factory=list)
    total_low: float = 0
    total_high: float = 0
    user_budget: Optional[float] = None
    verdict: str = ""
    advice: list[str] = Field(default_factory=list)
    basis: str = ""


class ChangeLog(BaseModel):
    version: int
    at: str
    reason: str
    changes: list[str] = Field(default_factory=list)


class CityProfile(BaseModel):
    city: str
    lodging_low: Optional[float] = None
    lodging_high: Optional[float] = None
    meal_low: Optional[float] = None
    meal_high: Optional[float] = None
    local_notes: list[str] = Field(default_factory=list)
    source: str = "llm"
    confidence: float = 0.55


class Trip(BaseModel):
    trip_id: str
    request: TripRequest
    days: list[DayPlan] = Field(default_factory=list)
    budget: Optional[BudgetPlan] = None
    intercity: list[TransportLeg] = Field(default_factory=list)
    stay_area: Optional[dict] = None
    profiles: dict[str, CityProfile] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)          # 行程级数据来源清单
    version: int = 1
    engine: str = "rules"
    warnings: list[str] = Field(default_factory=list)
    weather_overrides: dict[str, dict] = Field(default_factory=dict)
    history: list[ChangeLog] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


# --------------------------------------------------------------------------- #
# 工具与对话
# --------------------------------------------------------------------------- #
class ToolResult(BaseModel):
    tool: str
    ok: bool = True
    data: Any = None
    source: str = ""
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    confidence: float = 0.8
    errors: list[str] = Field(default_factory=list)
    needs_confirmation: bool = False


class ToolTrace(BaseModel):
    step: int
    tool: str
    ok: bool
    source: str = ""
    confidence: float = 0
    summary: str = ""
    latency_ms: int = 0
    degraded: bool = False


class ChatRequest(BaseModel):
    message: str
    trip_id: Optional[str] = None
    day: Optional[str] = None


class ChatResponse(BaseModel):
    intent: str
    reply: str
    engine: str = "rules"
    trip: Optional[Trip] = None
    trace: list[ToolTrace] = Field(default_factory=list)
    slots: dict = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)
    warning: Optional[str] = None
