"""分层规划（文档 §3.5 目标、计划与决策）。

四层：
- 战略层：长期目标与价值观权重（低频更新，来自角色卡）
- 战术层：当前任务与下一步行动选择（本模块）
- 执行层：具体工具调用与动作序列（由工具层 + 游戏引擎执行）
- 反应层：即时情绪、受击、危险等打断（优先级高于闲聊）

设计要点：计划不是"一段文字"，而是"可执行动作序列"，
且必须在权限与前置条件允许的范围内（文档 §3.5 实现建议）。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ..schemas import CharacterCard, NpcRuntimeState, Perception, PlanCandidate, Understanding

# 意图 → 计划骨架
INTENT_PLAYBOOK: dict[str, tuple[str, list[str]]] = {
    "info_query": ("检索可知事实后作答，不改变世界状态",
                   ["回忆可知范围内的知识", "核对当前世界状态", "以角色口吻作答"]),
    "action_request": ("校验权限与前置条件后申请执行动作",
                       ["确认请求目标", "检查权限与前置条件", "申请调用受控工具", "汇报权威结果"]),
    "quest_op": ("查询任务权威状态并做状态迁移",
                 ["读取任务服务状态", "判断是否满足发布/推进条件", "执行迁移并生成确认对话"]),
    "trade_request": ("核对库存、价格与银币后成交",
                      ["读取双方库存与银币", "按市价计算总价", "申请交易", "汇报成交或失败原因"]),
    "ambiguous": ("先追问补齐关键参数，暂不执行",
                  ["识别缺失参数", "向玩家澄清", "等待补充后重新规划"]),
    "out_of_scope": ("拒答并守住角色边界",
                     ["识别越界点", "引用角色禁忌", "给出安全的替代问法"]),
    "social": ("以角色语气回应，不改变世界状态",
               ["判断情绪基调", "组织符合人设的回应", "必要时记录关系事件"]),
}

# 关键词 → 首选工具（仅在角色有该权限时启用）
TOOL_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("开城门", "开门", "放我过去", "通行"), "open_gate"),
    (("附近", "周围", "谁在", "有没有人"), "query_nearby_entities"),
    (("库存", "有什么货", "多少钱", "价格"), "query_inventory"),
    (("几点", "什么时候", "天气", "宵禁", "城门"), "query_world_state"),
    (("任务", "委托", "线索", "进度"), "query_quest"),
    (("传闻", "听说", "知道什么", "打听"), "reveal_lore"),
    (("买", "卖", "来一份", "收购", "交易"), "trade_offer"),
]


class Planner:
    def __init__(self, allowed_tools: Iterable[str]) -> None:
        self.allowed = set(allowed_tools)

    # ------------------------------------------------------------------ #
    def plan(
        self,
        understanding: Understanding,
        perception: Perception,
        card: CharacterCard,
        text: str,
    ) -> PlanCandidate:
        tactical, steps = INTENT_PLAYBOOK.get(
            understanding.intent, INTENT_PLAYBOOK["social"]
        )
        plan = PlanCandidate(
            strategic=(card.goals.current[0] if card.goals.current
                       else (card.goals.long_term[0] if card.goals.long_term else "")),
            tactical=tactical,
            executive=list(steps),
        )

        # 反应层：情绪强度高时优先处理（文档 §3.5 反应层优先于闲聊）
        state = perception.npc_state
        if state is not None and state.emotion.intensity >= 0.6:
            plan.reactive = f"情绪处于「{state.emotion.label}」，回应需先带出该情绪"
            if understanding.intent == "social":
                plan.tactical = "先处理即时情绪，再谈正事"

        # 超出边界 → 不调用任何工具
        if understanding.intent in ("out_of_scope", "ambiguous"):
            return plan

        # 选工具 + 构造参数
        tool = self._choose_tool(understanding, text)
        if tool and tool in self.allowed:
            plan.tool = tool
            plan.tool_args = self._build_args(tool, understanding, text)
        return plan

    # ------------------------------------------------------------------ #
    def _choose_tool(self, understanding: Understanding, text: str) -> Optional[str]:
        intent = understanding.intent
        slots = understanding.slots

        if intent == "quest_op":
            if any(k in text for k in ("接受", "接下", "想接", "要接", "接这个", "领这个",
                                       "给我这任务", "我来做", "我要做")):
                return "offer_quest"
            if any(k in text for k in ("完成", "回报", "交任务", "办好了", "推进")):
                return "advance_quest"
            return "query_quest"

        if intent == "trade_request":
            # 问价 / 问库存 / 看货 / 问行情 → 走查询，不直接发起交易
            # （既避免无谓的二次确认，也让商人能如实报货）
            asking_only = any(k in text for k in (
                "多少钱", "什么价", "价格", "价钱", "怎么卖", "贵不贵", "什么价位",
                "有什么货", "有什么东西卖", "卖什么", "有什么卖", "货架", "库存",
                "有什么货色", "展示一下", "看看货", "有什么可买",
            ))
            intent_to_buy = any(k in text for k in ("我要", "我买", "给我来", "来一份",
                                                    "来两份", "来三份", "我要买"))
            if asking_only and not intent_to_buy:
                return "query_inventory"
            return "trade_offer"

        if intent in ("info_query", "action_request"):
            for keywords, tool in TOOL_HINTS:
                if any(k in text for k in keywords):
                    return tool
            return understanding.candidate_tool

        return None

    @staticmethod
    def _build_args(tool: str, understanding: Understanding, text: str) -> dict[str, Any]:
        slots = understanding.slots
        if tool == "trade_offer":
            side = "sell" if any(k in text for k in ("卖给你", "卖", "收购", "出手")) else "buy"
            # 认不出具体物品就别下单：以前这里默认写死 yin_leaf，
            # 玩家说「我要买两份止血膏」而解析失败时，会静默按银叶草成交——错得无法发现。
            # 现在交给工具层报 unknown_item，由 NPC 如实说"你要哪种"。
            return {
                "item_id": slots.get("item_id", ""),
                "quantity": int(slots.get("quantity", 1) or 1),
                "side": side,
                # 注意：默认不带 confirm，由二次确认流程补齐（文档 §3.9 工具层）
            }
        if tool == "query_inventory":
            nth = ("你有什么", "库存", "有什么货", "多少钱", "价格", "价钱",
                   "怎么卖", "你这里", "你这儿", "卖什么")
            return {"who": "npc" if any(k in text for k in nth) else "player"}
        if tool == "open_gate":
            return {"gate_id": "north_gate", "open": not any(k in text for k in ("关", "闭"))}
        if tool == "reveal_lore":
            return {"topic": slots.get("topic", "")}
        if tool in ("offer_quest", "advance_quest"):
            qid = slots.get("quest_id")
            args: dict[str, Any] = {}
            if qid:
                args["quest_id"] = qid
            return args
        return {}

    # ------------------------------------------------------------------ #
    def refresh(
        self,
        card: CharacterCard,
        state: Optional[NpcRuntimeState],
        perception: Perception,
        goal: str = "",
    ) -> PlanCandidate:
        """给 /plan 接口：基于当前世界状态刷新一份可执行计划。"""
        strategic = goal or (card.goals.current[0] if card.goals.current
                             else (card.goals.long_term[0] if card.goals.long_term else ""))
        tactical = "维持日常行为，等待玩家或世界事件触发"
        executive: list[str] = []
        reactive: Optional[str] = None

        if state is not None:
            if state.hp < state.max_hp * 0.4:
                tactical = "自保优先：脱离战斗并寻找掩护"
                executive = ["评估威胁", "申请撤退移动意图", "恢复状态"]
                reactive = "生命值偏低"
            elif state.emotion.intensity >= 0.6:
                tactical = f"受「{state.emotion.label}」影响，先稳定情绪"
                executive = ["平复情绪", "恢复日常行为"]
                reactive = "情绪强度偏高"

        quests = perception.quests
        active = [q for q in quests if q.status == "active"]
        if active and tactical.startswith("维持"):
            tactical = f"跟进在办委托：{active[0].quest_id}"
            executive = ["查看任务阶段", "准备推进所需物品", "与玩家交接"]

        if not executive:
            executive = ["观察环境", "维持日常行为"]

        return PlanCandidate(strategic=strategic, tactical=tactical,
                             executive=executive, reactive=reactive)
