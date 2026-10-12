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

# 期望的五张图（文件名 = 渲染模板 FIGURE:1..5 必须对上的那几张；图 6 已取消）
WANT_FIGS = [
    "对比_长循环_STD_vs_1%Q④.svg",
    "对比_首效VQ_STD_vs_1%Q④.svg",
    "对比_平均电压_STD_vs_1%Q④.svg",
    "对比_倍率_STD_vs_1%Q④.svg",
    # ★ 2026-10-12：dQ/dV 又改回「每样品一张」（图 5、6）—— 图 6 回归，共 6 张
    "dQdV_STD.svg",
    "dQdV_1%Q④.svg",
]
BLOCKS = ("S11", "S12", "S31A", "S31B", "S31C", "S32A", "S32B", "S32C", "S32D",
          "S33A", "S33B", "S34A", "S34B", "S34C", "S35A", "S35B", "S35C",
          "S41A", "S41B", "S41C", "S41D",
          "S42INTRO", "S42A", "S42B", "S42C", "S42D", "S42E",
          "S43A", "S43B", "S43C", "S43D",
          "S51A", "S51B", "S51C", "S52A", "S52B", "S52C", "S52D")


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
        # 另造一份"已结束的正式数据集"（同通道、更早的日期）：用来测 --source official
        od = DATA / f"channel={ch}" / f"testid={tag[:-2]}0"
        od.mkdir(parents=True, exist_ok=True)
        (od / "detail.csv").write_text(chr(10).join(rows) + chr(10), encoding="utf-8")
        (od / "progress.json").write_text(
            json.dumps({"testid": tag[:-2] + "0", "rows_collected": n, "total_on_device": n,
                        "updated_at": "2026-09-20T02:00:00" if tag.endswith("3")
                        else "2026-09-25T02:00:00"}, ensure_ascii=False), encoding="utf-8")
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
    sys.path.insert(0, str(SCAFFOLD))      # 报告脚本的唯一出处是脚手架（2026-10-04 起）
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



