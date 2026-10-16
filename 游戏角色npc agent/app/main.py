"""FastAPI 应用装配（文档 §4 总体技术架构：Agent 服务挂在游戏服务一侧）。

客户端（游戏内 UI / 调试面板）只与 HTTP 交互；
所有状态变更都经服务端鉴权、校验与事务处理（文档 §6 技术选型原则）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import ROOT, settings
from .content import content_service

WEB_DIR = ROOT / "web"


def create_app() -> FastAPI:
    app = FastAPI(
        title="游戏角色 NPC Agent",
        description=(
            "依据《游戏角色 NPC Agent 需求分析与技术路线》实现的 MVP：\n"
            "- 角色卡驱动的身份与边界（§3.1 / §3.9）\n"
            "- 意图识别与多轮对话（§3.2）\n"
            "- 感知 / 记忆 / 关系 / 情绪（§3.3 / §3.4 / §3.6）\n"
            "- 分层决策与受控工具调用（§3.5 / §3.7）\n"
            "- 权威世界状态、审计与评测（§5 / §8）\n"
        ),
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(str(WEB_DIR / "index.html"))

    @app.on_event("startup")
    def _startup() -> None:
        cards = content_service.list_cards()
        print(f"[npc-agent] 引擎={settings.resolved_engine()} "
              f"(NPC_ENGINE={settings.engine}, llm_configured={settings.llm_configured})")
        print(f"[npc-agent] 已加载角色卡 {len(cards)} 张："
              f"{', '.join(f'{c.name}({c.id})' for c in cards)}")

    return app


app = create_app()
