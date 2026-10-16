"""编排层：把 NLU、工具、规则引擎、存储串成一个 Agent。

对外只暴露三件事：
- plan(request)  生成行程
- chat(message)  对话式规划/微调/咨询
- status()       运行状态（引擎、数据源、工具清单）
"""
from __future__ import annotations

import re
from datetime import date as _date, datetime, timedelta
from typing import Any, Optional

from ..llm import LLMError, OpenAICompatClient, strip_reasoning
from ..models import (ChatRequest, ChatResponse, Poi, ToolResult, ToolTrace, Trip,
                      TripRequest)
from ..prompts import NARRATE_SYSTEM, QA_SYSTEM, brief_for_llm, narrate_user, qa_user
from ..providers import build_providers
from ..settings import SETTINGS, Settings
from ..store import STORE, Store
from ..tools import build_registry
from ..tools.base import ToolContext
from . import nlu, planner, rules
from .planner import PlanDeps

FOLLOWUPS = ["这几天怎么穿衣服？", "帮我换成更省钱的方案", "把第 2 天排松一点", "有哪些需要提前预约？"]


class Orchestrator:
    def __init__(self, *, settings: Settings | None = None, store: Store | None = None,
                 llm: Any = None) -> None:
        self.settings = settings or SETTINGS
        self.store = store or STORE
        self.providers = build_providers(self.settings)
        self._injected_llm = llm
        self.llm = self._resolve_llm(llm)
        self.ctx = ToolContext(settings=self.settings, providers=self.providers,
                               llm=self.llm, store=self.store)
        self.registry = build_registry(self.ctx)

    # ------------------------------------------------------------------ #
    def _resolve_llm(self, llm: Any) -> Any:
        if llm is not None:
            return llm
        if not self.settings.use_llm:
            return None
        if self.settings.engine == "demo" or self.settings.demo_brain:
            from ..demo_brain import build_demo_brain
            return build_demo_brain()
        client = OpenAICompatClient(self.settings)
        return client if client.available else None

    def _deps(self) -> PlanDeps:
        return PlanDeps(registry=self.registry, ctx=self.ctx)

    @property
    def using_demo_brain(self) -> bool:
        return bool(getattr(self.llm, "demo", False))

    def status(self) -> dict:
        from ..settings import APP_NAME, APP_VERSION
        status = self.settings.status()
        status.update({
            "app": APP_NAME, "version": APP_VERSION,
            "providers": {
                "geocoder": getattr(self.providers.geocoder, "name", "?"),
                "weather": getattr(self.providers.forecaster, "name", "?"),
                "router": getattr(self.providers.router, "name", "?"),
                "geo_note": self.providers.geo_note(),
            },
            "tools": [t["name"] for t in self.registry.specs()],
            "tool_schemas": self.registry.specs(),
            "llm_live": bool(self.llm),
        })
        return status

    # ------------------------------------------------------------------ #
    def plan(self, request: TripRequest) -> tuple[Trip, list[ToolTrace]]:
        deps = self._deps()
        trip = planner.build_trip(request, deps)
        self._flag_demo(trip)
        self.store.save(trip, reason="生成行程", changes=[f"共 {len(trip.days)} 天"])
        return trip, deps.traces

    def _flag_demo(self, trip: Trip) -> None:
        if not self.using_demo_brain:
            return
        from ..demo_brain import DEMO_NOTICE
        trip.warnings = _dedupe([DEMO_NOTICE] + list(trip.warnings))

    # ------------------------------------------------------------------ #
    def chat(self, req: ChatRequest) -> ChatResponse:
        message = (req.message or "").strip()
        trip = self.store.get(req.trip_id) if req.trip_id else None
        if trip is None:
            trip = self._latest_trip()
        known = self._known_from(trip)
        parsed = nlu.parse(message, known=known, llm=self.llm, settings=self.settings)

        reply_engine = "llm" if parsed.engine == "llm" else "rules"

        if parsed.intent == "chitchat":
            return ChatResponse(intent="chitchat", reply=parsed.reply or "在的，请说～",
                                engine=reply_engine, trip=trip,
                                warning=parsed.error, suggestions=FOLLOWUPS)

        if parsed.intent == "ask":
            return self._handle_ask(message, trip, parsed, reply_engine)

        if parsed.intent == "refine" or parsed.edit.get("action"):
            if trip is None:
                # 没有既有行程可改 → 当作新建需求继续往下走
                parsed.intent = "plan"
                parsed.edit = {}
            else:
                return self._handle_refine(message, trip, parsed, reply_engine)

        # intent == plan
        # 本次消息自带目的地 ⇒ 新建行程，不继承旧行程的槽位（详见 _merge_slots）。
        fresh = bool(parsed.slots.get("destinations"))
        merged, missing = self._merge_slots(parsed.slots, known, fresh=fresh)
        if missing:
            # 追问文案严格按缺失项生成：用户已经说了「玩 5 天」就不要再回头问「玩几天」。
            dests = "、".join(merged.get("destinations") or [])
            head = f"好的，去{dests}。" if dests else ""
            ask = (f"{head}还需要补充：{'、'.join(missing)}。"
                   f"例如「下周五出发，玩 4 天，预算 6000」。")
            return ChatResponse(intent="plan", reply=ask, engine=reply_engine, trip=trip,
                                slots=merged, warning=parsed.error,
                                suggestions=["下周五出发，4 天", "预算 6000 元", "偏好自然风光和美食"])
        try:
            request = TripRequest(**merged)
        except Exception as exc:                     # noqa: BLE001
            return ChatResponse(intent="plan", reply=f"需求还差一点信息：{exc}", engine=reply_engine,
                                trip=trip, slots=merged, warning=parsed.error)

        trip, traces = self.plan(request)
        return ChatResponse(intent="plan", reply=self.narrate(trip), engine="llm" if self.llm else "rules",
                            trip=trip, trace=traces, slots=merged,
                            suggestions=FOLLOWUPS, warning=parsed.error)

    # ------------------------------------------------------------------ #
    # 咨询
    # ------------------------------------------------------------------ #
    def _handle_ask(self, message: str, trip: Optional[Trip], parsed: nlu.NLUResult,
                    engine: str) -> ChatResponse:
        if trip is None:
            if self.llm:
                try:
                    resp = self.llm.chat([
                        {"role": "system", "content":
                         "你是旅行规划助手，回答要简洁（120 字内）。若问题需要具体行程数据，"
                         "请引导用户先提供目的地和日期。"},
                        {"role": "user", "content": message}], max_tokens=500)
                    return ChatResponse(intent="ask", reply=resp.content.strip() or "请补充目的地和日期。",
                                        engine="llm", warning=parsed.error, suggestions=FOLLOWUPS)
                except LLMError as exc:
                    return ChatResponse(intent="ask", reply="还没有行程可以回答这个问题。先告诉我目的地和日期吧。",
                                        engine=engine, warning=f"{parsed.error or ''} {exc}".strip(),
                                        suggestions=FOLLOWUPS)
            return ChatResponse(intent="ask",
                                reply=(parsed.reply or "还没有行程。先给我目的地和日期，我帮你排一份带天气、"
                                                       "路线与预算的行程。"),
                                engine=engine, warning=parsed.error, suggestions=FOLLOWUPS)

        brief = brief_for_llm(trip.model_dump(mode="json"))
        if self.llm:
            try:
                resp = self.llm.chat([
                    {"role": "system", "content": QA_SYSTEM},
                    {"role": "user", "content": qa_user(message, brief, trip_text(trip))}],
                    max_tokens=800)
                text = (resp.content or "").strip()
                if text:
                    return ChatResponse(intent="ask", reply=text, engine="llm", trip=trip,
                                        warning=parsed.error, suggestions=FOLLOWUPS)
            except LLMError as exc:
                return ChatResponse(intent="ask", reply=self.local_answer(message, trip), engine="rules",
                                    trip=trip, warning=str(exc), suggestions=FOLLOWUPS)
        return ChatResponse(intent="ask", reply=self.local_answer(message, trip), engine="rules",
                            trip=trip, warning=parsed.error, suggestions=FOLLOWUPS)

    # ------------------------------------------------------------------ #
    # 微调
    # ------------------------------------------------------------------ #
    def _handle_refine(self, message: str, trip: Optional[Trip], parsed: nlu.NLUResult,
                       engine: str) -> ChatResponse:
        if trip is None:
            return ChatResponse(intent="refine", reply="还没有可修改的行程，先告诉我目的地和日期吧。",
                                engine=engine, warning=parsed.error, suggestions=FOLLOWUPS)
        edit = dict(parsed.edit or {})
        if not edit.get("action"):
            edit = self._infer_edit(message) or {}
        if not edit.get("action"):
            return ChatResponse(intent="refine",
                                reply="没太听懂要改什么，可以试试「把第 2 天的 XX 换成 YY」「加一个博物馆」"
                                      "「删掉第 3 天的夜市」。",
                                engine=engine, trip=trip, warning=parsed.error, suggestions=FOLLOWUPS)

        deps = self._deps()
        changes, problems, traces = self._apply_edit(trip, edit, deps, message)
        if problems and not changes:
            return ChatResponse(intent="refine", reply="；".join(problems), engine=engine, trip=trip,
                                trace=traces, warning=parsed.error, suggestions=FOLLOWUPS)

        self._refresh(trip, deps)
        self._flag_demo(trip)
        self.store.save(trip, reason=f"用户微调：{message[:40]}", changes=changes, bump=True)
        reply = self._refine_reply(changes, problems)
        return ChatResponse(intent="refine", reply=reply, engine="llm" if self.llm else "rules",
                            trip=trip, trace=traces, warning=parsed.error, suggestions=FOLLOWUPS)

    def _infer_edit(self, message: str) -> dict:
        for verb in sorted(nlu.REFINE_VERBS, key=len, reverse=True):
            if verb in message:
                target = nlu._extract_target(message, verb)
                day_m = re.search(r"第\s*(\d{1,2})\s*天", message)
                day = f"__index__:{int(day_m.group(1))}" if day_m else None
                return {"action": nlu.REFINE_VERBS[verb], "target": target,
                        "day": day, "note": message}
        return {}

    def _apply_edit(self, trip: Trip, edit: dict, deps: PlanDeps,
                    message: str) -> tuple[list[str], list[str], list[ToolTrace]]:
        action = edit.get("action")
        target = (edit.get("target") or "").strip()
        changes: list[str] = []
        problems: list[str] = []

        day = self._resolve_day(trip, edit.get("day"), message)

        if action == "remove":
            if not target:
                return [], ["要删掉哪一个？告诉我名字，例如「删掉第 3 天的夜市」。"], deps.traces
            hit = _find_item(trip, target, day.date if day else None)
            if hit is None:
                return [], [f"行程里没找到「{target}」。"], deps.traces
            d, item = hit
            d.items = [i for i in d.items if i.item_id != item.item_id]
            changes.append(f"{d.date} 移除 {item.name}")
            if not d.items:
                problems.append(f"{d.date} 已无安排（候选不足或该日无法再排）")
            self._relayout(trip, d.date, deps)

        elif action == "add":
            if not target:
                return [], ["要加什么？告诉我名称，例如「加一个博物馆」。"], deps.traces
            if day is None:
                return [], ["请指定加到哪一天，例如「第 3 天加一个博物馆」。"], deps.traces
            got = deps.call("discover_pois", city=day.city,
                            interests=[target] + list(trip.request.preferences),
                            days=1, want=8,
                            exclude=[i.name for d in trip.days for i in d.items])
            poi = _match_poi(got.data if got.ok else [], target, deps)
            if poi is None:
                resolved = deps.call("resolve_place", name=target, city=day.city)
                if not resolved.ok:
                    return [], [f"没能找到「{target}」，可以换个说法或确认名称。"], deps.traces
                poi = Poi(id=f"user-{abs(hash(target)) % 10**8}", name=target, city=day.city,
                          category="景点", lat=resolved.data["lat"], lng=resolved.data["lng"],
                          precision="approximate", geo_source=resolved.source,
                          stay_minutes=90, desc="用户指定", source="user", confidence=0.45)
            deps.ctx.poi_cache[poi.id] = poi
            day.items.append(planner.poi_to_item(poi, day.date, len(day.items) + 1))
            changes.append(f"{day.date} 新增 {poi.name}")
            self._relayout(trip, day.date, deps)

        elif action == "replace":
            old, new = _split_replace(message, target)
            if not old or not new:
                return [], ["换哪个、换成什么，说清楚一点，例如「把第 2 天的 A 换成 B」。"], deps.traces
            hit = _find_item(trip, old, day.date if day else None)
            if hit is None:
                return [], [f"行程里没找到「{old}」。"], deps.traces
            d, item = hit
            d.items = [i for i in d.items if i.item_id != item.item_id]
            sub_edit = {"action": "add", "target": new, "day": d.date}
            added, probs, _ = self._apply_edit(trip, sub_edit, deps, message)
            if added:
                changes.append(f"{d.date} {old} → {new}")
            problems.extend(probs)

        elif action == "move":
            hit = _find_item(trip, target, None)
            if hit is None:
                return [], [f"行程里没找到「{target}」。"], deps.traces
            src, item = hit
            if day is None:
                return [], ["要挪到哪一天？例如「把夜市挪到第 4 天」。"], deps.traces
            src.items = [i for i in src.items if i.item_id != item.item_id]
            poi = planner.poi_from_item(item)
            poi.city = day.city or poi.city
            deps.ctx.poi_cache[poi.id] = poi
            day.items.append(planner.poi_to_item(poi, day.date, len(day.items) + 1))
            changes.append(f"{item.name} 从 {src.date} 移到 {day.date}")
            self._relayout(trip, src.date, deps)
            self._relayout(trip, day.date, deps)
        else:
            return [], [f"暂不支持的操作：{action}"], deps.traces

        return changes, problems, deps.traces

    def _resolve_day(self, trip: Trip, day_value: Any, message: str):
        if not trip.days:
            return None
        m = re.search(r"第\s*(\d{1,2})\s*天", message or "")
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(trip.days):
                return trip.days[idx]
        if isinstance(day_value, str) and day_value.startswith("__index__:"):
            idx = int(day_value.split(":")[1]) - 1
            if 0 <= idx < len(trip.days):
                return trip.days[idx]
        if isinstance(day_value, str) and re.match(r"\d{4}-\d{2}-\d{2}", day_value):
            for d in trip.days:
                if d.date == day_value:
                    return d
        return next((d for d in trip.days), None)

    def _relayout(self, trip: Trip, date: str, deps: PlanDeps) -> None:
        new_plan = planner.relayout_day(trip, date, deps)
        if new_plan is None:
            return
        for idx, d in enumerate(trip.days):
            if d.date == date:
                trip.days[idx] = new_plan
                break

    def _refresh(self, trip: Trip, deps: PlanDeps) -> None:
        got = deps.call("estimate_budget", trip=trip.model_dump(mode="json"))
        if got.ok:
            from ..models import BudgetPlan
            try:
                trip.budget = BudgetPlan.model_validate(got.data)
            except Exception:                        # noqa: BLE001
                pass
        trip.warnings = _dedupe([w for w in trip.warnings if not w.startswith("自检：")]
                                + rules.check_conflicts(trip.request, trip.days, trip.budget))
        check = deps.call("verify_trip", trip=trip.model_dump(mode="json"))
        if check.ok and isinstance(check.data, dict) and not check.data.get("ok"):
            trip.warnings.extend(f"自检：{i}" for i in check.data.get("issues", [])[:6])
        trip.warnings = _dedupe(trip.warnings)

    def _refine_reply(self, changes: list[str], problems: list[str]) -> str:
        parts = []
        if changes:
            parts.append("已调整：" + "；".join(changes) + "。")
        if problems:
            parts.append("注意：" + "；".join(problems))
        parts.append("时间轴已按就近顺序重排，预算同步更新。")
        return " ".join(parts)

    # ------------------------------------------------------------------ #
    # 生成文案
    # ------------------------------------------------------------------ #
    def narrate(self, trip: Trip) -> str:
        if self.llm:
            try:
                resp = self.llm.chat([
                    {"role": "system", "content": NARRATE_SYSTEM},
                    {"role": "user", "content": narrate_user(brief_for_llm(trip.model_dump(mode="json")))}],
                    max_tokens=500)
                # 推理模型会把思维链写进 content，必须剥掉再给用户看，
                # 否则界面上会出现「让我分析一下……」这类内部推理过程。
                text = strip_reasoning(resp.content or "").strip()
                if text:
                    return text
            except LLMError:
                pass
        return self.overview(trip)

    def overview(self, trip: Trip) -> str:
        req = trip.request
        dest = "、".join(req.destinations)
        wx = [d.weather for d in trip.days if d.weather and d.weather.basis != "unavailable"]
        wx_text = ""
        if wx:
            rain = sum(1 for w in wx if w.is_rainy)
            lows = min(w.temp_low for w in wx)
            highs = max(w.temp_high for w in wx)
            wx_text = f" 行程期间气温约 {lows:.0f}~{highs:.0f}℃，"
            wx_text += f"其中 {rain} 天降雨概率偏高，已把室内项目优先排入。" if rain else "天气整体适合出行。"
        elif any(d.weather for d in trip.days):
            wx_text = " 天气数据部分不可用，已按常规季节安排。"
        budget = ""
        if trip.budget:
            budget = f" 预估总花费 ¥{trip.budget.total_low:.0f}~{trip.budget.total_high:.0f}。"
        items = sum(len(d.items) for d in trip.days)
        engine_note = "（大模型驱动）" if trip.engine == "llm" else "（规则引擎驱动）"
        return (f"{dest} {len(trip.days)} 天行程已生成{engine_note}，共 {items} 个安排。"
                f"{wx_text}{budget} "
                f"地图里可以直接点开每天的时段、交通方式和数据来源。")

    def local_answer(self, message: str, trip: Trip) -> str:
        q = message or ""
        brief = brief_for_llm(trip.model_dump(mode="json"))
        if any(k in q for k in ("穿", "衣服", "气温", "天气")):
            wx = [d.weather for d in trip.days if d.weather and d.weather.basis != "unavailable"]
            if not wx:
                return "这次行程没有取到可用天气数据，建议出行前再查一次预报。"
            lows = min(w.temp_low for w in wx)
            highs = max(w.temp_high for w in wx)
            return (f"行程期间气温 {lows:.0f}~{highs:.0f}℃。"
                    f"{'有降雨，带伞和防水外套。' if any(w.is_rainy for w in wx) else '降雨概率不高。'}"
                    f"昼夜温差按 8℃ 预留一件外套。")
        if any(k in q for k in ("预算", "多少钱", "花费", "费用")):
            if trip.budget:
                return (f"预估总花费 ¥{trip.budget.total_low:.0f}~{trip.budget.total_high:.0f}。"
                        f"{trip.budget.verdict}。{' '.join(trip.budget.advice[:2])}")
            return "还没有预算数据。"
        if any(k in q for k in ("预约", "门票")):
            notes = [n for d in trip.days for n in getattr(d, "notes", []) if "预约" in n]
            return "；".join(notes) if notes else "当前行程未发现强制预约项，热门景区仍建议提前 1-3 天线上预约。"
        if any(k in q for k in ("几天", "第几天", "安排")):
            lines = [f"{d.date} {d.city}：" + "、".join(i.name for i in d.items) for d in trip.days]
            return "\n".join(lines)
        if trip.warnings:
            return "当前有几条需要注意：" + "；".join(trip.warnings[:3])
        return (f"这份行程共 {len(trip.days)} 天，覆盖 {'、'.join(trip.request.destinations)}。"
                "可以问我「第几天怎么安排」「预算多少」「要不要预约」。")

    # ------------------------------------------------------------------ #
    def _known_from(self, trip: Optional[Trip]) -> dict:
        if trip is None:
            return {}
        req = trip.request
        return {"origin": req.origin, "destinations": req.destinations,
                "start_date": req.start_date.isoformat(), "end_date": req.end_date.isoformat(),
                "budget": req.budget, "preferences": req.preferences,
                "pace": req.pace, "travelers": req.travelers, "mobility": req.mobility,
                "trip_id": trip.trip_id}

    def _merge_slots(self, slots: dict, known: dict, *, fresh: bool = False) -> tuple[dict, list[str]]:
        """把本次解析结果与「已有行程上下文」合并成完整的 TripRequest。

        fresh=True 表示用户已明确给出新目的地 ⇒ 当作**新建行程**：
        日期跨度、预算、偏好、节奏、人数、行动能力一律以本次消息为准，不继承旧行程。
        （之前无条件继承，会让「去杭州玩5天」被上一次落盘行程的日期跨度覆盖成 3 天。）
        """
        carry = {} if fresh else known
        merged = {
            "origin": slots.get("origin") or known.get("origin") or "",
            "destinations": list(slots.get("destinations") or []),
            "start_date": slots.get("start_date") or carry.get("start_date"),
            "end_date": slots.get("end_date") or carry.get("end_date"),
            "budget": slots.get("budget") if slots.get("budget") is not None else carry.get("budget"),
            "preferences": sorted(set((slots.get("preferences") or []) +
                                     (carry.get("preferences") or []))),
            "pace": slots.get("pace") or carry.get("pace") or "normal",
            "travelers": slots.get("travelers") or carry.get("travelers") or 1,
            "mobility": slots.get("mobility") or carry.get("mobility") or "normal",
        }
        if not merged["destinations"]:
            merged["destinations"] = list(known.get("destinations") or [])

        missing: list[str] = []
        if not merged["destinations"]:
            missing.append("目的地")
        if not merged["start_date"]:
            missing.append("出发日期")
        if not merged["end_date"]:
            if not merged.get("start_date"):
                pass                      # 出发日期缺失，已在上方记入 missing
            elif slots.get("days"):
                extra = max(0, int(slots["days"]) - 1)
                merged["end_date"] = (datetime.fromisoformat(str(merged["start_date"]))
                                      + timedelta(days=extra)).isoformat()
            else:
                missing.append("行程天数")
        for key in ("start_date", "end_date"):
            if isinstance(merged.get(key), _date):
                merged[key] = merged[key].isoformat()
        if merged.get("end_date") and merged.get("start_date") and merged["end_date"] < merged["start_date"]:
            merged["end_date"] = merged["start_date"]
        if merged.get("travelers"):
            merged["travelers"] = max(1, min(30, int(merged["travelers"])))
        if merged.get("budget") is not None:
            try:
                merged["budget"] = float(merged["budget"])
            except (TypeError, ValueError):
                merged["budget"] = None
        return merged, missing

    def _latest_trip(self) -> Optional[Trip]:
        items = self.store.list(limit=1)
        return self.store.get(items[0]["trip_id"]) if items else None