def test_placeholder_gate(c: Checker) -> None:
    """占位闸门：判据只许有一处，且「验不了」绝不能当成「通过」。

    这一套源自 2026-10-10 的实测事故：闸门自己 `grep '数据不足'` 判占位，结果两头都错 ——
    真占位【该项数据未提供】里没有那四个字（**漏检**），模型正文"现有数据**不足以**判定"
    却命中（**误报 exit 1**，报告被判"先别拿给人看"）。
    """
    sys.path.insert(0, str(SCAFFOLD))
    import validate_paragraphs as V

    # ---- 判据本身：按结构（整行【…】），不再按关键词猜 ----
    prose = "现有数据不足以判定实验变量带来了显著的性能提升，需以平行样品复测确认。"
    c.ok(not V.is_placeholder(prose),
         "★ 正文里的「现有数据不足以判定」**不算**占位（实测误报就是这一句）")
    c.ok(V.is_placeholder("【该项数据未提供】"), "程序占位【该项数据未提供】算占位")
    c.ok(V.is_placeholder("【图 2 未生成：对比_倍率_x.svg 缺失，需人工复核】"),
         "★ 渲染期的【图 N 未生成…】也算（段落扫描原本看不见它）")
    c.ok(V.is_placeholder("平均电压与极化表现分化" + chr(10) + "【该项数据未提供】"),
         "★ 标题行 + 占位行 也算（整行是【…】即认）")
    long_prose = (prose + "两组的线性拟合衰减速率均为（该项数据未提供）。"
                  + "该判断需以平行样品复测确认，并对照个体波动范围。")
    c.ok(not V.is_placeholder(long_prose),
         "★ 长正文里顺带提一句「（该项数据未提供）」不算占位"
         "（否则 S31C 那种既缺数据又抄指令的段落会整段溜过）")

    # ---- --scan 的对外契约：.sh 只认这几行，取不到就是「没验成」 ----
    def run_scan(pkg: Path) -> tuple[int, str]:
        r = subprocess.run([sys.executable, "validate_paragraphs.py", "--scan",
                            "--package", str(pkg)],
                           cwd=str(SCAFFOLD), capture_output=True, text=True,
                           encoding="utf-8")
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    pkg = TMP / "scan_pkg"
    shutil.rmtree(pkg, ignore_errors=True)
    (pkg / "段落").mkdir(parents=True)
    (pkg / "block_data").mkdir()
    (pkg / "global_context.json").write_text("{}", encoding="utf-8")
    (pkg / "段落" / "S35C.txt").write_text(prose, encoding="utf-8")
    rc, out = run_scan(pkg)
    c.ok(rc == 0 and "PLACEHOLDERS=0" in out and "HARD=0" in out,
         "干净任务包 → PLACEHOLDERS=0 且 HARD=0（S35C 那种正文不该再被误报）", out.strip())

    (pkg / "段落" / "S42A.txt").write_text("【该项数据未提供】", encoding="utf-8")
    rc, out = run_scan(pkg)
    c.ok("PLACEHOLDERS=1" in out and "PLACEHOLDER_BLOCKS=S42A" in out,
         "★ 真占位能被抓到，并报出是哪一段", out.strip())
    (pkg / "段落" / "S42A.txt").unlink()

    # 缺 block_data / global_context → HARD=? ：.sh 必须据此拒绝，而不是当成 0
    (pkg / "global_context.json").unlink()
    rc, out = run_scan(pkg)
    c.ok("HARD=?" in out, "★ 缺 global_context → HARD=?（让闸门能区分「通过」与「没验成」）",
         out.strip())

    # 连 段落/ 都没有 → 不给任何哨兵行（.sh 靠这个判定失败）
    empty = TMP / "scan_empty"
    shutil.rmtree(empty, ignore_errors=True)
    empty.mkdir(parents=True)
    rc, out = run_scan(empty)
    c.ok(rc != 0 and "PLACEHOLDERS=" not in out,
         "★ 没有 段落/ → 非零退出且**不给哨兵行**（没验成 != 通过）", f"rc={rc} {out.strip()}")

    # ---- 闸门脚本本身：不许再自己 grep 中文判据 ----
    sh_src = (SCAFFOLD / "换通道出报告.sh").read_text(encoding="utf-8")
    c.ok("validate_paragraphs.py --scan" in sh_src, "★ 闸门改问 --scan（判据唯一出处）")
    c.ok("grep -l '数据不足'" not in sh_src,
         "★ 闸门里不再残留 `grep '数据不足'`（那是误报+漏检的根源）")
    c.ok('if [ -z "${ph_cnt:-}" ]' in sh_src,
         "★ 闸门要求必须拿到哨兵行：拿不到就报错，而不是默认通过")
    c.ok('[ "$hard" = "?" ]' in sh_src,
         "★ 闸门把 HARD=? 当成「没验成」，直接卡住（不当成 0）")
    c.ok("成品自检" in sh_src and "【[^】]*】" in sh_src,
         "★ 闸门还有一道渲染后的成品自检（扫成品 HTML 里的【…】—— 段落扫描看不见图没生成）")
    # ---- AI 段的"不许写测试数值"只在 ai_written=True 时生效 ----
    # split_batch 曾经漏传这个参数（2026-10-10 实测）：它的"即时校验"说 21 段全过，
    # 最后一道闸门却判 S42E 硬性不合格 —— 同一个 100%。后果是"不合格自动重写"的回灌
    # 循环对这类违规**从不触发**，模型没被要求改，于是每次都在最后翻车。
    _t = "以上推断需以平行样品复测确认，稳态 CE 贴近 100% 等尚非直接证据（属推测）。"
    _d = {"figure_ref": "图 5", "number": 100.0}
    _h_ai, _ = V.check_block("S42E", _t, _d, {}, ai_written=True)
    _h_no, _ = V.check_block("S42E", _t, _d, {}, ai_written=False)
    c.ok(any("测试数值" in h for h in _h_ai),
         "★ ai_written=True：抓出 AI 段里的 100", str(_h_ai))
    c.ok(not any("测试数值" in h for h in _h_no),
         "★ ai_written=False：这条规则不启用 → 同一个 100% 被放过（漏传就是这样静默通过的）",
         str(_h_no))
    sb = (SCAFFOLD / "split_batch.py").read_text(encoding="utf-8")
    c.ok("ai_written=bid in V.AI_BLOCKS" in sb,
         "★ split_batch 的即时校验也带 ai_written（与 check_package 同判据）")

    # ---- 内层脚本不许自己当判官 ----
    inner = (SCAFFOLD / "出报告.sh").read_text(encoding="utf-8")
    _val = next((ln for ln in inner.splitlines() if "validate_paragraphs.py" in ln), "")
    c.ok(_val and "|| exit" not in _val,
         "★ 内层校验失败时**不中断**（判定权归外层闸门：中断会让成品根本不渲染）",
         _val.strip())


