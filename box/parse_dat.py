#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东华 .dat 解码器 v1.0（只读，不修改任何原文件）
============================================================
已解出两种存储家族（均经软件导出 txt 逐点对账验证）：

【EIS 阻抗谱】每点 = 7 个 float32：Zre(Ω), Zim(Ω), |Z|(Ω), Freq(Hz), phase(°), |Zim|(Ω), E(mV)
    记录两点一页：页周期 584 字节、页内第 2 点在 +101；记录起点可能落在 ≡3 (mod 4) 相位，
    故扫描步长必须为 1。定位与串联靠**物理自检**（|Z|≈hypot、phase≈atan2、|Zim| 一致、
    频率单调下降）——不依赖 txt。

【LSV / CV】一条记录 142 字节、含 2 个点：
    +0  Time_A(s)   +25 Time_B(s)
    +50 E_A(mV)     +54 E_B(mV)
    +58 Ewe_A(mV)   +62 Ewe_B(mV)
    +66 I_A(mA)     +70 I_B(mA)
    +74 I2_A(mA)    +78 I2_B(mA)
    +90 IRand_A(mA) +94 IRand_B(mA)
    块内为 142 字节周期；段与段之间有间隔 → 用"断点重同步"跨过。

用法：
    python parse_dat.py <文件夹或.dat文件> [...]      # 打印解码摘要
    python parse_dat.py <文件夹> --json out.json     # 导出逐点数值
