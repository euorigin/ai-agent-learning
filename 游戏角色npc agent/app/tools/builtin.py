"""内置受控工具集（文档 §3.7 表格中的五类工具）。

| 类别 | 工具 | 权限点 |
|---|---|---|
| 查询 | query_world_state / query_nearby_entities / query_inventory / query_quest | query.* |
| 知识 | reveal_lore | lore.reveal |
| 剧情 | offer_quest / advance_quest | quest.* |
| 交互 | trade_offer / give_item / open_gate | trade.offer / interact.* |
| 移动 | move_intent（只给意图，引擎执行） | move.intent |
| 战斗 | combat_intent（只给意图，引擎执行） | combat.intent |
| 关系 | propose_relation_event（模型建议 → 规则确认） | 无（规则层兜底校验） |
"""

from __future__ import annotations

from typing import Any

from ..content import content_service
from .base import Proposal, ToolContext, ToolRegistry
from ..schemas import ToolSpec

NPC_BUY_MARGIN = 0.6  # 商人收货只给市价的六成


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------- #
# 查询类
# --------------------------------------------------------------------------- #
def _query_world_state(ctx: ToolContext) -> Proposal:
    env = ctx.world.environment(ctx.instance_id)
    region = ctx.args.get("region") or ctx.world.npc_state(ctx.npc_id).location
    info = ctx.world.region_info(ctx.instance_id, region) or {}
    gate_id = "north_gate"
    return Proposal(
        message=f"{env['time']}，{env['weather']}；{region}：{info.get('note', '一切如常')}",
        data={
            "environment": env,
            "region": {"name": region, **info},
            "gate_open": ctx.world.is_gate_open(ctx.instance_id, gate_id),
        },
    )


def _query_nearby_entities(ctx: ToolContext) -> Proposal:
    nearby = ctx.world.nearby(ctx.npc_id, ctx.instance_id)
    return Proposal(
        message=f"附近共有 {len(nearby)} 个可见对象",
        data={"nearby": nearby, "visibility": "seen", "note": "仅限同一场景内亲眼可见"},
    )


def _query_inventory(ctx: ToolContext) -> Proposal:
    who = ctx.args.get("who", "player")
    if who == "npc":
        items = ctx.world.npc_resources(ctx.npc_id)
        names = {ctx.world.item_name(k): v for k, v in items.items()}
        prices = {ctx.world.item_name(k): ctx.world.price(k) for k in items}
        quote = "、".join(f"{n}×{items[k]}（{ctx.world.price(k)} 枚/份）"
                         for k, n in ((k, ctx.world.item_name(k)) for k in items))
        return Proposal(
            message=f"{ctx.card.name} 手上的货：{quote or '空货架'}",
            data={"who": "npc", "inventory": items, "inventory_named": names,
                  "prices": prices, "gold": ctx.world.npc_gold.get(ctx.npc_id, 0)},
        )
    items = ctx.world.inventory(ctx.player_id)
    names = {ctx.world.item_name(k): v for k, v in items.items()}
    return Proposal(
        message=f"你身上：{names or '什么都没有'}",
        data={"who": "player", "inventory": items, "inventory_named": names,
              "gold": ctx.world.player(ctx.player_id).gold},
    )


def _query_quest(ctx: ToolContext) -> Proposal:
    quests = ctx.world.quest_status_for(ctx.npc_id, ctx.player_id)
    if not quests:
        return Proposal(message=f"{ctx.card.name} 手上没有需要你办的委托。",
                        data={"quests": []})
    q = quests[0]
    status_cn = {"available": "可以接", "active": "进行中",
                 "locked": "暂不可接", "completed": "已完成", "failed": "已失败"}.get(q["status"], q["status"])
    return Proposal(
        message=f"「{q['title']}」（{status_cn}）——{q['stage_name']}",
        data={"quests": quests},
    )


def _reveal_lore(ctx: ToolContext) -> Proposal:
    topic = ctx.args.get("topic", "")
    known = content_service.knowledge_for(ctx.card)
    if topic:
        known = [e for e in known if topic in e.get("topics", []) or topic in e.get("text", "")]
    if not known:
        # 文档 §3.3：明确区分"未知信息"，不能编造
        unknown = content_service.unknown_topics(ctx.card)
        return Proposal(
            ok=False, reason_code="unknown_knowledge",
            error=f"关于「{topic or '此事'}」，{ctx.card.name} 并不知情"
                  + (f"（其可知范围之外：{'、'.join(unknown[:4])}）" if unknown else ""),
        )
    picks = known[:4]
    return Proposal(
        message=f"{ctx.card.name} 说出了 {len(picks)} 条自己知道的事",
        data={"entries": [{"text": e["text"], "source": e["source"],
                           "confidence": e["confidence"]} for e in picks]},
    )


