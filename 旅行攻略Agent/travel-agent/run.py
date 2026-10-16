#!/usr/bin/env python
"""一键启动：python run.py

用法：
    python run.py                    # 默认 127.0.0.1:8000
    python run.py --port 8060        # 换端口
    python run.py --reload           # 开发模式热重载
    python run.py --engine demo      # 离线演示脑：不填 Key 也能看完整效果（合成地点）
    python run.py --engine rules     # 纯规则引擎：不调大模型，也拿不到地点数据
    python run.py --check            # 只做环境自检，不启动服务
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _check() -> int:
    print("== 环境自检 ==")
    ok = True
    try:
        import fastapi, uvicorn, pydantic                     # noqa: F401
        print(f"  依赖      : fastapi/uvicorn/pydantic 已安装")
    except ImportError as exc:
        print(f"  依赖      : 缺失 → {exc}")
        print("              请执行：pip install -r requirements.txt")
        return 1

    from app.settings import SETTINGS, ENV_PATH
    print(f"  .env 文件 : {'已找到 ' + str(ENV_PATH) if ENV_PATH.exists() else '未找到（将使用默认值）'}")
    st = SETTINGS.status()
    # demo 引擎内部走「演示脑」，status() 里会体现为 llm；这里显式标出来，避免误判成真接了模型。
    engine_label = "demo（离线演示脑）" if st.get("demo_brain") else st["engine"]
    print(f"  运行引擎  : {engine_label}（TRAVEL_ENGINE={st['engine_setting']}）")
    print(f"  大模型    : {st['llm_model']} @ {st['llm_base_url']}")
    print(f"  API Key   : {st['llm_api_key']}")
    print(f"  坐标源    : {st['geo_source']}")
    if st["engine"] == "rules":
        # rules 只做需求解析与数值计算，POI 发现没有离线数据源 → 行程会是空的。
        # 想让「不填 Key 也能看完整效果」成立，必须用 demo 引擎（合成地点）。
        print("  注意      : rules 引擎拿不到地点数据（无高德 Key 也无大模型），"
              "生成的行程不含任何安排。")
        print("              想离线看完整效果请改用：python run.py --engine demo")
    if not st["llm_configured"]:
        print("  建议      : 在 .env 里填好 3 个必填项，能力最完整")

    try:
        from app.agent.orchestrator import Orchestrator
        eng = Orchestrator()
        print(f"  工具数量  : {len(eng.registry.specs())} 个 → "
              f"{', '.join(t['name'] for t in eng.registry.specs())}")
    except Exception as exc:                                   # noqa: BLE001
        print(f"  工具装载  : 失败 → {type(exc).__name__}: {exc}")
        ok = False

    try:
        from app.http import request_json
        geo = request_json("https://geocoding-api.open-meteo.com/v1/search",
                           params={"name": "丽江", "count": 1, "language": "zh"}, timeout=10)
        hit = (geo or {}).get("results") or []
        print(f"  外网连通  : 正常（Open-Meteo 命中 {len(hit)} 条）")
    except Exception as exc:                                   # noqa: BLE001
        print(f"  外网连通  : 失败 → {type(exc).__name__}: {exc}")
        print("              天气/坐标会退化为估算，大模型为本地或内网地址时可忽略")
        ok = False
    print("== 自检结束 ==")
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="旅行攻略 Agent 助手")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    parser.add_argument("--engine", choices=["auto", "llm", "rules", "demo"], default=None,
                        help="覆盖 .env 中的 TRAVEL_ENGINE")
    parser.add_argument("--check", action="store_true", help="只做环境自检")
    args = parser.parse_args()

    if args.engine:
        import os
        os.environ["TRAVEL_ENGINE"] = args.engine

    if args.check:
        return _check()

    from app.settings import SETTINGS
    host = args.host or SETTINGS.host
    port = args.port or SETTINGS.port

    if not _port_free(host, port):
        print(f"端口 {host}:{port} 已被占用，请换一个：python run.py --port {port + 1}")
        return 1

    # 让启动横幅与 /api/status 反映真实监听地址：它们读的是 SETTINGS，
    # 而 --host/--port 此前只传给了 uvicorn，于是 --port 8070 时横幅仍报 8000。
    # 环境变量是为了 --reload（子进程重新导入 app.settings）也能拿到同样的值。
    import os
    os.environ["TRAVEL_HOST"], os.environ["TRAVEL_PORT"] = host, str(port)
    SETTINGS.host, SETTINGS.port = host, port

    import uvicorn
    print(f"\n浏览器打开 → http://{host}:{port}")
    if SETTINGS.status()["engine"] == "rules":
        print("当前为 rules 引擎：行程不含任何安排（拿不到地点数据）。"
              "想看完整效果请用 --engine demo，或在 .env 配好大模型。")
    print()
    uvicorn.run("app.main:app", host=host, port=port, reload=args.reload,
                log_level="info" if args.reload else "warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
