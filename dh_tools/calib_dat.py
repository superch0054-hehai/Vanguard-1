# -*- coding: utf-8 -*-
"""
.dat 列定位/标定工具 —— 用同一实验的 .txt 导出当"金标准"。

原理：.dat 的数据区是「按信号（列）连续存放的 float32/float64 数组」，
      而 txt 导出里每一列的显示值就是这些数（只是四舍五入 + 加了单位前缀）。
      所以拿 txt 某一列的前 k 个显示值，在 .dat 里找「连续 k 个都在容差内」
      的位置 → 就定位到该列数组，同时得到：类型（f32/f64）、对齐相位、字节偏移。

用法：python calib_dat.py <含 txt+dat 的实验文件夹> [...]
"""
import os
import sys
import array
import struct

TOL_REL = 3e-3      # 显示值四舍五入带来的相对误差上限（1~2 位小数足够覆盖）
K = 4               # 要求连续 4 个数都在容差内


def txt_table(folder, maxrows=400):
    txts = [f for f in sorted(os.listdir(folder))
            if f.lower().endswith(".txt") and os.path.getsize(os.path.join(folder, f)) > 0]
    if not txts:
        return None, [], []
    path = os.path.join(folder, txts[0])
    lines = [l.strip() for l in open(path, encoding="utf-8-sig", errors="replace")]
    hi = None
    for i, l in enumerate(lines):
        if l and "," in l and all(ord(c) < 128 for c in l) and any(
                k in l for k in ("Freq", "Time", "E,", "t,")):
            hi = i
            break
    if hi is None:
        return None, [], []
    names = [n.strip() for n in lines[hi].split(",")]
    rows = []
    for l in lines[hi + 1:]:
        if not l:
            continue
        cells = l.split(",")
        vals = []
        for c in cells:
            c = c.strip()
            for i, ch in enumerate(c):
                if ch not in "+-.0123456789eE":
                    c = c[:i]
                    break
            try:
                vals.append(float(c))
            except ValueError:
                vals.append(None)
        rows.append(vals)
        if len(rows) >= maxrows:
            break
    return names, rows, [os.path.basename(p) for p in [path]]


def locate(buf, vals, tol_rel=TOL_REL, k=K, start=0x400, limit_mb=40):
    """vals: 该列的前 k 个显示值。返回 [(类型, 相位, 字节偏移, 长度估计), ...]"""
    vals = [v for v in vals[:k]]
    if len(vals) < k or any(v is None for v in vals):
        return []
    found = []
    for kind, sz, ch in (("f32", 4, "f"), ("f64", 8, "d")):
        if len(buf) > limit_mb * 1024 * 1024:
            break
        for phase in range(sz):
            seg = buf[start + phase:]
            seg = seg[: len(seg) // sz * sz]
            if not seg:
                continue
            a = array.array(ch)
            a.frombytes(seg)
            n = len(a)
            v0 = vals[0]
            t0 = tol_rel * abs(v0)
            for i in range(n - k):
                x = a[i]
                if x != x or abs(x - v0) > t0:
                    continue
                ok = True
                for j in range(1, k):
                    vj = vals[j]
                    xj = a[i + j]
                    if xj != xj or abs(xj - vj) > tol_rel * abs(vj):
                        ok = False
                        break
                if ok:
                    # 往前/往后延展，估计该列数组的长度（值变得不合理为止）
                    lo = i
                    while lo > 0 and abs(a[lo - 1]) < 1e13 and a[lo - 1] == a[lo - 1]:
                        lo -= 1
                    hi_ = i + k
                    while hi_ < n and abs(a[hi_]) < 1e13 and a[hi_] == a[hi_]:
                        hi_ += 1
                    found.append((kind, phase, start + phase + i * sz, hi_ - lo))
                    break
            if found and found[-1][0] == kind:
                continue
    return found


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    for folder in args:
        if not os.path.isdir(folder):
            continue
        dats = [f for f in sorted(os.listdir(folder)) if f.lower().endswith(".dat")]
        if not dats:
            continue
        names, rows, txts = txt_table(folder)
        print("=" * 76)
        print(folder.split("/")[-1])
        dat = os.path.join(folder, dats[0])
        buf = open(dat, "rb").read()
        print("  dat=%-10s %9d B | txt列=%s | 行数(取样)=%d" % (dats[0], len(buf), names, len(rows)))
        if names and rows:
            for ci, nm in enumerate(names):
                col = [r[ci] if ci < len(r) else None for r in rows]
                hits = locate(buf, [v for v in col if v is not None][:K])
                if hits:
                    for kind, phase, off, ln in hits[:2]:
                        print("    %-8s -> %-4s 偏移 0x%06x 相位%d 连续长度≈%d 个" % (nm, kind, off, phase, ln))
                else:
                    print("    %-8s -> 未定位到（可能是多段/多圈数据、或该列非连续存放）" % nm)


if __name__ == "__main__":
    main()
