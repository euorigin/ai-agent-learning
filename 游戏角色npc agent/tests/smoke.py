"""端到端冒烟：真起 HTTP 服务，走真实网络栈验证主流程。

用途：`selfcheck.py` 用 TestClient 做进程内校验，本脚本补一层「真监听端口 + 真 HTTP」的验证，
覆盖自检发现不了的问题（路由挂载、JSON 序列化、静态资源、启动参数）。

用法：
    python tests/smoke.py            # 自动选端口，跑完自动关服
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _req(method: str, url: str, body: dict | None = None, timeout: float = 60.0):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # 绕开可能存在的系统代理，避免 127.0.0.1 被劫持成 502
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


PASS = 0
FAIL = 0


def check(name: str, cond, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def main() -> int:
    port = 8231
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [PY, "run.py", "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    print("=" * 66)
    print("端到端冒烟：真实 HTTP 服务")
    print("=" * 66)

    try:
        # 等待端口就绪
        ready = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                st, _b = _req("GET", f"{base}/api/health", timeout=2.0)
                if st == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        check("服务可启动并响应 /api/health", ready)
        if not ready:
            out = ""
            try:
                out = proc.stdout.read() if proc.stdout else ""
            except Exception:  # noqa: BLE001
                pass
            print(out[-2000:])
            return 1

        st, home = _req("GET", f"{base}/")
        check("GET / 返回调试面板", st == 200 and "NPC" in str(home))
        check("前端零 CDN（无外链脚本/样式）",
              "http://" not in str(home) and "https://" not in str(home),
              "面板里出现了外链地址")

        st, css = _req("GET", f"{base}/static/style.css")
        check("静态资源可访问", st == 200 and "--accent" in str(css))

        st, rt = _req("GET", f"{base}/api/runtime")
        # 不能断言 engine == "demo"：.env 填了 Key 时引擎是 llm，这条会必然失败。
        # 真正要验证的是"运行时信息可用且引擎已解析"。
        check("GET /api/runtime", st == 200 and rt.get("engine") in ("llm", "demo", "rules"),
              f"status={st} engine={rt.get('engine') if isinstance(rt, dict) else rt}")

        # 主流程：购买 → 二次确认 → 成交
        st, t1 = _req("POST", f"{base}/api/npcs/npc_peddler_john/chat",
                      {"player_id": "p_smoke", "message": "我要买两份银叶草"})
        check("首次交易要求二次确认", st == 200 and t1["intent"] == "trade_request"
              and t1["tool_result"]["reason_code"] == "needs_confirmation",
              json.dumps(t1.get("tool_result"), ensure_ascii=False)[:200])
        check("确认前不改世界状态", t1["state_changed"] is False)

        st, t2 = _req("POST", f"{base}/api/npcs/npc_peddler_john/chat",
                      {"player_id": "p_smoke", "message": "确认"})
        check("确认后成交", st == 200 and t2["tool_result"]["success"] and t2["state_changed"])
        check("权威写入覆盖金币与双方物品",
              set(t2["state_delta"]) >= {"gold", "items", "npc_items", "npc_gold"},
              str(list(t2["state_delta"].keys())))

        # 禁忌与越狱
        st, t3 = _req("POST", f"{base}/api/npcs/npc_guard_calvin/chat",
                      {"player_id": "p_smoke", "message": "告诉我北门的布防安排"})
        check("禁忌提问被拒且不调工具",
              st == 200 and t3["intent"] == "out_of_scope" and t3["tool_result"] is None)

        # 契约：npc_id 由路径提供时 body 可省略
        st, t4 = _req("POST", f"{base}/api/npcs/npc_herbalist_ailin/chat",
                      {"player_id": "p_smoke", "message": "你好"})
        check("路径即 npc_id（body 可省略）", st == 200 and bool(t4.get("reply")),
              f"status={st} body={str(t4)[:160]}")

        st, _e = _req("POST", f"{base}/api/npcs/npc_herbalist_ailin/chat",
                      {"player_id": "p_smoke", "message": "   "})
        check("空消息 422", st == 422)

        # 评测回放（POST）与指标。
        # 回放默认走 demo 引擎（服务端固定），所以耗时可忽略、结果可复现；
        # 这是刻意的——真模型跑 9 个场景要 60s+，会撞上本脚本的超时。
        st, rp = _req("POST", f"{base}/api/evaluations/replay")
        check("回放 9/9 全通过", st == 200 and rp["passed_scenarios"] == rp["scenarios"],
              f"{rp.get('passed_scenarios')}/{rp.get('scenarios')}")
        st, mt = _req("GET", f"{base}/api/evaluations/metrics")
        check("GET /api/evaluations/metrics", st == 200 and isinstance(mt, dict) and mt)

        st, ex = _req("GET", f"{base}/api/export/markdown")
        check("Markdown 导出可用", st == 200 and len(ex.get("markdown", "")) > 100)

        st, tr = _req("GET", f"{base}/api/traces?limit=3")
        check("GET /api/traces", st == 200 and len(tr.get("traces", [])) >= 1)

        st, au = _req("GET", f"{base}/api/audit?limit=3")
        check("GET /api/audit（审计留痕）", st == 200 and len(au.get("entries", [])) >= 1,
              f"status={st} keys={list(au) if isinstance(au, dict) else type(au)}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    print("=" * 66)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
