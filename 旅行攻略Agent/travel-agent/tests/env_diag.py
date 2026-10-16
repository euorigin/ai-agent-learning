"""诊断 .env 解析结果：只看键名、长度、首尾字符，不打印完整密钥。

401 的常见成因：
1) .env 里值带行内注释（TRAVEL_LLM_API_KEY=xxx   # 备注）→ 注释被当成 key 的一部分；
2) 值被引号包住（= "xxx" ）→ 引号进了 key；
3) 复制时带了换行 / 不可见字符；
4) 读取的不是用户刚编辑的那份 .env（环境变量优先级更高，把 .env 覆盖了）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KEYS = [
    "TRAVEL_LLM_BASE_URL", "TRAVEL_LLM_MODEL", "TRAVEL_LLM_API_KEY",
    "TRAVEL_ENGINE", "TRAVEL_HOST", "TRAVEL_PORT", "AMAP_KEY",
]

env_path = ROOT / ".env"
print(".env 路径 :", env_path, "| 存在:", env_path.exists())
if env_path.exists():
    st = env_path.stat()
    print("  修改时间:", __import__("datetime").datetime.fromtimestamp(st.st_mtime))
    print("  文件大小:", st.st_size, "B")

print("\n--- 进程环境变量（优先于 .env，可能是元凶） ---")
for k in KEYS:
    v = os.environ.get(k)
    print(f"  {k:22s} = {'<未设置>' if v is None else repr(v)[:80]}")

print("\n--- .env 原文（值为密钥时只显示首尾） ---")
if env_path.exists():
    raw = env_path.read_bytes()
    print("  含 BOM:", raw[:3] == b"\xef\xbb\xbf", "| 换行风格:",
          "CRLF" if b"\r\n" in raw else "LF")
    for i, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        s = line.rstrip()
        if not s or s.lstrip().startswith("#"):
            print(f"  {i:>3}| {s}")
            continue
        if "=" not in s:
            print(f"  {i:>3}| {s!r}   <-- 没有等号，格式可疑")
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        if "KEY" in k.upper() or "SECRET" in k.upper():
            shown = f"len={len(v)} {v[:6]!r}...{v[-6:]!r}"
        else:
            shown = repr(v)
        print(f"  {i:>3}| {k} = {shown}")

print("\n--- SETTINGS 实际生效值 ---")
from app.settings import SETTINGS  # noqa: E402
st = SETTINGS.status()
for k in ("engine", "engine_setting", "demo_brain", "llm_model",
          "llm_base_url", "llm_configured"):
    print(f"  {k:18s} = {st.get(k)}")
key = getattr(SETTINGS, "llm_api_key", None)
if key:
    print(f"  {'llm_api_key':18s} = len={len(key)} {key[:6]!r}...{key[-4:]!r}")
    print("  可疑字符检查:", {"尾部空白": key != key.strip(),
                            "含引号": '"' in key or "'" in key,
                            "含#": "#" in key,
                            "含不可见": any(ord(c) < 32 for c in key)})
