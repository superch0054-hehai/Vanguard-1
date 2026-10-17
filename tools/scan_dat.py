# -*- coding: utf-8 -*-
"""
.dat 结构扫描器（只读）——把文件切成「文本块(内嵌 UTF-16 XML)」与「数值区」，
并对每个数值区尝试 float32 / float64 解码，报告其规模与样值。

若同目录有配对的 txt（金标准），还会对每一列的前 3 行取值在文件里定位，
算出「同一列相邻两行的位置差」= 该数组的步长（stride）。

用法：python scan_dat.py <实验文件夹> [...]
"""
import os
import sys
import array
import struct


def utf16_mask(buf, minchars=48):
    """标记出属于「较长 UTF-16LE 可读文本」的字节。"""
    n = len(buf)
    mask = bytearray(n)
    i = 0
    while i + 2 <= n:
        if buf[i + 1] == 0 and 32 <= buf[i] < 127:
            j = i
            cnt = 0
            while j + 2 <= n:
                ch = buf[j] | (buf[j + 1] << 8)
                if (32 <= ch < 127) or (0x4E00 <= ch <= 0x9FFF) or ch in (9, 10, 13):
                    cnt += 1
                    j += 2
                else:
                    break
            if cnt >= minchars:
                for k in range(i, min(i + 2 * cnt, n)):
                    mask[k] = 1
                i = i + 2 * cnt
                continue
        i += 1
    return mask


def regions(mask, minlen=128):
    """数值区 = 非文本且连续长度 >= minlen 的片段"""
    n = len(mask)
    out = []
    i = 0
    while i < n:
        if mask[i]:
            i += 1
            continue
        j = i
        while j < n and not mask[j]:
            j += 1
        if j - i >= minlen:
            out.append((i, j - i))
        i = j
    return out


def sane(vals):
    if not vals:
        return 0.0
    ok = 0
    for v in vals:
        if v == v and abs(v) < 1e13 and (v == 0 or abs(v) > 1e-13):
            ok += 1
    return ok / len(vals)


def decode(buf, off, ln, kind):
    sz = 4 if kind == "f32" else 8
    seg = buf[off:off + ln]
    seg = seg[: len(seg) // sz * sz]
    if len(seg) < sz * 4:
        return None
    a = array.array("f" if kind == "f32" else "d")
    a.frombytes(seg)
    return a


def txt_cols(folder, nrows=3):
    txts = [f for f in sorted(os.listdir(folder))
            if f.lower().endswith(".txt") and os.path.getsize(os.path.join(folder, f)) > 0]
    if not txts:
        return None, []
    lines = [l.strip() for l in open(os.path.join(folder, txts[0]), encoding="utf-8-sig", errors="replace")]
    hi = None
    for i, l in enumerate(lines):
        if l and "," in l and all(ord(c) < 128 for c in l) and any(k in l for k in ("Freq", "Time", "E,", "t,")):
            hi = i
            break
    if hi is None:
        return None, []
    names = [x.strip() for x in lines[hi].split(",")]
    rows = []
    for l in lines[hi + 1:]:
        if not l:
            continue
        vals = []
        for c in l.split(","):
            c = c.strip()
            for k, ch in enumerate(c):
                if ch not in "+-.0123456789eE":
                    c = c[:k]
                    break
            try:
                vals.append(float(c))
            except ValueError:
                vals.append(None)
        rows.append(vals)
        if len(rows) >= nrows:
            break
    return names, rows


def positions(buf, v, kind, tol=3e-3, maxn=6):
    sz = 4 if kind == "f32" else 8
    out = []
    for phase in range(sz):
        seg = buf[phase:]
        seg = seg[: len(seg) // sz * sz]
        if not seg:
            continue
        a = array.array("f" if kind == "f32" else "d")
        a.frombytes(seg)
        for i, x in enumerate(a):
            if x == x and x != 0 and abs(x - v) <= tol * abs(v):
                out.append(phase + sz * i)
                if len(out) >= maxn:
                    return sorted(out)
    return sorted(out)


def main():
    for folder in sys.argv[1:]:
        if not os.path.isdir(folder):
            continue
        dats = [f for f in sorted(os.listdir(folder)) if f.lower().endswith(".dat")]
        if not dats:
            continue
        path = os.path.join(folder, dats[0])
        buf = open(path, "rb").read()
        print("=" * 78)
        print("%s  |  %s  %d B" % (folder.split("/")[-1], dats[0], len(buf)))

        mask = utf16_mask(buf)
        regs = regions(mask)
        text_bytes = sum(mask)
        print("  文本块(内嵌XML)占 %d B（%.0f%%）| 数值区 %d 个" % (text_bytes, 100.0 * text_bytes / len(buf), len(regs)))
        for off, ln in regs[:8]:
            line = "   数值区 @0x%06x  %7d B" % (off, ln)
            for kind in ("f32", "f64"):
                a = decode(buf, off, ln, kind)
                if not a:
                    continue
                sample = list(a[:6])
                line += "  | %s: n=%-6d 合理值%.0f%% 前6=%s" % (
                    kind, len(a), 100 * sane(list(a[:4000])), ["%.6g" % x for x in sample])
            print(line)

        names, rows = txt_cols(folder)
        if names and rows:
            print("  --- 用 txt 金标准定位各列（前 3 行）---")
            for ci, nm in enumerate(names):
                vals = [r[ci] for r in rows if ci < len(r) and r[ci] is not None]
                if len(vals) < 3:
                    continue
                for kind in ("f32", "f64"):
                    ps = [positions(buf, v, kind, maxn=1) for v in vals[:3]]
                    if all(ps):
                        p1, p2, p3 = [p[0] for p in ps]
                        print("    %-7s %-4s 位置 0x%06x 0x%06x 0x%06x | 步长 %d, %d"
                              % (nm, kind, p1, p2, p3, p2 - p1, p3 - p2))
                        break
                else:
                    print("    %-7s 未能定位（f32/f64 都找不到前三行值）" % nm)


if __name__ == "__main__":
    main()
