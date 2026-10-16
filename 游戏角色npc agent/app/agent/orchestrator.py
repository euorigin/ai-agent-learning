"""Agent 编排器：文档 §4.2「Agent 工作流」的六步实现。

    1. 接收玩家输入或游戏事件，确认 NPC / 玩家 / 场景 / 实例身份
    2. 读取世界状态、角色状态、关系摘要、近期记忆和任务状态
    3. 识别玩家意图，判断是回答、追问、拒绝、工具调用还是行为切换
    4. 生成回答或行动计划候选，并校验角色边界、工具权限和游戏规则
    5. 调用游戏服务执行动作，获取权威结果和状态差异
    6. 生成最终文本、语音、表情和动作标签，写入记忆与审计日志

贯穿始终的核心原则（文档 §10）：
    模型负责理解和提出决策，游戏服务负责确认事实和修改状态，
    行为系统负责执行动作，策划配置负责角色边界，评测与审计负责长期可控。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from ..config import settings
from ..content import content_service
from ..llm import DemoBrain, LLMError, get_brain
from ..llm.demo_brain import TASK_REPLY
from ..memory import memory_service
from ..schemas import (
    AgentTrace,
    ChatRequest,
    ChatResponse,
    MemoryWriteSuggestion,
    PerceiveRequest,
    Perception,
    PlanCandidate,
    PlanRequest,
    RelationChange,
    ToolResult,
    Turn,
    Understanding,
)
from ..store import repository
from ..tools import ToolContext, tool_registry
from ..world import apply_event, baseline_emotion, decay_emotion, detect_event, world_state
from .nlu import NLU
from .planner import Planner
from .policy import CharacterPolicy, strip_reasoning
from .presentation import PresentationBuilder

CONFIRM_WORDS = ("确认", "确定", "可以", "成交", "好的", "好", "行", "同意", "来吧", "就这么办")
CANCEL_WORDS = ("算了", "不用了", "取消", "不要了", "别提了")

# 工具层的 message 是"给编排层看的内部汇报"，不是台词。这些是它的特征长相：
# 出现即说明台词退回了内部汇报（玩家视角就是 NPC 在念岗位日志）。
_INTERNAL_REPORT_MARKERS = (
    "说出了", "说出口", "共有", "条自己知道", "条可见", "已由规则服务",
)


def _tool_speech(tool_result) -> str:
    """把工具结果里"能被 NPC 说出口的内容"提炼成一段话。

    工具层返回两层信息：
    - `message`：内部汇报，如"卡尔文说出了 3 条自己知道的事"；
    - `data`：真正的业务内容，如 `entries`（知识条目）、`quests`（委托列表）。
    玩家要听的是后者。这里按工具种类把 data 转成人话，
    拿不到就返回空串——**宁可让模型/模板自己组织，也不能把内部汇报当台词念**。

    Args:
        tool_result: 工具返回值；None 或失败时返回空串。

    Returns:
        可直接转述的内容文本；无可转述内容时为空串。
    """
    if tool_result is None or not tool_result.success:
        return ""
    data = tool_result.data or {}

    # 知识查询：把条目正文拼起来（这是玩家问"你知道什么"时真正想听的东西）
    entries = data.get("entries")
    if isinstance(entries, list) and entries:
        texts = [str(e.get("text", "")).strip() for e in entries if isinstance(e, dict)]
        texts = [t for t in texts if t]
        if texts:
            return "；".join(texts)

    # 委托查询：把当前阶段说出来
    quests = data.get("quests")
    if isinstance(quests, list) and quests:
        q = quests[0]
        status_cn = {"available": "可以接", "active": "进行中", "locked": "暂不可接",
                     "completed": "已完成", "failed": "已失败"}.get(q.get("status"), "")
        detail = f"「{q.get('title', '')}」{status_cn}——{q.get('stage_name', '')}".strip("——")
        return detail

    # 库存查询：列出货名与价格。注意 query_inventory 的 data 是
    # {"inventory": {item_id: 数量}, "inventory_named": {物品名: 数量}, "prices": {物品名: 价}}
    named = data.get("inventory_named")
    prices = data.get("prices") or {}
    if isinstance(named, dict) and named:
        parts = []
        for name, count in named.items():
            price = prices.get(name)
            parts.append(f"{name}（{price} 枚/份，{count} 份）" if price else f"{name}×{count}")
        return "、".join(parts)

    # 环境查询：时间/天气/地点
    env = data.get("environment")
    if isinstance(env, dict) and env:
        region = (data.get("region") or {}).get("name", "")
        return f"{env.get('time', '')}，{env.get('weather', '')}；{region}".strip("；")

    # 附近对象：nearby 里只有 id，必须换成可读名字，否则 NPC 会念一堆 npc_xxx 标识符
    nearby = data.get("nearby")
    if isinstance(nearby, list) and nearby:
        names = []
        for n in nearby:
            if not isinstance(n, dict):
                continue
            ent_id = str(n.get("id", ""))
            if not ent_id:
                continue
            names.append(_display_name(ent_id))
        names = [n for n in names if n]
        if names:
            return "、".join(names)

    return ""


def _display_name(entity_id: str) -> str:
    """把实体 id 换成人能读的名字（NPC 念 id 是明显的非角色化输出）。

    Args:
        entity_id: 形如 `npc_guard_calvin` / `p_001` 的实体标识。

    Returns:
        角色卡里的中文名；查不到时退回玩家名或原始 id。
    """
    if entity_id.startswith("npc_"):
        card = content_service.get(entity_id)
        return card.name if card is not None else entity_id
    if entity_id.startswith("p_"):
        player = world_state.player(entity_id)
        return player.name or entity_id
    return entity_id


@dataclass
class PendingAction:
    tool: str
    args: dict[str, Any]
    requested_at: float


class Orchestrator:
    def __init__(self, engine: Optional[str] = None) -> None:
        self.engine = (engine or settings.resolved_engine()).lower()
        self.brain = get_brain(self.engine)
        self.degraded = isinstance(self.brain, DemoBrain)
        self.nlu = NLU(self.brain)
        self.pending: dict[tuple[str, str, str], PendingAction] = {}
        self.disabled_tools: set[str] = set()      # 运营层：禁用工具
        self.disabled_npcs: set[str] = set()       # 运营层：禁用角色

    # ================================================================== #
    # 主入口：对话
    # ================================================================== #
    def chat(self, req: ChatRequest) -> ChatResponse:
        started = time.perf_counter()
        npc_id, pid, iid = req.npc_id, req.player_id, req.instance_id
        message = (req.message or "").strip()
        key = (npc_id, pid, iid)

        trace = AgentTrace(npc_id=npc_id, player_id=pid, instance_id=iid,
                           user_message=message, engine=self.engine,
                           prompt_version=settings.engine)
        degradations: list[str] = []
        if self.degraded:
            degradations.append("未接入大模型：使用离线演示脑（预设话术 + 规则行为），地点与数值仍来自权威世界状态")

        # 0) 身份校验（步骤 1）
        card = content_service.get(npc_id)
        if card is None:
            return self._error_response(trace, f"未知 NPC：{npc_id}", degradations)
        if npc_id in self.disabled_npcs or not content_service.is_enabled(npc_id):
            return self._refusal_response(
                trace, card,
                f"「{card.name}」暂时不在线（已被运营停用）。",
                degradations, reason_code="npc_disabled",
            )

        policy = CharacterPolicy(card)

        # 情绪自然衰减（文档 §3.6）
        npc_state = world_state.npc_state(npc_id, iid)
        if decay_emotion(npc_state):
            if npc_state.emotion.intensity <= 0:
                npc_state.emotion = baseline_emotion(card, repository.relationship(pid, npc_id).stage)

        # 1) 二次确认闭环（文档 §3.9 工具层「二次确认」）
        pending = self.pending.get(key)
        if pending is not None:
            if self._is_cancel(message):
                self.pending.pop(key, None)
                return self._simple_response(
                    trace, card, intent="social",
                    reply=f"那就算了。{card.voice.catchphrases[0] if card.voice.catchphrases else ''}".strip(),
                    degradations=degradations,
                )
            if self._is_confirm(message):
                self.pending.pop(key, None)
                args = dict(pending.args)
                args["confirm"] = True
                result = self._call_tool(pending.tool, npc_id, pid, iid, card, args, trace)
                return self._respond(
                    trace=trace, card=card, pid=pid, iid=iid, message=message,
                    understanding=Understanding(intent="social", reason="二次确认后执行",
                                                engine="rules"),
                    plan=PlanCandidate(tactical="执行已确认的动作",
                                       executive=["调用受控工具", "汇报权威结果"]),
                    tool_result=result, perception=None, degradations=degradations,
                    policy=policy,
                )
            # 玩家说了别的 → 放弃待确认动作，作为新一轮处理
            self.pending.pop(key, None)

        # 2) 输入审核（模型层，文档 §3.9）
        screen = policy.screen_input(message)
        if not screen.ok:
            repository.add_audit({"kind": "screen_input", "npc_id": npc_id,
                                  "player_id": pid, "reason": screen.reason,
                                  "tags": screen.tags, "ts": time.time()})
            trace.errors.append(f"输入被拦截：{screen.reason}")
            self._record_turns(npc_id, pid, iid, message, screen.refusal)
            return self._simple_response(
                trace, card, intent="out_of_scope", reply=screen.refusal,
                degradations=degradations, reason_code="input_blocked",
                extra_trace_tags=screen.tags,
            )

        # 3) 感知（步骤 2）
        perception = self.perceive_bundle(npc_id, pid, iid, card, query=message)

        # 4) 意图识别（步骤 3）
        understanding = self.nlu.parse(message, card, perception)
        trace.intent = understanding.intent
        trace.engine = understanding.engine
        if self.nlu.last_error:
            trace.errors.append(f"NLU 回退规则解析：{self.nlu.last_error}")
            degradations.append("意图识别已降级为规则解析")

        # 5) 规划（步骤 4）
        allowed = self._allowed_tools(card)
        planner = Planner(allowed)
        plan = planner.plan(understanding, perception, card, message)

        # 6) 工具执行（步骤 5）
        tool_result: Optional[ToolResult] = None
        if plan.tool:
            result = self._call_tool(plan.tool, npc_id, pid, iid, card, plan.tool_args, trace)
            tool_result = result
            if result.reason_code == "needs_confirmation":
                self.pending[key] = PendingAction(plan.tool, dict(plan.tool_args), time.time())

        # 7) 生成回复（步骤 6）
        return self._respond(
            trace=trace, card=card, pid=pid, iid=iid, message=message,
            understanding=understanding, plan=plan, tool_result=tool_result,
            perception=perception, degradations=degradations, policy=policy,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    # ================================================================== #
    # 感知
    # ================================================================== #
    def perceive_bundle(
        self, npc_id: str, pid: str, iid: str, card, query: str = ""
    ) -> Perception:
        state = world_state.npc_state(npc_id, iid)
        player = world_state.player(pid)
        rel = repository.relationship(pid, npc_id)
        env = world_state.environment(iid)
        env["recent_summary"] = memory_service.summarize(npc_id, pid, iid)
        quests = [world_state.quest_state(q.id, pid)
                  for q in world_state.quest_defs_by_giver(npc_id)]
        recalled = memory_service.recall(query, owner=npc_id, player_id=pid)
        return Perception(
            npc_state=state,
            player_state=player,
            relationship=rel,
            environment=env,
            nearby=world_state.nearby(npc_id, iid),
            quests=quests,
            recalled=recalled,
            unknown_notes=content_service.unknown_topics(card),
        )

    # ================================================================== #
    # 工具
    # ================================================================== #
    def _allowed_tools(self, card) -> list[str]:
        out = []
        for item in tool_registry.available_for(card):
            if item["allowed"] and item["name"] not in self.disabled_tools:
                out.append(item["name"])
        return out

    def _call_tool(self, name, npc_id, pid, iid, card, args, trace: AgentTrace) -> ToolResult:
        if name in self.disabled_tools:
            result = ToolResult(tool=name, success=False, blocked=True,
                                error=f"工具 {name} 已被运营禁用", reason_code="tool_disabled")
            trace.tool_calls.append({"tool": name, "success": False,
                                     "reason_code": "tool_disabled"})
            return result
        ctx = ToolContext(npc_id=npc_id, player_id=pid, instance_id=iid,
                          card=card, world=world_state, args=args)
        result = tool_registry.call(name, ctx)
        trace.tool_calls.append({
            "tool": name,
            "args": {k: v for k, v in args.items() if k != "confirm"},
            "success": result.success,
            "reason_code": result.reason_code,
            "error": result.error,
            "state_delta": result.state_delta,
            "latency_ms": result.latency_ms,
        })
        if not result.success and result.reason_code not in ("needs_confirmation",):
            trace.errors.append(f"工具 {name} 失败：{result.error}")
        return result

    def execute_tool(self, name: str, req) -> ToolResult:
        """给 /api/tools/{name}/execute 用：由 Agent 申请受控游戏工具。"""
        card = content_service.get(req.npc_id) if req.npc_id else None
        if card is None:
            return ToolResult(tool=name, success=False, blocked=True,
                              error="缺少或未知 npc_id", reason_code="bad_identity")
        return self._call_tool(name, card.id, req.player_id, req.instance_id,
                               card, dict(req.arguments or {}), AgentTrace())

    # ================================================================== #
    # 回复生成
    # ================================================================== #
    def _respond(
        self,
        *,
        trace: AgentTrace,
        card,
        pid: str,
        iid: str,
        message: str,
        understanding: Understanding,
        plan: PlanCandidate,
        tool_result: Optional[ToolResult],
        perception: Optional[Perception],
        degradations: list[str],
        policy: CharacterPolicy,
        elapsed_ms: float = 0.0,
    ) -> ChatResponse:
        npc_id = card.id
        rel = repository.relationship(pid, npc_id)
        state = world_state.npc_state(npc_id, iid)

        # ---- 关系事件（规则服务确认，文档 §3.6）----
        relation_changes: list[RelationChange] = []
        tool_handled_relation = bool(
            tool_result and tool_result.tool == "propose_relation_event" and tool_result.success
        )
        if not tool_handled_relation:
            event = (understanding.slots.get("event")
                     or detect_event(message, understanding.intent))
            if event:
                outcome = apply_event(rel, state, card, event, note=card.name)
                relation_changes = outcome.relation_changes
                if outcome.emotion is not None:
                    state.emotion = outcome.emotion
        elif tool_result is not None:
            for item in tool_result.data.get("relation_changes", []):
                relation_changes.append(RelationChange(**item))
            emo = tool_result.data.get("emotion")
            if emo:
                from ..schemas import EmotionState
                state.emotion = EmotionState(**emo)

        # ---- 回复文本 ----
        reply = self._render_reply(card, understanding, plan, tool_result, rel, state,
                                   policy)
        reply, notes = policy.screen_output(reply, settings.max_utterance_chars)
        if notes:
            trace.errors.extend(f"输出审核：{n}" for n in notes)

        # ---- 记忆写入（白名单策略）----
        memory_writes = self._write_memory(npc_id, pid, iid, message, understanding,
                                          tool_result, relation_changes, reply)

        # ---- 表现标签 ----
        presentation = PresentationBuilder(card).build(
            intent=understanding.intent, emotion=state.emotion,
            tool_result=tool_result, reply=reply,
            tool_name=plan.tool or (tool_result.tool if tool_result else ""),
        )

        # ---- 短期记忆 ----
        self._record_turns(npc_id, pid, iid, message, reply)

        # ---- 轨迹 ----
        trace.degraded = bool(degradations)
        trace.degradations = degradations
        trace.latency_ms = round(elapsed_ms or (time.perf_counter() * 0), 2)
        repository.add_trace(trace)

        state_changed = bool(tool_result and tool_result.success and tool_result.state_delta)
        return ChatResponse(
            trace_id=trace.trace_id,
            npc_id=npc_id,
            player_id=pid,
            intent=understanding.intent,
            reply=reply,
            tool_result=tool_result,
            presentation=presentation,
            state_changed=state_changed,
            state_delta=tool_result.state_delta if tool_result else {},
            relation_changes=relation_changes,
            memory_writes=memory_writes,
            sources=self._collect_sources(tool_result, perception, card),
            plan=plan,
            degraded=trace.degraded,
            degradations=degradations,
            confidence=understanding.confidence,
        )

    # ------------------------------------------------------------------ #
    def _render_reply(
        self, card, understanding, plan, tool_result, rel, state, policy: CharacterPolicy
    ) -> str:
        from .responder import ReplyContext, template_reply

        rc = ReplyContext(
            npc_name=card.name,
            tone=card.voice.tone,
            catchphrase=(card.voice.catchphrases[0] if card.voice.catchphrases else ""),
            intent=understanding.intent,
            tool=tool_result.tool if tool_result else (plan.tool or ""),
            tool_success=tool_result.success if tool_result else True,
            tool_message=(tool_result.message if tool_result and tool_result.success else ""),
            tool_error=(tool_result.error or "") if tool_result and not tool_result.success else "",
            stage=rel.stage,
            emotion=state.emotion.label if state.emotion else "平静",
            clarify=understanding.clarify_question if understanding.need_clarify else "",
            refusal=("这种要求我不能答应。" if understanding.intent == "out_of_scope" else ""),
            quest_title=str((tool_result.data.get("title") if tool_result else "") or ""),
            quest_stage=str((tool_result.data.get("stage_name") if tool_result else "") or ""),
            speech=_tool_speech(tool_result),
        )
        if not rc.quest_title and tool_result and tool_result.tool in ("offer_quest", "advance_quest"):
            rc.quest_title = "委托"

        # 二次确认：把待确认动作讲清楚
        if tool_result is not None and tool_result.reason_code == "needs_confirmation":
            rc.needs_confirmation = True
            rc.tool_message = ""
            action_name = {"trade_offer": "这笔交易", "open_gate": "开闭城门",
                           "give_item": "这份赠予"}.get(tool_result.tool, "这个动作")
            rc.tool_error = f"{action_name}需要你点头确认——同意就说一声。"
            prompt = tool_result.data.get("confirmation_prompt") or ""
            if prompt:
                rc.facts = [prompt]

        system = self._reply_system_prompt(card, policy, rc, rel, state, tool_result)
        user = "请以角色身份说出你这一句台词。"
        try:
            out = self.brain.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=220, temperature=settings.llm_temperature,
            )
            text = (out.text or "").strip()
            # 推理型模型会把思维链写进 content；先剥掉，剥完为空说明整段都是思考，
            # 此时宁可退回预设话术，也不能把思维链当台词播给玩家。
            text, stripped = strip_reasoning(text)
            if stripped:
                # 降级必须留痕（§8.2-8）：剥离说明模型没按回复契约输出，属于质量问题
                degradations.append("模型回复含思维链，已剥离后使用")
            if text:
                return text
            degradations.append("模型回复被思维链占满，已降级为预设话术")
        except LLMError as exc:
            # 模型不可用 → 立即降级为预设话术（不抛给上层）
            return template_reply(rc) + f"（提示：模型不可用 {exc.kind}，已降级为预设话术）"
        except Exception:  # noqa: BLE001
            pass
        return template_reply(rc)

    @staticmethod
    def _reply_system_prompt(card, policy, rc, rel, state, tool_result) -> str:
        env = world_state.environment(state.instance_id)
        lines = [
            f"你是游戏《灰港》里的 NPC「{card.name}」。",
            policy.describe_boundary(),
            f"语气：{card.voice.tone}；说话习惯：{card.voice.speaking_style}",
            "",
            f"[当前处境] {env['time']}，{env['weather']}；你在{state.location}。",
            f"[与玩家的关系] 阶段 {rel.stage}，信任 {rel.trust:.0f}，好感 {rel.affinity:.0f}。",
            f"[当前情绪] {state.emotion.label}（强度 {state.emotion.intensity:.2f}）。",
        ]
        if card.goals.current:
            lines.append(f"[你眼下最挂心的事] {card.goals.current[0]}")
        if tool_result is not None:
            if tool_result.success:
                lines.append(f"[游戏服务返回的权威结果] 成功。{tool_result.message}")
                # tool_result.message 是给编排层看的内部汇报（如"说出了 3 条自己知道的事"），
                # 不是台词。真正要转述给玩家的内容在 data 里，必须单独喂给模型，
                # 否则模型只能照念那句汇报——这正是玩家看到"说出了 3 条…"的根因。
                speech = _tool_speech(tool_result)
                if speech:
                    lines.append(f"[你应当转述给玩家的内容] {speech}")
                if tool_result.state_delta:
                    lines.append(f"[实际生效的状态变更] {tool_result.state_delta}")
            else:
                lines.append(f"[游戏服务返回的权威结果] 失败：{tool_result.error}。"
                             "你必须如实说明失败原因，绝不能谎称已经完成。")
        lines += [
            "",
            f"要求：只输出 {card.name} 说的一句台词（不超过 {settings.max_utterance_chars} 字），"
            "不要旁白说明、不要 JSON、不要括号动作以外的解释。",
            TASK_REPLY,
            rc.to_json(),
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def _write_memory(self, npc_id, pid, iid, message, understanding, tool_result,
                      relation_changes, reply) -> list[MemoryWriteSuggestion]:
        suggestions: list[MemoryWriteSuggestion] = []

        def push(kind: str, text: str, conf: float, reason: str, subject: str = "") -> None:
            ev = memory_service.write_event(
                kind=kind, text=text, owner=npc_id, player_id=pid,
                source="agent_turn", confidence=conf, subject=subject,
                tags=[understanding.intent],
            )
            if ev is not None:
                suggestions.append(MemoryWriteSuggestion(
                    text=text, subject=ev.subject, confidence=conf, reason=reason,
                ))

        event = understanding.slots.get("event")
        if event == "helped":
            push("player_helped", f"玩家对{npc_id}出手相助：{message[:40]}", 0.85, "玩家表达了帮助")
        elif event == "cheated":
            push("player_cheated", f"玩家对{npc_id}撒了谎：{message[:40]}", 0.7, "玩家表达中包含欺骗")
        elif event in ("threatened",):
            push("player_threatened", f"玩家威胁了{npc_id}：{message[:40]}", 0.8, "玩家表达中包含威胁")
        elif event in ("promise_kept", "promise_broken"):
            push("player_promised", f"玩家就某事做出承诺：{message[:40]}", 0.6, "玩家表达中包含承诺")

        if relation_changes:
            detail = "、".join(f"{c.field}{c.delta:+.1f}（{c.reason}）" for c in relation_changes)
            push("relationship_change", f"与玩家的关系发生变化：{detail}", 1.0,
                 "规则服务确认的关系变更", subject="关系变化")

        if tool_result is not None and tool_result.success and tool_result.state_delta:
            if "quests" in tool_result.state_delta:
                for q in tool_result.state_delta["quests"]:
                    push("quest_fact",
                         f"委托 {q.get('quest_id')} 变更为 {q.get('status')}/阶段{q.get('stage')}",
                         1.0, "任务服务确认", subject="任务事实")
            if "items" in tool_result.state_delta or "gold" in tool_result.state_delta:
                push("trade", f"与玩家完成一次交易：{tool_result.message}", 0.95, "交易服务确认",
                     subject="交易记录")
            if "gates" in tool_result.state_delta:
                push("observation", f"城门状态被改变：{tool_result.state_delta['gates']}", 1.0,
                     "世界服务确认", subject="世界变更")
        return suggestions

    # ------------------------------------------------------------------ #
    def _collect_sources(self, tool_result, perception, card) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = [
            {"kind": "content", "detail": f"角色卡 {card.id} v{card.version}（approved）",
             "confidence": 1.0},
        ]
        if perception is not None:
            env = perception.environment
            sources.append({"kind": "world_service",
                            "detail": f"{env.get('name', '')} {env.get('time', '')} {env.get('weather', '')}",
                            "confidence": 1.0})
            for ev in perception.recalled[:3]:
                sources.append({"kind": "memory", "detail": ev.text[:40],
                                "confidence": ev.confidence, "source": ev.source})
            if perception.unknown_notes:
                sources.append({"kind": "boundary",
                                "detail": "不知情范围：" + "、".join(perception.unknown_notes[:4]),
                                "confidence": 1.0})
        if tool_result is not None:
            sources.append({"kind": "tool", "detail": f"{tool_result.tool} → "
                            f"{'success' if tool_result.success else 'failed'}"
                            + (f"（{tool_result.reason_code}）" if tool_result.reason_code else ""),
                            "confidence": 1.0, "source": tool_result.source})
            for entry in tool_result.data.get("entries", [])[:3]:
                sources.append({"kind": "knowledge", "detail": entry.get("text", "")[:40],
                                "confidence": entry.get("confidence", 0.5),
                                "source": entry.get("source", "knowledge")})
        return sources

    # ================================================================== #
    # 其他接口
    # ================================================================== #
    def perceive(self, req: PerceiveRequest) -> dict[str, Any]:
        """提交事件并更新 NPC 感知（文档 §5.2 /perceive）。"""
        event = dict(req.event or {})
        etype = event.get("type", "")
        applied: dict[str, Any] = {}
        if etype == "move":
            npc_id = event.get("npc_id") or event.get("entity_id")
            if npc_id:
                applied = world_state.move_npc(npc_id, event.get("location", ""))
        elif etype in ("time", "weather"):
            applied = world_state.set_environment(req.instance_id, **{etype: event.get("value", "")})
        elif etype == "gate":
            res = world_state.apply_state_delta(
                {"gates": {event.get("gate_id"): bool(event.get("open"))}},
                actor="world_event", source="perceive", instance_id=req.instance_id,
            )
            applied = res.applied
            if res.errors:
                return {"ok": False, "errors": res.errors}
        elif etype == "world_event":
            repository.add_event({"type": "world_event", "detail": event.get("detail", ""),
                                  "ts": time.time(), "actor": "world_event",
                                  "source": "perceive"})
            applied = {"event_logged": True}
        else:
            return {"ok": False, "errors": [f"未知事件类型：{etype}"]}

        npc_id = event.get("npc_id") or event.get("entity_id") or req.npc_id
        card = content_service.get(npc_id)
        bundle = (self.perceive_bundle(npc_id, req.player_id, req.instance_id, card, "")
                  if card else None)
        return {
            "ok": True,
            "applied": applied,
            "perception": bundle.model_dump(mode="json") if bundle else None,
        }

    def plan(self, req: PlanRequest, npc_id: str) -> PlanCandidate:
        card = content_service.get(npc_id or req.npc_id) or content_service.list_cards()[0]
        state = world_state.npc_state(card.id, req.instance_id)
        perception = self.perceive_bundle(card.id, req.player_id, req.instance_id, card, "")
        planner = Planner(self._allowed_tools(card))
        return planner.refresh(card, state, perception, goal=req.goal)

    # ================================================================== #
    # 辅助
    # ================================================================== #
    def _record_turns(self, npc_id, pid, iid, player_text, npc_text) -> None:
        if player_text:
            memory_service.record_turn(npc_id, pid, iid, Turn(role="player", text=player_text))
        if npc_text:
            memory_service.record_turn(npc_id, pid, iid, Turn(role="npc", text=npc_text))

    @staticmethod
    def _is_confirm(text: str) -> bool:
        t = (text or "").strip()
        return len(t) <= 8 and any(w in t for w in CONFIRM_WORDS)

    @staticmethod
    def _is_cancel(text: str) -> bool:
        return any(w in (text or "") for w in CANCEL_WORDS)

    # ------------------------------------------------------------------ #
    def _error_response(self, trace, msg, degradations) -> ChatResponse:
        trace.errors.append(msg)
        trace.degraded = True
        trace.degradations = degradations
        repository.add_trace(trace)
        return ChatResponse(trace_id=trace.trace_id, npc_id=trace.npc_id,
                            player_id=trace.player_id, reply=f"（{msg}）",
                            degraded=True, degradations=degradations + [msg],
                            confidence=0.0)

    def _refusal_response(self, trace, card, reply, degradations, reason_code="") -> ChatResponse:
        trace.errors.append(reason_code or "refused")
        trace.degraded = bool(degradations)
        trace.degradations = degradations
        repository.add_trace(trace)
        return ChatResponse(trace_id=trace.trace_id, npc_id=card.id,
                            player_id=trace.player_id, reply=reply,
                            presentation=PresentationBuilder(card).build(
                                intent="out_of_scope", emotion=None,
                                tool_result=None, reply=reply),
                            degraded=bool(degradations), degradations=degradations,
                            confidence=1.0)

    def _simple_response(self, trace, card, intent, reply, degradations,
                         reason_code="", extra_trace_tags=None) -> ChatResponse:
        trace.intent = intent
        trace.degraded = bool(degradations)
        trace.degradations = degradations
        if extra_trace_tags:
            trace.errors.extend(f"tag:{t}" for t in extra_trace_tags)
        repository.add_trace(trace)
        return ChatResponse(trace_id=trace.trace_id, npc_id=card.id,
                            player_id=trace.player_id, intent=intent, reply=reply,
                            presentation=PresentationBuilder(card).build(
                                intent=intent, emotion=None, tool_result=None, reply=reply),
                            degraded=bool(degradations), degradations=degradations,
                            confidence=0.9)

    def runtime_info(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "brain": getattr(self.brain, "engine", "unknown"),
            "degraded": self.degraded,
            "llm_configured": settings.llm_configured,
            "nlu_last_engine": self.nlu.last_engine,
            "disabled_tools": sorted(self.disabled_tools),
            "disabled_npcs": sorted(self.disabled_npcs),
            "pending_confirmations": len(self.pending),
        }


orchestrator = Orchestrator()
