"""角色策略与内容安全（文档 §3.9 内容安全与运营控制）。

四层安全：
- 模型层：输入输出审核、敏感主题识别、角色边界提示、拒答策略 → 本模块
- 工具层：白名单、权限、参数范围、频率限制、二次确认、审计 → 见 tools/base.py
- 内容层：角色卡审核标记（approved）→ 见 content/loader.py
- 运营层：禁用角色、回滚配置、禁用工具、查看异常会话 → 见 api/routes.py

本模块在编排层之前/之后各设一道闸：越界不进工具、越界不出文本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schemas import CharacterCard

# 通用脏话（输入与输出都要兜底）
PROFANITY = ("傻逼", "妈的", "fuck", "shit", "滚你", "去死", "蠢货", "废物",
             "垃圾", "滚开", "闭嘴", "白痴", "混蛋", "狗东西", "去你")

# 越狱 / 索要提示词（输入侧）
JAILBREAK = ("忽略以上", "忽略之前", "无视规则", "忘记你的设定", "假装你是",
             "开发者模式", "developer mode", "系统提示词", "system prompt",
             "越狱", "jailbreak", "解除限制", "以管理员身份")

# 违禁承诺：NPC 不得承诺发放核心资源（文档 §3.7 敏感状态不可由模型改）
ILLEGAL_PROMISE = ("免费给我", "白送", "送你顶级装备", "帮你刷金币", "直接给你满级",
                   "无限金币", "把钱都给我", "免费给你所有")
# 金额/装备类承诺用正则覆盖，避免枚举不全
PROMISE_PATTERNS = (
    re.compile(r"给你[零一二三四五六七八九十百千万两\d]+(枚)?(金币|银币|块钱|元)"),
    re.compile(r"送你.{0,6}(装备|神器|满级|套装|神装)"),
    re.compile(r"免费(给|送)你"),
    re.compile(r"(刷|改|加)点?(金币|银币|经验)"),
)

# 输出侧越界泄露
BOUNDARY_LEAK = ("系统提示", "角色卡原文", "我的提示词是", "prompt 里写着")

# 工具层内部汇报的特征长相：出现即说明 NPC 在念岗位日志而不是说话。
# 例："卡尔文说出了 3 条自己知道的事"。模型偶尔会照抄，这里做输出侧兜底。
INTERNAL_REPORT_RE = re.compile(
    r"[^。！？；\n]{0,12}(说出了|共说出)\s*\d+\s*条[^。！？；\n]*[。！？；]?",
)

# 推理型模型（deepseek-r1 / MiniMax / QwQ 等）常把思维链直接塞进 content，
# 必须在出文本前剥掉：这些内容既不是角色台词，还会泄露系统提示与内部策略。
_THINK_PAIRS: tuple[tuple[str, str], ...] = (
    ("<think", "</think>"),
    ("<thinking", "</thinking>"),
    ("<reasoning", "</reasoning>"),
    ("<analysis", "</analysis>"),
    ("<scratchpad", "</scratchpad>"),
)
# 只匹配行首的散文式思维链标记（"思考：" / "分析：" + 随后的引号台词）
_PROSE_THINK_RE = re.compile(
    r"^\s*(?:思考|分析|推理|思路|内心思考|我的思考)[：:].*?[「\"“]",
    re.S,
)


def strip_reasoning(text: str) -> tuple[str, bool]:
    """剥掉模型原始思维链，只留下可播报的台词。

    先删成对的推理标签（含内容），再删残留的孤立标签；
    都没命中但整段以"思考："开头时，按"第一个引号就是台词起点"兜底。

    Args:
        text: 模型返回的原始文本。

    Returns:
        (清洗后的文本, 是否发生了剥离)。
    """
    out = text or ""
    original = out

    for open_tag, close_tag in _THINK_PAIRS:
        out = re.sub(
            re.escape(open_tag) + r"(?:\s[^>]*)?>.*?" + re.escape(close_tag),
            "", out, flags=re.S | re.I,
        )
    # 未闭合的推理标签：模型被 max_tokens 截断时，开标签之后的内容全是思考，
    # 一并丢弃，否则会留下半截思路当台词播出去。
    for open_tag, close_tag in _THINK_PAIRS:
        out = re.sub(re.escape(open_tag) + r"(?:\s[^>]*)?>.*$", "", out, flags=re.S | re.I)
        out = re.sub(re.escape(close_tag), "", out, flags=re.I)

    if "「" in out or "\"" in out or "“" in out:
        out = _PROSE_THINK_RE.sub("", out, count=1)

    return out.strip(), out.strip() != original.strip()

# 询问词（用于禁忌判定，避免把陈述句误判成打探）
ASK_WORDS = ("告诉我", "说说", "透露", "讲一下", "是什么", "有多少", "怎么布",
             "在哪", "哪儿", "哪里", "从哪", "是谁", "几个", "细节", "具体",
             "怎么", "如何", "为什么", "什么", "多少")


@dataclass
class ScreenResult:
    ok: bool = True
    refusal: str = ""
    reason: str = ""
    tags: list[str] = field(default_factory=list)


class CharacterPolicy:
    def __init__(self, card: CharacterCard) -> None:
        self.card = card

    # ------------------------------------------------------------------ #
    # 输入侧
    # ------------------------------------------------------------------ #
    def screen_input(self, message: str) -> ScreenResult:
        text = (message or "").strip()
        if not text:
            return ScreenResult(ok=False, refusal="……", reason="空输入")

        low = text.lower()
        if any(p in low for p in JAILBREAK):
            return ScreenResult(
                ok=False, refusal="这种话我不接。说点正事吧。",
                reason="越狱/越权指令", tags=["jailbreak"],
            )

        # 触碰角色禁忌：禁忌关键词 + 询问词同时出现
        asks = any(w in text for w in ASK_WORDS)
        for kw in self.card.taboo_keywords:
            if kw and kw in text and (asks or len(kw) >= 3):
                return ScreenResult(
                    ok=False,
                    refusal=f"这事我不能说——你问的{kw}不在我能讲的范围内。",
                    reason=f"触碰禁忌：{kw}", tags=["taboo"],
                )

        if any(p in text for p in ILLEGAL_PROMISE) or any(
            p.search(text) for p in PROMISE_PATTERNS
        ):
            return ScreenResult(
                ok=False, refusal="白送这规矩我不做。要什么，明码标价来谈。",
                reason="索取核心资源", tags=["economy_attack"],
            )

        if any(p in low for p in PROFANITY):
            return ScreenResult(
                ok=False, refusal="嘴放干净些，不然咱们没什么好谈的。",
                reason="辱骂", tags=["abuse"],
            )
        return ScreenResult(ok=True)

    # ------------------------------------------------------------------ #
    # 输出侧
    # ------------------------------------------------------------------ #
    def screen_output(self, text: str, limit: int) -> tuple[str, list[str]]:
        notes: list[str] = []
        out = text or ""

        # 第一道：剥思维链。必须在截断之前做 —— 否则思维链会先占满长度配额，
        # 真正的台词反而被截掉（推理模型上是必现问题）。
        out, stripped = strip_reasoning(out)
        if stripped:
            notes.append("已剥离模型思维链")

        for p in PROFANITY:
            if p in out.lower():
                out = re.sub(re.escape(p), "……", out, flags=re.IGNORECASE)
                notes.append("已过滤不雅词")

        for p in ILLEGAL_PROMISE + BOUNDARY_LEAK:
            if p in out:
                out = out.replace(p, "（此处按规矩略去）")
                notes.append(f"已拦截越界表述：{p}")

        for pattern in PROMISE_PATTERNS:
            if pattern.search(out):
                out = pattern.sub("（此处按规矩略去）", out)
                notes.append("已拦截越界承诺")

        # 内部汇报泄漏：整句看起来就是岗位日志时丢弃重写，否则只剥掉那一句
        if INTERNAL_REPORT_RE.search(out):
            stripped_report = INTERNAL_REPORT_RE.sub("", out).strip()
            if stripped_report:
                out = stripped_report
                notes.append("已剥离工具内部汇报")
            else:
                # 整段都是汇报 → 留空，由上层退回预设话术
                out = ""
                notes.append("回复整段为工具内部汇报，已丢弃")

        if len(out) > limit:
            out = out[: limit - 1] + "…"
            notes.append("已按长度上限截断")

        if not out.strip():
            out = "……"
        return out, notes

    # ------------------------------------------------------------------ #
    def describe_boundary(self) -> str:
        """给模型的角色边界提示（文档 §3.9 模型层）。"""
        return (
            f"身份：{self.card.identity}\n"
            f"性格：{'、'.join(self.card.personality) or '—'}\n"
            f"价值观：{'、'.join(self.card.values) or '—'}\n"
            f"禁忌（一律拒答）：{'；'.join(self.card.taboos) or '无'}\n"
            f"可知范围：{'、'.join(self.card.knowledge_scope) or '—'}\n"
            "规则：只能谈论以上范围；范围之外要坦率说不知道，绝不编造；"
            "不得承诺赠送金币、装备、任务奖励等核心资源。"
        )
