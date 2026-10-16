"""工具层入口：注册全部工具，并向大模型暴露 OpenAI 兼容的 function schema。"""
from __future__ import annotations

from typing import Any

from ..models import ToolResult
from . import environment, places, planning
from .base import ToolContext, ToolRegistry, ToolSpec, fail, ok

TOOL_COUNT = 9


# --------------------------------------------------------------------------- #
# 适配层：把工具函数包装成「只接受 JSON 参数」的 handler
# --------------------------------------------------------------------------- #
def _h_resolve_place(ctx: ToolContext, name: str, city: str = "") -> ToolResult:
    return places.resolve_place(ctx, name, city=city)


def _h_discover_pois(ctx: ToolContext, city: str, interests: list[str] | None = None,
                     days: int = 3, want: int = 18, exclude: list[str] | None = None) -> ToolResult:
    return places.discover_pois(ctx, city, interests=interests, days=days,
                                want=want, exclude=exclude)


def _h_city_profile(ctx: ToolContext, city: str, dates: list[str] | None = None) -> ToolResult:
    return places.city_profile(ctx, city, dates=dates)


def _h_city_weather(ctx: ToolContext, city: str, dates: list[str],
                    lat: float | None = None, lng: float | None = None) -> ToolResult:
    return environment.city_weather(ctx, city, dates=dates, lat=lat, lng=lng)


def _h_route_between(ctx: ToolContext, origin: list[float], destination: list[float],
                     mode: str = "taxi", city: str = "") -> ToolResult:
    if not (isinstance(origin, list) and isinstance(destination, list)
            and len(origin) == 2 and len(destination) == 2):
        return fail("route_between", "origin/destination 必须是 [lat, lng]")
    return environment.route_between(ctx, (float(origin[0]), float(origin[1])),
                                     (float(destination[0]), float(destination[1])),
                                     mode=mode, city=city)


def _h_opening_check(ctx: ToolContext, pois: list[Any], dates: list[str]) -> ToolResult:
    return planning.opening_check(ctx, pois=pois, dates=dates)


def _h_score_candidates(ctx: ToolContext, city: str = "", preferences: list[str] | None = None,
                        anchor: list[float] | None = None, weather: dict | None = None,
                        limit: int = 20) -> ToolResult:
    return planning.score_candidates(ctx, city=city, preferences=preferences,
                                     anchor=anchor, weather=weather, limit=limit)


def _h_estimate_budget(ctx: ToolContext, trip: dict) -> ToolResult:
    return planning.estimate_budget(ctx, trip=trip)


def _h_verify_trip(ctx: ToolContext, trip: dict) -> ToolResult:
    return planning.verify_trip(ctx, trip=trip)