# --------------------------------------------------------------------------- #
# 剧情类
# --------------------------------------------------------------------------- #
def _resolve_quest_id(ctx: ToolContext, want_status: str) -> tuple[str, Optional[str]]:
    """玩家没点名 q_xxx 时，若该 NPC 手上只有一条符合条件的委托，就默认用它。

    多条则必须澄清，绝不猜（对应文档 §3.2 模糊请求先追问）。
    """
    candidates = []
    for qdef in ctx.world.quest_defs_by_giver(ctx.npc_id):
        st = ctx.world.quest_state(qdef.id, ctx.player_id)
        if st.status == want_status:
            candidates.append(qdef)
    if len(candidates) == 1:
        return candidates[0].id, None
    if not candidates:
        return "", f"{ctx.card.name} 手头没有处于「{want_status}」状态的委托"
    names = "、".join(f"{q.id}（{q.title}）" for q in candidates)
    return "", f"有多条委托符合条件，请你指明是哪一条：{names}"


def _offer_quest(ctx: ToolContext) -> Proposal:
    qid = ctx.args.get("quest_id", "")
    if not qid:
        qid, err = _resolve_quest_id(ctx, "available")
        if err:
            return Proposal(ok=False, reason_code="need_quest_id", error=err)
    qdef = ctx.world.quest_def(qid)
    if qdef is None:
        return Proposal(ok=False, reason_code="unknown_quest", error=f"没有编号为 {qid} 的委托")
    if qdef.giver != ctx.npc_id:
        return Proposal(ok=False, reason_code="not_your_quest",
                        error=f"这条委托的发布者是 {qdef.giver}，{ctx.card.name} 无权发布")
    state = ctx.world.quest_state(qid, ctx.player_id)
    if state.status == "completed":
        return Proposal(ok=False, reason_code="already_completed", error=f"「{qdef.title}」已经完成了")
    if state.status == "locked":
        from ..store import repository
        rel_obj = repository.relationship(ctx.player_id, ctx.npc_id)
        return Proposal(
            ok=False, reason_code="precondition_failed",
            error=f"{ctx.card.name} 还信不过你（信任 {rel_obj.trust:.0f}，需要 {qdef.min_trust:.0f}）",
            data={"trust": rel_obj.trust, "required_trust": qdef.min_trust},
        )
    if state.status == "active":
        return Proposal(
            message=f"「{qdef.title}」你已经接下了，当前进度：{qdef.stages[state.stage]}",
            data={"quest_id": qid, "title": qdef.title, "status": "active",
                  "stage": state.stage, "stage_name": qdef.stages[state.stage]},
        )
    return Proposal(
        message=f"{ctx.card.name} 把「{qdef.title}」交给了你。眼下该做的是：{qdef.stages[0]}",
        data={"quest_id": qid, "title": qdef.title, "summary": qdef.summary,
              "stages": qdef.stages, "stage": 0, "stage_name": qdef.stages[0],
              "rewards": qdef.rewards, "location": qdef.location},
        delta={"quests": [{"quest_id": qid, "status": "active", "stage": 0}]},
    )


def _advance_quest(ctx: ToolContext) -> Proposal:
    qid = ctx.args.get("quest_id", "")
    if not qid:
        qid, err = _resolve_quest_id(ctx, "active")
        if err:
            return Proposal(ok=False, reason_code="need_quest_id", error=err)
    qdef = ctx.world.quest_def(qid)
    if qdef is None:
        return Proposal(ok=False, reason_code="unknown_quest", error=f"没有编号为 {qid} 的委托")
    state = ctx.world.quest_state(qid, ctx.player_id)
    if state.status != "active":
        return Proposal(ok=False, reason_code="precondition_failed",
                        error=f"「{qdef.title}」当前状态为 {state.status}，无法推进")
    # 前置条件：任务要求物品时必须持有
    cond = qdef.conditions or {}
    need_item = cond.get("need_item")
    need_qty = int(cond.get("quantity", 1))
    if need_item:
        have = ctx.world.inventory(ctx.player_id).get(need_item, 0)
        if have < need_qty:
            return Proposal(
                ok=False, reason_code="precondition_failed",
                error=f"还差 {ctx.world.item_name(need_item)} ×{need_qty - have}"
                      f"（现有 {have}）",
                data={"need_item": need_item, "need": need_qty, "have": have},
            )
    next_stage = state.stage + 1
    completed = next_stage >= len(qdef.stages)
    if completed:
        next_stage = len(qdef.stages) - 1
    return Proposal(
        message=(f"「{qdef.title}」已完成。{ctx.card.name} 兑现了报酬。" if completed
                 else f"「{qdef.title}」推进到：{qdef.stages[next_stage]}"),
        data={"quest_id": qid, "title": qdef.title, "stage": next_stage,
              "stage_name": qdef.stages[next_stage],
              "status": "completed" if completed else "active",
              "rewards": qdef.rewards if completed else {}},
        delta={"quests": [{
            "quest_id": qid, "stage": next_stage,
            "status": "completed" if completed else "active",
        }]},
    )


