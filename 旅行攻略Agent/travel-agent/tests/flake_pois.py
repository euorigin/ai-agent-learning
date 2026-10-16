"""判断 discover_pois 的失败是「参数问题」还是「随机抖动」：同一参数连打 5 次。

若同一 want 时通时不通 → 是网关/模型抖动，必须靠**重试**兜住，
而不是靠调小参数（调小也照样随机失败）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.providers import build_providers  # noqa: E402
from app.llm import OpenAICompatClient  # noqa: E402
from app.settings import SETTINGS  # noqa: E402
from app.tools import build_registry  # noqa: E402
from app.tools.base import ToolContext  # noqa: E402

ctx = ToolContext(settings=SETTINGS, llm=OpenAICompatClient(),
                  providers=build_providers(SETTINGS))
reg = build_registry(ctx)

want = int(sys.argv[1]) if len(sys.argv) > 1 else 6
N = int(sys.argv[2]) if len(sys.argv) > 2 else 5
ok = 0
for i in range(N):
    t0 = time.time()
    r = reg.call("discover_pois", city="成都", interests=["美食", "自然风光"],
                 days=4, want=want, exclude=[])
    dt = time.time() - t0
    n = len(r.data or [])
    print(f"  #{i+1} ok={r.ok} 条数={n} 耗时={dt:.1f}s  "
          f"{'err: ' + str(r.errors)[:60] if r.errors else ''}")
    ok += 1 if r.ok else 0
print(f"\nwant={want}: {ok}/{N} 成功")
