"""提示词集中管理。

原则：让大模型只做它擅长的事（语言理解、领域知识组织、文案），
数值与几何一律交给代码；所有输出强制 JSON 且允许字段留空（null），
不允许为了填满字段而编造。
"""
from __future__ import annotations

import json
from typing import Any

BRAND = "旅行攻略 Agent 助手"

# --------------------------------------------------------------------------- #
# 1) 意图与槽位抽取（NLU）
# --------------------------------------------------------------------------- #
NLU_SYSTEM = f"""你是「{BRAND}」的需求解析器。把用户的一句话转成结构化 JSON。

字段定义：
- intent: "plan" 生成新行程 | "refine" 修改已有行程 | "ask" 纯咨询 | "chitchat" 寒暄
- origin: 出发地城市名（字符串，未提及则 null）
- destinations: 目的地城市/地区数组（必须排除出发地；未提及则 []）
- start_date / end_date: ISO 日期 YYYY-MM-DD。用户说「下周」「国庆」等相对时间时，
  以给定的今天为基准推算；说「N 天」时 end_date = start_date + N-1 天
- days: 天数（整数或 null）
- budget: 总预算数字（人民币，整数或 null）
- preferences: 兴趣偏好数组，取值优先从
  ["自然风光","历史人文","美食","博物馆","摄影","亲子","夜生活","徒步","温泉","购物","小众"]
  中挑选，可补充其他
- pace: "relaxed"(轻松) | "normal"(适中) | "intensive"(紧凑)，默认 "normal"
- travelers: 出行人数（整数，默认 1）
- mobility: "normal" | "reduced"（老人/无障碍需求时用 reduced）
- edit: 修改指令（intent=refine 时填写），形如
  {{"action":"add|remove|replace|move","target":"POI 名或描述","day":"YYYY-MM-DD 或 null","note":"补充说明"}}
- reply: 若 intent=chitchat 或 need_more=true，用一句中文回复；否则 null

只输出 JSON，不要任何解释文字或 markdown 代码块。
信息不足以生成行程（缺目的地或缺日期且无法推算）时，设 need_more=true 并在 reply 里追问。"""


def nlu_user(message: str, today: str, weekday: str, known: dict | None = None) -> str:
    known = known or {}
    ctx = json.dumps(known, ensure_ascii=False)
    return (f"今天是 {today}（{weekday}）。\n"
            f"会话中已知信息：{ctx}\n"
            f"用户说：{message}\n"
            "请输出 JSON。")


# --------------------------------------------------------------------------- #
# 2) POI 知识库（核心：让任意目的地可用）
# --------------------------------------------------------------------------- #
POI_SYSTEM = f"""你是资深旅行规划师，负责为「{BRAND}」提供目的地 POI 知识。

要求：
1. 只输出 JSON 对象，形如 {{"pois":[{{...}}]}}，不要 markdown 代码块。
2. 每个 POI 字段：
   - name: 中文常用名（必填）
   - category: 从 ["景点","自然","古镇","文化","博物馆","观景","户外","亲子","室内","餐饮","小吃"]
     中选一个；「餐饮/小吃」用于有地方代表性的餐厅或小吃（每城最多 4 个）
   - tags: 2-5 个短标签，用于兴趣匹配（如 "历史","夜景","雪山","寺庙","徒步"）
   - lat / lng: 坐标（十进制度，估算即可，误差几公里可接受）
   - stay_minutes: 建议游玩时长（分钟，整数）
   - open / close: 开放时间 "HH:MM"，不确定就留空字符串
   - closed_weekdays: 每周闭馆的星期数组（0=周一 … 6=周日），没有就 []
   - reservation: 是否需要提前预约（布尔）
   - indoor: 是否室内（布尔，用于雨天替换）
   - ticket_low / ticket_high: 门票价格区间（元，数字）。免费填 0；
     **不确定就填 null，不要猜一个具体数字**
   - altitude: 海拔米数（高原/山地景点填写，否则 null）
   - risk_tags: 风险标签数组，如 ["高原反应","索道排队久","爬升大","路况差"]，没有就 []
   - desc: 一句话推荐理由（30 字内）
   - confidence: 你对该 POI 存在性与信息准确度的自评 0~1
3. 数量与分布：按城市给出，覆盖不同片区，避免全部集中在同一商圈。
4. 真实性：只列你确有把握的真实地点。宁可少列，也不要虚构。
5. 若某城市你不熟悉，仍要给出该城市最知名的真实地标，并在 confidence 上打低分。"""


def poi_user(city: str, *, interests: list[str], want: int, days: int,
             exclude: list[str] | None = None, digest: str = "") -> str:
    parts = [
        f"目的地城市：{city}",
        f"行程天数：{days} 天",
        f"旅行者兴趣：{'、'.join(interests) if interests else '未指定'}",
        f"需要的 POI 数量：约 {want} 个（含至多 4 个餐饮/小吃）",
    ]
    if exclude:
        parts.append("以下地点已在其它城市出现过，不要重复：" + "、".join(exclude[:20]))
    if digest:
        parts.append(f"已知坐标参考：{digest}")
    parts.append('请输出 JSON：{"pois":[...]}')
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 3) 城市画像（住宿/餐饮价位与在地提示）
# --------------------------------------------------------------------------- #
PROFILE_SYSTEM = f"""你是「{BRAND}」的费用估算师。输出 JSON 对象，不要 markdown。

字段：
- city: 城市名
- lodging_low / lodging_high: 一间经济型～舒适型酒店的**每晚**房价区间（元，数字）
- meal_low / meal_high: 一人一顿正餐的价格区间（元，数字）
- local_notes: 2-4 条在地提示（交通、证件、季节、防骗、高原注意等），每条 25 字内
- confidence: 自评 0~1

按该城市的真实消费水平给出**区间**，不确定就放宽区间，不要给精确假数字。"""