# --------------------------------------------------------------------------- #
# 交互 / 交易类
# --------------------------------------------------------------------------- #
def _trade_offer(ctx: ToolContext) -> Proposal:
    item_id = ctx.args.get("item_id", "")
    qty = int(ctx.args.get("quantity", 1) or 1)
    side = ctx.args.get("side", "buy")
    if qty <= 0 or qty > 99:
        return Proposal(ok=False, reason_code="bad_argument", error="数量需在 1~99 之间")
    if not item_id:
        return Proposal(ok=False, reason_code="missing_item",
                        error=f"{ctx.card.name} 没听清你要的是哪样东西，报个名或者先看看货")
    if not ctx.world.has_item(item_id):
        return Proposal(ok=False, reason_code="unknown_item",
                        error=f"{ctx.card.name} 不知道「{item_id}」是什么东西")
    price = ctx.world.price(item_id)
    name = ctx.world.item_name(item_id)
    player = ctx.world.player(ctx.player_id)

    if side == "buy":
        total = int(price * qty)
        stock = int(ctx.world.npc_state(ctx.npc_id).resources.get(item_id, 0))
        if stock < qty:
            return Proposal(ok=False, reason_code="out_of_stock",
                            error=f"{name} 只剩 {stock} 份，给不了 {qty} 份")
        if player.gold < total:
            return Proposal(ok=False, reason_code="not_enough_gold",
                            error=f"你只有 {player.gold} 枚银币，{name} ×{qty} 要 {total} 枚")
        return Proposal(
            message=f"成交。{name} ×{qty}，共 {total} 枚银币。",
            data={"item_id": item_id, "quantity": qty, "unit_price": price,
                  "total": total, "side": "buy"},
            delta={"gold": -total, "items": {item_id: qty},
                   "npc_items": {ctx.npc_id: {item_id: -qty}},
                   "npc_gold": {ctx.npc_id: total}},
        )

    # side == sell：NPC 按六成市价收购
    unit = int(price * NPC_BUY_MARGIN)
    total = unit * qty
    have = ctx.world.inventory(ctx.player_id).get(item_id, 0)
    if have < qty:
        return Proposal(ok=False, reason_code="not_enough_item",
                        error=f"你身上只有 {name} ×{have}，卖不出 {qty} 份")
    if ctx.world.npc_gold.get(ctx.npc_id, 0) < total:
        return Proposal(ok=False, reason_code="npc_not_enough_gold",
                        error=f"{ctx.card.name} 手头银币不够收这批货")
    return Proposal(
        message=f"收货。{name} ×{qty}，给你 {total} 枚银币。",
        data={"item_id": item_id, "quantity": qty, "unit_price": unit,
              "total": total, "side": "sell"},
        delta={"gold": total, "items": {item_id: -qty},
               "npc_items": {ctx.npc_id: {item_id: qty}},
               "npc_gold": {ctx.npc_id: -total}},
    )


def _give_item(ctx: ToolContext) -> Proposal:
    item_id = ctx.args.get("item_id", "")
    qty = int(ctx.args.get("quantity", 1) or 1)
    if qty <= 0 or qty > 99:
        return Proposal(ok=False, reason_code="bad_argument", error="数量需在 1~99 之间")
    if not ctx.world.has_item(item_id):
        return Proposal(ok=False, reason_code="unknown_item", error=f"没有「{item_id}」这种物品")
    stock = int(ctx.world.npc_state(ctx.npc_id).resources.get(item_id, 0))
    name = ctx.world.item_name(item_id)
    if stock < qty:
        return Proposal(ok=False, reason_code="out_of_stock",
                        error=f"{ctx.card.name} 手上没有足够的{name}（现有 {stock}）")
    return Proposal(
        message=f"{ctx.card.name} 把 {name} ×{qty} 递给了你。",
        data={"item_id": item_id, "quantity": qty, "item_name": name},
        delta={"items": {item_id: qty}, "npc_items": {ctx.npc_id: {item_id: -qty}}},
    )


