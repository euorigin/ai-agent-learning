"""extract_json 单元回归：覆盖各类推理模型输出形态。

背景（2026-10-14 实测 MiniMax M2 端点）：模型把思维链写进 content，
形如 <think>…常含 { } 和数字…</think>{"pois":[…]}。
旧的「首个 { 到最后一个 }」切法会从思维链里切出非法 JSON → 行程 0 安排。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.llm import extract_json, LLMError  # noqa: E402

FENCE = "```"

CASES: list[tuple[str, str, object]] = [
    ("思维链+JSON",
     '<think>需要返回JSON，含{}和1,2,3</think>{"pois":[{"name":"宽窄巷子"}]}',
     {"pois": [{"name": "宽窄巷子"}]}),
    ("思维链含换行与花括号",
     '<think>推理...\n{乱写的}</think>\n\n{"a":1}',
     {"a": 1}),
    ("全角竖线 thinking 分隔",
     '<｜begin▁of▁thinking｜>推理{}{}<｜end▁of▁thinking｜>{"b":2}',
     {"b": 2}),
    ("截断的 think（模型没写完）",
     '<think>没写完的推理 {"x":1}',
     None),                       # 无完整 JSON，应报错
    ("fence 包裹",
     f'前面废话\n{FENCE}json\n{{"c":3}}\n{FENCE}\n后面废话',
     {"c": 3}),
    ("正文含多个对象",
     '说明{"干扰":0} 真答案{"d":4}',
     {"干扰": 0}),                 # raw_decode 取第一个可解析对象
    ("顶层数组",
     '<think>{}</think>[{"e":5}]',
     [{"e": 5}]),
    ("纯散文无 JSON",
     "抱歉，我无法完成。",
     None),
    ("空字符串",
     "",
     None),
    ("只有思维链",
     "<think>我还在想</think>",
     None),
    ("JSON 内嵌 think 字样字符串",
     '{"g":"<think>不是标签</think>"}',
     {"g": "<think>不是标签</think>"}),
]


def main() -> int:
    fails = 0
    for name, text, want in CASES:
        try:
            got = extract_json(text)
        except LLMError as e:
            if want is None:
                print(f"  [OK] {name:26s} 如期报错: {str(e)[:52]}")
            else:
                fails += 1
                print(f"  [FAIL] {name:26s} 期望 {want!r}，却报错: {e}")
            continue
        if want is None:
            fails += 1
            print(f"  [FAIL] {name:26s} 期望报错，却解出 {got!r}")
        elif got == want:
            print(f"  [OK] {name:26s} -> {got!r}")
        else:
            fails += 1
            print(f"  [FAIL] {name:26s} 期望 {want!r}，得到 {got!r}")
    print(f"\n{'[FAIL]' if fails else '[PASS]'} extract_json 用例 {len(CASES)} 个，失败 {fails} 个")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
