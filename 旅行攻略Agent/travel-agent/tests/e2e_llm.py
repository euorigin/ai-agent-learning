"""真模型端到端：不启服务，直接把一句话需求喂给编排器，检查产出是否为真行程。

字段契约（本工程，勿与 NPC agent 混）：
  ChatResponse : intent / reply / engine / trip / trace(单数) / slots / suggestions / warning(单数)
  Trip         : trip_id / request / days[list[DayPlan]] / budget / intercity / engine / warnings
  DayPlan      : date / weekday / city / weather / items[list[ItineraryItem]] / walking_km
  ItineraryItem: name / category / city / arrive / leave / stay_minutes / score / evidence

检查项（全部针对「llm 模式是否真的在工作」）：
  1. 引擎是 llm 且未退化成演示脑；
  2. 行程安排数 > 0（rules 模式典型症状就是 0）；
  3. 出现合成数据来源即判失败（demo 脑会带 synthetic 标记）；
  4. 天数由代码算，与输入一致；
  5. warning / trip.warnings 里没有降级告警残留。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.orchestrator import Orchestrator  # noqa: E402
from app.models import ChatRequest  # noqa: E402
from app.settings import SETTINGS  # noqa: E402

MSG = sys.argv[1] if len(sys.argv) > 1 else (
    "从北京出发，下周五去成都玩4天，预算6000，喜欢美食和自然风光，两个人"
)
WANT_DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 4

st = SETTINGS.status()
print("引擎:", st["engine"], "| 演示脑:", st.get("demo_brain"), "| 模型:", st.get("llm_model"))
if st["engine"] != "llm" or st.get("demo_brain"):
    print("[FAIL] 期望真模型 llm 引擎，实际不是")
    raise SystemExit(1)

orch = Orchestrator()
t0 = time.time()
resp = orch.chat(ChatRequest(message=MSG))
dt = time.time() - t0
print(f"\n输入: {MSG}")
print(f"耗时: {dt:.1f}s  |  intent={resp.intent}  |  engine={resp.engine}")

fails: list[str] = []
if resp.warning:
    fails.append(f"顶层降级告警: {resp.warning}")

reply = (resp.reply or "").strip()
# 模型可能在正文前后夹思维链（MiniMax M2 会写进 content）。
# 解析层（extract_json）已能剥掉它，但用来展示给用户的 reply 仍需清理，
# 否则用户看到的是「让我分析一下……」这类思维过程。
disp = reply
if "<think" in disp or "</think>" in disp or "end▁of▁thinking" in disp:
    import re as _re
    disp = _re.sub(r"<think\b[^>]*>.*?</think>", "", disp, flags=_re.S | _re.I)
    disp = _re.sub(r"<\｜?begin▁of▁thinking\｜?>.*?(<\｜?end▁of▁thinking\｜?>|$)",
                   "", disp, flags=_re.S)
    disp = _re.sub(r"<think\b.*$", "", disp, flags=_re.S | _re.I)
    disp = disp.strip()
    fails.append("正文里残留思维链（narration 未剥离 <think>）")
print("回复:", disp[:200].replace("\n", " ") or "（空）")

trip = resp.trip
if trip is None:
    print("\n[FAIL] 未生成行程 —— 追问或失败:", disp[:200])
    raise SystemExit(1)

req = trip.request
print(f"\n行程: {req.origin} → {'、'.join(req.destinations)}  "
      f"{req.start_date}~{req.end_date}  {len(trip.days)} 天")
print(f"人数: {req.travelers}  |  预算: {req.budget}  |  偏好: {req.preferences}")

items = [it for d in trip.days for it in d.items]
print(f"安排总数: {len(items)}")

if trip.warnings:
    print("trip.warnings:")
    for w in trip.warnings:
        print("  -", w)
        if any(k in w for k in ("演示脑", "合成", "synthetic", "demo")):
            fails.append(f"行程级降级残留: {w}")

if not items:
    fails.append("行程安排数为 0（llm 模式不该如此）")

synth = set()
for it in items:
    blob = " ".join([str(it.evidence or ""), str(it.reason or "")]).lower()
    if "synthetic" in blob or "演示" in blob:
        synth.add(it.name)
if synth:
    fails.append(f"出现合成数据: {sorted(synth)[:5]}")

span = (trip.days[-1].date, trip.days[0].date) if trip.days else (None, None)
print(f"日期跨度: {len(trip.days)} 天（要求 {WANT_DAYS}）")
if len(trip.days) != WANT_DAYS:
    fails.append(f"天数不符：要求 {WANT_DAYS}，得到 {len(trip.days)}")

# 时刻合法性：修掉「排到 27:40」之前，这里不会有人告警。
bad_times = []
for d in trip.days:
    for it in d.items:
        for label, t in (("arrive", it.arrive), ("leave", it.leave)):
            if not t:
                continue
            hh, _, mm = t.partition(":")
            try:
                if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                    bad_times.append(f"{d.date} {it.name} {label}={t}")
            except ValueError:
                bad_times.append(f"{d.date} {it.name} {label}={t!r}")
if bad_times:
    fails.append(f"出现跨午夜的非法时刻 {bad_times[:3]}")

print("\n--- 每日安排 ---")
for d in trip.days:
    print(f"[{d.date} {d.weekday}] {d.city}  "
          f"{len(d.items)} 项  步行 {d.walking_km}km  "
          f"天气 {(d.weather.temp_low if d.weather else '?')}~"
          f"{(d.weather.temp_high if d.weather else '?')}℃")
    for it in d.items:
        print(f"    {it.arrive}-{it.leave} {it.name[:22]:24s} "
              f"({it.category}) ¥{it.cost_low:.0f}~{it.cost_high:.0f}")

if trip.budget:
    b = trip.budget
    print(f"\n预算: ¥{b.total_low:,.0f} ~ ¥{b.total_high:,.0f} "
          f"（用户 ¥{b.user_budget}）→ {b.verdict}")
    for ln in b.lines:
        print(f"    {ln.category:8s} ¥{ln.low:,.0f}~{ln.high:,.0f}  {ln.basis[:40]}")

print(f"\n工具调用 {len(resp.trace)} 次:",
      ", ".join(f"{t.tool}{'*' if getattr(t, 'degraded', False) else ''}"
                for t in resp.trace) or "（无）")

print("\n" + ("[FAIL] " + " | ".join(fails) if fails else "[PASS] 真模型端到端通过"))
raise SystemExit(1 if fails else 0)
