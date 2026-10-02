#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告链路的离线端到端测试：合成数据 → report_build → 任务包 → render → HTML。

为什么要有这一套：这条链路出过两个**从结果上看不出来**的错 ——
① 平均电压图生成了、但渲染模板里没有它的图位，于是报告里整张图消失
   （用户对着报告才发现"没有平均电压图"）；
② report_build 整目录重建时把 Agent 已写的 45 段正文一起删了。
两个都是"跑得通、日志全绿"的错，所以必须钉成断言。

    python selftest_report.py
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = HERE / "_report_test"
DATA = TMP / "data"
OUT = TMP / "任务包"
SCAFFOLD = HERE / "报告任务_脚手架"
BAR = "=" * 66

# 期望的六张图（文件名 = 渲染模板 FIGURE:1..6 必须对上的那几张）
WANT_FIGS = [
    "对比_长循环_STD_vs_1%Q④.svg",
    "对比_首效VQ_STD_vs_1%Q④.svg",
    "对比_平均电压_STD_vs_1%Q④.svg",
    "对比_倍率_STD_vs_1%Q④.svg",
    "dQdV_STD.svg",
    "dQdV_1%Q④.svg",
]
BLOCKS = ("S11", "S12", "S31A", "S31B", "S31C", "S32A", "S32B", "S32C", "S32D",
          "S33A", "S33B", "S34A", "S34B", "S35A", "S35B", "S35C",
          "S41A", "S41B", "S41C", "S41D",
          "S42INTRO", "S42A", "S42B", "S42C", "S42D", "S42E",
          "S43A", "S43B", "S43C", "S43D",
          "S51A", "S51B", "S51C", "S52A", "S52B", "S52C")


def make_fake_live_data() -> None:
    """两颗合成电芯 × 4 圈（明细层，cycleid 从 0 起 —— 比人话圈号小 1）。"""
    if TMP.exists():
        shutil.rmtree(TMP)
    for ch, tag, chg, dis in (("27-188-10-1", "263", 0.0010, 0.00090),
                              ("27-188-10-2", "264", 0.0011, 0.00095)):
        d = DATA / "live" / f"channel={ch}" / f"testid={tag}"
        d.mkdir(parents=True)
        rows = ["seqid,stepid,cycleid,steptype,testtime,atime,volt,curr,cap,eng,CPU,dbc"]
        n = 0
        for cyc in range(4):
            # 容量逐圈略降（第 4 圈故意砍半 —— 模拟"进行中、末圈被截断"）
            scale = 1.0 - 0.02 * cyc
            if cyc == 3:
                scale = 0.4
            for st, cap0, v0, v1, cur in (("cc", chg * scale, 1.0, 3.3, 1.42e-5),
                                          ("dc", dis * scale, 3.3, 1.0, -1.42e-5)):
                for i in range(6):
                    n += 1
                    rows.append(
                        f"{n},{cyc * 2 + (1 if st == 'cc' else 2)},{cyc},{st},"
                        f"{i * 1000},{cyc},{v0 + (v1 - v0) * i / 5:.4f},{cur},"
                        f"{cap0 * i / 5:.6f},{cap0 * i / 5 * 2.5:.6f},34.9,[]")
        (d / "detail.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (d / "progress.json").write_text(
            json.dumps({"testid": tag, "rows_collected": n + 1, "total_on_device": n + 1,
                        "updated_at": "2026-10-02T02:00:29"}, ensure_ascii=False),
            encoding="utf-8")


def build() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "report_build.py",
         "--ch-a", "10-1", "--label-a", "STD",
         "--ch-b", "10-2", "--label-b", "1%Q④",
         "--mass-mg", "2", "--data-dir", str(DATA),
         "--out", str(OUT), "--scaffold-dir", str(SCAFFOLD)],
        cwd=str(HERE), capture_output=True, text=True, encoding="utf-8")


