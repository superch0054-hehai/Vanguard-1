# -*- coding: utf-8 -*-
"""
东华原生格式探针（只读，不修改任何原文件）

回答：.dat / .dhsln 里到底有没有"数据"，以什么形式存？

诊断项：
  1) 每个实验文件夹里 .dat / .dhsln / .txt 的大小
  2) 从 txt（若有）取已知数值，在 .dat / .dhsln 里做三重搜索：
       - 二进制精确匹配（float64/float32、小端/大端）
       - 按容差的 float64 匹配（覆盖"显示值被四舍五入"的情况）
       - 文本匹配（UTF-8，顺带能命中 UTF-16 文本内容）
  3) .dat 成分：零字节占比、64KB 非零密度图、UTF-16 文本块位置与内容预览
  4) .dhsln：最长行预览（判断是"纯配置"还是"含数据"）

用法：
  python probe_dh_native.py <文件夹或文件> [...]
"""
import os
import sys
import glob
import array
import struct

FMTS = ("<d", "<f", ">d", ">f")


def count(buf, b):
    n = 0
    i = buf.find(b)
    while i >= 0:
        n += 1
        i = buf.find(b, i + 1)
    return n


def num_of(cell):
    c = cell.strip()
    for i, ch in enumerate(c):
        if ch not in "+-.0123456789eE":
            c = c[:i]
            break
    try:
        return float(c)
    except ValueError:
        return None


def txt_values(txt_path):
    try:
        with open(txt_path, encoding="utf-8-sig", errors="replace") as f:
            lines = [l.strip() for l in f]
    except Exception:
        return None, []
    hi = None
    for i, l in enumerate(lines):
        if l and "," in l and all(ord(c) < 128 for c in l) and any(
            k in l for k in ("Freq", "Time", "E,", "t,")
        ):
            hi = i
            break
    if hi is None:
        return None, []
    hdr = lines[hi]
    row = None
    for l in lines[hi + 1:]:
        if l and l[0] in "+-.0123456789":
            row = l
            break
    if not row:
        return hdr, []
    vals = [v for v in (num_of(c) for c in row.split(",")) if v is not None]
    return hdr, vals


def nonzero_map(buf, blk=65536):
    out = []
    for i in range(0, len(buf), blk):
        seg = buf[i:i + blk]
        nz = 0
        for b in seg:
            if b:
                nz += 1
        out.append(int(9.0 * nz / len(seg)))
    return "".join(str(d) for d in out)


def _printable_at(buf, k, n):
    if k + 1 >= n:
        return False
    ch = buf[k] | (buf[k + 1] << 8)
    return (32 <= ch < 127) or (0x4E00 <= ch <= 0x9FFF) or ch in (9, 10, 13)


def utf16_runs(buf, minchars=30, maxruns=4):
    n = len(buf)
    runs = []
    i = 0
    while i + 6 < n and len(runs) < maxruns:
        if _printable_at(buf, i, n) and _printable_at(buf, i + 2, n) and _printable_at(buf, i + 4, n):
            j = i
            cnt = 0
            while _printable_at(buf, j, n):
                cnt += 1
                j += 2
            if cnt >= minchars:
                txt = buf[i:i + 2 * cnt].decode("utf-16-le", errors="replace")
                runs.append((i, cnt, txt.replace("\r", "").replace("\n", "⏎")))
                i = j
            else:
                i += 1
        else:
            i += 1
    return runs


def f64_hits(buf, v, rel=2e-3):
    """按 8 种字节相位解码 float64，统计与 v 相对差 < rel 的值个数"""
    hits = 0
    first = None
    for phase in range(8):
        seg = buf[phase:]
        seg = seg[: len(seg) // 8 * 8]
        if not seg:
            continue
        a = array.array("d")
        a.frombytes(seg)
        for idx, x in enumerate(a):
            if x == x and x != 0 and abs(x - v) <= rel * abs(v):
                hits += 1
                if first is None:
                    first = phase + 8 * idx
    return hits, first


def report_path(path):
    low = path.lower()
    size = os.path.getsize(path)
    name = os.path.basename(path)
    print("=" * 74)
    print("%-10s %10d  %s" % (low.rsplit(".", 1)[-1], size, path))
    if size == 0:
        print("   (空文件)")
        return
    buf = open(path, "rb").read()

    if low.endswith(".dat"):
        z = buf.count(b"\x00")
        print("   零字节 %.1f%% | 内嵌UTF-16 '<' x%d" % (100.0 * z / len(buf), buf.count(b"<\x00")))
        print("   64KB 非零密度图(0=全零,9=全满): %s" % nonzero_map(buf)[:120])
        for off, cnt, txt in utf16_runs(buf):
            print("   UTF-16 文本块 0x%06x (%d 字): %s" % (off, cnt, txt[:110].replace("\t", " ")))
        print("   统计: 最长 UTF-16 文本块 = %d 字" % (max([c for _, c, _ in utf16_runs(buf, minchars=4, maxruns=200)] + [0])))

    elif low.endswith(".dhsln"):
        text = buf.decode("utf-8", errors="replace")
        lines = text.splitlines()
        longest = max(lines, key=len) if lines else ""
        print("   文本行数 %d | 最长行 %d 字符" % (len(lines), len(longest)))
        print("   最长行预览: %s" % longest[:150])
        print("   XML 标签计数: Param=%d ExperimentParam=%d"
              % (text.count("<UIParam "), text.count("<ExperimentParam")))

    elif low.endswith(".txt"):
        text = buf.decode("utf-8-sig", errors="replace")
        lines = text.splitlines()
        print("   文本行数 %d | 首行: %s" % (len(lines), lines[0][:80] if lines else ""))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    for a in args:
        if os.path.isfile(a):
            report_path(a)
            continue
        for dirpath, _d, filenames in os.walk(a):
            if not any(f.lower().endswith((".dat", ".dhsln", ".txt")) for f in filenames):
                continue
            print("\n#### %s" % dirpath)
            for f in sorted(filenames):
                if f.lower().endswith((".dat", ".dhsln", ".txt")):
                    report_path(os.path.join(dirpath, f))
            # 交叉验证：用 txt 的值去 dat/dhsln 里搜
            txt = [f for f in sorted(filenames) if f.lower().endswith(".txt")]
            others = [f for f in sorted(filenames) if f.lower().endswith((".dat", ".dhsln"))]
            if txt and others and os.path.getsize(os.path.join(dirpath, txt[0])) > 0:
                hdr, vals = txt_values(os.path.join(dirpath, txt[0]))
                print("   ---- 交叉验证 (txt 表头 %s, 首行值 %s) ----" % (hdr, vals[:4]))
                for v in vals[:3]:
                    for f in others:
                        buf = open(os.path.join(dirpath, f), "rb").read()
                        text = buf.decode("utf-8", errors="ignore")
                        h, first = f64_hits(buf, v)
                        print("      %-40s v=%-10g 容差内float64 %d 个%s | 文本 %d 次"
                              % (f[:40], v, h,
                                 ("(首个@0x%x)" % first) if first is not None else "",
                                 text.count("%g" % v)))


if __name__ == "__main__":
    main()
