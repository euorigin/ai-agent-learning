"""受控工具层（文档 §3.7 游戏内工具调用与动作执行）。

铁律：
1. 模型不能直接修改金币、物品、生命值、任务奖励或排行榜等敏感状态；
2. 工具必须明确输入参数、权限、前置条件、失败原因和幂等策略；
3. 工具层永不抛异常——任何失败都返回 `ToolResult(success=False, error=...)`。

调用链（顺序即安全闸门顺序）：
    模型/编排 → ToolRegistry.call()
        → ① 白名单/权限
        → ② 频率限制
        → ③ handler 校验前置条件（做得到才继续）
        → ④ 敏感工具二次确认
        → ⑤ WorldStateService.apply_state_delta() 权威确认
        → ToolResult(success / state_delta / message / error)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import settings
from ..schemas import CharacterCard, ToolResult, ToolSpec
from ..store import repository
from ..world.state import WorldStateService


@dataclass
class ToolContext:
    """一次工具调用的上下文（身份 + 权限 + 依赖）。"""

    npc_id: str
    player_id: str
    instance_id: str
    card: CharacterCard
    world: WorldStateService
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Proposal:
    """handler 的产出：对状态的"申请"，由服务端确认后才落地。"""

    ok: bool = True
    message: str = ""
    delta: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    reason_code: str = ""


Handler = Callable[[ToolContext], Proposal]


def permission_covered(granted: list[str], required: list[str]) -> tuple[bool, str]:
    """权限点匹配，支持 `query.*` 通配与 `*` 全权。"""
    for req in required:
        ok = False
        for g in granted:
            if g == "*" or g == req:
                ok = True
                break
            if g.endswith(".*") and req.startswith(g[:-1]):
                ok = True
                break
        if not ok:
            return False, req
    return True, ""


class _Entry:
    __slots__ = ("spec", "handler")

    def __init__(self, spec: ToolSpec, handler: Handler) -> None:
        self.spec = spec
        self.handler = handler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, _Entry] = {}
        self.call_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def register(self, spec: ToolSpec, handler: Handler) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册: {spec.name}")
        self._tools[spec.name] = _Entry(spec, handler)

    def specs(self) -> list[ToolSpec]:
        return [e.spec for e in self._tools.values()]

    def get(self, name: str) -> Optional[ToolSpec]:
        entry = self._tools.get(name)
        return entry.spec if entry else None

    def openai_schemas(self, card: Optional[CharacterCard] = None) -> list[dict[str, Any]]:
        """按角色工具权限过滤后暴露给模型的 schema（文档 §3.1 行为边界）。"""
        out = []
        for entry in self._tools.values():
            if card is not None:
                allowed, _ = permission_covered(card.tool_permissions, entry.spec.permissions)
                if not allowed:
                    continue
            out.append(entry.spec.openai_schema())
        return out

    def available_for(self, card: CharacterCard) -> list[dict[str, Any]]:
        out = []
        for entry in self._tools.values():
            allowed, _ = permission_covered(card.tool_permissions, entry.spec.permissions)
            out.append({
                "name": entry.spec.name,
                "category": entry.spec.category,
                "read_only": entry.spec.read_only,
                "allowed": allowed,
                "requires_confirmation": entry.spec.requires_confirmation,
                "permissions": entry.spec.permissions,
                "precondition_note": entry.spec.precondition_note,
                "description": entry.spec.description,
            })
        return out

    # ------------------------------------------------------------------ #
    def call(self, name: str, ctx: ToolContext) -> ToolResult:
        started = time.perf_counter()
        entry = self._tools.get(name)
        if entry is None:
            return self._finish(
                name, ctx, ToolResult(
                    tool=name, success=False, error=f"未知工具：{name}",
                    reason_code="unknown_tool", blocked=True,
                ), started,
            )
        spec = entry.spec

        # 1) 权限白名单
        allowed, missing = permission_covered(ctx.card.tool_permissions, spec.permissions)
        if not allowed:
            return self._finish(
                name, ctx, ToolResult(
                    tool=name, success=False, blocked=True,
                    error=f"{ctx.card.name} 无权使用该能力（缺少权限点 {missing}）",
                    reason_code="permission_denied", read_only=spec.read_only,
                ), started,
            )

        # 2) 频率限制（仅写操作）
        if not spec.read_only:
            ok, used = repository.rate_check(
                ctx.npc_id, ctx.player_id, name,
                settings.tool_rate_limit, settings.tool_rate_window, time.time(),
            )
            if not ok:
                return self._finish(
                    name, ctx, ToolResult(
                        tool=name, success=False, blocked=True,
                        error=f"操作过于频繁（{settings.tool_rate_window:.0f}s 内已调用 {used} 次）",
                        reason_code="rate_limited",
                    ), started,
                )

        # 3) 执行业务 handler（前置条件在此校验：只有"做得到"的动作才值得让玩家确认）
        try:
            proposal = entry.handler(ctx)
        except Exception as exc:  # noqa: BLE001 - 工具层必须兜底
            proposal = Proposal(ok=False, error=f"工具内部异常：{exc.__class__.__name__}: {exc}",
                                reason_code="handler_error")

        if not proposal.ok:
            return self._finish(
                name, ctx, ToolResult(
                    tool=name, success=False,
                    error=proposal.error or "工具执行失败",
                    reason_code=proposal.reason_code or "failed",
                    message=proposal.message, data=proposal.data,
                    read_only=spec.read_only,
                ), started,
            )

        # 4) 二次确认（文档 §3.9 工具层）：前置条件已通过，才向玩家确认
        if spec.requires_confirmation and not ctx.args.get("confirm"):
            payload = dict(proposal.data)
            payload["confirmation_prompt"] = spec.precondition_note or proposal.message
            payload["would_apply"] = proposal.delta
            return self._finish(
                name, ctx, ToolResult(
                    tool=name, success=False, blocked=True,
                    error="该操作需要二次确认（请在参数中带 confirm=true）",
                    reason_code="needs_confirmation",
                    message=proposal.message, data=payload,
                ), started,
            )

        # 5) 权威确认：状态变更必须经 WorldStateService
        applied: dict[str, Any] = {}
        errors: list[str] = []
        events: list[dict[str, Any]] = list(proposal.data.pop("events", []) or [])
        if proposal.delta:
            result = ctx.world.apply_state_delta(
                proposal.delta, actor=ctx.npc_id, source=f"tool:{name}",
                instance_id=ctx.instance_id, player_id=ctx.player_id,
            )
            applied = result.applied
            errors = result.errors
            events.extend(result.events)
            if errors:
                return self._finish(
                    name, ctx, ToolResult(
                        tool=name, success=False,
                        error="；".join(errors),
                        reason_code="state_rejected",
                        message=proposal.message, data=proposal.data,
                        read_only=spec.read_only,
                    ), started,
                )

        return self._finish(
            name, ctx, ToolResult(
                tool=name, success=True, message=proposal.message,
                state_delta=applied, data=proposal.data,
                read_only=spec.read_only,
            ), started,
        )

    # ------------------------------------------------------------------ #
    def _finish(self, name: str, ctx: ToolContext, result: ToolResult,
                started: float) -> ToolResult:
        result.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        record = {
            "tool": name,
            "npc_id": ctx.npc_id,
            "player_id": ctx.player_id,
            "args": {k: v for k, v in ctx.args.items() if k != "confirm"},
            "success": result.success,
            "reason_code": result.reason_code,
            "error": result.error,
            "state_delta": result.state_delta,
            "latency_ms": result.latency_ms,
            "read_only": result.read_only,
            "ts": time.time(),
        }
        self.call_log.append(record)
        if len(self.call_log) > 500:
            del self.call_log[:-500]
        repository.add_audit({"kind": "tool_call", **record})
        return result

    def stats(self) -> dict[str, Any]:
        by_tool: dict[str, int] = {}
        failures = 0
        for rec in self.call_log:
            by_tool[rec["tool"]] = by_tool.get(rec["tool"], 0) + 1
            if not rec["success"]:
                failures += 1
        return {"total": len(self.call_log), "failures": failures, "by_tool": by_tool}
