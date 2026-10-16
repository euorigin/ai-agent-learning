"""世界层导出：全局唯一的权威状态服务实例。"""

from __future__ import annotations

from ..content import content_service
from .rules import (
    EVENT_EFFECTS,
    STAGE_THRESHOLDS,
    RuleOutcome,
    apply_event,
    baseline_emotion,
    decay_emotion,
    detect_event,
    stage_of,
)
from .state import DeltaResult, WorldStateService

# 全进程唯一实例：权威世界状态
world_state = WorldStateService(content_service.world)

__all__ = [
    "WorldStateService",
    "DeltaResult",
    "world_state",
    "RuleOutcome",
    "apply_event",
    "baseline_emotion",
    "decay_emotion",
    "detect_event",
    "stage_of",
    "STAGE_THRESHOLDS",
    "EVENT_EFFECTS",
]
