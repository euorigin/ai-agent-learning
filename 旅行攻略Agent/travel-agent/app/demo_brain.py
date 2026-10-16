"""离线演示脑：不联网、不花 Token 也能跑通完整链路。

用途有两个：
1. 你在填 API Key 之前，能立刻看到完整的前后端效果与数据结构；
2. 作为自动化测试的「可预测大脑」，让编排层不依赖真实模型也能被验证。

**重要**：本模块给出的 POI 是**合成数据**（通用类型化地点，不是真实推荐），
坐标一律留空并标记 unknown，行程会附上醒目的提示。请只把它当 UI/流程演示，
真实规划请配置大模型 API（见 .env）。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from .llm import LLMResponse, ToolCall  # noqa: F401
from .prompts import (NLU_SYSTEM, NARRATE_SYSTEM, OPENING_SYSTEM, POI_SYSTEM,
                      PROFILE_SYSTEM, QA_SYSTEM)

DEMO_NOTICE = "当前使用「离线演示脑」：地点为合成数据、坐标为未知，仅用于流程演示。" \
              "配置大模型 API（.env）后可获得真实推荐。"

# 通用类型化地点模板（不冒充真实景点，名字即说明）
POI_TEMPLATES = [
    ("历史街区", "文化", ["历史", "古镇", "人文"], 120, 0, 0, False, 0),
    ("博物馆", "博物馆", ["博物馆", "文化", "室内"], 120, 0, 60, True, 0),
    ("城市公园", "自然", ["自然", "湖", "风光"], 100, 0, 0, False, 0),
    ("观景平台", "观景", ["观景", "摄影", "夜景"], 80, 20, 60, False, 300),
    ("古寺庙宇", "文化", ["历史", "寺庙", "人文"], 90, 0, 50, False, 100),
    ("湿地/江畔步道", "自然", ["自然", "徒步", "风光"], 110, 0, 0, False, 0),
    ("艺术园区", "文化", ["文艺", "摄影", "小众"], 100, 0, 80, True, 0),
    ("山地徒步路线", "户外", ["徒步", "登山", "森林"], 180, 30, 80, False, 1200),
    ("主题乐园", "亲子", ["亲子", "乐园", "适合儿童"], 240, 120, 320, False, 0),
    ("温泉度假区", "自然", ["温泉", "泡汤", "休闲"], 150, 120, 260, False, 0),
    ("夜市街区", "景点", ["夜市", "美食", "夜景"], 90, 0, 0, False, 0),
    ("特色商业街", "景点", ["购物", "商业街", "美食"], 100, 0, 0, False, 0),
    ("地标广场", "观景", ["地标", "打卡", "热门"], 60, 0, 0, False, 0),
    ("老城区巷弄", "文化", ["小众", "人文", "街拍"], 90, 0, 0, False, 0),
]
MEAL_TEMPLATES = [
    ("本地特色餐厅", "餐饮", ["美食", "本地菜"], 70, 40, 90),
    ("老字号小吃店", "小吃", ["小吃", "美食", "平价"], 45, 15, 40),
    ("街边咖啡/茶馆", "咖啡", ["咖啡", "文艺", "休闲"], 50, 25, 60),
]


def _digest(text: str) -> int:
    return int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)


# --------------------------------------------------------------------------- #
class RoutedLLM:
    """按系统提示词特征路由到不同应答的离线模型。"""

    def __init__(self, routes: list[tuple[str, Callable[..., LLMResponse]]],
                 default: Callable[..., LLMResponse] | None = None,
                 *, name: str = "demo-brain") -> None:
        self.routes = routes
        self.default = default
        self.name = name
        self.calls: list[list[dict]] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def tool_mode(self) -> str:
        return "prompt"          # 不模拟原生工具调用，走 JSON 提示词路线

    def chat(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        self.calls.append(messages)
        system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        user = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        for marker, producer in self.routes:
            if marker in system:
                return producer(system, user)
        if self.default is not None:
            return self.default(system, user)
        return LLMResponse(content="{}")


# --------------------------------------------------------------------------- #
def _nlu(system: str, user: str) -> LLMResponse:
    """用规则解析器充当 NLU，保证演示脑与规则引擎口径一致。"""
    from .agent import nlu as nlu_mod
    from datetime import date as _date

    m = re.search(r"今天是\s*(\d{4}-\d{2}-\d{2})", user)
    today = _date.fromisoformat(m.group(1)) if m else _date.today()
    msg_m = re.search(r"用户说：(.+?)(?:\n|$)", user)
    message = msg_m.group(1).strip() if msg_m else user
    parsed = nlu_mod.parse(message, llm=None, today=today)
    payload = {
        "intent": parsed.intent,
        "origin": parsed.slots.get("origin"),
        "destinations": parsed.slots.get("destinations") or [],
        "start_date": parsed.slots.get("start_date"),
        "end_date": parsed.slots.get("end_date"),
        "days": parsed.slots.get("days"),
        "budget": parsed.slots.get("budget"),
        "preferences": parsed.slots.get("preferences") or [],
        "pace": parsed.slots.get("pace") or "normal",
        "travelers": parsed.slots.get("travelers") or 1,
        "mobility": parsed.slots.get("mobility") or "normal",
        "edit": parsed.edit or None,
        "reply": parsed.reply,
        "need_more": bool(parsed.need_more),
    }
    return LLMResponse(content=_json(payload))


def _pois(system: str, user: str) -> LLMResponse:
    city_m = re.search(r"目的地城市：(.+)", user)
    city = (city_m.group(1).strip() if city_m else "目的地").split("（")[0]
    want_m = re.search(r"需要的 POI 数量：约\s*(\d+)", user)
    want = int(want_m.group(1)) if want_m else 18
    rng = _digest(city)
    pois = []
    for idx, (suffix, category, tags, stay, low, high, indoor, altitude) in enumerate(POI_TEMPLATES):
        if len(pois) >= max(6, want - 4):
            break
        pois.append({
            "name": f"{city}·{suffix}",
            "category": category,
            "tags": list(tags),
            "lat": None, "lng": None,
            "stay_minutes": stay,
            "open": "08:30", "close": "18:00",
            "closed_weekdays": [0] if category == "博物馆" else [],
            "reservation": category in ("博物馆", "主题乐园"),
            "indoor": indoor,
            "ticket_low": low if low == 0 else low + (rng + idx) % 20,
            "ticket_high": high if high == 0 else high + (rng + idx) % 30,
            "altitude": altitude or None,
            "risk_tags": ["高海拔"] if altitude and altitude >= 2500 else [],
            "desc": f"{city}的{suffix}（演示条目，非真实推荐）",
            "confidence": 0.35,
        })
    for idx, (name, category, tags, stay, low, high) in enumerate(MEAL_TEMPLATES):
        pois.append({
            "name": f"{city}·{name}", "category": category, "tags": list(tags),
            "lat": None, "lng": None, "stay_minutes": stay,
            "open": "10:30", "close": "21:00", "closed_weekdays": [],
            "reservation": False, "indoor": True,
            "ticket_low": low, "ticket_high": high,
            "altitude": None, "risk_tags": [],
            "desc": f"{city}的{name}（演示条目）", "confidence": 0.35,
        })
    return LLMResponse(content=_json({"pois": pois}))


def _profile(system: str, user: str) -> LLMResponse:
    city_m = re.search(r"城市：(.+)", user)
    city = (city_m.group(1).strip() if city_m else "目的地").split("（")[0]
    rng = _digest(city) % 7
    return LLMResponse(content=_json({
        "city": city,
        "lodging_low": 200 + rng * 25, "lodging_high": 420 + rng * 60,
        "meal_low": 35 + rng * 5, "meal_high": 95 + rng * 12,
        "local_notes": ["演示脑按全国中位水平估算价位，非本地实测",
                        "出行前请再确认一次天气预报与景区预约政策"],
        "confidence": 0.3,
    }))


def _opening(system: str, user: str) -> LLMResponse:
    names = re.findall(r'"poi_name":\s*"([^"]+)"', user)
    iso = re.search(r"待校验日期：(.+)", user)
    dates = [d.strip() for d in (iso.group(1).split(",") if iso else []) if d.strip()]
    if not names:
        names = re.findall(r'"name":\s*"([^"]+)"', user)
    items = []
    for name in names[:20]:
        for d in dates:
            items.append({
                "poi_name": name, "date": d, "open": True, "window": "08:30-18:00",
                "reservation": False,
                "reason": "离线演示脑无权威开放数据，默认按开放处理，请出行前核实",
                "confidence": 0.25,
            })
    return LLMResponse(content=_json({"items": items}))


def _place(system: str, user: str) -> LLMResponse:
    return LLMResponse(content=_json({"lat": None, "lng": None, "confidence": 0.0}))


def _narrate(system: str, user: str) -> LLMResponse:
    return LLMResponse(content="（离线演示脑）行程骨架已按你的需求排好：每天的时间轴、"
                              "交通衔接与预算区间都由程序计算。地点为合成条目，"
                              "配置大模型 API 后会替换为真实推荐。")


def _qa(system: str, user: str) -> LLMResponse:
    return LLMResponse(content="（离线演示脑）我只能确认程序算出来的数据：天气、时长与预算。"
                              "具体景点怎么玩，建议配置大模型 API 后再问我。")


def _json(payload: Any) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
def build_demo_brain() -> RoutedLLM:
    brain = RoutedLLM([
        ("需求解析器", _nlu),
        ("POI 知识", _pois),
        ("费用估算师", _profile),
        ("开放信息校验器", _opening),
        ("地理信息助手", _place),
        ("行程概览", _narrate),
        ("当前这份行程", _qa),
    ])
    brain.demo = True          # 供上层标注「合成数据」
    return brain


__all__ = ["RoutedLLM", "build_demo_brain", "DEMO_NOTICE"]
