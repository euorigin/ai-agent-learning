"""一键启动 / 自检入口。

用法：
    python run.py                     # 按 .env 启动（默认 127.0.0.1:8080）
    python run.py --engine demo       # 强制离线演示脑，不连大模型
    python run.py --port 8090
    python run.py --check             # 只做启动前自检，不拉起服务
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def check() -> int:
    """启动前自检：配置、内容、世界、工具、编排是否就位。"""
    from app.config import settings
    from app.content import content_service
    from app.tools import tool_registry
    from app.world import world_state
    from app.agent.orchestrator import orchestrator
    from app.llm import get_brain

    print("=" * 62)
    print("启动前自检")
    print("=" * 62)

    engine = settings.resolved_engine()
    brain = get_brain(engine)
    print(f"[配置] NPC_ENGINE={settings.engine} → 实际引擎 {engine}")
    print(f"[配置] .env 路径 {ROOT / '.env'}（存在={ (ROOT / '.env').exists() }）")
    if settings.llm_configured:
        print(f"[配置] 大模型 {settings.llm_model} @ {settings.llm_base_url}")
    else:
        print("[配置] 未配置大模型 → 使用离线演示脑（预设话术 + 规则行为），功能完整可演示")

    cards = content_service.list_cards()
    print(f"[内容] 角色卡 {len(cards)} 张：{', '.join(c.name for c in cards)}")
    print(f"[内容] 世界知识 {len(content_service.lore)} 条；"
          f"版本记录 {sum(len(v) for v in content_service.versions.values())} 条")

    print(f"[世界] 委托 {len(world_state.quest_defs)} 条；物品 {len(world_state.items)} 种；"
          f"玩家 {len(world_state.player('p_001').inventory)} 类背包物品")

    specs = tool_registry.specs()
    print(f"[工具] 注册 {len(specs)} 个：{', '.join(s.name for s in specs)}")
    for card in cards:
        allowed = [t["name"] for t in tool_registry.available_for(card) if t["allowed"]]
        print(f"[工具] {card.name} 可用 {len(allowed)} 个：{', '.join(allowed)}")

    print(f"[编排] brain={getattr(brain, 'engine', '?')}，degraded={orchestrator.degraded}")
    print("[结论] 自检通过，可以启动服务。")
    print(f"[提示] 浏览器访问 http://{settings.host}:{settings.port}/ 打开调试面板")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="游戏角色 NPC Agent 服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--engine", default=None,
                        choices=["auto", "llm", "demo", "rules"],
                        help="覆盖 .env 中的 NPC_ENGINE")
    parser.add_argument("--check", action="store_true", help="只做启动前自检")
    args = parser.parse_args()

    if args.engine:
        import os

        os.environ["NPC_ENGINE"] = args.engine

    if args.check:
        return check()

    import uvicorn

    from app.config import settings

    host = args.host or settings.host
    port = args.port or settings.port
    check()
    print("-" * 62)
    uvicorn.run("app.main:app", host=host, port=port, reload=False, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
