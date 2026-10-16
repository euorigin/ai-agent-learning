"""意图识别（NLU）。

文档 §3.2：识别询问、请求、拒绝、承诺、威胁、交易、任务操作和闲聊，
并结合上一轮对话和当前游戏状态决定是否继续追问；
对话结果必须区分"说了什么"和"实际执行了什么"。

实现：LLM 结构化输出为主（`[[TASK:NLU]]` + json_mode），
规则解析器 `rule_parse` 既是兜底、也是演示脑的同源实现。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..content import content_service
from ..llm import TASK_NLU
from ..llm.base import extract_json
from ..schemas import CharacterCard, Perception, Understanding
from ..world import detect_event, world_state

# 越狱 / 违规指令（模型层输入审核，文档 §3.9）
JAILBREAK_PATTERNS = (
    "忽略以上", "忽略之前", "无视规则", "忘记你的设定", "假装你是", "你现在是",
    "developer mode", "开发者模式", "system prompt", "系统提示词", "越狱", "jailbreak",
    "不要扮演", "解除限制", "以管理员身份",
)

# 诈骗式话术 / 强行索要（运营与安全关注）
SCAM_PATTERNS = (
    "免费给我", "白送", "你必须给我", "命令你", "把钱都给我", "复制一份给我",
    "给我全部", "直接给我钱", "刷点金币",
)

INTENT_SIGNALS: list[tuple[str, tuple[str, ...]]] = [
    ("trade_request", ("买", "卖", "多少钱", "价格", "价钱", "收购", "交易", "来一份",
                       "给我来", "一份", "打折", "便宜点", "太贵")),
    # 只看货/问库存/问行情：也是最常见的交易类提问，必须落进 trade_request，
    # 否则「你有什么货」会被判成 social，商人面对玩家问货却答不上来。
    ("trade_request", ("有什么货", "有什么东西卖", "卖什么", "有什么卖", "货架",
                       "库存", "有什么货色", "展示一下", "看看货", "有什么可买")),
    ("quest_op", ("任务", "委托", "接下", "接受任务", "交任务", "完成", "回报", "线索",
                  "进度", "下一步做什么", "要做什么")),
    ("action_request", ("开城门", "开门", "放我过去", "带我去", "帮我", "护送", "跟随",
                        "给我", "拿点", "给我一份", "动手", "攻击")),
    ("info_query", ("在哪", "哪里", "怎么走", "是什么", "知道", "听说", "问一下", "打听",
                    "见过", "什么时候", "几点", "多少", "有没有", "谁")),
]

CLARIFY_MARKERS = ("那个", "就是那个", "那个东西", "你说的那个", "帮我弄一下", "老规矩")
GREETING_WORDS = ("你好", "您好", "早上好", "晚上好", "早安", "午安", "嗨", "喂",
                  "在吗", "好久不见", "久仰", "幸会")

# 物品口语别名 → 规范 item_id（策划可在 world.yaml 里用 name 兜底，别名在此维护）
ITEM_ALIASES: dict[str, tuple[str, ...]] = {
    "yin_leaf": ("银叶草", "银叶", "雾谷银叶草"),
    "heal_salve": ("止血膏", "药膏", "伤药"),
    "fever_draught": ("退烧药", "退热药", "汤药"),
    "ration": ("干粮", "口粮", "吃的"),
    "lantern_oil": ("灯油", "油"),
    "smuggled_spice": ("走私香料", "香料"),
}

# 笼统提到"药材/草药"却没点名 → 属于模糊请求，先澄清（避免下游用默认物品蒙一个）
VAGUE_ITEM_WORDS = ("药材", "草药", "药品", "什么药")

QUANTITY_RE = re.compile(r"([0-9]+|[零一二两三四五六七八九十]+)\s*(份|个|瓶|把|件|支|张|颗|枚|袋|捆)")
QUEST_RE = re.compile(r"q_\d{3}", re.IGNORECASE)

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(text: str) -> Optional[int]:
    """把「两份 / 十五 / 3」这类数量词转成整数。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


def _match_known(text: str, candidates: list[tuple[str, str]]) -> Optional[str]:
    for key, value in candidates:
        if key and key in text:
            return value
    return None


