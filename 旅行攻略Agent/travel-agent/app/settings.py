"""配置加载：轻量 .env 解析（不引入额外依赖）+ 全局常量。

优先级：系统环境变量 > .env 文件 > 代码默认值。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
ENV_PATH = BASE_DIR / ".env"

APP_NAME = "旅行攻略 Agent 助手"
APP_VERSION = "2.0.0"


# --------------------------------------------------------------------------- #
# .env 解析（支持 # 注释、引号、export 前缀、UTF-8 BOM）
# --------------------------------------------------------------------------- #
def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # 未加引号时的行内注释必须剥掉：模板里写了
            #     TRAVEL_LLM_TIMEOUT=90           # 大模型调用超时
            # 不剥的话 env_int("TRAVEL_LLM_TIMEOUT") 会拿到 "90           # 大模型调用超时"
            # 而 int() 解析失败 → 静默退回默认值；TRAVEL_LLM_TOOL_MODE 更糟，
            # 会变成 "auto        # auto | native | prompt"，等于 "auto"，恰好没暴露。
            # 「#」前必须有空白才算注释，否则带 # 的密钥/URL 片段会被误伤。
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key] = value
    return out


_ENV_FILE = _parse_env(ENV_PATH)


def env(key: str, default: str = "") -> str:
    if key in os.environ and os.environ[key] != "":
        return os.environ[key]
    return _ENV_FILE.get(key, default)


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except ValueError:
        return default


def mask(secret: str) -> str:
    """用于日志/状态接口的脱敏展示。"""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 6}{secret[-4:]}"


# --------------------------------------------------------------------------- #
# 运行时配置
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    host: str = field(default_factory=lambda: env("TRAVEL_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: env_int("TRAVEL_PORT", 8000))

    llm_base_url: str = field(default_factory=lambda: env("TRAVEL_LLM_BASE_URL", "").rstrip("/"))
    llm_api_key: str = field(default_factory=lambda: env("TRAVEL_LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: env("TRAVEL_LLM_MODEL", ""))
    llm_temperature: float = field(default_factory=lambda: env_float("TRAVEL_LLM_TEMPERATURE", 0.3))
    llm_timeout: int = field(default_factory=lambda: env_int("TRAVEL_LLM_TIMEOUT", 90))
    llm_tool_mode: str = field(default_factory=lambda: env("TRAVEL_LLM_TOOL_MODE", "auto"))
    llm_max_steps: int = field(default_factory=lambda: env_int("TRAVEL_LLM_MAX_STEPS", 8))
    # 推理模型的思考预算系数：MiniMax M2 / DeepSeek-R1 这类模型会先输出一大段
    # <think>…</think> 再给正文，思考部分同样计入 max_tokens。若按正文长度给预算，
    # 模型会在「想」到一半就被 finish_reason=length 截断，正文一个 token 都没输出
    # → 表现为「候选地点获取失败 / 城市画像失败」，行程全空。
    # 这里把各处的 max_tokens 统一乘上该系数，给思考留出余量。
    # 非推理模型把它设为 1.0 即可恢复原始预算。
    llm_output_ratio: float = field(default_factory=lambda: env_float("TRAVEL_LLM_OUTPUT_RATIO", 3.0))

    amap_key: str = field(default_factory=lambda: env("AMAP_KEY", ""))

    osrm_url: str = field(default_factory=lambda: env("TRAVEL_OSRM_URL", "https://router.project-osrm.org").rstrip("/"))
    poi_max_drift_km: float = field(default_factory=lambda: env_float("TRAVEL_POI_RESOLVE_LIMIT", 200.0))
    geocode_crosscheck_km: float = field(default_factory=lambda: env_float("TRAVEL_GEOCODE_CROSSCHECK_KM", 120.0))

    engine: str = field(default_factory=lambda: env("TRAVEL_ENGINE", "auto"))
    demo_brain: bool = field(default_factory=lambda: env("TRAVEL_DEMO_BRAIN", "").strip().lower()
                             in ("1", "true", "yes", "on"))

    @property
    def llm_ready(self) -> bool:
        """有 base_url + model 且（本地模型可无 Key）即可调用。"""
        if not (self.llm_base_url and self.llm_model):
            return False
        if "localhost" in self.llm_base_url or "127.0.0.1" in self.llm_base_url:
            return True
        return bool(self.llm_api_key)

    @property
    def use_llm(self) -> bool:
        if self.engine == "rules":
            return False
        if self.engine == "demo" or self.demo_brain:
            return True
        if self.engine == "llm":
            return True
        return self.llm_ready

    def status(self) -> dict:
        return {
            "engine": "llm" if (self.use_llm and (self.llm_ready or self.engine == "demo" or self.demo_brain))
                      else "rules",
            "engine_setting": self.engine,
            "llm_configured": self.llm_ready,
            "demo_brain": self.demo_brain or self.engine == "demo",
            "llm_base_url": self.llm_base_url or "(未配置)",
            "llm_model": self.llm_model or "(未配置)",
            "llm_api_key": mask(self.llm_api_key) or "(未配置)",
            "llm_tool_mode": self.llm_tool_mode,
            "amap_configured": bool(self.amap_key),
            "geo_source": "amap" if self.amap_key else "photon+open-meteo",
        }


SETTINGS = Settings()


# --------------------------------------------------------------------------- #
# 规划常量（确定性部分，与文档 §3.1 / §3.4 / §3.3 一致）
# --------------------------------------------------------------------------- #
PACE_RULES: dict[str, dict] = {
    "relaxed":   {"label": "轻松", "max_pois": 3, "day_start": "09:00", "day_end": "19:30"},
    "normal":    {"label": "适中", "max_pois": 4, "day_start": "08:30", "day_end": "20:00"},
    "intensive": {"label": "紧凑", "max_pois": 6, "day_start": "07:30", "day_end": "21:00"},
}

SCORE_WEIGHTS = {"interest": 0.40, "rating": 0.20, "distance": 0.15,
                 "budget": 0.10, "weather": 0.10, "crowd": 0.05}

WEATHER_RULES = {"rain_prob": 60, "heat_temp": 34, "wind_kmh": 39, "visibility_km": 5}

ARRIVAL_SHIFT_MINUTES = 180      # 抵达日预留的交通与接驳时间
DEPARTURE_CUT_MINUTES = 150      # 返程日提前收尾时间
MEAL_MINUTES = {"lunch": 60, "dinner": 70}
LUNCH_FROM, DINNER_FROM = 11 * 60, 17 * 60 + 15
BUFFER_RATIO = 0.08

CATEGORY_GROUPS = {
    "sight": {"景点", "自然", "古镇", "文化", "室内", "观景", "户外", "亲子"},
    "meal": {"餐饮", "小吃", "咖啡", "茶馆", "购物"},
}