# --------------------------------------------------------------------------- #
# 文本化与匹配辅助
# --------------------------------------------------------------------------- #
def trip_text(trip: Trip) -> str:
    lines: list[str] = []
    if trip.intercity:
        lines.append("城际交通：" + "；".join(f"{l.label} {l.minutes}分钟" for l in trip.intercity))
    for d in trip.days:
        wx = d.weather
        head = f"{d.date} {d.weekday} {d.city}"
        if wx:
            head += f" {wx.condition} {wx.temp_low:.0f}~{wx.temp_high:.0f}℃ 降雨{wx.rain_prob}%"
        lines.append(head)
        for i in d.items:
            trans = f"，{i.transport.label}{i.transport.minutes}分" if i.transport else ""
            lines.append(f"  {i.arrive}-{i.leave} {i.name}（{i.category}，{i.stay_minutes}分{trans}）")
    if trip.budget:
        lines.append("预算：" + "；".join(f"{l.category} {l.low:.0f}~{l.high:.0f}" for l in trip.budget.lines))
    return "\n".join(lines)


def _find_item(trip: Trip, name: str, day_date: Optional[str]):
    name = (name or "").strip()
    if not name:
        return None
    days = [d for d in trip.days if d.date == day_date] if day_date else trip.days
    days = days or trip.days
    for d in days:
        for item in d.items:
            if item.name == name:
                return d, item
    # 只允许「用户输入是完整名称的一部分」，不做反向放宽，避免误命中
    for d in days:
        for item in d.items:
            if len(name) >= 2 and name in item.name:
                return d, item
    return None


