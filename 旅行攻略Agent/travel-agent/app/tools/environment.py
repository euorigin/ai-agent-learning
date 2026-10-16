"""环境类工具：天气窗口、两点交通。数据来自 Open-Meteo / 高德 / OSRM，
全部是免费真实数据源，失败时明确降级并标注，不编造。"""
from __future__ import annotations

from typing import Any

from ..models import TransportLeg, ToolResult, WeatherDay
from .base import ToolContext, fail, ok


def city_weather(ctx: ToolContext, city: str, *, dates: list[str],
                 lat: float | None = None, lng: float | None = None) -> ToolResult:
    """取某城市逐日天气。超出预报窗口会自动回退到去年同期实测（basis=climatology）。"""
    city = (city or "").strip()
    dates = [d for d in (dates or []) if d]
    if not city or not dates:
        return fail("city_weather", "城市或日期为空")

    if lat is None or lng is None:
        from .places import resolve_place
        got = resolve_place(ctx, city, city=city)
        if not got.ok:
            return fail("city_weather", f"缺少「{city}」坐标且解析失败：{'; '.join(got.errors)}")
        lat, lng = got.data["lat"], got.data["lng"]

    forecaster = ctx.providers.forecaster if ctx.providers else None
    if forecaster is None:
        return fail("city_weather", "未配置天气数据源")
    try:
        days: list[WeatherDay] = forecaster.daily(city, lat, lng, dates)
    except Exception as exc:                        # noqa: BLE001
        return fail("city_weather", f"天气接口异常：{exc}")

    usable = [d for d in days if d.basis != "unavailable"]
    if not usable:
        return fail("city_weather", "天气数据全部不可用（网络异常或超出服务范围）",
                    data=[d.model_dump() for d in days])

    bases = sorted({d.basis for d in usable})
    source = usable[0].source
    note = ""
    if "climatology" in bases:
        note = "部分日期超出 15 天预报窗口，已回退为去年同期实测（仅供量级参考）"
    confidence = 0.9 if bases == ["forecast"] else (0.55 if "climatology" in bases else 0.7)
    result = ok("city_weather", [d.model_dump() for d in days], source=source,
                confidence=confidence)
    if note:
        result.errors.append(note)
    return result


def route_between(ctx: ToolContext, origin: tuple[float, float], dest: tuple[float, float],
                  *, mode: str = "taxi", city: str = "") -> ToolResult:
    """两点之间交通时长与费用。origin/dest 为 (lat, lng)。"""
    if not origin or not dest:
        return fail("route_between", "缺少起终点坐标")
    router = ctx.providers.router if ctx.providers else None
    if router is None:
        return fail("route_between", "未配置路径规划数据源")
    try:
        if mode == "transit" and ctx.providers and ctx.providers.amap and city:
            leg = ctx.providers.amap.transit(origin, dest, city)
        else:
            leg = router.route(origin, dest, mode)
    except Exception as exc:                        # noqa: BLE001
        return fail("route_between", f"路径规划失败：{exc}")

    if not isinstance(leg, TransportLeg):
        return fail("route_between", "路径规划返回结构异常")
    confidence = 0.85 if leg.source in ("osrm", "amap") else 0.55
    return ok("route_between", leg.model_dump(), source=leg.source, confidence=confidence)