class Checker:
    def __init__(self):
        self.passed, self.failed = 0, []

    def ok(self, cond, label, extra=""):
        if cond:
            self.passed += 1
            print("  PASS  " + label)
        else:
            self.failed.append(label)
            print("  FAIL  " + label + (f"   <- {extra}" if extra else ""))

    def report(self) -> int:
        print()
        if self.failed:
            print(f"通过 {self.passed} 项，失败 {len(self.failed)} 项")
            for f in self.failed:
                print("  失败：" + f)
            return 1
        print(f"通过 {self.passed} 项，失败 0 项")
        return 0



def test_validator(c: Checker) -> None:
    """程序侧校验器要抓得住三类"数字全对、结论却错"的问题。"""
    sys.path.insert(0, str(HERE))
    import validate_paragraphs as V

    data = {"groups": {"A": {"avg_v_trend": "下降", "dV_trend": "减小",
                             "peak_gap_trend": "减小",
                             "avg_v_first": 0.2231, "avg_v_last": 0.1908}},
            "figure_ref": "图 4", "table_ref": "表 7",
            "instructions": ["★ 效率数值由程序给出、模型只引用，不得据此判断电池好坏"],
            "length_range": [150, 250]}
    ctx = {"mass": {"mass_mg": 2}}

    ok_hard, _ = V.check_block("S32C", "平均放电电压由 223.1 mV 下降至 190.8 mV。", data, ctx)
    c.ok(not ok_hard, "正常段落 → 通过", str(ok_hard))

    hard, _ = V.check_block("S32C", "放电比容量为 999.99 mAh·g⁻¹。", data, ctx)
    c.ok(any("不可溯源" in h for h in hard), "★ 编造的数值 → 判失败", str(hard))

    hard, _ = V.check_block("S32B", "效率数值由程序给出、模型只引用，不得据此判断电池好坏。",
                            data, ctx)
    c.ok(any("instructions" in h for h in hard),
         "★ 把数据包里给模型的约束抄进正文 → 判失败（S31C 实测那次）", str(hard))

    hard, _ = V.check_block("S32D", "两组 ΔV 随循环增大并趋同。", data, ctx)
    c.ok(any("方向与数据相反" in h for h in hard),
         "★ 数据说减小、正文写增大 → 判失败（S34A 实测那次）", str(hard))

    hard, _ = V.check_block("S32C", "两组平均放电电压见图 6。", data, ctx)
    c.ok(any("图号引用越界" in h for h in hard), "★ 引用了数据包没给的图号 → 判失败", str(hard))

    hard, _ = V.check_block("S32C", "本段使用了显著提升的措辞。", data, ctx)
    c.ok(any("禁用词" in h for h in hard), "★ 禁用词 → 判失败", str(hard))

    hard, _ = V.check_block("S41A", "两组基本相当。", data, ctx)
    c.ok(any("n=1" in h for h in hard), "★ 结论段缺「波动范围/复测」→ 判失败", str(hard))

    hard, _ = V.check_block("S42A", "添加剂构建了稳定 SEI。", data, ctx)
    c.ok(any("机制措辞" in h for h in hard), "★ 机制段没写「可能/推测」→ 判失败", str(hard))

    _, warn = V.check_block("S32C", "短句。", data, ctx)
    c.ok(any("长度" in w for w in warn), "长度超区间 → 只告警不判失败", str(warn))
    c.ok("【修正任务】" in V.repair_prompt("S32C", ["数值不可溯源：999.99"])
         and "999.99" in V.repair_prompt("S32C", ["数值不可溯源：999.99"]),
         "★ 失败能生成 5-B 的回灌修复提示")


def test_scaffold_in_sync(c: Checker) -> None:
    """脚手架里的 render.py 必须与主代码**逐字节一致**。

    为什么：report_build 是把整个 `报告任务_脚手架/` 拷进任务包的，所以那份 render.py
    才是真正跑起来的那个。今天一直是"改完手工 cp 过去"，一次忘掉就会出现
    "本地测试全绿、盒子上跑的是旧渲染脚本" —— 这种错从结果上看不出来。
    """
    a = (HERE / "render.py").read_bytes()
    b = (SCAFFOLD / "render.py").read_bytes()
    c.ok(a == b, "★ 脚手架 render.py 与主代码逐字节一致（跑一句 cp render.py 报告任务_脚手架/）",
         f"{len(a)} vs {len(b)} 字节")