def test_compose_missing_rate_group(c: Checker) -> None:
    """末档少一圈（一颗样品的 keep/recover 为 None）时，3.2/4.1.2 模板不许崩。

    真机踩过：`{None:.1f}` 当场 TypeError，整条流水线在"建包"就断了 ——
    本地假数据的末档恰好都跑齐，所以原来的自检抓不到。
    """
    sys.path.insert(0, str(SCAFFOLD))
    import compose_paragraphs as CP
    g = {"variable": {"control_short": "实验样", "experimental_short": "对比样"},
         "test_conditions": {"rate_program": "0.2C→5C→0.2C，每档 6 圈、共 13 档 78 圈"}}
    d = {"rate_stats": {"pairs": ["实验样", "对比样"],
                        "steps": [{"label": "0.2C", "a": 271.85, "b": 278.74, "diff": -6.89},
                                  {"label": "5C", "a": 205.90, "b": 210.90, "diff": -5.0}],
                        "retention": {"hi_label": "5C", "keep_pct": [75.7, 75.7],
                                      "recover_pct": [97.2, None]}},
         "figure_ref": "图 2", "table_ref": "表 4"}
    for bid, fn in (("S33A", CP.s33a), ("S41B", CP.s41b)):
        try:
            txt = fn(CP.Ctx(d, g))
            c.ok("None" not in txt, f"★ {bid}：缺数时不崩、也不印字面 None", txt[:90])
            if bid == "S33A":      # 这一段的恢复率确实缺一颗 → 必须如实写"未提供"
                c.ok("未提供" in txt, "★ S33A：恢复率缺数写成「（该项数据未提供）」", txt[:90])
        except Exception as exc:      # noqa: BLE001
            c.ok(False, f"★ {bid}：缺数时不许抛异常", f"{type(exc).__name__}: {exc}")


def test_compose(c: Checker) -> None:
    """填空式合成器：**程序填的全篇必须能过自己的校验器**（否则等于把错误写进报告）。

    这条断言的分量在于它是端到端的：合成 → 校验 → 逐条规则，任何模板写错
    （引用了数据里没有的数、方向写反、漏了 n=1 兜底）都会在这里露出来。
    """
    import shutil
    tmp = TMP / "合成包"
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(OUT, tmp)
    sys.path.insert(0, str(SCAFFOLD))      # 报告脚本的唯一出处是脚手架（2026-10-04 起）
    import compose_paragraphs as CP
    import json as _json
    # 用空段落目录，保证全部由合成器产出
    shutil.rmtree(tmp / "段落")
    (tmp / "段落").mkdir()
    n, _ = CP.compose(tmp, "all", None)
    c.ok(n >= 38, f"合成 {n} 段（覆盖全部需要正文的块）")
    import validate_paragraphs as V
    bad = {}
    g = _json.loads((tmp / "global_context.json").read_text(encoding="utf-8-sig"))
    for bid in sorted(CP.TEMPLATES):
        p = tmp / "段落" / f"{bid}.txt"
        bd = tmp / "block_data" / f"{bid}.json"
        data = _json.loads(bd.read_text(encoding="utf-8-sig")) if bd.exists() else {}
        hard, _ = V.check_block(bid, p.read_text(encoding="utf-8-sig"), data, g)
        if hard:
            bad[bid] = hard
    c.ok(not bad, "★ 合成稿 100% 过校验器（模板写错就露馅）", str(bad)[:220])
    txt = (tmp / "段落" / "S31A.txt").read_text(encoding="utf-8-sig")
    c.ok("见图 1" in txt and "见图 图" not in txt, "★ 图号引用不重字（出过「见图 图 1」）", txt[-40:])
    s34 = (tmp / "段落" / "S34A.txt").read_text(encoding="utf-8-sig")
    c.ok("放电分支" in s34 and "充电分支" in s34, "S34A 充放两个分支都写了", s34[:60])



def test_two_report_sets(c: Checker) -> None:
    """两套报告：进行中（每日增量）与正式数据集，各有各的产物名与来源标注。

    为什么要分两套：Agent 曾经拿同通道的**旧正式数据集**（247/244）当"当前电芯"，
    比出"两颗不是平行样品"。两套都出、各自注明来源与截止时间，才不会互相冒充。
    """
    import subprocess as sp
    out2 = TMP / "正式包"
    r = sp.run([sys.executable, "report_build.py",
                "--ch-a", "10-1", "--label-a", "STD",
                "--ch-b", "10-2", "--label-b", "1%Q④",
                "--mass-mg", "2", "--data-dir", str(DATA), "--source", "official",
                "--out", str(out2), "--scaffold-dir", str(SCAFFOLD)],
               cwd=str(HERE), capture_output=True, text=True, encoding="utf-8")
    c.ok(r.returncode == 0, "★ 能用 --source official 建正式数据集的任务包",
         (r.stderr or "")[-300:])
    if r.returncode != 0:
        return
    g = json.loads((out2 / "global_context.json").read_text(encoding="utf-8-sig"))
    c.ok(g.get("data_source") == "official", "上下文标注来源=official", str(g.get("data_source")))
    c.ok("正式数据集" in (g.get("data_source_label") or ""),
         "★ 来源标注写的是「正式数据集」（不是每日增量）", str(g.get("data_source_label")))
    c.ok(bool(g.get("batch_warning")),
         "★ 两份正式数据集采集日期不同 → 明确标注「不是同一次测试、不作横向对比」",
         str(g.get("batch_warning")))
    rr = sp.run([sys.executable, "render.py"], cwd=str(out2),
                capture_output=True, text=True, encoding="utf-8")
    c.ok(rr.returncode == 0, "正式集报告能渲染", (rr.stderr or "")[-200:])
    h = out2 / "SOH分析报告_10-1_vs_10-2-正式数据集.html"
    c.ok(h.exists(), "★ 正式集报告落到**带来源与选择**的文件名（不覆盖进行中那份）")
    if h.exists():
        html = h.read_text(encoding="utf-8")
        c.ok("正式数据集" in html and "不是同一次测试" in html,
             "★ 报告横幅里写明了来源与「非同批」")
        c.ok(not (out2 / "SOH分析报告_10-1_vs_10-2.html").exists(),
             "正式集构建不会同时产出进行中那份（两套各自独立）")
    # 进行中那份的横幅必须写明"每日增量"
    live_html = (OUT / "SOH分析报告_10-1_vs_10-2.html")
    if live_html.exists():
        c.ok("每日增量" in live_html.read_text(encoding="utf-8"),
             "★ 进行中那份的横幅写明来源=每日增量")



