"""探活脚本：确认 .env 里配置的真模型能否应答（不发业务请求，只发一条最小 ping）。

用途：启动服务前先判断「引擎到底是 llm 还是悄悄退了 rules」——
llm 模式下若模型不通，行程会退化成空壳，光看 /api/status 是看不出来的。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.settings import SETTINGS  # noqa: E402


def main() -> int:
    st = SETTINGS.status()
    print("引擎    :", st["engine"], "| 演示脑:", st.get("demo_brain"))
    print("模型    :", st.get("llm_model"))
    print("BaseURL :", st.get("llm_base_url"))
    if not st.get("llm_configured"):
        print("[FAIL] 未配置大模型（缺 base_url / model / api_key 之一）")
        return 1

    url = st["llm_base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": st["llm_model"],
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "max_tokens": 16,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {st['llm_api_key']}"},
    )
    # 必须绕过系统代理：本机有代理时 127.0.0.1 / 内网地址会被误送出去（会得到 502）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[FAIL] HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {type(e).__name__}: {e}")
        return 1

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        print("[FAIL] 返回结构异常:", json.dumps(data, ensure_ascii=False)[:400])
        return 1
    print("[PASS] 模型应答:", repr(text.strip()[:60]))
    usage = data.get("usage") or {}
    if usage:
        print("        tokens:", usage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