def main() -> int:
    c = Checker()
    test_scaffold_in_sync(c)
    test_validator(c)

    print(BAR)
    print("① 合成数据 → report_build 重建任务包")
    print(BAR)
    make_fake_live_data()
    r = build()
    c.ok(r.returncode == 0, "report_build 正常退出", (r.stderr or r.stdout)[-800:])
    if r.returncode != 0:
        return c.report()

    print()
    print(BAR)
    print("② 六张图与图位一一对上（平均电压图消失过，就是这里没测）")
    print(BAR)
    for name in WANT_FIGS:
        p = OUT / "图" / name
        c.ok(p.exists() and p.stat().st_size > 800,
             f"图存在且非空：{name}", f"{p.stat().st_size if p.exists() else '缺失'} 字节")
    tmpl = (SCAFFOLD / "报告渲染模板.html").read_text(encoding="utf-8")
    slots = sorted(set(re.findall(r"<!--FIGURE:(\d+)-->", tmpl)), key=int)
    c.ok(slots == ["1", "2", "3", "4", "5", "6"],
         "★ 模板有 FIGURE:1~6 六个图位", str(slots))
    # ★ 章节与图号顺序必须与《协会第二版报告》一致：
    #   3.1 长循环 → 3.2 倍率 → 3.3 首圈库伦效率 → 3.4 平均电压 →
    #   3.5 dQ/dV → 3.6 横向对比（图 1~6 一一对应）
    body = tmpl.split("sec-3-1-长循环性能分析")[1].split("4.1  核心电化学性能结论")[0]
    seq = [a.strip() if a else "FIG" + b for a, b in
           re.findall(r"<h2[^>]*>([^<]+)</h2>|<!--FIGURE:(\d+)-->", body)
           if a or b]
    c.ok(seq == ["FIG1", "3.2  倍率性能分析", "FIG2", "3.3  首圈库伦效率", "FIG3",
                 "3.4  平均电压曲线分析", "FIG4", "3.5  dQ/dV 曲线分析", "FIG5",
                 "FIG6", "3.6  多组样品横向对比分析"],
         "★ 第 3 章小节与图号顺序与协会第二版报告一致", str(seq))
    src = (HERE / "render.py").read_text(encoding="utf-8")
    for pre in ("对比_长循环_", "对比_首效VQ_", "对比_平均电压_", "对比_倍率_", "dQdV_"):
        c.ok(pre in src, f"render.py 里有图名片段：{pre}")
    # 真正的交叉验证在第 ⑤ 节：render 找不到图会写成"图 N 未生成"，
    # 那时 <figure> 就不是 6 个了 —— 比名字对得上更硬。

    print()
    print(BAR)
    print("③ 数据契约：28 个块 + 全局上下文 + 动态数据截止时间")
    print(BAR)
    got = sorted(p.stem for p in (OUT / "block_data").glob("*.json"))
    c.ok(got == sorted(BLOCKS), f"block_data 恰好 {len(BLOCKS)} 块（含 4.2 机制段）",
         f"{len(got)} 块")
    ctx = json.loads((OUT / "global_context.json").read_text(encoding="utf-8-sig"))
    c.ok(ctx["variable"]["control_short"] == "STD"
         and ctx["variable"]["experimental_short"] == "1%Q④",
         "★ 样品名按 --label-a/-b 进上下文（报告封面/图例都用它）",
         str(ctx["variable"]))
    c.ok(bool(ctx.get("data_cutoff")), "数据截止时间写进上下文", str(ctx.get("data_cutoff")))
    ts = (OUT / "任务说明.md").read_text(encoding="utf-8-sig")
    c.ok("2026-10-01 02:00" not in ts and "数据截至" in ts,
         "★ 任务说明里的日期被换成动态值（不再是写死的 10-01）")
    win = ctx["test_conditions"]["voltage_window"]
    c.ok(win and "3.31" not in win,
         "★ 电压窗口从数据现算（写死的 3.31 V 是别的实验的窗口，会张冠李戴）", str(win))
    c.ok(win and win.startswith("0.0") or win and win.startswith("1."),
         "电压窗口形如「x.xx~x.xx V」", str(win))
    s34a = json.loads((OUT / "block_data" / "S34A.json").read_text(encoding="utf-8-sig"))
    grp = next(iter(s34a["groups"].values()))
    c.ok(grp.get("peak_positions_by_cycle"),
         "★ S34A 数据包给了逐圈峰位（正文要写「峰间距随圈数演变」）", str(list(grp)))
    c.ok(any("尚未到达" in n for n in s34a["notes"]),
         "★ S34A 的 notes 里留着可引用的缺圈事实", str(s34a["notes"]))
    c.ok(all("不得" not in n for n in s34a["notes"]),
         "★ 约束句（「不得外推」）不在 notes 里 —— 已被拆到 instructions",
         str(s34a["notes"]))
    c.ok(any("不得" in i for i in s34a.get("instructions") or []),
         "★ 约束句落在 instructions 里（正文不得引用）", str(s34a.get("instructions")))
    dq = json.loads((OUT / "block_data" / "S31A.json").read_text(encoding="utf-8-sig"))
    c.ok(len(dq["groups"]) == 2, "S31A 两组数据都在", str(list(dq["groups"])))

    # ---- 长循环/倍率/平均电压三张图的左轴必须是**放电比容量** ----
    # ★ 实测的 bug：report_build 忘了把质量传下去，图上左轴写成"容量 (mAh)" ——
    #   而"比容量"正是用户点名要的口径（不同载量的电池只有比容量能比）。
    for nm in ("对比_长循环_STD_vs_1%Q④.svg", "对比_倍率_STD_vs_1%Q④.svg"):
        svg = (OUT / "图" / nm).read_text(encoding="utf-8")
        c.ok("放电比容量" in svg, f"★ {nm} 左轴是放电比容量 (mAh g⁻¹)")
        c.ok("活性物质质量" in svg, f"★ {nm} 图注写明质量是暂定值")
    avgv_svg = (OUT / "图" / "对比_平均电压_STD_vs_1%Q④.svg").read_text(encoding="utf-8")
    c.ok("平均电压 (V)" in avgv_svg and "活性物质质量" in avgv_svg,
         "★ 平均电压图左轴是电压、图注带质量说明")
    rate_svg = (OUT / "图" / "对比_长循环_STD_vs_1%Q④.svg").read_text(encoding="utf-8")
    c.ok("已从图上剔除" in rate_svg,
         "★ 图注写明「末圈未走完、已剔除」（末圈画上去就是个掉到 0 的假点）")

    # ---- 图上不许有"进行中、未走完"的末圈 ----
    import figures as _F
    cell1 = _F.load_detail(DATA / "live" / "channel=27-188-10-1" / "testid=263"
                           / "detail.csv")
    sys.path.insert(0, str(HERE))
    import report_pack as _RP
    cc = _RP.load_cell(DATA, "27-188-10-1", 2.0)
    c.ok(len(cc["cs_plot"]) < len(cc["cs"]),
         "★ 画图用的圈比原始圈少（剔掉了没走完的末圈）",
         f"{len(cc['cs_plot'])} < {len(cc['cs'])}")
    c.ok(cc["dropped_cycles"] == [max(x["cycle"] for x in cc["cs"])],
         "剔掉的正是最后一圈", str(cc["dropped_cycles"]))
    c.ok(cell1 and cc["v_min"] is not None and cc["v_max"] is not None,
         "load_cell 给出了实测电压窗口", f"{cc['v_min']}~{cc['v_max']}")

    print()
    print(BAR)
    print("④ Agent 写好的段落不能被重建冲掉（实测丢过 45 段）")
    print(BAR)
    # 模拟 Agent：每个块写一段（S42* 是降级告警行）
    para = OUT / "段落"
    para.mkdir(exist_ok=True)
    for bid in BLOCKS:
        (para / f"{bid}.txt").write_text(f"{bid} 的合成段落" if not bid.startswith("S42")
                                         else "【本段未通过自动校验：变量未提供】",
                                         encoding="utf-8")
    r = build()
    c.ok(r.returncode == 0, "带着段落再重建一次，正常退出", (r.stderr or "")[-400:])
    kept = sorted(p.stem for p in (OUT / "段落").glob("*.txt"))
    c.ok(kept == sorted(BLOCKS), f"★ 重建后 {len(BLOCKS)} 段正文全部保住（不再被 rmtree 冲掉）",
         f"只剩 {len(kept)} 段")

    print()
    print(BAR)
    print("⑤ render.py 组装 HTML：图都嵌进去、没有残留占位符")
    print(BAR)
    rr = subprocess.run([sys.executable, "render.py"], cwd=str(OUT),
                        capture_output=True, text=True, encoding="utf-8")
    c.ok(rr.returncode == 0, "render 正常退出", (rr.stderr or "")[-400:])
    html_p = OUT / "SOH分析报告.html"
    c.ok(html_p.exists(), "产出 SOH分析报告.html")
    if not html_p.exists():
        return c.report()
    html = html_p.read_text(encoding="utf-8")
    c.ok(html.count("<figure>") == 6, f"★ 六张图全部内嵌（实得 {html.count('<figure>')}）")
    c.ok(html.count("data:image/svg+xml;base64,") == 6, "六张图都是 base64 内嵌（不依赖外部文件）")
    c.ok("充放电平均电压随循环的变化" in html,
         "★ 平均电压图的图注在报告里（这张图曾经整张消失）")
    left = [x for x in ("<!--BLOCK:", "<!--FIGURE:", "<!--TABLE:", "{{DOC_", "{{TOC_")
            if x in html]
    c.ok(not left, "★ 没有残留的占位符", str(left))
    c.ok("STD" in html and "1%Q④" in html, "样品名进了报告")
    c.ok("S31A 的合成段落" in html, "Agent 写的段落进了报告")

    # dQ/dV 逐圈图：圈号说明走图注，**不许**用图内说明框（说明框固定左上角，
    # 会压住 dQ/dV 的峰 —— 渲染出来一眼可见）
    import figures as _F
    for nm in ("dQdV_STD.svg", "dQdV_1%Q④.svg"):
        svg = (OUT / "图" / nm).read_text(encoding="utf-8")
        c.ok(_F.C_NOTE_BG not in svg, f"★ {nm} 图内没有说明框（不会压住峰）")
        c.ok("已画圈" in svg and "尚未到达的圈" in svg,
             f"★ {nm} 把「已画/未到」的圈号写在图注里（画布下方，谁都挡不住）")
        c.ok("符号口径" in svg, f"{nm} 注明了正负峰的口径（还原为正）")

    print()
    print(BAR)
    print("⑥ 段落缺失必须显式告警（不能静默留空）")
    print(BAR)
    (OUT / "段落" / "S31A.txt").unlink()
    subprocess.run([sys.executable, "render.py"], cwd=str(OUT),
                   capture_output=True, text=True, encoding="utf-8")
    html2 = (OUT / "SOH分析报告.html").read_text(encoding="utf-8")
    c.ok("段落文件缺失" in html2, "★ 缺段落 → 报告里写明「未通过自动校验、需人工复核」")

    print()
    print(BAR)
    print("⑦ 指定刻度：1/10/50/100/200/300（用户点名的圈号）")
    print(BAR)
    import figures as F
    cs = [dict(cycle=i, chg=1.0, dis=0.9, ce=90.0, ee=90.0, chg_eng=2.0,
               dis_eng=1.8, retention=100.0, dcir_mohm=None) for i in range(1, 6)]
    fig = F.chart_long_cycle("t", [{"label": "A", "cs": cs}])
    fig.set_xticks([1, 10, 50, 100, 200, 300], span=True)
    svg = fig.svg()
    c.ok(all(f">{t}</text>" in svg for t in ("1", "10", "50", "100", "200", "300")),
         "六个刻度全在图上（span=True 把轴撑到 300 圈）")

    shutil.rmtree(TMP, ignore_errors=True)
    print()
    print(BAR)
    print("验证：")
    print(BAR)
    return c.report()


if __name__ == "__main__":
    raise SystemExit(main())