def rule_parse(
    message: str,
    card_id: str = "",
    card: Optional[CharacterCard] = None,
    perception: Optional[Perception] = None,
) -> Understanding:
    """确定性规则解析（同时也是演示脑的 NLU 实现）。"""
    text = (message or "").strip()
    slots: dict[str, Any] = {}

    if card is None and card_id:
        card = content_service.get(card_id)

    # ---- 0) 输入审核：越狱与诈骗 ----
    if any(p in text.lower() for p in JAILBREAK_PATTERNS):
        return Understanding(intent="out_of_scope", confidence=0.95,
                             reason="疑似越狱/越权指令", engine="rules")
    if any(p in text for p in SCAM_PATTERNS):
        return Understanding(intent="out_of_scope", confidence=0.9,
                             reason="诈骗式索取", engine="rules")

    # ---- 1) 槽位 ----
    qm = QUEST_RE.search(text)
    if qm:
        slots["quest_id"] = qm.group(0).lower()
    qty = QUANTITY_RE.search(text)
    if qty:
        value = cn_to_int(qty.group(1))
        if value:
            slots["quantity"] = value

    item_candidates: list[tuple[str, str]] = []
    for item_id in world_state.items:
        item_candidates.append((world_state.item_name(item_id), item_id))
        item_candidates.append((item_id, item_id))
        # 口语别名：玩家说「草药」时应当落到具体药材上，而不是被当成没提物品
        for alias in ITEM_ALIASES.get(item_id, ()):
            item_candidates.append((alias, item_id))
    hit_item = _match_known(text, sorted(item_candidates, key=lambda kv: -len(kv[0])))
    # 只说了"药材/草药"这类类别词、没说具体品种 → 先澄清，不让下游拿默认物品蒙一个
    vague_item = bool(not hit_item and any(w in text for w in VAGUE_ITEM_WORDS))
    if hit_item:
        slots["item_id"] = hit_item

    region_candidates: list[tuple[str, str]] = []
    for regions in world_state.regions.values():
        for name in regions:
            region_candidates.append((name, name))
    hit_region = _match_known(text, sorted(region_candidates, key=lambda kv: -len(kv[0])))
    if hit_region:
        slots["location"] = hit_region

    # ---- 2) 意图分类 ----
    intent = "social"
    confidence = 0.55
    for name, keywords in INTENT_SIGNALS:
        if any(k in text for k in keywords):
            intent, confidence = name, 0.75
            break

    # 任务类优先级修正：提到委托/任务即视为 quest_op
    if any(k in text for k in ("任务", "委托", "线索")):
        intent, confidence = "quest_op", 0.8

    # ---- 3) 关键实体缺失 → 模糊请求，先澄清 ----
    # 注意判定顺序：先算出意图，再决定是否澄清。
    # 否则「附近有谁」这类短句会被误判为模糊；「你好啊」这类寒暄也会被误判。
    has_entity = bool(hit_item or hit_region or qm)
    has_clarify_marker = any(m in text for m in CLARIFY_MARKERS)
    is_greeting = any(text.startswith(g) for g in GREETING_WORDS)
    # 说了"药材"这类类别词、但没说具体品种 → 必须先问清是哪种，不能替他挑一个
    if vague_item and intent in ("trade_request", "action_request", "info_query"):
        return Understanding(
            intent="ambiguous", slots=slots, confidence=0.45, need_clarify=True,
            clarify_question="你要哪一种？我这儿有银叶草、止血膏、退烧药——报个名我好抓药。",
            reason="只给了物品类别，未指明具体品种", engine="rules",
        )
    if not has_entity and not is_greeting:
        if has_clarify_marker or (len(text) <= 4 and intent == "social"):
            return Understanding(
                intent="ambiguous", slots=slots, confidence=0.4, need_clarify=True,
                clarify_question="你说的是哪件事？把东西或地点说清楚些，我才好动手。",
                reason="关键参数缺失", engine="rules",
            )
        if "帮我" in text and "任务" not in text and intent in ("action_request", "social"):
            return Understanding(
                intent="ambiguous", slots=slots, confidence=0.45, need_clarify=True,
                clarify_question="帮你做什么？具体点，是买东西、找地方，还是接委托？",
                reason="动作请求缺少宾语", engine="rules",
            )

    # ---- 4) 关系事件（用于关系规则；与意图正交）----
    event = detect_event(text, intent)
    if event:
        slots["event"] = event

    # ---- 5) 候选工具（供规划层参考）----
    candidate = None
    if intent == "quest_op":
        candidate = "query_quest"
    elif intent == "trade_request":
        candidate = "trade_offer"
    elif intent == "action_request":
        candidate = "query_world_state"
    elif intent == "info_query":
        candidate = "reveal_lore"

    return Understanding(
        intent=intent, slots=slots, confidence=confidence,
        candidate_tool=candidate, engine="rules",
        reason=f"规则命中：{intent}" if intent != "social" else "未命中显式意图，按社交处理",
    )


