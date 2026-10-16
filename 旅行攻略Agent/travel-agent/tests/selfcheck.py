"""离线自检：不依赖外网与大模型，验证核心链路可用。

覆盖：
1. 模块导入、9 个工具全部注册
2. NLU 规则解析（目的地/日期/预算/偏好/相对日期跨年/时长与日期不混淆）
3. 打分与时间轴装配（用假路由，避免外网）
4. 预算汇总与冲突检测
5. 端到端规划（用「可预测大脑」离线跑通，零 Key）
6. 对话微调（删/加/咨询/寒暄）
7. 降级路径：无 POI、无坐标、无 LLM 时都不崩且给出告警

运行：python tests/selfcheck.py
"""
from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import nlu, rules                                  # noqa: E402
from app.agent.orchestrator import Orchestrator, trip_text        # noqa: E402
from app.demo_brain import RoutedLLM, _narrate, _nlu, _opening, _place, _profile, _qa  # noqa: E402
from app.llm import ScriptedLLM                                   # noqa: E402
from app.models import (ChatRequest, CityProfile, Poi, TransportLeg,  # noqa: E402
                        TripRequest, WeatherDay)
from app.providers.base import Place, haversine_km                 # noqa: E402
from app.settings import Settings                                 # noqa: E402
from app.store import Store                                       # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  → {detail}" if detail and not cond else ""))


# --------------------------------------------------------------------------- #
# 假数据源（离线、确定性）
# --------------------------------------------------------------------------- #
class FakeGeocoder:
    name = "fake-geo"
    COORDS = {"北京": (39.9042, 116.4074), "成都": (30.5728, 104.0668),
              "丽江": (26.8721, 100.2299), "杭州": (30.2741, 120.1551),
              "拉萨": (29.6520, 91.1721), "东京": (35.6762, 139.6503)}

    def search(self, query: str, *, count: int = 5) -> list[Place]:
        for key, (lat, lng) in self.COORDS.items():
            if key in query or query in key:
                return [Place(name=key, lat=lat, lng=lng, city=key,
                              precision="exact", source=self.name)]
        return []


class EmptyGeocoder:
    name = "none"

    def search(self, query: str, *, count: int = 5) -> list[Place]:
        return []


class FakeForecaster:
    name = "fake-weather"

    def daily(self, location: str, lat, lng, dates: list[str]) -> list[WeatherDay]:
        return [WeatherDay(location=location, date=d,
                           condition="中雨" if i % 3 == 1 else "晴间多云",
                           temp_low=12.0 + i, temp_high=22.0 + i,
                           rain_prob=70 if i % 3 == 1 else 10,
                           precip_mm=6.0 if i % 3 == 1 else 0.0,
                           wind_kmh=12.0, aqi=55, source=self.name, basis="forecast")
                for i, d in enumerate(dates)]


class FakeRouter:
    name = "fake-router"

    def route(self, origin, dest, mode="taxi", **kw) -> TransportLeg:
        km = haversine_km(origin[0], origin[1], dest[0], dest[1]) * 1.3
        return TransportLeg(mode=mode, label=mode, minutes=max(5, round(km / 22 * 60)),
                            distance_km=round(km, 2), cost_low=round(km * 2.3, 1),
                            cost_high=round(km * 3.1, 1), source="fake")


BASE = {"成都": (30.57, 104.06), "丽江": (26.87, 100.23), "东京": (35.67, 139.65),
        "杭州": (30.27, 120.15), "拉萨": (29.65, 91.17), "北京": (39.90, 116.41)}
TAG_POOL = [["历史", "古镇"], ["自然", "雪山"], ["博物馆", "文化"],
            ["夜市", "美食"], ["观景", "摄影"], ["徒步", "森林"]]


