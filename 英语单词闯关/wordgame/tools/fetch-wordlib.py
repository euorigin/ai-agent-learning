# -*- coding: utf-8 -*-
"""拉取公开考纲词表（KyleBing/english-vocabulary）到工作区缓存目录。

用法：
    python tools/fetch-wordlib.py            # 已有缓存则跳过
    python tools/fetch-wordlib.py --force    # 强制重新下载

说明：
  - 数据源为开源词库，txt 格式为「单词<TAB>释义」，**不含音标**；
    发音由游戏内的浏览器 TTS（speechSynthesis）实时朗读。
  - raw.githubusercontent.com 在部分网络下不可达，故按顺序尝试公共镜像。
  - 缓存目录刻意放在 wordgame/ 之外，避免源数据被打包上传到线上。
"""
import os
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

RAW = "https://raw.githubusercontent.com/KyleBing/english-vocabulary/master/"

# 按顺序尝试；空串表示直连。前两个为实测可用的公共镜像。
MIRRORS = [
    "https://ghproxy.net/",
    "https://ghfast.top/",
    "",
]

# 仓库 master 分支下的实际文件名（已做 URL 编码）
SOURCES = {
    "cet4":   "3%20%E5%9B%9B%E7%BA%A7-%E4%B9%B1%E5%BA%8F.txt",    # 四级-乱序
    "cet6":   "4%20%E5%85%AD%E7%BA%A7-%E4%B9%B1%E5%BA%8F.txt",    # 六级-乱序
    "kaoyan": "5%20%E8%80%83%E7%A0%94-%E4%B9%B1%E5%BA%8F.txt",    # 考研-乱序
}

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.normpath(os.path.join(HERE, "..", "..", ".build-wordlib"))


def fetch(name, path, force):
    dest = os.path.join(CACHE, name + ".txt")
    if os.path.exists(dest) and not force:
        print("  skip   " + name + "  (已缓存 " + str(os.path.getsize(dest)) + " bytes)")
        return dest

    last_err = None
    for mirror in MIRRORS:
        url = mirror + RAW + path
        label = mirror or "direct"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
            if not data:
                raise ValueError("空响应")
            with open(dest, "wb") as fp:
                fp.write(data)
            print("  ok     " + name + "  " + str(len(data)) + " bytes  <- " + label)
            return dest
        except Exception as e:
            last_err = e
            print("  retry  " + name + "  " + label + " 不可用: " + type(e).__name__)
    raise SystemExit("下载失败 " + name + "：" + str(last_err))


def main():
    force = "--force" in sys.argv
    os.makedirs(CACHE, exist_ok=True)
    print("缓存目录: " + CACHE)
    for key, path in SOURCES.items():
        fetch(key, path, force)
    print("完成。")


if __name__ == "__main__":
    main()
