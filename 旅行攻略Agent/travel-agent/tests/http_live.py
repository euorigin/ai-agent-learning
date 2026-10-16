"""通过真实 HTTP 打一遍接口：确认服务起来了，且 /api/chat 能返回真行程。

先起服务（背景），再轮询 /api/status 直到引擎就绪，然后发一次规划请求。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
BASE = f"http://127.0.0.1:{PORT}"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path: str, timeout: float = 20):
    with opener.open(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post(path: str, payload: dict, timeout: float = 600):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


proc = subprocess.Popen([PY, "run.py", "--port", str(PORT)],
                        cwd=str(ROOT), stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                        errors="replace")
try:
    print("等待服务就绪 ...")
    st = None
    for _ in range(40):
        time.sleep(0.5)
        try:
            st = get("/api/status", timeout=3)
            break
        except Exception:  # noqa: BLE001
            continue
    if st is None:
        print("[FAIL] 服务未起来"); raise SystemExit(1)
    print(f"[PASS] /api/status → 引擎={st.get('engine')} 模型={st.get('llm_model')} "
          f"地址={st.get('host')}:{st.get('port')}")
    if st.get("engine") != "llm":
        print("[FAIL] 期望 llm 引擎"); raise SystemExit(1)

    # 开放文档：确认路由齐全
    openapi = get("/openapi.json")
    paths = sorted(openapi.get("paths", {}))
    print(f"[PASS] /openapi.json → {len(paths)} 个路径")
    for p in paths:
        print("        ", p)

    print("\n发起一次真实规划 ...")
    t0 = time.time()
    resp = post("/api/chat", {"message": "从北京出发，下周五去成都玩4天，预算6000，喜欢美食和自然风光，两个人"})
    dt = time.time() - t0
    trip = resp.get("trip") or {}
    days = trip.get("days") or []
    items = [i for d in days for i in (d.get("items") or [])]
    print(f"[PASS] /api/chat 耗时 {dt:.1f}s  intent={resp.get('intent')}  "
          f"天数={len(days)}  安排数={len(items)}")
    print("回复:", (resp.get("reply") or "")[:150].replace("\n", " "))
    print("trip_id:", trip.get("trip_id"))
    for d in days:
        print(f"  [{d.get('date')}] {len(d.get('items') or [])} 项")
    if not items:
        print("[FAIL] 行程为空"); raise SystemExit(1)

    # 行程已落盘
    trips = get("/api/trips")
    print(f"[PASS] /api/trips → {len(trips.get('trips') or [])} 份已保存行程")
    print("\n[PASS] HTTP 全链路通过")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    out = proc.stdout.read() if proc.stdout else ""
    print("\n--- 服务端日志（尾部）---")
    print("\n".join(out.splitlines()[-12:]))