def make_pois(city: str, count: int = 12) -> list[dict]:
    base = BASE.get(city, (30.0, 104.0))
    out = []
    for i in range(count):
        cat = "餐饮" if i >= count - 3 else "景点"
        out.append({
            "id": f"{city}-{i}",
            "name": f"{city}{'小吃' if cat == '餐饮' else '景点'}{i}",
            "city": city, "category": cat,
            "tags": TAG_POOL[i % len(TAG_POOL)],
            "lat": base[0] + (i % 4) * 0.02, "lng": base[1] + (i % 5) * 0.02,
            "stay_minutes": 90 + (i % 3) * 30,
            "open": "08:30", "close": "18:00",
            "closed_weekdays": [0] if i % 5 == 0 else [],
            "reservation": i % 4 == 0, "indoor": i % 3 == 2,
            "ticket_low": 0 if i % 2 else 60, "ticket_high": 0 if i % 2 else 120,
            "altitude": 3200 if i % 7 == 0 else None,
            "risk_tags": ["高原反应"] if i % 7 == 0 else [],
            "desc": f"{city}的{i}号推荐点", "confidence": 0.75,
        })
    return out


def make_brain(*, pois_ok: bool = True) -> RoutedLLM:
    """可预测大脑：NLU/画像/开放/文案复用演示脑，POI 换成带坐标的测试数据。"""
    def pois(system: str, user: str):
        if not pois_ok:
            return ScriptedLLM.json({})
        m = re.search(r"目的地城市：(.+)", user)
        city = (m.group(1).strip() if m else "成都").split("（")[0]
        return ScriptedLLM.json({"pois": make_pois(city)})

    return RoutedLLM([
        ("需求解析器", _nlu),
        ("POI 知识", pois),
        ("费用估算师", _profile),
        ("开放信息校验器", _opening),
        ("地理信息助手", _place),
        ("行程概览", _narrate),
        ("当前这份行程", _qa),
    ])


def build_engine(*, brain=None, geocoder=None, engine: str = "auto") -> Orchestrator:
    settings = Settings(port=0)
    settings.engine = engine
    settings.llm_base_url = "https://scripted.local/v1"
    settings.llm_model = "scripted"
    settings.llm_api_key = ""
    eng = Orchestrator(settings=settings, store=Store(persist=False),
                       llm=brain if brain is not None else make_brain())
    eng.providers.geocoder = geocoder or FakeGeocoder()
    eng.providers.forecaster = FakeForecaster()
    eng.providers.router = FakeRouter()
    return eng


