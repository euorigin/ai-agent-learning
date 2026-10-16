"""Agent 层：意图理解 → 角色策略 → 分层规划 → 工具编排 → 表现标签。

注意：本包 `__init__` 刻意**不在导入期**拉取 orchestrator，
因为 `app.llm.demo_brain` 会在模块加载期反向依赖 `app.agent.responder`，
过早导入 orchestrator 会形成循环。需要编排器时请显式 `from app.agent.orchestrator import orchestrator`。
"""

from typing import Any


def __getattr__(name: str) -> Any:  # 惰性导出，兼顾便利与无循环
    if name in ("Orchestrator", "orchestrator"):
        from .orchestrator import Orchestrator, orchestrator

        return {"Orchestrator": Orchestrator, "orchestrator": orchestrator}[name]
    if name in ("NLU", "rule_parse"):
        from .nlu import NLU, rule_parse

        return {"NLU": NLU, "rule_parse": rule_parse}[name]
    if name == "CharacterPolicy":
        from .policy import CharacterPolicy

        return CharacterPolicy
    raise AttributeError(name)