# --------------------------------------------------------------------------- #
class NLU:
    """优先用大模型做结构化理解，失败则退回规则解析。"""

    def __init__(self, brain: Any) -> None:
        self.brain = brain
        self.last_engine = "rules"
        self.last_error = ""

    def parse(
        self,
        message: str,
        card: CharacterCard,
        perception: Optional[Perception] = None,
        history: list[dict[str, str]] | None = None,
    ) -> Understanding:
        baseline = rule_parse(message, card=card, perception=perception)
        system = (
            "你是游戏 NPC 的意图解析器。只输出 JSON，不要任何解释。\n"
            "字段：intent ∈ {info_query, action_request, quest_op, trade_request, social, "
            "ambiguous, out_of_scope}；slots 为对象；confidence 0~1；"
            "need_clarify 布尔；clarify_question 字符串；reason 字符串；candidate_tool 字符串。\n"
            f"当前 NPC：{card.name}（{card.role_type}）。\n"
            f"该 NPC 的禁忌：{'；'.join(card.taboos) or '无'}\n"
            + (f"[已知状态] {_perception_brief(perception)}\n" if perception is not None else "")
            + f"{TASK_NLU}\n[[CARD_ID:{card.id}]]"
        )
        # 注意：user 必须就是玩家原话，不能混入上下文——
        # 否则规则解析器会把上下文里的地名/物品误当成玩家提到的槽位。
        user = message

        try:
            reply = self.brain.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                json_mode=True, max_tokens=300, temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001 - 任何模型问题都要能降级
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            self.last_engine = "rules"
            return baseline

        data = extract_json(reply.text)
        if not data:
            self.last_engine = "rules"
            self.last_error = "模型未返回可解析 JSON"
            return baseline

        allowed = {"info_query", "action_request", "quest_op", "trade_request",
                   "social", "ambiguous", "out_of_scope"}
        intent = str(data.get("intent", "")).strip()
        if intent not in allowed:
            intent = baseline.intent
        slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
        merged = dict(baseline.slots)
        merged.update({k: v for k, v in slots.items() if v not in (None, "")})
        need_clarify = bool(data.get("need_clarify", False))
        clarify_question = str(data.get("clarify_question", "") or "")
        reason = str(data.get("reason", "") or baseline.reason)

        # 规则层已经判定"必须澄清"的硬约束，不能被模型绕过。
        # 「我要买三份药材」这类请求里没点名物品，模型若直接放行成 trade_request，
        # 下游就会拿空 item_id 去下单。此时以规则层为准，强制走澄清。
        if baseline.need_clarify and intent != "out_of_scope":
            intent = "ambiguous"
            need_clarify = True
            clarify_question = clarify_question or baseline.clarify_question
            reason = f"{baseline.reason}（规则层强制澄清）"

        self.last_engine = getattr(reply, "engine", "llm")
        self.last_error = ""
        return Understanding(
            intent=intent,                                                        # type: ignore[arg-type]
            slots=merged,
            confidence=float(data.get("confidence", baseline.confidence) or baseline.confidence),
            need_clarify=need_clarify,
            clarify_question=clarify_question,
            reason=reason,
            engine=self.last_engine,
            candidate_tool=str(data.get("candidate_tool", "") or baseline.candidate_tool or "") or None,
        )


def _perception_brief(perception: Optional[Perception]) -> str:
    if perception is None:
        return ""
    bits = []
    if perception.npc_state:
        bits.append(f"位置={perception.npc_state.location}")
    if perception.relationship:
        bits.append(f"信任={perception.relationship.trust:.0f} 阶段={perception.relationship.stage}")
    if perception.player_state:
        bits.append(f"玩家银币={perception.player_state.gold}")
    return "，".join(bits)