# --------------------------------------------------------------------------- #
def main() -> int:
    today = date.today()

    # ---------- 1. 工具注册 ----------
    eng = build_engine()
    names = [t["name"] for t in eng.registry.specs()]
    check("工具注册数 = 9", len(names) == 9, str(len(names)))
    check("关键工具齐全", all(n in names for n in
                            ["resolve_place", "discover_pois", "city_profile", "city_weather",
                             "route_between", "opening_check", "score_candidates",
                             "estimate_budget", "verify_trip"]), str(names))
    check("schema 可被大模型消费",
          all(t["parameters"].get("type") == "object" for t in eng.registry.specs()))

    # ---------- 2. NLU ----------
    p = nlu.parse("从北京出发，下周五去成都玩4天，预算6000，喜欢美食和自然风光",
                  llm=None, today=date(2026, 10, 13))
    check("NLU 出发地", p.slots.get("origin") == "北京", str(p.slots.get("origin")))
    check("NLU 目的地", p.slots.get("destinations") == ["成都"], str(p.slots.get("destinations")))
    check("NLU 日期（下周五=10-23，4天）", p.slots.get("end_date") == "2026-10-26",
          f"{p.slots.get('start_date')} ~ {p.slots.get('end_date')}")
    check("NLU 预算", p.slots.get("budget") == 6000.0, str(p.slots.get("budget")))
    check("NLU 偏好", {"美食", "自然风光"} <= set(p.slots.get("preferences", [])),
          str(p.slots.get("preferences")))
    check("NLU 意图=plan", p.intent == "plan", p.intent)

    check("日期与天数不混淆（12月28日玩5天）",
          nlu.parse_dates("12月28日出发玩5天", date(2026, 12, 20))[:2] == ("2026-12-28", "2027-01-01"),
          str(nlu.parse_dates("12月28日出发玩5天", date(2026, 12, 20))))
    check("明天出发", nlu.parse_dates("明天出发3天", today)[0] == (today + timedelta(days=1)).isoformat())
    check("已过日期进位次年", nlu.parse_dates("10月1日去", date(2026, 11, 5))[0] == "2027-10-01")
    check("连写地名可切分",
          nlu.parse("我想去云南大理丽江7天", llm=None, today=today).slots.get("destinations")
          == ["大理", "丽江"],
          str(nlu.parse("我想去云南大理丽江7天", llm=None, today=today).slots.get("destinations")))
    check("带老人自动降节奏",
          nlu.parse("带父母去成都玩4天，明天出发", llm=None, today=today).slots.get("mobility") == "reduced")
    check("修改意图：加", nlu.parse("第3天加一个古城", llm=None, today=today).edit.get("target") == "古城",
          str(nlu.parse("第3天加一个古城", llm=None, today=today).edit))
    check("修改意图：删",
          nlu.parse("删掉第4天的集市", llm=None, today=today).edit.get("target") == "集市",
          str(nlu.parse("删掉第4天的集市", llm=None, today=today).edit))
    check("修改意图：换",
          nlu.parse("把第2天的湿地换成古城", llm=None, today=today).edit.get("target") == "古城",
          str(nlu.parse("把第2天的湿地换成古城", llm=None, today=today).edit))

    # ---------- 2b. 回归：城市名剥离 / 行程槽位不串味 / CLI 入口 ----------
    # 「玩五天」这类中文数量词曾被当成城市名的一部分（杭州玩五天）
    check("中文数量词不污染城市名",
          nlu.parse("去杭州玩五天", llm=None, today=today).slots.get("destinations") == ["杭州"],
          str(nlu.parse("去杭州玩五天", llm=None, today=today).slots.get("destinations")))
    # 句首的指令词（帮我规划一下）曾被整段当成城市名
    check("指令前缀不污染城市名",
          nlu.parse("帮我规划一下成都3天行程", llm=None, today=today).slots.get("destinations") == ["成都"],
          str(nlu.parse("帮我规划一下成都3天行程", llm=None, today=today).slots.get("destinations")))
    # 时长短语只能在「数词+天/日/晚」处剪，不能把「十堰」「九寨沟」这类地名切碎
    check("地名不被时长短语切碎",
          [nlu.parse(t, llm=None, today=today).slots.get("destinations")
           for t in ("去十堰玩三天", "去九寨沟玩3天")] == [["十堰"], ["九寨沟"]],
          "十堰/九寨沟被切碎说明数词被当成了通用分隔符")

    # 新建行程（本次消息自带目的地）不得继承上一份行程的槽位：
    # 曾导致「去杭州玩5天」被旧行程的日期跨度覆盖成 3 天、并继承旧预算。
    eng_m = build_engine(engine="rules")
    known_old = {"origin": "北京", "destinations": ["成都"],
                 "start_date": "2026-10-23", "end_date": "2026-10-26",
                 "budget": 6000.0, "preferences": ["美食"],
                 "pace": "normal", "travelers": 1, "mobility": "normal"}
    base_new = {"origin": None, "destinations": ["杭州"], "start_date": None, "end_date": None,
                "budget": None, "preferences": [], "days": 5,
                "pace": "normal", "travelers": 1, "mobility": "normal"}
    merged_new, missing_new = eng_m._merge_slots(base_new, known_old, fresh=True)
    check("新建行程不继承旧行程的日期跨度",
          merged_new["start_date"] is None and merged_new["end_date"] is None,
          f"{merged_new['start_date']} ~ {merged_new['end_date']}")
    check("新建行程不继承旧行程的预算", merged_new["budget"] is None, str(merged_new["budget"]))
    check("新建行程不继承旧行程的偏好", merged_new["preferences"] == [], str(merged_new["preferences"]))
    check("新建行程仍沿用出发地默认值", merged_new["origin"] == "北京", str(merged_new["origin"]))
    check("已给出天数时只追问出发日期", missing_new == ["出发日期"], str(missing_new))

    # 补全场景（本次没给目的地）才继承上一轮的目的地
    merged_fill, _ = eng_m._merge_slots(
        dict(base_new, destinations=[], start_date="2026-10-30", end_date="2026-10-31", days=None),
        known_old, fresh=False)
    check("补全场景仍继承上一轮目的地",
          merged_fill["destinations"] == ["成都"], str(merged_fill["destinations"]))

    # README 承诺 `python run.py --engine demo` 是唯一的离线完整体验入口，
    # CLI 的 choices 漏掉 demo 会让这个入口直接报 invalid choice。
    cli_src = (ROOT / "run.py").read_text(encoding="utf-8")
    m_engine = re.search(r'add_argument\(\s*"--engine"\s*,\s*choices=\[([^\]]*)\]', cli_src)
    check("CLI 的 --engine 覆盖 demo（离线完整体验入口）",
          bool(m_engine) and "demo" in m_engine.group(1),
          m_engine.group(1) if m_engine else "未匹配到 --engine 的 choices")

    # ---------- 3. 打分与时间轴 ----------
    poi = Poi(id="p1", name="西湖", city="杭州", category="自然",
              tags=["自然", "湖", "风光"], lat=30.25, lng=120.14, rating=4.7,
              ticket_low=0, ticket_high=0, precision="exact", confidence=0.9)
    info = rules.score_poi(poi, preferences=["自然风光"], anchor=(30.27, 120.15))
    check("打分落在 0~100", 0 <= info["score"] <= 100, str(info["score"]))
    check("偏好命中加分", info["parts"]["interest"] > 0.6, str(info["parts"]["interest"]))

    rain = WeatherDay(location="杭州", date="2026-10-20", condition="大雨",
                      temp_low=18, temp_high=22, rain_prob=90)
    outdoor = rules.score_poi(poi, preferences=["自然风光"], weather=rain)
    museum = Poi(id="p2", name="浙江省博物馆", city="杭州", category="博物馆",
                 tags=["博物馆", "文化"], indoor=True, lat=30.25, lng=120.15,
                 precision="exact", confidence=0.9)
    indoor = rules.score_poi(museum, preferences=["博物馆"], weather=rain)
    check("雨天室内优先", indoor["score"] > outdoor["score"],
          f"室内 {indoor['score']} vs 户外 {outdoor['score']}")

    pois = [Poi.model_validate(x) for x in make_pois("成都", 10)]
    plan = rules.plan_day_timeline(poi_date="2026-10-20", city="成都", pois=pois, pace="normal",
                                   weather=None, anchor=(30.57, 104.06),
                                   preferences=["自然风光"], route_fn=FakeRouter().route)
    check("当日有安排", len(plan.items) > 0, str(len(plan.items)))
    check("时间轴单调不重叠",
          all(plan.items[i].leave <= plan.items[i + 1].arrive for i in range(len(plan.items) - 1)),
          str([(i.name, i.arrive, i.leave) for i in plan.items]))
    check("每项都带数据来源", all(i.evidence for i in plan.items))
    check("节奏上限生效", len(plan.items) <= rules.pace_cfg("normal")["max_pois"] + 2,
          f"{len(plan.items)} 项")

    loud = rules.plan_day_timeline(poi_date="2026-10-20", city="成都", pois=pois, pace="relaxed",
                                   weather=rain, anchor=(30.57, 104.06),
                                   preferences=["自然风光"], route_fn=FakeRouter().route)
    check("雨天优先排室内", any(i.indoor for i in loud.items),
          str([(i.name, i.indoor) for i in loud.items]))

    # ---------- 4. 预算与冲突 ----------
    req = TripRequest(origin="北京", destinations=["成都"], start_date="2026-10-20",
                      end_date="2026-10-23", budget=6000, preferences=["自然风光"])
    days = []
    for i in range(4):
        d = plan.model_copy(deep=True)
        d.date = f"2026-10-{20 + i}"
        days.append(d)
    budget = rules.estimate_budget(
        req, days, {"成都": CityProfile(city="成都", lodging_low=200, lodging_high=480,
                                        meal_low=40, meal_high=110)},
        [TransportLeg(mode="rail", label="高铁", minutes=560, distance_km=1800,
                      cost_low=750, cost_high=900)])
    check("预算有区间", budget.total_low > 0 and budget.total_high > budget.total_low,
          f"{budget.total_low}~{budget.total_high}")
    check("预算有结论与建议", bool(budget.verdict), budget.verdict)
    check("预算分项齐全", len(budget.lines) >= 5, str(len(budget.lines)))
    check("冲突检测可运行", isinstance(rules.check_conflicts(req, days, budget), list))
    check("天数均分", rules.city_day_allocation(["成都", "丽江"], 7) ==
          ["成都", "成都", "成都", "成都", "丽江", "丽江", "丽江"],
          str(rules.city_day_allocation(["成都", "丽江"], 7)))

    # ---------- 5. 端到端 ----------
    eng2 = build_engine()
    resp = eng2.chat(ChatRequest(message="从北京出发，下周五去成都玩4天，预算6000，喜欢美食和自然风光"))
    check("对话返回行程", resp.trip is not None, resp.reply[:80])
    trip = resp.trip
    if trip is None:
        return _report()

    check("行程天数正确", len(trip.days) == 4, str(len(trip.days)))
    check("有预算计划", trip.budget is not None)
    check("有城际交通", len(trip.intercity) >= 1, str(len(trip.intercity)))
    check("有城市画像", "成都" in trip.profiles, str(list(trip.profiles)))
    check("引擎=llm", trip.engine == "llm", trip.engine)
    check("有数据来源清单", len(trip.evidence) >= 3, str(len(trip.evidence)))
    check("每日都有安排", all(d.items for d in trip.days),
          str([len(d.items) for d in trip.days]))
    check("每天都有天气", all(d.weather for d in trip.days))
    check("首日延后出发（抵达到岗时间）",
          trip.days[0].items[0].arrive >= "09:00", trip.days[0].items[0].arrive)
    check("末日晚间提前收尾",
          not trip.days[-1].items or trip.days[-1].items[-1].leave <= "19:00",
          trip.days[-1].items[-1].leave if trip.days[-1].items else "无")
    check("有住宿锚点", bool(trip.stay_area), str(list(trip.stay_area)))
    check("trace 记录完整", len(resp.trace) >= 4, str(len(resp.trace)))
    check("trace 含降级标记字段", all(hasattr(t, "degraded") for t in resp.trace))
    text = trip_text(trip)
    check("行程可文本化", len(text) > 100, str(len(text)))
    check("未标注演示脑（应为真实大脑）",
          not any("离线演示脑" in w for w in trip.warnings), str(trip.warnings[:2]))

    # ---------- 6. 微调 ----------
    target = trip.days[1].items[0].name
    before = sum(len(d.items) for d in trip.days)
    r2 = eng2.chat(ChatRequest(message=f"删掉第2天的{target}", trip_id=trip.trip_id))
    after = sum(len(d.items) for d in r2.trip.days)
    check("删除生效", after == before - 1, f"{before} → {after}")
    check("版本递增 + 变更历史",
          r2.trip.version >= 2 and len(r2.trip.history) >= 1,
          f"v{r2.trip.version}, history={len(r2.trip.history)}")
    check("删除后仍无时间冲突",
          all(r2.trip.days[i].items[j].leave <= r2.trip.days[i].items[j + 1].arrive
              for i in range(len(r2.trip.days))
              for j in range(len(r2.trip.days[i].items) - 1)))

    new_name = make_pois("成都")[7]["name"]
    r3 = eng2.chat(ChatRequest(message=f"第3天加一个{new_name}", trip_id=trip.trip_id))
    check("添加有反馈", "新增" in r3.reply, r3.reply[:80])

    r4 = eng2.chat(ChatRequest(message="这几天怎么穿衣服？", trip_id=trip.trip_id))
    check("咨询能回答", len(r4.reply) > 5 and r4.intent == "ask", r4.reply[:60])

    r5 = eng2.chat(ChatRequest(message="你好呀"))
    check("寒暄不崩", r5.intent in ("chitchat", "ask"), r5.intent)

    r6 = eng2.chat(ChatRequest(message="我想去巴黎，下个月10号出发"))
    check("缺失天数时主动追问", r6.trip is not None and "天数" in r6.reply or "天" in r6.reply,
          r6.reply[:80])

    # ---------- 7. 降级路径 ----------
    eng3 = build_engine(brain=make_brain(pois_ok=False), geocoder=EmptyGeocoder())
    r7 = eng3.chat(ChatRequest(message="去东京玩3天，明天出发"))
    check("无 POI 源时不抛异常", r7.trip is not None, r7.reply[:80])
    if r7.trip is not None:
        check("无数据时给出告警", len(r7.trip.warnings) > 0, str(len(r7.trip.warnings)))
        check("无 POI 时明确说明",
              any("候选地点" in w or "坐标" in w or "不可用" in w for w in r7.trip.warnings),
              str(r7.trip.warnings[:3]))

    eng4 = build_engine(engine="rules")
    eng4.settings.engine = "rules"
    eng4.llm = None
    eng4.ctx.llm = None
    r8 = eng4.chat(ChatRequest(message="从北京去成都玩3天，明天出发"))
    check("规则模式仍可解析需求", r8.trip is not None, r8.reply[:80])

    # 演示脑
    from app.demo_brain import build_demo_brain
    eng5 = build_engine(brain=build_demo_brain())
    r9 = eng5.chat(ChatRequest(message="去拉萨玩4天，明天出发"))
    check("演示脑可独立跑通", r9.trip is not None, r9.reply[:80])
    if r9.trip:
        check("演示脑带醒目提示",
              any("离线演示脑" in w for w in r9.trip.warnings), str(r9.trip.warnings[:2]))

    # ---------- 8. 真实 HTTP 客户端（验证填了 Key 之后走的那条路径） ----------
    _test_openai_client()

    # ---------- 9. 地理编码排序 ----------
    _test_geo_ranking()

    # ---------- 10. 前端接线（静态检查，防止 JS 里的 id 拼错） ----------
    _test_frontend_wiring()

    if "--online" in sys.argv:
        _test_online_sources()

    return _report()


