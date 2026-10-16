"""NLU：把用户自然语言转成结构化需求。

第一优先用大模型（它对「下周」「国庆前后」「带老人」这类表达最稳）；
大模型不可用或返回异常时，退回到正则规则解析，保证无 Key 也能跑通基本流程。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta

from ..llm import LLMError, extract_json
from ..prompts import NLU_SYSTEM, nlu_user
from ..settings import SETTINGS, Settings
from . import rules

CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
          "八": 8, "九": 9, "十": 10, "半": 0, "十一": 11, "十二": 12}

WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 时长短语：必须「数字/中文数词 + 天|日|晚」，才敢剪。
# 不能把「一二三…十」直接塞进字符类去切分——否则「十堰」「九寨沟」这类地名会被切碎。
_DURATION_RE = re.compile(
    r"(?:玩|住|待|停留|共|游|行程|安排|去)?\s*"
    r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})\s*[天日晚]"
)

REFINE_VERBS = {"加": "add", "添加": "add", "增加": "add", "再加": "add",
                "删": "remove", "去掉": "remove", "删除": "remove", "不想去": "remove",
                "换": "replace", "替换": "replace", "改成": "replace", "改为": "replace",
                "挪": "move", "移到": "move", "调整到": "move"}

ASK_HINTS = ("怎么去", "要多久", "多少钱", "值得吗", "天气", "穿什么", "推荐", "可以吗",
             "怎么样", "有什么", "注意什么")

# 规则兜底用的地名提示表：只用于「切分连写地名」与「剥离动词后缀」，
# 不参与行程数据生成——任意目的地仍由大模型负责。
KNOWN_CITIES = {
    # 直辖市 / 省会 / 计划单列
    "北京", "上海", "天津", "重庆", "广州", "深圳", "成都", "杭州", "南京", "武汉",
    "西安", "苏州", "长沙", "郑州", "青岛", "沈阳", "大连", "宁波", "厦门", "福州",
    "济南", "合肥", "昆明", "南昌", "贵阳", "南宁", "太原", "石家庄", "哈尔滨", "长春",
    "兰州", "西宁", "银川", "乌鲁木齐", "呼和浩特", "拉萨", "海口", "三亚", "珠海", "佛山",
    "东莞", "无锡", "常州", "南通", "温州", "嘉兴", "绍兴", "金华", "泉州", "烟台",
    "潍坊", "洛阳", "开封", "徐州", "扬州", "镇江", "泰州", "盐城", "惠州", "中山",
    # 热门旅游目的地
    "丽江", "大理", "香格里拉", "西双版纳", "腾冲", "泸沽湖", "桂林", "阳朔", "北海",
    "张家界", "凤凰", "黄山", "婺源", "景德镇", "九寨沟", "峨眉山", "乐山", "稻城",
    "都江堰", "青城山", "敦煌", "嘉峪关", "张掖", "青海湖", "茶卡", "喀纳斯", "伊犁",
    "吐鲁番", "呼伦贝尔", "阿尔山", "延吉", "长白山", "威海", "秦皇岛", "北戴河", "承德",
    "平遥", "大同", "五台山", "泰山", "曲阜", "洛阳", "少林寺", "神农架", "恩施", "宜昌",
    "武当山", "庐山", "三清山", "武夷山", "泉州", "潮州", "汕头", "湛江", "涠洲岛", "万宁",
    "陵水", "儋州", "安顺", "荔波", "黔东南", "兴义", "赤水", "雅安", "甘孜", "阿坝",
    # 港澳台与境外常用
    "香港", "澳门", "台北", "高雄", "花莲", "垦丁", "东京", "大阪", "京都", "北海道",
    "冲绳", "首尔", "釜山", "济州", "曼谷", "清迈", "普吉", "新加坡", "吉隆坡", "巴厘岛",
    "河内", "岘港", "胡志明", "迪拜", "伊斯坦布尔", "巴黎", "伦敦", "罗马", "米兰", "威尼斯",
    "巴塞罗那", "马德里", "柏林", "慕尼黑", "阿姆斯特丹", "苏黎世", "因特拉肯", "维也纳",
    "布拉格", "布达佩斯", "雅典", "圣托里尼", "莫斯科", "圣彼得堡", "纽约", "洛杉矶",
    "旧金山", "西雅图", "拉斯维加斯", "夏威夷", "温哥华", "多伦多", "悉尼", "墨尔本",
    "奥克兰", "开罗", "开普敦", "内罗毕", "里约热内卢", "布宜诺斯艾利斯", "利马", "墨西哥城",
}

PROVINCES = {
    "云南", "四川", "贵州", "广西", "广东", "福建", "浙江", "江苏", "安徽", "江西",
    "湖南", "湖北", "河南", "河北", "山东", "山西", "陕西", "甘肃", "青海", "宁夏",
    "新疆", "西藏", "内蒙古", "辽宁", "吉林", "黑龙江", "海南", "台湾", "香港", "澳门",
}


@dataclass
class NLUResult:
    intent: str = "ask"
    slots: dict = field(default_factory=dict)
    edit: dict = field(default_factory=dict)
    reply: str | None = None
    need_more: bool = False
    engine: str = "rules"
    error: str | None = None


# --------------------------------------------------------------------------- #
def parse(message: str, *, known: dict | None = None, llm=None,
          settings: Settings | None = None, today: _date | None = None) -> NLUResult:
    """解析用户输入；llm 为 OpenAICompatClient 或 None。"""
    s = settings or SETTINGS
    today = today or _date.today()
    message = (message or "").strip()
    if not message:
        return NLUResult(intent="chitchat", reply="想让我帮你规划哪里的旅行？告诉我目的地和日期就行。",
                         engine="rules")

    if llm is not None and getattr(llm, "available", False):
        try:
            resp = llm.chat(
                [{"role": "system", "content": NLU_SYSTEM},
                 {"role": "user", "content": nlu_user(message, today.isoformat(),
                                                      rules.weekday_of(today.isoformat()), known)}],
                json_mode=True, temperature=0.1, max_tokens=900,
            )
            payload = extract_json(resp.content)
            result = _from_payload(payload, today)
            if result is not None:
                result.engine = "llm"
                return result
        except (LLMError, ValueError) as exc:
            fallback = _rule_parse(message, today)
            fallback.error = f"大模型解析失败，已退回规则解析：{exc}"
            return fallback

    return _rule_parse(message, today)


def _from_payload(payload: dict, today: _date) -> NLUResult | None:
    if not isinstance(payload, dict):
        return None
    intent = str(payload.get("intent") or "ask")
    if intent not in ("plan", "refine", "ask", "chitchat"):
        intent = "ask"
    slots = {
        "origin": payload.get("origin") or None,
        "destinations": [d for d in (payload.get("destinations") or []) if isinstance(d, str) and d.strip()],
        "start_date": _norm_date(payload.get("start_date")),
        "end_date": _norm_date(payload.get("end_date")),
        "budget": payload.get("budget"),
        "preferences": [p for p in (payload.get("preferences") or []) if isinstance(p, str)],
        "pace": payload.get("pace") if payload.get("pace") in ("relaxed", "normal", "intensive") else "normal",
        "travelers": payload.get("travelers") or 1,
        "mobility": "reduced" if payload.get("mobility") == "reduced" else "normal",
    }
    days = payload.get("days")
    if slots["start_date"] and not slots["end_date"] and isinstance(days, int) and days > 0:
        slots["end_date"] = (datetime.fromisoformat(slots["start_date"]).date()
                             + timedelta(days=days - 1)).isoformat()
    if slots["start_date"] and slots["end_date"] and slots["end_date"] < slots["start_date"]:
        slots["end_date"] = slots["start_date"]
    reply = payload.get("reply")
    edit = payload.get("edit") if isinstance(payload.get("edit"), dict) else {}
    need_more = bool(payload.get("need_more"))
    if intent in ("plan", "refine") and (not slots["destinations"] or not slots["start_date"]):
        if not need_more and not reply:
            return None                    # 信息不足又没说原因 → 交给规则兜底
        need_more = True
    return NLUResult(intent=intent, slots=slots, edit=edit,
                     reply=str(reply) if reply else None, need_more=need_more)


def _norm_date(value) -> str | None:
    if not value:
        return None
    text = str(value)[:10].replace("/", "-").replace(".", "-")
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 规则兜底
# --------------------------------------------------------------------------- #
def _rule_parse(message: str, today: _date) -> NLUResult:
    slots: dict = {"origin": None, "destinations": [], "start_date": None, "end_date": None,
                   "budget": None, "preferences": [], "pace": "normal",
                   "travelers": 1, "mobility": "normal"}
    edit: dict = {}

    # --- 修改意图（长动词优先，避免「换成」被「换」截断） ---
    for verb in sorted(REFINE_VERBS, key=len, reverse=True):
        if verb in message:
            target = _extract_target(message, verb)
            if target:
                edit = {"action": REFINE_VERBS[verb], "target": target, "day": None, "note": message}
            break

    # --- 出发地 ---
    m = re.search(r"从\s*([\u4e00-\u9fa5]{2,6}?)\s*(?:出发|走|去|到)", message)
    if m:
        slots["origin"] = m.group(1)
    else:
        m = re.search(r"([\u4e00-\u9fa5]{2,6}?)\s*出发", message)
        if m:
            slots["origin"] = m.group(1)

    # --- 目的地 ---
    dests: list[str] = []
    for m in re.finditer(r"(?:去|到|游|玩|逛|飞往|前往)\s*([\u4e00-\u9fa5]{2,9})", message):
        dests.extend(_split_cities(m.group(1)))
    if not dests:
        for m in re.finditer(r"([\u4e00-\u9fa5]{2,9}?)(?:旅行|旅游|攻略|自由行|怎么玩|哪里好玩)", message):
            dests.extend(_split_cities(m.group(1)))
    if not dests:
        m = re.search(r"([\u4e00-\u9fa5]{2,9}?)(?:\d+\s*[天日])", message)
        if m:
            dests.extend(_split_cities(m.group(1)))
    origin = slots["origin"]
    clean: list[str] = []
    for city in dests:
        name = _clean_city(city)
        if not name or name == origin or name in clean:
            continue
        if name.endswith(("出发", "旅游", "旅行", "攻略")):
            continue
        clean.append(name)
    # 若同时出现「省 + 具体城市」，丢掉省名
    if any(c not in PROVINCES for c in clean):
        clean = [c for c in clean if c not in PROVINCES]
    slots["destinations"] = clean[:6]

    # --- 日期 ---
    start, end, days = parse_dates(message, today)
    slots["start_date"], slots["end_date"] = start, end
    if days:
        slots["days"] = days

    # --- 预算 ---
    m = re.search(r"(?:预算|大概|大约|不超过|控制在)\s*(\d{3,7})\s*(?:元|块|rmb|RMB)?", message)
    if m:
        slots["budget"] = float(m.group(1))
    else:
        m = re.search(r"(\d{3,7})\s*(?:元|块)", message)
        if m:
            slots["budget"] = float(m.group(1))

    # --- 偏好 ---
    for pref, keys in rules.INTEREST_SYNONYMS.items():
        if pref in message or any(k in message for k in keys[:3]):
            slots["preferences"].append(pref)
    if any(k in message for k in ("老人", "父母", "长辈", "无障碍", "腿脚")):
        slots["mobility"] = "reduced"
        slots["pace"] = "relaxed"
    if any(k in message for k in ("轻松", "慢", "不赶", "休闲", "悠闲")):
        slots["pace"] = "relaxed"
    if any(k in message for k in ("紧凑", "深度", "尽量多", "打卡多", "特种兵")):
        slots["pace"] = "intensive"
    m = re.search(r"(\d{1,2})\s*(?:人|个人|位)", message)
    if m:
        slots["travelers"] = max(1, min(30, int(m.group(1))))
    if any(k in message for k in ("亲子", "带娃", "孩子", "小孩")):
        slots["preferences"] = sorted(set(slots["preferences"]) | {"亲子"})

    # --- 意图判定 ---
    has_dest = bool(slots["destinations"])
    has_date = bool(slots["start_date"])
    if edit.get("action"):
        intent = "refine"
    elif has_dest and (has_date or slots.get("days")):
        intent = "plan"
    elif has_dest:
        intent = "plan"
    elif any(h in message for h in ASK_HINTS):
        intent = "ask"
    else:
        intent = "ask"

    result = NLUResult(intent=intent, slots=slots, edit=edit, engine="rules")
    if intent == "plan" and not has_date:
        result.need_more = True
        result.reply = (f"好的，去{'、'.join(slots['destinations'])}。"
                        f"打算什么时候出发、玩几天？（例如「下周五出发，5 天」）")
    elif intent == "ask" and not has_dest and not edit.get("action"):
        result.reply = "告诉我目的地和出行日期，我就能帮你排一份带天气、路线和预算的行程。"
    return result


def _extract_target(message: str, verb: str) -> str | None:
    idx = message.find(verb)
    if idx < 0:
        return None
    tail = message[idx + len(verb):]
    tail = re.sub(r"^\s*(换成|改为|替换成|替换为|变成|成|为|掉|除|去)\s*", "", tail)
    tail = re.sub(r"^\s*第\s*\d{1,2}\s*天\s*的?\s*", "", tail)
    tail = re.sub(r"^\s*(一下|一点|一些|一个|一处|个|点)\s*", "", tail)
    tail = re.split(r"[，,。；;！!？?\s]|换|改|加|删|去掉", tail)[0]
    tail = re.sub(r"(吧|的|了|哦|啊|呢|嘛)+$", "", tail)
    return tail.strip() or None


def _clean_city(text: str) -> str:
    """从「成都玩4天」这类片段里剥出干净的城市名。"""
    text = _DURATION_RE.sub(" ", text or "")
    # 数字/标点之外，活动动词（玩/逛/住/待/游/吃/喝）也作为切分点——
    # 已核对 KNOWN_CITIES ∪ PROVINCES 中没有任何地名含这些字，切分安全。
    text = re.split(r"[\d\s，,。、；;！!？?（）()]|玩|逛|住|待|游|吃|喝", text)[0]
    for suffix in ("自由行", "旅行", "旅游", "攻略", "出发", "怎么玩", "玩", "游", "逛",
                   "住", "待", "的", "了", "吧", "看", "吃", "和", "与"):
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[: -len(suffix)]
    return text.strip("的了个和与、,， ")


def _split_cities(text: str) -> list[str]:
    """切分目的地串：优先按分隔符，其次用地名提示表贪婪匹配。"""
    raw_parts = [p for p in re.split(r"[、,，和与及\s]+", text or "") if p.strip()]
    out: list[str] = []
    for part in raw_parts:
        cleaned = _clean_city(part) or part.strip()
        if not cleaned:
            continue
        if cleaned in KNOWN_CITIES or cleaned in PROVINCES:
            out.append(cleaned)
            continue
        # 连写地名（如「云南大理丽江」）→ 扫描全文匹配已知地名。
        # 未命中的字逐个跳过、继续往后找；**不能**像以前那样把已匹配到的城市整段丢掉，
        # 否则「帮我规划一下成都」会因为开头「帮我规划」不是地名而丢掉已命中的「成都」。
        matched: list[str] = []
        i = 0
        while i < len(cleaned):
            hit = None
            for size in (4, 3, 2):
                frag = cleaned[i:i + size]
                if frag in KNOWN_CITIES or frag in PROVINCES:
                    hit = frag
                    break
            if hit:
                matched.append(hit)
                i += len(hit)
            else:
                i += 1
        # 一个已知地名都没命中 → 整段保留（新兴/小众目的地，交给大模型处理）
        out.extend(matched or [cleaned])
    return [c for c in out if c]


# --------------------------------------------------------------------------- #
# 日期解析（相对日期一律以 today 为基准，跨年自动进位）
# --------------------------------------------------------------------------- #
def parse_dates(message: str, today: _date) -> tuple[str | None, str | None, int | None]:
    start: _date | None = None
    days: int | None = None

    if "今天" in message:
        start = today
    elif "明天" in message:
        start = today + timedelta(days=1)
    elif "后天" in message:
        start = today + timedelta(days=2)

    if start is None:
        m = re.search(r"(\d{1,2})\s*个?\s*月\s*(?:后|以后)", message)
        if m:
            start = _shift_months(today, int(m.group(1)))
    if start is None:
        m = re.search(r"(\d{1,3})\s*天\s*(?:后|以后)", message)
        if m:
            start = today + timedelta(days=int(m.group(1)))
    if start is None:
        m = re.search(r"(?:下|本|这)?\s*周\s*([一二三四五六日天])", message)
        if m:
            target = WEEKDAY_CN[m.group(1)]
            delta = (target - today.weekday()) % 7
            if "下" in m.group(0):
                delta = delta + 7 if delta == 0 else delta + 7
            elif "本" in m.group(0) or "这" in m.group(0):
                pass
            else:
                delta = delta or 7
            start = today + timedelta(days=delta or 7)
    if start is None and "周末" in message:
        delta = (5 - today.weekday()) % 7
        start = today + timedelta(days=delta or 7)
    if start is None and any(k in message for k in ("国庆", "十一")):
        oct1 = _date(today.year, 10, 1)
        start = oct1 if oct1 >= today else _date(today.year + 1, 10, 1)
    if start is None and "五一" in message:
        may1 = _date(today.year, 5, 1)
        start = may1 if may1 >= today else _date(today.year + 1, 5, 1)
    if start is None and "春节" in message:
        start = today + timedelta(days=90)          # 保守估计，交由用户确认
    if start is None:
        m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", message)
        if m:
            try:
                start = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                start = None
    if start is None:
        m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", message)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            try:
                candidate = _date(today.year, month, day)
            except ValueError:
                candidate = None
            if candidate is not None and candidate < today:
                try:
                    candidate = _date(today.year + 1, month, day)
                except ValueError:
                    candidate = None
            start = candidate

    # 时长：先把日期片段抠掉，避免把「28日」「10月1日」误当行程天数
    scrub = _scrub_dates(message)

    # 「下个月10号出发」这类：先有月份锚点，再被日号覆盖
    if start is not None:
        m = re.search(r"(\d{1,2})\s*[日号]", scrub)
        if m:
            start = _set_day(start, int(m.group(1)))

    for pattern, cast in (
        (r"(?:玩|住|待|停留|共|游|行程|安排|去)\s*(\d{1,2})\s*[天日]", int),
        (r"(\d{1,2})\s*天", int),
        (r"(?:玩|住|待|停留|共|游|行程|安排)\s*([一两二三四五六七八九十]{1,3})\s*[天日]", None),
    ):
        m = re.search(pattern, scrub)
        if not m:
            continue
        if cast is int:
            value = int(m.group(1))
        else:
            value = CN_NUM.get(m.group(1))
        if value and 1 <= value <= 30:
            days = value
            break

    end = None
    if start and days:
        end = start + timedelta(days=days - 1)
    else:
        m = re.search(r"(?:到|至|返回|回来)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", message)
        if m and start:
            try:
                candidate = _date(start.year, int(m.group(1)), int(m.group(2)))
            except ValueError:
                candidate = None
            if candidate is not None and candidate < start:
                try:
                    candidate = _date(start.year + 1, int(m.group(1)), int(m.group(2)))
                except ValueError:
                    candidate = None
            end = candidate
    if end and start and not days:
        days = (end - start).days + 1

    return (start.isoformat() if start else None, end.isoformat() if end else None, days)


def _scrub_dates(text: str) -> str:
    """去掉日期写法，避免与「天数」混淆。"""
    text = re.sub(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*[日号]?", " ", text or "")
    text = re.sub(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]", " ", text)
    return text


def _set_day(d: _date, day: int) -> _date:
    """把日期改到当月第 day 天，越界则夹到月末。"""
    import calendar
    last = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=max(1, min(day, last)))


def _shift_months(d: _date, months: int) -> _date:
    month = d.month + months
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    try:
        return d.replace(year=year, month=month)
    except ValueError:
        return d.replace(year=year, month=month, day=28)
