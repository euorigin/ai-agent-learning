#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工程自检脚本
用法：  python tools/check-project.py
校验：  JSON 合法性 / JS 语法 / 页面文件完整性 / tabBar 一致性
        / usingComponents 路径 / require 相对路径 / WXML 事件绑定是否存在实现
需要本机有 node（可用环境变量 NODE_BIN 指定路径）。
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MP = os.path.join(ROOT, "miniprogram")
NODE = os.environ.get("NODE_BIN", "node")

errors, warns = [], []
js_files, json_files, wxml_files = [], [], []

for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", ".workbuddy")]
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        if fn.endswith(".js"):
            js_files.append(p)
        elif fn.endswith(".json"):
            json_files.append(p)
        elif fn.endswith(".wxml"):
            wxml_files.append(p)


def rel(p):
    return os.path.relpath(p, ROOT)


# 1) JSON 合法性
for p in json_files:
    try:
        with open(p, "r", encoding="utf-8") as f:
            json.load(f)
    except Exception as e:
        errors.append("JSON 解析失败: %s -> %s" % (rel(p), e))

# 2) JS 语法
for p in js_files:
    try:
        r = subprocess.run([NODE, "--check", p], capture_output=True, text=True)
    except FileNotFoundError:
        warns.append("未找到 node，跳过 JS 语法检查（可设置 NODE_BIN）")
        break
    if r.returncode != 0:
        errors.append("JS 语法错误: %s\n%s" % (rel(p), (r.stderr or r.stdout).strip()[:600]))

# 3) app.json 页面文件完整性
app_path = os.path.join(MP, "app.json")
with open(app_path, "r", encoding="utf-8") as f:
    app = json.load(f)
for page in app.get("pages", []):
    for ext in (".js", ".json", ".wxml", ".wxss"):
        fp = os.path.join(MP, page.replace("/", os.sep) + ext)
        if not os.path.exists(fp):
            errors.append("app.json 页面缺文件: %s" % rel(fp))

# 4) tabBar 页 vs pages 一致性
for item in app.get("tabBar", {}).get("list", []):
    if item.get("pagePath") not in app.get("pages", []):
        errors.append("tabBar 页面不在 pages 中: %s" % item.get("pagePath"))

# 5) custom tabBar 四件套
if app.get("tabBar", {}).get("custom"):
    for ext in (".js", ".json", ".wxml", ".wxss"):
        fp = os.path.join(MP, "custom-tab-bar", "index" + ext)
        if not os.path.exists(fp):
            errors.append("custom-tab-bar 缺文件: %s" % rel(fp))

# 6) usingComponents 路径可解析
for p in json_files:
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        continue
    for name, r_path in (cfg.get("usingComponents") or {}).items():
        cand = os.path.normpath(os.path.join(os.path.dirname(p), r_path.replace("/", os.sep)))
        if not (os.path.exists(cand + ".js") and os.path.exists(cand + ".wxml")):
            errors.append("usingComponents 无法解析: %s -> %s" % (rel(p), r_path))

# 7) require 相对路径可解析（这类错误 node --check 抓不到）
require_re = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
for p in js_files:
    with open(p, "r", encoding="utf-8") as f:
        code = f.read()
    for r_path in sorted(set(require_re.findall(code))):
        if not r_path.startswith("."):
            continue
        cand = os.path.normpath(os.path.join(os.path.dirname(p), r_path.replace("/", os.sep)))
        if not (os.path.exists(cand + ".js") or os.path.exists(os.path.join(cand, "index.js"))):
            errors.append("require 无法解析: %s -> %s" % (rel(p), r_path))

# 8) WXML 事件绑定 -> JS 中是否有实现
handler_re = re.compile(r'(?:bind|catch)[:]?[a-zA-Z]+="([A-Za-z_][A-Za-z0-9_]*)"')
for wp in wxml_files:
    js = wp[:-5] + ".js"
    if not os.path.exists(js):
        continue
    with open(wp, "r", encoding="utf-8") as f:
        wxml = f.read()
    with open(js, "r", encoding="utf-8") as f:
        code = f.read()
    for h in sorted(set(handler_re.findall(wxml))):
        if not re.search(r'(?:^|\s)' + re.escape(h) + r'\s*[:(=]', code):
            warns.append("WXML 绑定了未找到的方法: %s -> %s" % (rel(wp), h))

print("=" * 66)
print("JS %d 个 / JSON %d 个 / WXML %d 个" % (len(js_files), len(json_files), len(wxml_files)))
print("=" * 66)
for w in warns:
    print("  ! " + w)
if errors:
    print("发现 %d 个错误：" % len(errors))
    for e in errors:
        print("  x " + e)
    sys.exit(1)
print("自检通过：语法 / JSON / 文件完整性 / 组件与 require 引用 / 事件绑定 均正常")