def _test_frontend_wiring() -> None:
    web = ROOT / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    css = (web / "style.css").read_text(encoding="utf-8")

    check("前端三件套齐备", all((web / n).exists() for n in
                            ["index.html", "app.js", "style.css", "favicon.svg"]))
    check("前端零外部依赖（无 CDN）",
          "http://" not in html.replace("http://www.w3.org", "") and
          "cdn." not in html and "unpkg" not in html and "jsdelivr" not in html)

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(re.findall(r"\$\('([^']+)'\)", js)) | set(re.findall(r"getElementById\('([^']+)'\)", js))
    missing = sorted(i for i in js_ids if i not in html_ids)
    check("JS 引用的元素 id 都存在于 HTML", not missing, f"缺失：{missing}")

    # JS 里写入的 class 是否至少有样式定义
    used = set(re.findall(r'class="([a-z0-9 \-]+)"', js))
    used_cls = {c for group in used for c in group.split() if c}
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    unstyled = sorted(c for c in used_cls if c not in defined)
    check("JS 使用的样式类都有定义", not unstyled, f"未定义：{unstyled}")


def _test_geo_ranking() -> None:
    from app.providers.composite import score_place

    city = Place(name="丽江市", lat=26.86, lng=100.22, country="中国", feature="city",
                 source="photon")
    village = Place(name="丽江", lat=28.62, lng=113.88, country="中国", feature="PPL",
                    source="open-meteo")
    check("地物类型参与排序（城市 > 同名村落）",
          score_place(city, "丽江") > score_place(village, "丽江"),
          f"{score_place(city, '丽江')} vs {score_place(village, '丽江')}")
    capital = Place(name="巴黎", lat=48.853, lng=2.349, country="France", feature="PPLC",
                    population=2138551, source="open-meteo")
    shop = Place(name="巴黎", lat=22.656, lng=120.296, country="中国台湾",
                 feature="apartments", source="photon")
    check("人口与行政级别参与排序（首都 > 同名商店）",
          score_place(capital, "巴黎") > score_place(shop, "巴黎"),
          f"{score_place(capital, '巴黎')} vs {score_place(shop, '巴黎')}")