"""
import os
import sys
import json
import math
import struct

# ============================== EIS ==============================
EIS_PAGE = 584
EIS_SLOT2 = 101
EIS_NCOL = 7
EIS_COLUMNS = ["Zre_ohm", "Zim_ohm", "Z_ohm", "Freq_Hz", "phase_deg", "ZimAbs_ohm", "E_mV"]


def eis_record(buf, off):
    """读一条 EIS 点记录（7 个 float32）并物理自检；通过返回 7 个值，否则 None"""
    if off < 0 or off + 4 * EIS_NCOL > len(buf):
        return None
    v = [struct.unpack_from("<f", buf, off + 4 * i)[0] for i in range(EIS_NCOL)]
    for x in v:
        if x != x:
            return None
    zre, zim, zabs, freq, phase, zimabs, emv = v
    if not (1e-6 < abs(zre) < 1e5):
        return None
    if abs(zim) > 1e7 or abs(zimabs) > 1e7:
        return None
    if not (0 < freq < 1e8):
        return None
    if not (-360 < phase < 360):
        return None
    zt = math.hypot(zre, zim)
    if zt <= 0 or abs(zabs - zt) > 0.01 * zt:
        return None
    ph = math.degrees(math.atan2(zim, zre))
    if abs(ph - phase) > 0.5:
        return None
    if abs(zimabs - abs(zim)) > 0.02 * max(abs(zim), 1e-12):
        return None
    return v


def eis_chain(buf, start, maxpts=4000):
    """按 584/101 页几何从 start 串点"""
    pts = []
    k = 0
    while len(pts) < maxpts:
        off = start + (k // 2) * EIS_PAGE + (EIS_SLOT2 if k % 2 else 0)
        v = eis_record(buf, off)
        if not v:
            break
        pts.append((off, v))
        k += 1
    return pts


def find_eis_chains(buf, lo=0x400, want_decreasing=True):
    """扫描找 EIS 点链（逐字节扫：记录可能落在非 4 字节相位上）"""
    hi = len(buf) - 4 * EIS_NCOL
    chains = []
    seen = set()
    off = lo
    while off < hi:
        if off in seen or not eis_record(buf, off):
            off += 1
            continue
        pts = eis_chain(buf, off)
        if len(pts) < 5:
            off += 1
            continue
        freqs = [p[1][3] for p in pts]
        drops = sum(1 for a, b in zip(freqs, freqs[1:]) if b < a)
        if want_decreasing and drops < 0.8 * (len(freqs) - 1):
            off += 1
            continue
        for o, _ in pts:
            seen.add(o)
        chains.append(pts)
        off += 1
    chains.sort(key=len, reverse=True)
    return chains


def merge_chains(chains, minpts=5):
    """多条 EIS 链合并成一条曲线（按频率降序、去重偏移）"""
    pts, seen = [], set()
    for ch in chains:
        if len(ch) < minpts:
            continue
        for off, v in ch:
            if off in seen:
                continue
            seen.add(off)
            pts.append((off, v))
    pts.sort(key=lambda pv: -abs(pv[1][3]))
    return pts


def _eis_summary(buf):
    merged = merge_chains(find_eis_chains(buf))
    if not merged or len(merged) < 4:
        return None
    fs = [v[3] for _, v in merged]
    zr = [v[0] for _, v in merged]
    return {"kind": "eis", "points": len(merged),
            "freq_min": min(fs), "freq_max": max(fs),
            "zre_min": min(zr), "zre_max": max(zr),
            "columns": EIS_COLUMNS,
            "rows": [[v[0], v[1], v[2], v[3], v[4], v[5], v[6]] for _, v in merged]}


# ============================ LSV / CV ============================
LSV_PER = 142
LSV_COLUMNS = ["Time_s", "E_mV", "I_mA", "IRand_mA", "Ewe_mV"]


def lsv_record(buf, off):
    """读一条 LSV/CV 记录（2 个点）并自检；返回 10 个值（A 组 5 + B 组 5），否则 None"""
    if off < 0 or off + 98 > len(buf):
        return None
    f = lambda k: struct.unpack_from("<f", buf, off + k)[0]
    tA, tB = f(0), f(25)
    eA, eB = f(50), f(54)
    wA, wB = f(58), f(62)
    iA, iB = f(66), f(70)
    rA, rB = f(90), f(94)
    vals = [tA, tB, eA, eB, wA, wB, iA, iB, rA, rB]
    for x in vals:
        if x != x:
            return None
    if not (0 < tA < 1e6 and 0 < tB < 1e6):
        return None
    if not (1e-6 < (tB - tA) < 100):
        return None
    if not (abs(eA) < 2e4 and abs(eB) < 2e4 and abs(eA - eB) < 200):
        return None
    if not (abs(wA - eA) < 200 and abs(wB - eB) < 200):
        return None
    if not (0 < abs(rA) < 1e3 and 0 < abs(rB) < 1e3):
        return None
    if abs(iA) > 1e3 or abs(iB) > 1e3:
        return None
    return [tA, eA, iA, rA, wA, tB, eB, iB, rB, wB]


def scan_lsv(buf, lo=0x400, maxpts=200000):
    """扫 LSV/CV 记录链：142 字节周期 + 断点重同步（跨段块间隔）"""
    n = len(buf)
    chains = []
    i = lo
    while i < n - 100:
        if not lsv_record(buf, i):
            i += 1
            continue
        def _cont(prev, cur):
            d1 = prev[5] - prev[0]
            d2 = cur[5] - cur[0]
            if not (1e-6 < d2 < 1e4):
                return False
            return 0.01 * d1 < d2 < 100 * d1

        pts = [lsv_record(buf, i)]
        j = i + LSV_PER
        while len(pts) < maxpts:
            r2 = lsv_record(buf, j)
            if r2 and _cont(pts[-1], r2):
                pts.append(r2)
                j += LSV_PER
                continue
            nxt = None
            for k in range(j + 1, min(j + 3000, n - 100)):
                rk = lsv_record(buf, k)
                if rk and _cont(pts[-1], rk):
                    nxt = k
                    break
            if nxt is None:
                break
            pts.append(lsv_record(buf, nxt))
            j = nxt + LSV_PER
        if len(pts) >= 3:
            chains.append((i, pts))
            i = j
        else:
            i += 1
    chains.sort(key=lambda c: -len(c[1]))
    return chains


def _lsv_summary(buf):
    chains = scan_lsv(buf)
    if not chains:
        return None
    pts = []
    for _, recs in chains:
        pts += recs
    if len(pts) < 10:
        return None
    pts.sort(key=lambda q: q[0])
    rows = []
    for q in pts:
        rows.append(q[:5])
        rows.append(q[5:10])
    # 边界过滤：丢掉时间异常（denormal / 非正）的错位记录
    rows = [r for r in rows if r[0] >= 1e-6]
    ts = [r[0] for r in rows]
    es = [r[1] for r in rows]
    inc = sum(1 for a, b in zip(ts, ts[1:]) if b >= a)
    if inc < 0.9 * (len(ts) - 1):
        return None
    if max(es) - min(es) > 2e4:
        return None
    return {"kind": "lsv", "points": len(rows),
            "t_min": min(ts), "t_max": max(ts),
            "e_min": min(es), "e_max": max(es),
            "columns": LSV_COLUMNS,
            "rows": rows}


# ============================ 对外入口 ============================

def decode_summary(path, max_bytes=0):
    """收件员入口：先试 LSV/CV，再试 EIS；各自独立容错。max_bytes=0 → 按文件大小自适应"""
    try:
        size = os.path.getsize(path)
        cap = max_bytes or min(max(size, 1500000), 12000000)
        with open(path, "rb") as fh:
            buf = fh.read(cap)
    except Exception:
        return None
    if len(buf) < 0x1000:
        return None
    for fn in (_lsv_summary, _eis_summary):
        try:
            r = fn(buf)
        except Exception:
            r = None
        if r:
            return r
    return None


def brief(s):
    """一行摘要文案（收件员用）"""
    if not s:
        return ""
    if s.get("kind") == "eis":
        return ("EIS：%d 点，%.4g~%.4g Hz，Zre %.4g~%.4g Ω"
                % (s["points"], s["freq_min"], s["freq_max"], s["zre_min"], s["zre_max"]))
    return ("LSV/CV：%d 点，t %.4g~%.4g s，E %.4g~%.4g mV"
            % (s["points"], s["t_min"], s["t_max"], s["e_min"], s["e_max"]))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    json_out = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    out = []
    for a in args:
        paths = []
        if os.path.isdir(a):
            paths = [os.path.join(a, f) for f in sorted(os.listdir(a)) if f.lower().endswith(".dat")]
        elif os.path.isfile(a):
            paths = [a]
        for p in paths:
            s = decode_summary(p)
            if not s:
                print("%-30s → 未识别（无已知结构）" % os.path.basename(p))
                continue
            print("%-30s → %s" % (os.path.basename(p), brief(s)))
            out.append({"file": p, "kind": s["kind"], "points": s["points"],
                        "columns": s["columns"], "rows": s["rows"]})
    if json_out and out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False)
        print("已写出 %s（%d 个文件）" % (json_out, len(out)))


if __name__ == "__main__":
    main()
