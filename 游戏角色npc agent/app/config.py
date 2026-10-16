"""全局配置。

对应文档《游戏角色 NPC Agent 需求分析与技术路线》§6 技术栈、§7 非功能需求。

设计要点：
- 所有可调项集中在此，运行期不散落魔数；
- 大模型凭据从 `.env` 读取，代码里不出现任何密钥；
- `NPC_ENGINE` 控制降级：auto / llm / demo / rules（文档 §8.2「关闭模型仍能降级」）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与引号，不覆盖已存在的真实环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    """运行期设置。"""

    # ---- 引擎与降级（文档 §7 实时性 / §8.2 验收）----
    engine: str = field(default_factory=lambda: _env("NPC_ENGINE", "auto").lower())

    # ---- 大模型（OpenAI 兼容协议）----
    llm_base_url: str = field(default_factory=lambda: _env("NPC_LLM_BASE_URL"))
    llm_model: str = field(default_factory=lambda: _env("NPC_LLM_MODEL"))
    llm_api_key: str = field(default_factory=lambda: _env("NPC_LLM_API_KEY"))
    llm_timeout: float = field(default_factory=lambda: _env_float("NPC_LLM_TIMEOUT", 45.0))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("NPC_LLM_MAX_TOKENS", 800))
    llm_temperature: float = field(default_factory=lambda: _env_float("NPC_LLM_TEMPERATURE", 0.7))

    # ---- 上下文预算（文档 §7 成本）----
    short_term_turns: int = field(default_factory=lambda: _env_int("NPC_SHORT_TERM_TURNS", 8))
    short_term_token_budget: int = field(default_factory=lambda: _env_int("NPC_SHORT_TERM_TOKEN_BUDGET", 1200))
    recall_top_k: int = field(default_factory=lambda: _env_int("NPC_RECALL_TOP_K", 5))

    # ---- 工具安全（文档 §3.7 / §3.9）----
    tool_rate_limit: int = field(default_factory=lambda: _env_int("NPC_TOOL_RATE_LIMIT", 12))
    tool_rate_window: float = field(default_factory=lambda: _env_float("NPC_TOOL_RATE_WINDOW", 60.0))

    # ---- 表现（文档 §3.8）----
    max_utterance_chars: int = field(default_factory=lambda: _env_int("NPC_MAX_UTTERANCE_CHARS", 120))

    # ---- 服务 ----
    host: str = field(default_factory=lambda: _env("NPC_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("NPC_PORT", 8080))

    # ---- 存储 ----
    world_file: Path = field(default_factory=lambda: ROOT / "data" / "world.json")

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model and self.llm_api_key)

    def resolved_engine(self) -> str:
        """把 auto 解析成实际引擎；`demo` 表示离线演示脑，`rules` 表示纯规则降级。"""
        if self.engine in ("llm", "demo", "rules"):
            return self.engine
        return "llm" if self.llm_configured else "demo"


settings = Settings()