def _test_online_sources() -> None:
    """联网体检：确认免费数据源实时可用（不通过也不影响离线用例）。"""
    from app.providers.base import haversine_km
    from app.providers.composite import CompositeGeocoder
    from app.providers.open_meteo import OpenMeteoForecast, OpenMeteoGeocoder
    from app.providers.osrm import OsrmRouter
    from app.providers.photon import PhotonGeocoder

    print("\n-- 联网体检 --")
    geo = CompositeGeocoder([PhotonGeocoder(), OpenMeteoGeocoder()])
    expect = {"丽江": (26.87, 100.23), "大理": (25.60, 100.25), "成都": (30.57, 104.07),
              "上海": (31.23, 121.47), "杭州": (30.27, 120.15)}
    for name, (lat, lng) in expect.items():
        best = geo.best(name)
        if best is None:
            check(f"地理编码 {name}", False, "无结果")
            continue
        drift = haversine_km(best.lat, best.lng, lat, lng)
        check(f"地理编码 {name}（误差 {drift:.0f}km）", drift < 60,
              f"{best.lat:.3f},{best.lng:.3f} ← {best.source}/{best.feature}")

    fc = OpenMeteoForecast()
    dates = [(date.today() + timedelta(days=i)).isoformat() for i in range(3)]
    days = fc.daily("成都", 30.57, 104.07, dates)
    check("天气预报可用", all(d.basis != "unavailable" for d in days),
          str([(d.date, d.condition, d.basis) for d in days]))

    leg = OsrmRouter().route((30.57, 104.07), (30.65, 104.06), "taxi")
    check("OSRM 路网可用", leg.source == "osrm" and leg.distance_km > 0,
          f"{leg.source} {leg.distance_km}km {leg.minutes}min")