def test_docs_match_scripts(c: Checker) -> None:
    """文档里点名的脚本必须真实存在，且不再推荐会毁数据的老命令。

    为什么要这条：同一条流程曾被抄在 5 个文件里，代码一改文档就落后 —— 2026-10-04
    实测"新入口 出报告.sh 在文档里出现 0 次"、而文档还在教 `--mode all`
    （那条命令把 AI 写的 9 段冲回了模板句）。文档与代码的一致性得由测试看着。
    """
    # ★ 2026-10-09：Agent 壳已清（任务说明.md / 批量写作*.md 删除）。报告链路不经过
    #   Agent —— 工作流就是 出报告.sh 那一条命令。_system_prompt.txt 要留：
    #   它是**模型的**系统提示词，由 write_ai_sections.py 读。
    docs = [SCAFFOLD / "_system_prompt.txt"]
    src = (HERE / "make_share.py").read_text(encoding="utf-8")     # 入口页文本在这里生成
    text = chr(10).join(d.read_text(encoding="utf-8") for d in docs if d.exists()) + src
    named = sorted(set(re.findall(r"(?:python3|bash)\s+([A-Za-z_][\w\-]*\.(?:py|sh))", text)))
    c.ok(bool(named), "文档/入口页里点名了脚本", str(named))
    for m in named:
        c.ok((SCAFFOLD / m).exists() or (HERE / m).exists(),
             f"★ 文档点名的脚本真实存在：{m}")

    # 原来这里断言 任务说明.md 的内容（它是 Agent 的工作流出处）。
    # Agent 壳清掉后，流程的唯一出处就是 出报告.sh 本身。
    for extra in (SCAFFOLD / "出报告.sh", SCAFFOLD / "write_ai_sections.py"):
        c.ok(extra.exists(), f"脚手架里有 {extra.name}")


def test_empty_newest_test(c: Checker) -> None:
    """★ 通道换了新测试之后，最新那个可能**一圈都没跑完** —— 不能闷头出一份空报告。

    实测 2026-10-04：10-1 换到测试 282（0 圈），程序取了"最新"就往下走，
    报告全空（图上没点、表里没数），却看着像"报告出来了" —— 从结果上看不出来。
    """
    import report_pack as RP
    tmp = TMP / "_空测试"
    if tmp.exists():
        shutil.rmtree(tmp)
    ch = tmp / "live" / "channel=27-188-10-1"

    old = ch / "testid=263"          # 老测试：一个完整的充放对 → 1 圈
    old.mkdir(parents=True)
    (old / "detail.csv").write_text(
        "seqid,stepid,cycleid,steptype,testtime,atime,volt,curr,cap,eng,CPU,dbc\n"
        "1,1,0,cc,0,2026-10-01 00:00:00,1.0,1.4e-05,0,0,34.9,[]\n"
        "2,1,0,cc,1000,2026-10-01 00:00:01,3.3,1.4e-05,0.001,0.002,34.9,[]\n"
        "3,2,0,dc,0,2026-10-01 00:01:00,3.3,-1.4e-05,0,0,34.9,[]\n"
        "4,2,0,dc,1000,2026-10-01 00:01:01,1.0,-1.4e-05,0.0009,0.0018,34.9,[]\n",
        encoding="utf-8")
    new = ch / "testid=282"          # 新测试：只有表头（刚起、0 圈）
    new.mkdir(parents=True)
    (new / "detail.csv").write_text(
        "seqid,stepid,cycleid,steptype,testtime,atime,volt,curr,cap,eng,CPU,dbc\n",
        encoding="utf-8")
    # 该通道还另有一份"已结束的正式数据集"、测试号不同（244）——
    # ★ 用来钉住一个真实的 bug：live 里明明有 264、却因为在 official 里找不到而误报"没有"
    #   （2026-10-04 实测踩到，原因是"找不到就报错"写在了循环里、被第二个根触发）。
    off = tmp / "channel=27-188-10-1" / "testid=244"
    off.mkdir(parents=True)
    (off / "detail.csv").write_text((old / "detail.csv").read_text(encoding="utf-8"),
                                    encoding="utf-8")

    got = RP.load_cell(tmp, "27-188-10-1", 2.0, source="live")
    c.ok(bool(got.get("error")) and "0 圈" in got.get("error", ""),
         "★ 最新测试一圈未跑完 → 明确报错（不交空报告）",
         str(got.get("error"))[:70])
    ok = RP.load_cell(tmp, "27-188-10-1", 2.0, source="live", testid=263)
    c.ok(not ok.get("error") and ok.get("cs"),
         "★ 显式指定 testid=263 → 正常拿到数据（另一份正式数据集号不同，不该误报）",
         str(ok.get("error")))
    bad = RP.load_cell(tmp, "27-188-10-1", 2.0, source="live", testid=999)
    c.ok(bool(bad.get("error")) and "999" in bad.get("error", ""),
         "指定不存在的 testid → 报错并列出该通道有哪些", str(bad.get("error"))[:70])
    shutil.rmtree(tmp, ignore_errors=True)