def _open_gate(ctx: ToolContext) -> Proposal:
    gate_id = ctx.args.get("gate_id", "north_gate")
    want_open = bool(ctx.args.get("open", True))
    gate = ctx.world.gate(ctx.instance_id, gate_id)
    if gate is None:
        return Proposal(ok=False, reason_code="unknown_gate", error=f"未知门禁：{gate_id}")
    gate_name = gate.get("name", gate_id)
    if ctx.world.is_gate_open(ctx.instance_id, gate_id) == want_open:
        return Proposal(message=f"{gate_name}已经是{'开启' if want_open else '关闭'}状态，无需变更",
                        data={"gate_id": gate_id, "gate_name": gate_name,
                              "open": want_open, "changed": False},
                        delta={})
    return Proposal(
        message=f"{ctx.card.name} {'下令开' if want_open else '下令关闭'}了{gate_name}。",
        data={"gate_id": gate_id, "gate_name": gate_name, "open": want_open,
              "changed": True, "note": gate.get("note", "")},
        delta={"gates": {gate_id: want_open}},
    )


# --------------------------------------------------------------------------- #
# 移动 / 战斗：Agent 只给意图，由引擎确定性执行（文档 §3.7）
# --------------------------------------------------------------------------- #
def _move_intent(ctx: ToolContext) -> Proposal:
    target = ctx.args.get("target", "")
    if not target:
        return Proposal(ok=False, reason_code="bad_argument", error="缺少移动目标")
    return Proposal(
        message=f"{ctx.card.name} 决定前往{target}",
        data={"intent": "move", "target": target, "executor": "game_engine",
              "note": "移动由游戏引擎/寻路组件执行并校验，Agent 只给意图"},
    )


def _combat_intent(ctx: ToolContext) -> Proposal:
    target = ctx.args.get("target", "")
    stance = ctx.args.get("stance", "防御")
    return Proposal(
        message=f"{ctx.card.name} 进入{stance}姿态",
        data={"intent": "combat", "target": target, "stance": stance,
              "executor": "game_engine",
              "note": "实时战斗逻辑由引擎控制，Agent 只给意图"},
    )