def _test_openai_client() -> None:
    import http.server
    import json as _json
    import socketserver
    import threading

    from app.llm import LLMError, OpenAICompatClient, extract_json

    state = {"status": 200, "body": {
        "choices": [{"message": {
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "city_weather",
                                         "arguments": "{\"city\": \"成都\", \"dates\": [\"2026-10-20\"]}"}}],
        }}]}}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):                                   # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            body = _json.dumps(state["body"], ensure_ascii=False).encode("utf-8")
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):                        # noqa: A003
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        st = Settings(port=0)
        st.llm_base_url = f"http://127.0.0.1:{port}/v1"
        st.llm_model = "unit-test-model"
        st.llm_api_key = "sk-" + "x" * 20
        client = OpenAICompatClient(st)
        check("OpenAI 兼容客户端可用", client.available)
        resp = client.chat([{"role": "user", "content": "成都天气"}])
        check("解析原生 tool_calls",
              bool(resp.tool_calls) and resp.tool_calls[0].name == "city_weather"
              and resp.tool_calls[0].arguments.get("city") == "成都",
              str(resp.tool_calls))
        check("客户端走原生工具模式", client.tool_mode == "native")

        # 鉴权失败要给出可操作的提示
        state["status"] = 401
        try:
            client.chat([{"role": "user", "content": "x"}])
            check("401 被识别为鉴权失败", False, "没有抛错")
        except LLMError as exc:
            check("401 被识别为鉴权失败", "API_KEY" in str(exc) or "鉴权" in str(exc), str(exc))
        srv.shutdown()

    st2 = Settings(port=0)
    st2.llm_base_url = ""
    st2.llm_model = ""
    check("未配置时提示明确", not OpenAICompatClient(st2).available)
    check("extract_json 容错（代码块包裹）",
          extract_json("前言```json\n{\"a\": 1}\n```后语") == {"a": 1})
    check("extract_json 容错（前后废话）", extract_json("好的：{\"b\": [1,2]} 以上") == {"b": [1, 2]})


def _report() -> int:
    print("\n" + "=" * 62)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败用例：" + "、".join(FAILED))
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