def _match_poi(data: Any, target: str, deps: PlanDeps) -> Optional[Poi]:
    best: Optional[Poi] = None
    best_score = 0
    for raw in data if isinstance(data, list) else []:
        try:
            poi = Poi.model_validate(raw)
        except Exception:                            # noqa: BLE001
            continue
        score = _name_sim(poi.name, target)
        if score > best_score:
            best, best_score = poi, score
    if best is not None and best_score >= 0.5:
        return best
    # 折中：目标词命中城市名或类别也算
    for raw in data if isinstance(data, list) else []:
        try:
            poi = Poi.model_validate(raw)
        except Exception:                            # noqa: BLE001
            continue
        if target and (target in poi.category or target in poi.desc):
            return poi
    return None


def _name_sim(a: str, b: str) -> float:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    common = len(set(a) & set(b))
    return common / max(len(set(a)), len(set(b)))


def _split_replace(message: str, target: str) -> tuple[Optional[str], Optional[str]]:
    for sep in ("换成", "替换成", "替换为", "改为", "变成", "改去"):
        if sep in message:
            head, _, tail = message.partition(sep)
            new = re.split(r"[，,。；;！!？?\s]", tail.strip())[0].strip(" 的")
            old = re.sub(r"^(把|将)", "", head.strip()).strip()
            old = re.sub(r"^第\s*\d{1,2}\s*天\s*的?", "", old).strip()
            old = re.sub(r"^(把|将)", "", old).strip().strip("的")
            if not old:
                old = (target or "").strip()
            return (old or None), (new or None)
    return None, None


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = (it or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