def test_bucket_layout_source(c: Checker) -> None:
    """桶布局下 --source 的语义：**状态由 progress.json 决定，不由目录决定**。

    为什么单列一条：2026-10-09 把 --source 的筛选错加在"目录"上，于是
    `--source live` 把真正的桶（在 data/channel=/testid=/ 下）整个排除，
    报告报"找不到数据目录" —— 而老夹具（有 live/ 目录）照样绿，**没抓住**。
    所以这里用**真实的桶布局**钉住这条语义。
    """
    import report_pack as RP
    tmp = TMP / "_桶布局"
    if tmp.exists():
        shutil.rmtree(tmp)
    ch = tmp / "channel=27-188-10-1" / "testid=282"      # 桶直接在 data 根下
    ch.mkdir(parents=True)
    NL = chr(10)
    lines = ["seqid,stepid,cycleid,steptype,testtime,atime,volt,curr,cap,eng,CPU,dbc",
             "1,1,0,cc,0,2026-10-01 00:00:00,1.0,1.4e-05,0,0,34.9,[]",
             "2,1,0,cc,1000,2026-10-01 00:00:01,3.3,1.4e-05,0.001,0.002,34.9,[]",
             "3,2,0,dc,0,2026-10-01 00:01:00,3.3,-1.4e-05,0,0,34.9,[]",
             "4,2,0,dc,1000,2026-10-01 00:01:01,1.0,-1.4e-05,0.0009,0.0018,34.9,[]"]
    (ch / "detail.csv").write_text(NL.join(lines) + NL, encoding="utf-8")

    def write_prog(finished: bool) -> None:
        (ch / "progress.json").write_text(json.dumps(
            {"testid": "282", "rows_collected": 4, "total_on_device": 4,
             "finished": finished}), encoding="utf-8")

    write_prog(False)
    live = RP.load_cell(tmp, "27-188-10-1", 2.0, source="live")
    c.ok(bool(live) and not live.get("error"),
         "★ 桶布局（finished=false）+ --source live → 找得到数据", str(live.get("error"))[:60])
    c.ok(bool(live.get("cs")), "★ 桶布局下确实取到了内容")
    write_prog(True)
    c.ok(not RP.load_cell(tmp, "27-188-10-1", 2.0, source="live"),
         "★ 同一目录、标成 finished=true 后，--source live 就查不到了")
    c.ok(bool(RP.load_cell(tmp, "27-188-10-1", 2.0, source="official")),
         "★ 同一目录、--source official 能查到（状态随 progress.json 走）")
    shutil.rmtree(tmp, ignore_errors=True)


def test_scaffold_is_single_home(c: Checker) -> None:
    """报告脚本（render / validate_paragraphs / compose_paragraphs）只允许存在一处：`报告任务_脚手架/`。

    为什么：report_build 是把整个脚手架**拷进任务包**的，所以脚手架那份才是真正跑起来的。
    2026-10-04 之前仓库根还留着一份"主代码"副本、靠"改完手工 cp 过去"保持同步 ——
    那正是"改了这边、跑的是那边"的来源（本地测试全绿、盒子上跑的是旧脚本，还看不出来）。
    现在只留一处：**重复出现就报错**，而不是再靠"记得 cp"。
    """
    for fn in ("render.py", "validate_paragraphs.py", "compose_paragraphs.py"):
        c.ok((SCAFFOLD / fn).exists(), f"脚手架里有 {fn}（真正跑的那份）")
        c.ok(not (HERE / fn).exists(),
             f"★ 仓库根不再有 {fn} 的副本（唯一出处 = 报告任务_脚手架/）")


