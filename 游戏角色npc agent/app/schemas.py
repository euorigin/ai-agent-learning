"""领域数据模型。

严格对齐文档：
- §3.1 角色卡      → CharacterCard
- §3.3 感知范围    → Perception
- §3.4 记忆        → MemoryEvent / Turn
- §3.5 分层决策    → PlanCandidate
- §3.6 情绪与关系  → EmotionState / Relationship / RelationChange
- §3.7 工具调用    → ToolSpec / ToolResult
- §5.1 核心数据表  → QuestState / AgentTrace / PlayerState / NpcRuntimeState
- §5.2/5.3 接口    → ChatRequest / ChatResponse

通用约定：
- 每个"外部事实"都带来源（source）与可信度（confidence），文档 §3.4 明确要求；
- 记忆条目带 deletable（可删除标记），满足 §7 隐私要求。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #

MemoryKind = Literal["short_term", "event", "relationship", "quest", "world_knowledge"]
IntentType = Literal[
    "info_query",      # 信息查询
    "action_request",  # 行动请求
    "quest_op",        # 任务操作
    "trade_request",   # 交易请求
    "social",          # 拒绝/承诺/威胁/闲聊等社交表达
    "ambiguous",       # 模糊请求 → 先澄清
    "out_of_scope",    # 越界/违规请求
]
RelationStage = Literal["敌对", "陌生", "熟悉", "盟友"]


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Sourced(BaseModel):
    """带来源与可信度的事实（文档 §3.4：来源、时间、可信度）。"""

    source: str = Field(default="system", description="事实来源，如 card/knowledge/rumor/rule/memory")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    timestamp: float = Field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# 内容层：角色卡（文档 §3.1）
# --------------------------------------------------------------------------- #


class CharacterGoal(BaseModel):
    long_term: list[str] = Field(default_factory=list, description="长期目标")
    current: list[str] = Field(default_factory=list, description="当前目标")


class VoiceStyle(BaseModel):
    tone: str = "平静"
    speaking_style: str = ""
    catchphrases: list[str] = Field(default_factory=list)


class CharacterCard(BaseModel):
    """角色卡：策划维护的结构化配置，是角色行为的唯一事实源。"""

    id: str
    version: str = "1.0.0"
    name: str
    role_type: str = Field(description="任务发布者/伙伴/敌对/经营/社会/剧情")
    identity: str
    personality: list[str] = Field(default_factory=list)
    values: list[str] = Field(default_factory=list)
    goals: CharacterGoal = Field(default_factory=CharacterGoal)
    knowledge_scope: list[str] = Field(default_factory=list, description="可知知识主题")
    taboos: list[str] = Field(default_factory=list, description="行为边界/禁忌")
    behavior_boundaries: list[str] = Field(default_factory=list)
    forbidden_topics: list[str] = Field(default_factory=list, description="拒答主题")
    taboo_keywords: list[str] = Field(
        default_factory=list,
        description="禁忌触发词（策划维护，命中即拒答）；自动分词不可靠，故显式配置",
    )
    tool_permissions: list[str] = Field(default_factory=list)
    relationship_focus: list[str] = Field(default_factory=lambda: ["trust", "affinity"])
    voice: VoiceStyle = Field(default_factory=VoiceStyle)
    greeting: str = ""
    approved: bool = Field(default=True, description="内容审核标记（文档 §3.9 内容层）")
    changelog: list[dict[str, Any]] = Field(default_factory=list, description="版本与审批记录")


# --------------------------------------------------------------------------- #
# 运行时状态（文档 §5.1 npc_runtime_states / player_relationships）
# --------------------------------------------------------------------------- #


class EmotionState(BaseModel):
    label: str = "平静"
    intensity: float = Field(default=0.0, ge=0.0, le=1.0)
    updated_at: float = Field(default_factory=time.time)


class NpcRuntimeState(BaseModel):
    npc_id: str
    instance_id: str = "world_01"
    location: str = ""
    hp: int = 100
    max_hp: int = 100
    action_state: str = "idle"
    current_goal: str = ""
    resources: dict[str, Any] = Field(default_factory=dict)
    emotion: EmotionState = Field(default_factory=EmotionState)
    # 上次决策以来的行为计划（供 /plan 接口与前端展示）
    plan: list[str] = Field(default_factory=list)


class PlayerState(BaseModel):
    player_id: str
    name: str = "玩家"
    level: int = 1
    location: str = ""
    gold: int = 200
    hp: int = 100
    inventory: dict[str, int] = Field(default_factory=dict)
    flags: dict[str, Any] = Field(default_factory=dict)


class RelationChange(BaseModel):
    """关系变更必须带原因（文档 §3.6：有变化原因）。"""

    field: str
    delta: float
    reason: str
    before: float
    after: float


class Relationship(BaseModel):
    player_id: str
    npc_id: str
    trust: float = Field(default=30.0, description="信任度 0-100")
    affinity: float = Field(default=30.0, description="好感度 0-100")
    debt: float = Field(default=0.0, description="债务，可为负")
    fear: float = Field(default=0.0, description="恐惧 0-100")
    stage: RelationStage = "陌生"
    events: list[dict[str, Any]] = Field(default_factory=list, description="关系事件记录")
    updated_at: float = Field(default_factory=time.time)

    def stage_of(self, thresholds: list[tuple[float, str]]) -> str:
        for low, name in thresholds:
            if self.trust >= low:
                pass
        # thresholds 升序：取最后一个满足的
        result = thresholds[0][1]
        for low, name in thresholds:
            if self.trust >= low:
                result = name
        return result


# --------------------------------------------------------------------------- #
# 任务（文档 §5.1 quest_states）
# --------------------------------------------------------------------------- #


class QuestDef(BaseModel):
    id: str
    title: str
    giver: str
    summary: str
    stages: list[str]
    conditions: dict[str, Any] = Field(default_factory=dict)
    rewards: dict[str, Any] = Field(default_factory=dict)
    location: str = ""
    min_trust: float = 0.0
    min_level: int = 1


class QuestState(BaseModel):
    quest_id: str
    player_id: str
    stage: int = 0
    status: Literal["locked", "available", "active", "completed", "failed"] = "available"
    version: int = 1
    updated_at: float = Field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# 记忆（文档 §3.4 / §5.1 memory_events）
# --------------------------------------------------------------------------- #


class MemoryEvent(Sourced):
    id: str = Field(default_factory=lambda: _uid("mem"))
    subject: str = ""
    kind: str = "event"
    text: str = ""
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    deletable: bool = True
    owner: str = Field(default="", description="归属 NPC（空=全局世界知识）")
    player_id: str = ""


class Turn(BaseModel):
    role: Literal["player", "npc", "system"]
    text: str
    ts: float = Field(default_factory=time.time)
    meta: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 感知（文档 §3.3 感知范围：自身/局部/社会/世界 + 约束）
# --------------------------------------------------------------------------- #


class PerceivedFact(Sourced):
    key: str
    value: Any
    scope: str = Field(default="self", description="self/local/social/world")


class Perception(BaseModel):
    npc_state: Optional[NpcRuntimeState] = None
    player_state: Optional[PlayerState] = None
    relationship: Optional[Relationship] = None
    environment: dict[str, Any] = Field(default_factory=dict)
    nearby: list[dict[str, Any]] = Field(default_factory=list)
    quests: list[QuestState] = Field(default_factory=list)
    recalled: list[MemoryEvent] = Field(default_factory=list)
    unknown_notes: list[str] = Field(default_factory=list, description="明确标注的未知信息")


# --------------------------------------------------------------------------- #
# 意图理解（文档 §3.2）
# --------------------------------------------------------------------------- #


class Understanding(BaseModel):
    intent: IntentType = "social"
    slots: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.5
    need_clarify: bool = False
    clarify_question: str = ""
    reason: str = ""
    engine: str = Field(default="rules", description="llm / rules / demo")
    candidate_tool: Optional[str] = None


# --------------------------------------------------------------------------- #
# 计划（文档 §3.5 分层决策）
# --------------------------------------------------------------------------- #


class PlanCandidate(BaseModel):
    strategic: str = Field(default="", description="战略层：长期目标")
    tactical: str = Field(default="", description="战术层：当前行动选择")
    executive: list[str] = Field(default_factory=list, description="执行层：动作序列")
    reactive: Optional[str] = Field(default=None, description="反应层：即时反应")
    tool: Optional[str] = None
    tool_args: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 工具（文档 §3.7）
# --------------------------------------------------------------------------- #


class ToolSpec(BaseModel):
    name: str
    category: Literal["query", "move", "interact", "quest", "combat"]
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict, description="JSON Schema")
    read_only: bool = False
    permissions: list[str] = Field(default_factory=list, description="所需权限点")
    requires_confirmation: bool = False
    idempotent: bool = True
    precondition_note: str = ""

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
                or {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }


class ToolResult(BaseModel):
    """工具返回值契约：success / state_delta / message / error（文档 §3.7）。"""

    tool: str
    success: bool
    message: str = ""
    state_delta: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    reason_code: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = True
    latency_ms: float = 0.0
    blocked: bool = Field(default=False, description="是否被权限/限流/前置拦截")
    source: str = Field(default="game_service", description="结果来源，客户端不得当作模型草稿")


# --------------------------------------------------------------------------- #
# 表现（文档 §3.8）
# --------------------------------------------------------------------------- #


class PresentationTags(BaseModel):
    emotion: str = "平静"
    intensity: float = 0.0
    expression: str = "neutral"
    gaze_target: str = "player"
    action: str = "idle"
    speech_style: str = "平静"
    subtitle: str = ""
    interruptible: bool = True


# --------------------------------------------------------------------------- #
# 轨迹（文档 §5.1 agent_traces）
# --------------------------------------------------------------------------- #


class AgentTrace(BaseModel):
    trace_id: str = Field(default_factory=lambda: _uid("trace"))
    npc_id: str = ""
    player_id: str = ""
    instance_id: str = ""
    user_message: str = ""
    intent: str = ""
    engine: str = ""
    prompt_version: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    degraded: bool = False
    degradations: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: float = Field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# 接口模型（文档 §5.3）
# --------------------------------------------------------------------------- #


class ChatContext(BaseModel):
    location: str = ""
    quest_ids: list[str] = Field(default_factory=list)
    scene: str = ""


class ChatRequest(BaseModel):
    player_id: str = "p_001"
    # 与 PerceiveRequest / PlanRequest 保持一致：走 /npcs/{npc_id}/chat 时由路径提供，
    # 单独调用（如评测回放）时可在 body 里显式给出。路由侧做「路径优先」合并。
    npc_id: str = ""
    instance_id: str = "world_01"
    message: str
    context: ChatContext = Field(default_factory=ChatContext)


class MemoryWriteSuggestion(BaseModel):
    text: str
    subject: str = ""
    confidence: float = 0.6
    reason: str = ""


class ChatResponse(BaseModel):
    trace_id: str = ""
    npc_id: str = ""
    player_id: str = ""
    intent: str = ""
    reply: str = ""
    tool_result: Optional[ToolResult] = None
    presentation: PresentationTags = Field(default_factory=PresentationTags)
    state_changed: bool = False
    state_delta: dict[str, Any] = Field(default_factory=dict)
    relation_changes: list[RelationChange] = Field(default_factory=list)
    memory_writes: list[MemoryWriteSuggestion] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    plan: Optional[PlanCandidate] = None
    degraded: bool = False
    degradations: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class PerceiveRequest(BaseModel):
    npc_id: str = ""
    player_id: str = "p_001"
    instance_id: str = "world_01"
    event: dict[str, Any] = Field(default_factory=dict)


class PlanRequest(BaseModel):
    npc_id: str = ""
    player_id: str = "p_001"
    instance_id: str = "world_01"
    goal: str = ""


class ToolExecuteRequest(BaseModel):
    player_id: str = "p_001"
    npc_id: str = ""
    instance_id: str = "world_01"
    arguments: dict[str, Any] = Field(default_factory=dict)


class RelationshipPatch(BaseModel):
    trust: Optional[float] = None
    affinity: Optional[float] = None
    debt: Optional[float] = None
    fear: Optional[float] = None
    reason: str = "运营手动调整"


class ReplayRequest(BaseModel):
    scenario_ids: list[str] = Field(default_factory=list, description="空=跑全部场景")
    npc_id: str = ""
    engine: str = Field(
        default="demo", description="回放用的引擎；默认 demo（可复现），传 llm 可看真模型表现"
    )
