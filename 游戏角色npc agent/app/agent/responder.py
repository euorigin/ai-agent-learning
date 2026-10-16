"""确定性回复模板（离线降级路径的"预设话术"，文档 §8.2 验收第 8 条）。

当模型不可用（超时/鉴权失败/未配置）时，NPC 仍必须能说话且不越界，
因此回复由结构化上下文 + 角色语气确定性地拼出，绝不空转。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

MARK_OPEN = "<!--DEMO_CTX"
MARK_CLOSE = "DEMO_CTX-->"


@dataclass
class ReplyContext:
    """离线回复所需的全部结构化信息。"""

    npc_name: str = ""
    tone: str = "平静"
    catchphrase: str = ""
    intent: str = "social"
    tool: str = ""
    tool_success: bool = True
    tool_message: str = ""
    tool_error: str = ""
    speech: str = ""
    stage: str = "陌生"
    emotion: str = "平静"
    clarify: str = ""
    refusal: str = ""
    quest_title: str = ""
    quest_stage: str = ""
    relation_summary: str = ""
    degraded: bool = False
    needs_confirmation: bool = False
    facts: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return MARK_OPEN + " " + _dumps(asdict(self)) + " " + MARK_CLOSE


def _dumps(data: dict[str, Any]) -> str:
    import json
    return json.dumps(data, ensure_ascii=False)


def parse_marker(system_prompt: str) -> ReplyContext | None:
    """从系统提示里取出 DEMO_CTX 标记（真实模型会忽略它，演示脑读取它）。"""
    import json

    start = system_prompt.find(MARK_OPEN)
    if start == -1:
        return None
    end = system_prompt.find(MARK_CLOSE, start)
    if end == -1:
        return None
    raw = system_prompt[start + len(MARK_OPEN):end].strip()
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return ReplyContext(**data)


# 工具层的 message 是"给编排层看的内部汇报"，不是角色台词。
# 一旦它混进对话，玩家看到的是 NPC 在念岗位日志（如"卡尔文说出了 3 条自己知道的事"）。
# 这里做最后一道兜底：命中特征就丢弃，改用 facts —— 宁可少说，不可说错。
_INTERNAL_REPORT_MARKERS = (
    "说出了", "共有", "条自己知道", "条可见", "已由规则服务",
)


def speaker_text(rc: "ReplyContext") -> str:
    """挑出「NPC 可以说出口」的那段文本。

    优先用 speech（编排层从工具 data 里提炼的业务内容），
    其次 tool_message（工具自带的可读结果，如"成交。银叶草 ×2，共 60 枚银币。"），
    命中内部汇报特征则跳过；最后退回 facts。

    Args:
        rc: 回复上下文。

    Returns:
        可播报文本；都不可用时返回空串，由调用方给默认话术。
    """
    for candidate in (rc.speech, rc.tool_message):
        text = (candidate or "").strip()
        if not text:
            continue
        if any(m in text for m in _INTERNAL_REPORT_MARKERS):
            continue
        return text
    if rc.facts:
        return " ".join(f for f in rc.facts[:2] if f).strip()
    return ""


# --------------------------------------------------------------------------- #
_TABOO_PREFIX = {
    "敌对": "「{name}」眯起眼睛，手按在武器上。",
    "陌生": "「{name}」上下打量了你一番。",
    "熟悉": "「{name}」放下手里的活。",
    "盟友": "「{name}」朝你点点头。",
}


def template_reply(rc: ReplyContext) -> str:
    """按意图分派，拼出符合角色语气的一段话。"""
    head = _TABOO_PREFIX.get(rc.stage, "").format(name=rc.npc_name)
    lead = f"{head} " if head else ""
    # 一律走 speaker_text，绝不直接念 rc.tool_message —— 那是内部汇报，不是台词
    said = speaker_text(rc)

    # 1) 越界 / 拒答
    if rc.refusal:
        return f"{lead}{rc.refusal}"

    # 2) 需要澄清
    if rc.clarify:
        return f"{lead}{rc.clarify}"

    # 2.5) 需要二次确认 —— 讲清楚要做什么，等玩家点头
    if rc.needs_confirmation:
        return (f"{lead}{rc.tool_error}"
                + (f" {rc.catchphrase}" if rc.catchphrase else ""))

    # 3) 工具被拦截或执行失败 —— 必须说清失败原因，不得谎称已完成（§3.7 / §8.2）
    if rc.tool and not rc.tool_success:
        return (f"{lead}这事我办不了：{rc.tool_error}"
                + (f" {rc.catchphrase}" if rc.catchphrase else ""))

    # 4) 任务
    if rc.intent == "quest_op" and rc.quest_title:
        if rc.tool_success:
            return (f"{lead}「{rc.quest_title}」——就这么说定了。"
                    f"眼下该做的是：{rc.quest_stage}。"
                    + (f" {rc.catchphrase}" if rc.catchphrase else ""))
        return f"{lead}关于「{rc.quest_title}」，还差些火候。"

    # 5) 行动请求
    if rc.intent == "action_request":
        return (f"{lead}{said or '我这就去办。'}"
                + (f" {rc.catchphrase}" if rc.catchphrase else ""))

    # 6) 交易
    if rc.intent == "trade_request":
        # 交易意图但没有工具结果 = 这个 NPC 不做买卖（如艾琳只给不收）。
        # 绝不能回"价钱可以再谈"——那是在暗示一笔根本不会发生的交易。
        if not rc.tool:
            return (f"{lead}这个我不做买卖。要交易，你去找旧市集的老约翰。"
                    + (f" {rc.catchphrase}" if rc.catchphrase else ""))
        return (f"{lead}{said or '价钱咱们可以再谈。'}"
                + (f" {rc.catchphrase}" if rc.catchphrase else ""))

    # 7) 信息查询 —— 有实际内容就转述，没有就承认不知道，别念内部汇报
    if rc.intent == "info_query":
        if said:
            return f"{lead}{said}"
        if rc.tool and rc.tool_success:
            return (f"{lead}这事我知道的不多，怕说岔了。"
                    + (f" {rc.catchphrase}" if rc.catchphrase else ""))

    # 8) 社交 / 闲聊
    if rc.emotion and rc.emotion != "平静":
        return (f"{lead}（{rc.emotion}）{said or '你说，我听着。'}"
                + (f" {rc.catchphrase}" if rc.catchphrase else ""))
    return f"{lead}{said or '嗯，我在听。'}" + (
        f" {rc.catchphrase}" if rc.catchphrase else "")