def main() -> int:
    c = Checker()
    test_scaffold_is_single_home(c)
    test_bucket_layout_source(c)
    test_empty_newest_test(c)
    test_docs_match_scripts(c)
    test_validator(c)
    test_placeholder_gate(c)

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
    print("② 五张图与图位一一对上（平均电压图消失过，就是这里没测）")
    print(BAR)
    for name in WANT_FIGS:
        p = OUT / "图" / name
        c.ok(p.exists() and p.stat().st_size > 800,
             f"图存在且非空：{name}", f"{p.stat().st_size if p.exists() else '缺失'} 字节")
    tmpl = (SCAFFOLD / "报告渲染模板.html").read_text(encoding="utf-8")
    slots = sorted(set(re.findall(r"<!--FIGURE:(\d+)-->", tmpl)), key=int)
    c.ok(slots == ["1", "2", "3", "4", "5", "6"],
         "★ 模板有 FIGURE:1~6 六个图位（图 5/6 = 每样品一张 dQ/dV）", str(slots))
    tslots = sorted(set(re.findall(r"<!--TABLE:(\d+)-->", tmpl)), key=int)
    c.ok(tslots == [str(i) for i in range(1, 10)],
         "★ 模板表槽位 = 1~9（原表 3 已删、顺延补号无缺口，B4）", str(tslots))
    # ★ 样式块必须真的把 CSS 包在里面：2026-10-03 起模板里多出一对**空的**
    #   `<style></style>`，CSS 被挡到正文里 —— 报告打开就是满屏样式文字。
    #   因为谁也没直接打开 HTML 看，一直潜伏到 10-09 才被发现。
    c.ok(tmpl.count("<style>") == 1 and tmpl.count("</style>") == 1,
         "模板只有一对 <style>/</style>",
         "[开=%d 闭=%d]" % (tmpl.count("<style>"), tmpl.count("</style>")))
    _i, _j = tmpl.find("<style>"), tmpl.find("</style>")
    c.ok(_i < _j and ":root{" in tmpl[_i:_j],
         "★ CSS 真的在 <style> 里面（否则样式会当正文显示）")
    # ★ 章节与图号顺序必须与《协会第二版报告》一致：
    #   3.1 长循环 → 3.2 倍率 → 3.3 首圈库伦效率 → 3.4 平均电压 →
    #   3.5 dQ/dV → 3.6 横向对比（图 1~5 一一对应；图 6 已取消）
    body = tmpl.split("sec-3-1-长循环性能分析")[1].split("4.1  核心电化学性能结论")[0]
    seq = [a.strip() if a else "FIG" + b for a, b in
           re.findall(r"<h2[^>]*>([^<]+)</h2>|<!--FIGURE:(\d+)-->", body)
           if a or b]
    c.ok(seq == ["FIG1", "3.2  倍率性能分析", "FIG2", "3.3  首圈库伦效率", "FIG3",
                 "3.4  平均电压曲线分析", "FIG4", "3.5  dQ/dV 曲线分析", "FIG5",
                 "FIG6", "3.6  多组样品横向对比分析"],
         "★ 第 3 章小节与图号顺序与协会第二版报告一致", str(seq))
    src = (SCAFFOLD / "render.py").read_text(encoding="utf-8")   # 唯一出处：脚手架
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
    # （原来这里断言包里的 任务说明.md 日期是动态的；随 Agent 壳已删除。）
    win = ctx["test_conditions"]["voltage_window"]
    c.ok(win and "3.31" not in win and "2.0" in win,
         "★ 电压窗口 = 设定窗口 0.01~2.0 V（修改清单 B1；实测范围不再冒充设定窗口）",
         str(win))
    _tc = ctx["test_conditions"]
    c.ok(_tc.get("temperature") == "28 ℃",
         "★ 环境温度 = 28 ℃（修改清单 B1，用户批注值）", str(_tc.get("temperature")))
    c.ok(_tc.get("nominal_specific_capacity") and "300" in _tc["nominal_specific_capacity"],
         "★ 硬碳标称比容量 = 300 mAh/g（修改清单 B1）")
    c.ok(_tc.get("formation") == "0.1C × 3 圈",
         "★ 化成活化 = 0.1C × 3 圈（修改清单 B2）", str(_tc.get("formation")))
    c.ok("78" in (_tc.get("rate_program") or ""),
         "★ 倍率程序 = 0.2C→5C→0.2C 共 78 圈（修改清单 B2）", str(_tc.get("rate_program")))
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

    test_compose(c)
    test_compose_missing_rate_group(c)
    test_two_report_sets(c)

    print()
    print(BAR)
    print("⑤ render.py 组装 HTML：图都嵌进去、没有残留占位符")
    print(BAR)
    rr = subprocess.run([sys.executable, "render.py"], cwd=str(OUT),
                        capture_output=True, text=True, encoding="utf-8")
    c.ok(rr.returncode == 0, "render 正常退出", (rr.stderr or "")[-400:])
    html_p = OUT / "SOH分析报告_10-1_vs_10-2.html"      # 产物名带本次选择
    c.ok(html_p.exists(), "产出 SOH分析报告_<A>_vs_<B>.html（文件名带选择）")
    if not html_p.exists():
        return c.report()
    html = html_p.read_text(encoding="utf-8")
    c.ok(html.count("<figure>") == 6,
         f"★ 六张图全部内嵌（实得 {html.count('<figure>')}）")
    c.ok(html.count("data:image/svg+xml;base64,") == 6, "六张图都是 base64 内嵌（不依赖外部文件）")
    # ★ 产物里样式块也必须真的生效（模板对了，渲染仍可能把 CSS 漏在 body 里）
    c.ok(html.count("<style>") == 1 and html.count("</style>") == 1,
         "产物只有一对 <style>/</style>",
         "[开=%d 闭=%d]" % (html.count("<style>"), html.count("</style>")))
    _tail = html.split("</style>")[-1]
    c.ok(":root{" not in _tail and "--paper:" not in _tail,
         "★ CSS 没有漏进正文（漏了就是「未渲染」那种一片样式文字）")
    c.ok("充放电平均电压随循环的变化" in html,
         "★ 平均电压图的图注在报告里（这张图曾经整张消失）")
    left = [x for x in ("<!--BLOCK:", "<!--FIGURE:", "<!--TABLE:", "{{DOC_", "{{TOC_")
            if x in html]
    c.ok(not left, "★ 没有残留的占位符", str(left))
    c.ok("STD" in html and "1%Q④" in html, "样品名进了报告")
    c.ok("S31A 的合成段落" in html, "Agent 写的段落进了报告")

    # ---- 2026-10-10 修改清单 B 部分验收（B1~B9）----
    c.ok("**" not in html, "★ 全文无裸 **（B8：加粗符号不许漏进正文）")
    for _key in ("cell_type", "counter_electrode", "current_collector",
                 "electrolyte_control", "nominal_specific_capacity"):
        c.ok(_key not in html, f"★ 表内无英文程序字段名：{_key}（B3/B9）")
    c.ok("环境（未提供）" not in html and "标称比容量取（未提供）" not in html
         and "（（未提供））" not in html,
         "★ B1/B2：2.2/2.3 的三处「（未提供）」不再出现（表格里数据真缺的仍如实标）")
    c.ok("平均库伦效率 / %" in html and "保持率-基线" not in html,
         "★ 长循环表删「保持率-基线」、换「平均库伦效率」（B6）")
    c.ok("容量保持率（基线第 4 圈→最新）" in html and "（数据未到）" in html
         and "稳态平均库伦效率" in html,
         "★ 全维度对比表扩维度：没到的保留行标「（数据未到）」（B7）")
    c.ok("核心测试工步与数据处理口径" not in html and "表 10" not in html,
         "★ 原表 3 已删、表号顺延无缺口（B4）")
    c.ok("None 为工作电极" not in html and "**" not in html,
         "★ 正文不再出现字面 None / 裸 **（B9/B8）")
    c.ok("28 ℃" in html and "0.01~2.0 V" in html,
         "★ 2.2 的设定条件进了正文（B1）")
    # ---- 2026-10-12 用户第二轮五处修改的口径 ----
    c.ok("各倍率档位放电比容量统计（每档最后一圈）" in html
         and "实验样品 / (mAh·g⁻¹)" in html and "对比样品 / (mAh·g⁻¹)" in html,
         "★ 倍率表：每档最后一圈 + 实验样/对比样/差值三列（用户 2026-10-12）")
    _s3 = (OUT / "图" / WANT_FIGS[1]).read_text(encoding="utf-8")   # [1] = 首效VQ 那张
    c.ok("比容量 (mAh" in _s3 and ">容量 (mAh)<" not in _s3,
         "★ 图 3 横坐标 = 比容量（不是绝对容量）")
    # 添加剂名钉在**代码**上（假数据本身就叫 1%Q④，不能拿渲染结果当判据）
    _r = (SCAFFOLD / "render.py").read_text(encoding="utf-8")
    c.ok("1%Q④-1C" not in _r and "1%PBSF-1C" in _r,
         "★ 2.4 的样品编号写 PBSF（不是 Q④）")
    _rp = (HERE / "report_pack.py").read_text(encoding="utf-8")
    c.ok('= "Q④ 添加剂体系' not in _rp,
         "★ 4.3/5.2 收尾段不再写死 Q④（跟着本批变量走）")
    # B9 的**两个分支**单独钉住（假包走的是"体系齐全"分支，收敛分支在 html 里看不到）：
    sys.path.insert(0, str(SCAFFOLD))
    import render as _R
    _ctx2 = {"system": {"cell_type": "扣式电池", "chemistry": None,
                        "working_electrode": None, "counter_electrode": None,
                        "separator": None, "current_collector": None,
                        "electrolyte_control": None, "electrolyte_experimental": None,
                        "ions": None},
             "variable": {"type": "电解液添加剂（PBSF，1 wt%）",
                          "control_short": "长循环实验样",
                          "experimental_short": "长循环对比样",
                          "experimental_dosage": "1 wt%"},
             "test_conditions": {"temperature": "28 ℃", "humidity": None,
                                 "voltage_window": "0.01~2.0 V（设定窗口）",
                                 "nominal_specific_capacity": "300 mAh·g⁻¹",
                                 "formation": "0.1C × 3 圈",
                                 "rate_program": "0.2C→5C→0.2C，每档 6 圈、共 13 档 78 圈",
                                 "long_cycle_rate": "1C 恒流充放电，自第 4 圈起",
                                 "retention_baseline_cycle": 4}}
    _f21 = _R.chapter2_frag("S21A", _ctx2)
    c.ok("None" not in _f21 and "待实验室提供" in _f21 and "实验组" in _f21,
         "★ B9：体系缺信息时 S21A 收敛为「待实验室提供」，绝不印字面 None", _f21[:120])
    _f22 = _R.chapter2_frag("S22", _ctx2)
    c.ok("28 ℃" in _f22 and "0.01~2.0 V" in _f22 and "（未提供）" not in _f22
         and "待实验室提供" in _f22,
         "★ B1/A2：S22 设定条件齐全、湿度占位为「待实验室提供」", _f22[:120])
    _f23 = _R.chapter2_frag("S23A", _ctx2)
    c.ok("0.1C × 3 圈" in _f23 and "（（" not in _f23,
         "★ B2：工步参数进正文且不再出现「（（…））」双括号", _f23[:120])
    # B5 配色：按**角色名**判（实验样红/对比样蓝），不再按传入顺序
    import figures as _F2
    c.ok(_F2.cmp_color("长循环实验样", 0) == _F2.EXP_COLOR
         and _F2.cmp_color("长循环对比样", 1) == _F2.CTL_COLOR
         and _F2.cmp_color("STD", 0) == _F2.CMP_COLORS[0],
         "★ 配色按角色名：实验样=红、对比样=蓝，判不出角色再按座次（B5）")
    _lc = (OUT / "图" / WANT_FIGS[0]).read_text(encoding="utf-8")
    c.ok(_F2.EXP_COLOR in _lc and _F2.CTL_COLOR in _lc,
         "★ 图 1 里两种角色色都在")

    # dQ/dV 逐圈图：圈号说明走图注，**不许**用图内说明框（说明框固定左上角，
    # 会压住 dQ/dV 的峰 —— 渲染出来一眼可见）
    import figures as _F
    # ★ 2026-10-12：dQ/dV 改成"每样品一张"（图 5/6）—— 每张只放一个样品、多圈渐变
    for nm, lab in (("dQdV_STD.svg", "STD"), ("dQdV_1%Q④.svg", "1%Q④")):
        svg = (OUT / "图" / nm).read_text(encoding="utf-8")
        c.ok(_F.C_NOTE_BG not in svg, f"★ {nm} 图内没有说明框（不会压住峰）")
        c.ok("符号口径" in svg, f"{nm} 注明了正负峰的口径（还原为正）")
        c.ok(lab in svg, f"★ {nm} 只放**这一个样品**（{lab}）")
        # 只看**图例**（"<样品名> 第 N 圈"）—— 图注里本来就会列两颗样品的质量
        _other = "1%Q④" if lab == "STD" else "STD"
        c.ok(f"{_other} 第" not in svg, f"★ {nm} 的图例里没有另一个样品（每样品一张，不混）")
    _d5 = (OUT / "图" / "dQdV_STD.svg").read_text(encoding="utf-8")
    c.ok(any(x in _d5 for x in _F.EXP_RAMP), "★ 实验样那张用红系渐变色阶")
    _d6 = (OUT / "图" / "dQdV_1%Q④.svg").read_text(encoding="utf-8")
    c.ok(any(x in _d6 for x in _F.CTL_RAMP), "★ 对比样那张用蓝系渐变色阶")
    print()
    print(BAR)
    print("⑥ 段落缺失必须显式告警（不能静默留空）")
    print(BAR)
    (OUT / "段落" / "S31A.txt").unlink()
    subprocess.run([sys.executable, "render.py"], cwd=str(OUT),
                   capture_output=True, text=True, encoding="utf-8")
    # 产物名带本次选择（同 ⑥ 上面那条断言），重跑后读的也该是同一个名字
    # —— 原来这里漏改，还写着旧的 `SOH分析报告.html`，于是整套 rc=1 提前抛异常。
    html2 = (OUT / "SOH分析报告_10-1_vs_10-2.html").read_text(encoding="utf-8")
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