# --------------------------------------------------------------------------- #
def build_registry(ctx: ToolContext) -> ToolRegistry:
    reg = ToolRegistry(ctx)

    reg.register(ToolSpec(
        name="resolve_place",
        description="把地名（城市/景区/车站）解析为带经纬度的地点。城市级坐标来自免费地理编码，"
                    "景点级坐标在无高德时由大模型估算并标记 approximate。",
        tags=["geo"],
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "description": "地名，如「丽江」「玉龙雪山」"},
            "city": {"type": "string", "description": "所在城市，用于消歧，可空"},
        }, "required": ["name"], "additionalProperties": False},
        handler=lambda name, city="": _h_resolve_place(ctx, name, city),
    ))

    reg.register(ToolSpec(
        name="discover_pois",
        description="获取某城市可用于排行程的候选地点（景点/餐饮），含标签、建议时长、门票区间、"
                    "是否需要预约、是否室内等。这是「任意目的地」的核心数据来源。",
        tags=["poi"],
        parameters={"type": "object", "properties": {
            "city": {"type": "string"},
            "interests": {"type": "array", "items": {"type": "string"},
                          "description": "兴趣偏好，如 ['自然风光','美食']"},
            "days": {"type": "integer", "description": "在該城市停留天数"},
            "want": {"type": "integer", "description": "期望返回数量，默认 18"},
            "exclude": {"type": "array", "items": {"type": "string"}, "description": "需排除的名称"},
        }, "required": ["city"], "additionalProperties": False},
        handler=lambda city, interests=None, days=3, want=18, exclude=None:
            _h_discover_pois(ctx, city, interests, days, want, exclude),
    ))

    reg.register(ToolSpec(
        name="city_profile",
        description="获取城市的住宿/餐饮价格区间与在地提示，用于预算估算。",
        tags=["poi", "budget"],
        parameters={"type": "object", "properties": {
            "city": {"type": "string"},
            "dates": {"type": "array", "items": {"type": "string"}, "description": "出行日期"},
        }, "required": ["city"], "additionalProperties": False},
        handler=lambda city, dates=None: _h_city_profile(ctx, city, dates),
    ))

    reg.register(ToolSpec(
        name="city_weather",
        description="获取某城市逐日天气（温度、降雨概率、风力、空气质量）。"
                    "超出 15 天预报窗口会回退为去年同期实测并在 basis 标注 climatology。",
        tags=["weather"],
        parameters={"type": "object", "properties": {
            "city": {"type": "string"},
            "dates": {"type": "array", "items": {"type": "string"},
                      "description": "ISO 日期数组 YYYY-MM-DD"},
            "lat": {"type": "number"}, "lng": {"type": "number"},
        }, "required": ["city", "dates"], "additionalProperties": False},
        handler=lambda city, dates, lat=None, lng=None: _h_city_weather(ctx, city, dates, lat, lng),
    ))

    reg.register(ToolSpec(
        name="route_between",
        description="计算两点之间的交通方式、时长与费用区间。"
                    "drive/taxi 走真实路网；walk/bike/transit 用球面距离与城市速度估算。",
        tags=["route"],
        parameters={"type": "object", "properties": {
            "origin": {"type": "array", "items": {"type": "number"},
                       "description": "[lat, lng]"},
            "destination": {"type": "array", "items": {"type": "number"},
                            "description": "[lat, lng]"},
            "mode": {"type": "string", "enum": ["walk", "bike", "transit", "taxi", "drive"]},
            "city": {"type": "string", "description": "所在城市（用于公交方案）"},
        }, "required": ["origin", "destination"], "additionalProperties": False},
        handler=lambda origin, destination, mode="taxi", city="":
            _h_route_between(ctx, origin, destination, mode, city),
    ))

    reg.register(ToolSpec(
        name="opening_check",
        description="校验一组地点在指定日期是否开放、是否需要预约。"
                    "会先按每周固定闭馆日做本地判断，再让大模型补充说明。",
        tags=["poi", "verify"],
        parameters={"type": "object", "properties": {
            "pois": {"type": "array", "items": {"type": "object"},
                     "description": "POI 对象数组，至少包含 name/category"},
            "dates": {"type": "array", "items": {"type": "string"}},
        }, "required": ["pois", "dates"], "additionalProperties": False},
        handler=lambda pois, dates: _h_opening_check(ctx, pois, dates),
    ))

    reg.register(ToolSpec(
        name="score_candidates",
        description="对候选池按「偏好契合/评分/距离/预算/天气/拥挤度」六维加权打分排序。",
        tags=["rank"],
        parameters={"type": "object", "properties": {
            "city": {"type": "string"},
            "preferences": {"type": "array", "items": {"type": "string"}},
            "anchor": {"type": "array", "items": {"type": "number"},
                       "description": "住宿锚点 [lat, lng]"},
            "weather": {"type": "object", "description": "当日 WeatherDay 对象"},
            "limit": {"type": "integer"},
        }, "required": [], "additionalProperties": False},
        handler=lambda city="", preferences=None, anchor=None, weather=None, limit=20:
            _h_score_candidates(ctx, city, preferences, anchor, weather, limit),
    ))

    reg.register(ToolSpec(
        name="estimate_budget",
        description="按行程明细汇总预算：城际交通、住宿、餐饮、门票、市内交通、机动杂项，"
                    "并与用户预算对比给出结论与省钱建议。",
        tags=["budget"],
        parameters={"type": "object", "properties": {
            "trip": {"type": "object", "description": "完整 Trip 对象"},
        }, "required": ["trip"], "additionalProperties": False},
        handler=lambda trip: _h_estimate_budget(ctx, trip),
    ))

    reg.register(ToolSpec(
        name="verify_trip",
        description="交付前自检：天数一致性、时间冲突、跨城串日、证据完整性、坐标精度、"
                    "步行强度，返回问题清单与统计。",
        tags=["verify"],
        parameters={"type": "object", "properties": {
            "trip": {"type": "object", "description": "完整 Trip 对象"},
        }, "required": ["trip"], "additionalProperties": False},
        handler=lambda trip: _h_verify_trip(ctx, trip),
    ))

    return reg


__all__ = ["build_registry", "ToolContext", "ToolRegistry", "ToolSpec", "ok", "fail", "TOOL_COUNT"]