# --------------------------------------------------------------------------- #
# 关系：模型只能"建议"，规则服务确认后才生效（文档 §3.6）
# --------------------------------------------------------------------------- #
def _propose_relation_event(ctx: ToolContext) -> Proposal:
    from ..store import repository
    from ..world.rules import EVENT_EFFECTS, apply_event

    kind = ctx.args.get("event_kind", "")
    if kind not in EVENT_EFFECTS:
        return Proposal(ok=False, reason_code="bad_argument",
                        error=f"未知关系事件：{kind}",
                        data={"allowed": sorted(EVENT_EFFECTS)})
    rel = repository.relationship(ctx.player_id, ctx.npc_id)
    outcome = apply_event(
        rel, ctx.world.npc_state(ctx.npc_id), ctx.card, kind,
        note=ctx.args.get("note", ""),
    )
    return Proposal(
        message=f"关系事件「{kind}」已由规则服务确认",
        data={
            "relation_changes": [c.model_dump(mode="json") for c in outcome.relation_changes],
            "stage": outcome.stage_after,
            "emotion": outcome.emotion.model_dump(mode="json") if outcome.emotion else None,
            "authority": "rule_service",
        },
    )


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def build_registry() -> ToolRegistry:
    reg = ToolRegistry()

    reg.register(ToolSpec(
        name="query_world_state", category="query", read_only=True,
        description="查询当前时间、天气、所在区域的氛围与城门开闭状态。只读。",
        permissions=["query.world"],
        parameters=_obj({"region": {"type": "string", "description": "要查询的区域名，缺省用自己所在位置"}}),
    ), _query_world_state)

    reg.register(ToolSpec(
        name="query_nearby_entities", category="query", read_only=True,
        description="查询同一场景内你亲眼可见的附近角色。只读，受距离与实例限制。",
        permissions=["query.nearby"],
        parameters=_obj({}),
    ), _query_nearby_entities)

    reg.register(ToolSpec(
        name="query_inventory", category="query", read_only=True,
        description="查询自己或对方的货物与银币。只读。",
        permissions=["query.inventory"],
        parameters=_obj({"who": {"type": "string", "enum": ["player", "npc"],
                                 "description": "player=玩家，npc=自己"}}),
    ), _query_inventory)

    reg.register(ToolSpec(
        name="query_quest", category="query", read_only=True,
        description="查询由你发布的、与该玩家相关的委托及其进度。只读。",
        permissions=["query.quest"],
        parameters=_obj({}),
    ), _query_quest)

    reg.register(ToolSpec(
        name="reveal_lore", category="query", read_only=True,
        description="回忆自己知识范围内的一条见闻。超出范围会明确返回「不知情」，不得编造。",
        permissions=["lore.reveal"],
        parameters=_obj({"topic": {"type": "string", "description": "话题，如 药材/治安/商品行情"}}),
    ), _reveal_lore)

    reg.register(ToolSpec(
        name="offer_quest", category="quest", read_only=False,
        description="把自己掌管的委托交给玩家。需满足信任与前置条件，否则返回失败原因。",
        permissions=["quest.offer"], idempotent=False,
        precondition_note="仅当委托处于 available 状态且玩家满足最低信任时才能发布",
        parameters=_obj({"quest_id": {"type": "string"}}, ["quest_id"]),
    ), _offer_quest)

    reg.register(ToolSpec(
        name="advance_quest", category="quest", read_only=False,
        description="推进玩家已接下的委托阶段；缺前置物品时返回失败原因，不得谎称完成。",
        permissions=["quest.advance"], idempotent=False,
        precondition_note="任务须为 active；若任务要求物品，玩家必须已持有",
        parameters=_obj({"quest_id": {"type": "string"}, "note": {"type": "string"}}, ["quest_id"]),
    ), _advance_quest)

    reg.register(ToolSpec(
        name="trade_offer", category="interact", read_only=False,
        description="与玩家成交：buy=卖给玩家，sell=向玩家收货。库存、价格与银币均由服务端校验。",
        permissions=["trade.offer"], requires_confirmation=True, idempotent=False,
        precondition_note="买：自己有货且玩家银币够；卖：玩家有货且自己银币够",
        parameters=_obj({
            "item_id": {"type": "string"},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 99},
            "side": {"type": "string", "enum": ["buy", "sell"]},
        }, ["item_id", "quantity", "side"]),
    ), _trade_offer)

    reg.register(ToolSpec(
        name="give_item", category="interact", read_only=False,
        description="把自己手上的物品赠予玩家。需自己确实持有。",
        permissions=["interact.give_item"], requires_confirmation=True, idempotent=False,
        parameters=_obj({
            "item_id": {"type": "string"},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 99},
        }, ["item_id", "quantity"]),
    ), _give_item)

    reg.register(ToolSpec(
        name="open_gate", category="interact", read_only=False,
        description="开闭城门。只有持卫队权限者可执行，且会变更世界状态。",
        permissions=["interact.open_gate"], requires_confirmation=True, idempotent=False,
        precondition_note="需要 interact.open_gate 权限点（卫队成员）",
        parameters=_obj({
            "gate_id": {"type": "string"},
            "open": {"type": "boolean"},
        }, ["gate_id"]),
    ), _open_gate)

    reg.register(ToolSpec(
        name="move_intent", category="move", read_only=True,
        description="表达移动意图（前往某地）。真正的寻路与移动由游戏引擎执行。",
        permissions=["move.intent"],
        parameters=_obj({"target": {"type": "string"}}, ["target"]),
    ), _move_intent)

    reg.register(ToolSpec(
        name="combat_intent", category="combat", read_only=True,
        description="表达战斗意图（姿态与目标）。实时战斗由引擎控制，本工具不改变状态。",
        permissions=["combat.intent"],
        parameters=_obj({
            "target": {"type": "string"},
            "stance": {"type": "string", "enum": ["进攻", "防御", "撤退", "呼叫支援"]},
        }),
    ), _combat_intent)

    reg.register(ToolSpec(
        name="propose_relation_event", category="interact", read_only=False,
        description="当玩家做出了值得记住的举动时，建议一次关系变化。"
                    "最终数值由规则服务计算并夹取，模型无权直接设定数值。",
        permissions=[], idempotent=False,
        parameters=_obj({
            "event_kind": {"type": "string",
                           "enum": ["helped", "gift", "promise_kept", "promise_broken",
                                    "cheated", "threatened", "insulted", "quest_completed",
                                    "quest_failed", "trade_fair", "trade_haggled_hard",
                                    "greeted_politely"]},
            "note": {"type": "string"},
        }, ["event_kind"]),
    ), _propose_relation_event)

    return reg


tool_registry = build_registry()
