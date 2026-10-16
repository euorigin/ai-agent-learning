# -*- coding: utf-8 -*-
"""把 .build-wordlib/ 下的原始词表转换成游戏可按需加载的 data/*.js。

用法：
    python tools/build-wordlib.py

产物：
    wordgame/data/{cet4,cet6,kaoyan}.js
    格式： window.__WQ_WORDS__.cet4 = [["access","v. 获取"], ...]
    用数组对而非 {w,cn} 对象，2.3 万词可省约 180KB。

解析要点：
  - 原始释义是完整词典释义，直接当选项会过长（例：`v. 坚持；执意；继续存在[发生]；持续`），
    因此只取「第一个词性段」并最多保留 3 个义项，总长超过 MAX_CN 再截断。
  - 剔除非纯字母词（含连字符/空格/大写专名的词），保证拼写模式的字母池始终可解。
"""
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.normpath(os.path.join(HERE, "..", "..", ".build-wordlib"))
OUT = os.path.normpath(os.path.join(HERE, "..", "data"))

LEVELS = ["cet4", "cet6", "kaoyan"]

POS = ["n", "vt", "vi", "v", "adj", "adv", "prep", "conj", "pron", "num", "art", "int", "aux", "abbr"]
POS_RE = re.compile(r"(?:^|\s)(" + "|".join(POS) + r")\.\s*")
BRACKET_RE = re.compile(r"[\[【(（].*?[\]】)）]")
WORD_RE = re.compile(r"[a-z]{2,20}")

MAX_CN = 24      # 释义总长上限（字符）
MAX_SENSE = 3    # 单个词性下最多保留几个义项
SPLIT_RE = re.compile(r"[；;、,，/]")


def clean_gloss(raw):
    """把完整词典释义裁剪成适合做选择题选项的短释义。"""
    s = BRACKET_RE.sub("", raw.strip())      # 去掉 [复数 parties]、(Very) 这类补充
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""

    marks = list(POS_RE.finditer(s))
    if marks:
        end = marks[1].start() if len(marks) > 1 else len(s)
        seg = POS_RE.sub("", s[marks[0].start():end]).strip()
        prefix = marks[0].group(1) + ". "
    else:
        seg, prefix = s, ""

    parts = [p.strip() for p in SPLIT_RE.split(seg) if p.strip()]
    if not parts:
        return ""
    body = "，".join(parts[:MAX_SENSE])

    out = (prefix + body).rstrip("，,；;、 ")
    if len(out) > MAX_CN:
        out = out[:MAX_CN].rstrip("，,；;、 ") + "…"
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    total = 0
    counts, raws = {}, {}
    for lv in LEVELS:
        src = os.path.join(CACHE, lv + ".txt")
        if not os.path.exists(src):
            raise SystemExit("缺少源文件 " + src + "，请先执行 fetch-wordlib.py")

        pairs, seen = [], set()
        bad_word = dup = bad_gloss = 0
        with open(src, encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.rstrip("\r\n")
                if "\t" not in line:
                    continue
                word, gloss = line.split("\t", 1)
                word = word.strip().lower()
                if not WORD_RE.fullmatch(word):
                    bad_word += 1
                    continue
                if word in seen:
                    dup += 1
                    continue
                cn = clean_gloss(gloss)
                if not cn:
                    bad_gloss += 1
                    continue
                seen.add(word)
                pairs.append([word, cn])

        dest = os.path.join(OUT, lv + ".js")
        payload = ",".join(
            json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in pairs
        )
        content = (
            "/* 词库自动生成，请勿手工编辑。源: github.com/KyleBing/english-vocabulary */\n"
            "window.__WQ_WORDS__=window.__WQ_WORDS__||{};\n"
            "window.__WQ_WORDS__." + lv + "=[" + payload + "];\n"
        )
        with open(dest, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(content)

        counts[lv] = len(pairs)
        raws[lv] = len(pairs) + dup + bad_word + bad_gloss
        total += len(pairs)
        kb = round(os.path.getsize(dest) / 1024)
        print("  " + lv.ljust(7) + str(len(pairs)).rjust(5) + " 词  ->  data/" + lv + ".js  " +
              str(kb).rjust(4) + " KB" +
              "   (剔除非法词 " + str(bad_word) + " / 同表重复 " + str(dup) + " / 空释义 " + str(bad_gloss) + ")")

    # 元信息（约 80 字节）：首页只加载它就能显示真实词量，无需先下整包词库
    meta = dict(counts)
    meta["total"] = total
    meta["src"] = raws
    with open(os.path.join(OUT, "meta.js"), "w", encoding="utf-8", newline="\n") as fp:
        fp.write("/* 词库元信息自动生成 */\n")
        fp.write("window.__WQ_META__=" +
                 json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print("合计 " + str(total) + " 词  ->  data/meta.js")


if __name__ == "__main__":
    main()