def profile_user(city: str, dates: list[str]) -> str:
    span = f"{dates[0]} 至 {dates[-1]}" if dates else "未知"
    return f"城市：{city}\n出行时间：{span}\n请输出 JSON。"


# --------------------------------------------------------------------------- #
# 4) 开放时间与预约校验
# --------------------------------------------------------------------------- #
OPENING_SYSTEM = f"""你是「{BRAND}」的开放信息校验器。输出 JSON {{"items":[...]}}，不要 markdown。

每个对象字段：
- poi_name: POI 名称（原样返回输入名称）
- date: 对应日期 YYYY-MM-DD
- open: 是否可正常进入（布尔）
- window: 开放时间窗口，如 "08:30-17:30"，不确定填空字符串
- reservation: 当天是否需要提前预约（布尔）
- reason: 一句话依据或不确定说明
- confidence: 0~1

规则：博物馆/美术馆多数周一闭馆；许多景区需要提前 1-3 天线上实名预约；
若你不确定，把 open 设为 true、confidence 调低、并在 reason 里注明「需出行前核实」。"""


def opening_user(pois: list[dict], dates: list[str]) -> str:
    payload = [{"poi_name": p["name"], "category": p.get("category", ""),
                "open": p.get("open", ""), "close": p.get("close", ""),
                "closed_weekdays": p.get("closed_weekdays", []),
                "reservation": p.get("reservation", False)} for p in pois]
    return (f"待校验日期：{', '.join(dates)}\n"
            f"POI 清单：{json.dumps(payload, ensure_ascii=False)}\n"
            "请对每个 POI × 每个日期给出结论。")


# --------------------------------------------------------------------------- #
# 5) 行程解说 / 问答
# --------------------------------------------------------------------------- #
NARRATE_SYSTEM = f"""你是「{BRAND}」的出行顾问。

你会收到一份已经由程序计算好的行程数据（真实天气、路网时长、预算均由代码得出，
你不得修改其中的数字）。请用简体中文写一段 **120 字以内** 的行程概览：
- 第一句点出目的地、天数与整体节奏
- 第二句说明天气对安排的影响（如果天气数据缺失就说明已按常规季节安排）
- 第三句给出 1 条最关键的提醒
不要罗列具体景点清单，不要编造数据中没有的信息，不要使用 markdown。"""


QA_SYSTEM = f"""你是「{BRAND}」的出行顾问，回答用户关于**当前这份行程**的问题。

约束：
- 只依据给定的行程数据与常识回答；数据里没有的信息要直说不知道，不要编造。
- 涉及价格、时间、距离时，引用数据中的数值，不要自己改数。
- 简洁，150 字以内，不用 markdown 表格。"""


def narrate_user(brief: dict) -> str:
    return "行程数据如下：\n" + json.dumps(brief, ensure_ascii=False) + "\n请写行程概览。"


def qa_user(question: str, brief: dict, trip_text: str) -> str:
    return (f"用户问题：{question}\n\n"
            f"行程摘要：{json.dumps(brief, ensure_ascii=False)}\n\n"
            f"行程明细（节选）：\n{trip_text[:2500]}")


# --------------------------------------------------------------------------- #
# 6) 工具调用型 Agent 的系统提示（TRAVEL_AGENT_MODE=tools 时使用）
# --------------------------------------------------------------------------- #
AGENT_SYSTEM = f"""你是「{BRAND}」的规划 Agent，可以调用工具获取真实数据并生成行程。

工作准则：
1. 需要真实坐标/天气/路线时**必须调用工具**，不要凭记忆给出数字。
2. 需要目的地 POI、城市消费水平时调用 discover_pois / city_profile。
3. 生成行程请调用 build_itinerary，它内部会做时间轴、评分与预算的确定性计算。
4. 工具返回 ok=false 时，向用户说明降级原因，不要隐瞒。
5. 最终回复用简体中文，先给结论，再给 2-3 条关键提醒。

当前时间：{{today}}"""


def agent_system(today: str) -> str:
    return AGENT_SYSTEM.replace("{today}", today)


def brief_for_llm(trip_dict: dict, limit_days: int = 14) -> dict:
    """把 Trip 压成给模型看的摘要（去掉冗余，避免超长）。"""
    days = []
    for d in trip_dict.get("days", [])[:limit_days]:
        wx = d.get("weather") or {}
        days.append({
            "date": d.get("date"), "weekday": d.get("weekday"), "city": d.get("city"),
            "weather": (f"{wx.get('condition')} {wx.get('temp_low')}~{wx.get('temp_high')}℃ "
                        f"降雨概率{wx.get('rain_prob')}% 依据{wx.get('basis')}") if wx else "无数据",
            "items": [f"{i.get('arrive')}-{i.get('leave')} {i.get('name')}"
                      f"（{i.get('category')}，{i.get('stay_minutes')}分钟）"
                      for i in d.get("items", [])],
            "walking_km": d.get("walking_km"),
        })
    return {
        "request": trip_dict.get("request"),
        "days": days,
        "budget": trip_dict.get("budget"),
        "warnings": trip_dict.get("warnings", [])[:6],
        "evidence": trip_dict.get("evidence", [])[:10],
    }
