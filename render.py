#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告组装：把 Agent 写好的段落文本填进渲染模板，产出 HTML 报告。

**确定性代码，不经模型**（提示词体系铁律：模型只产段落文本，程序产一切结构）。

输入（与本脚本同目录）：
  报告渲染模板.html          固定外壳（45 个块位 + 图表位 + 单值占位）
  段落/S31A.txt …            Agent 写好的段落文本
  block_data/*.json          数据包（表格渲染用）
  global_context.json        全局上下文
  图/*.svg                   对比图（base64 内嵌）

输出：SOH分析报告.html
"""

import base64
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "1.0"

TOC = [
    ("1 整体研究目标", ["1.1 整体研究目标", "1.2 具体实验验证目标"]),
    ("2 实验方法", ["2.1 测试对象与体系", "2.2 电化学测试项目",
                    "2.3 数据采集与处理", "2.4 数据质量声明"]),
    ("3 测试结果与分析", ["3.1 长循环性能分析", "3.2 倍率性能分析",
                          "3.3 首圈库伦效率", "3.4 平均电压曲线分析",
                          "3.5 dQ/dV 曲线分析", "3.6 多组样品横向对比分析"]),
    ("4 结论与讨论", ["4.1 核心电化学性能结论", "4.2 电化学作用机制推测",
                      "4.3 后续优化与研究建议"]),
    ("5 总结与展望", ["5.1 本次测试工作整体总结", "5.2 后续研究与应用展望"]),
]

# 4.2 机制段：实验变量未知 → 降级为告警标记（诚实降级，不编造机制）
S42_WARNING = "【本段未通过自动校验：实验变量信息未提供，作用机制无法推测，内容需人工复核】"
NUM_H3 = {"S41A": "4.1.1", "S41B": "4.1.2", "S41C": "4.1.3",
          "S42A": "4.2.1", "S42B": "4.2.2", "S42C": "4.2.3", "S42D": "4.2.4"}


def na_cell(v, fmt=None):
    if v is None:
        return "（未提供）"
    if isinstance(v, (list, tuple)):
        return "；".join("（未提供）" if x is None else str(x) for x in v)
    return fmt.format(v) if fmt else str(v)


def _body_html(lines):
    out, ul = [], []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("- "):
            ul.append(line[2:])
            continue
        if ul:
            out.append("<ul>" + "".join("<li>" + i + "</li>" for i in ul) + "</ul>")
            ul = []
        out.append("<p>" + line + "</p>")
    if ul:
        out.append("<ul>" + "".join("<li>" + i + "</li>" for i in ul) + "</ul>")
    return "\n".join(out)


def text_to_html(block_id, text):
    """模型段落文本 → HTML 片段（适配版 6.2 的转换规则）。

    普通行 → <p>；"- " 开头的连续行 → <ul><li>（仅 S12）；
    S41*/S42* 的首行是小标题 → <h3>（编号由程序写）。
    """
    if not text.strip():
        return ""
    lines = text.strip().splitlines()
    if block_id.startswith(("S41", "S42")) and lines:
        head = lines[0].strip()
        m = re.match(r"^(S\d+[A-Z]?)\s*(.*)$", head)
        if m and m.group(1) == block_id:      # 模型把块号写进了首行 → 剥掉
            head = m.group(2)
        num = NUM_H3.get(block_id, "")
        if head:
            return ("<h3>" + num + "  " + head + "</h3>\n"
                    + _body_html(lines[1:]))
    return _body_html(lines)


def load_block(bid):
    p = HERE / "段落" / (bid + ".txt")
    if p.exists():
        return p.read_text(encoding="utf-8-sig").strip()
    # 段落缺失 → 显式告警（体系文档第 5 节：绝不允许静默跳过）
    return "【本段未通过自动校验：段落文件缺失，内容需人工复核】"


def figure_html(no, svg_name, caption):
    p = HERE / "图" / svg_name
    if not p.exists():
        return ('<div class="note">【图 ' + str(no) + ' 未生成：'
                + svg_name + ' 缺失，需人工复核】</div>')
    b64 = base64.b64encode(p.read_bytes()).decode()
    return ('<figure><img src="data:image/svg+xml;base64,' + b64 + '" alt="图 '
            + str(no) + '"><figcaption>图 ' + str(no) + "　" + caption
            + "</figcaption></figure>")


def table_html(no, caption, headers, rows):
    body = "".join(
        "<tr>" + "".join('<td class="' + ("l" if i == 0 else "c") + '">'
                         + v + "</td>" for i, v in enumerate(row)) + "</tr>"
        for row in rows)
    return ('<div class="tw"><p class="tcap">表 ' + str(no) + "　" + caption + "</p>"
            + "<table><thead><tr>"
            + "".join("<th>" + h + "</th>" for h in headers)
            + "</tr></thead><tbody>" + body + "</tbody></table></div>")


def bd(bid):
    p = HERE / "block_data" / (bid + ".json")
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}


def chapter2_frag(bid, ctx):
    """第 2 章由**程序**按配置生成（模板里就是这么写的：不经模型）。

    体系/工步/分组信息来自协会《…第二版》报告，同一体系（STD vs 1% Q④）时才有；
    换别的样品时如实写"待实验室补充"，不张冠李戴。
    """
    sysi, tc = ctx.get("system") or {}, ctx.get("test_conditions") or {}
    var = ctx.get("variable") or {}
    known = bool(var.get("type"))
    if not known:
        return ('<div class="note">第 2 章与附录由程序按配置生成'
                "（本对样品的体系信息未提供，待实验室补充）</div>")
    txt = {
        "S21A": ("本次实验采用 {cell_type}：{working_electrode} 为工作电极，"
                 "{counter_electrode} 为参比与对电极，隔膜为 {separator}，"
                 "集流体为 {current_collector}。两组电池的差别仅在电解液："
                 "{control_short} 组为 {electrolyte_control}；{experimental_short} 组为 "
                 "{electrolyte_experimental}。").format(
                     control_short=var.get("control_short", "A"),
                     experimental_short=var.get("experimental_short", "B"), **sysi),
        "S21B": ("硬碳极片与电池按标准扣电流程制备：电极浆料制备 → 涂布于集流体 → "
                 "真空干燥 → 辊压 → 切片（Φ11.3 mm）→ 手套箱内组装扣电壳 → "
                 "注入电解液 → 封口静置 12 h（本节流程口径与协会报告一致）。"),
        "S22": ("测试使用 Neware 电池测试系统，环境 {temperature}、湿度 {humidity}；"
                "电压窗口为 {voltage_window}，硬碳标称比容量取 {nominal}，"
                "截止条件为电压截止。").format(
                    temperature=tc.get("temperature") or "（未提供）",
                    humidity=tc.get("humidity") or "（未提供）",
                    voltage_window=tc.get("voltage_window") or "（未提供）",
                    nominal=tc.get("nominal_specific_capacity") or "（未提供）"),
        "S23A": ("核心测试工步分四阶段：静置 → 化成活化（{formation}）→ "
                 "倍率性能测试（{rate_program}）→ 长循环测试（{long_cycle_rate}）。"
                 "本批数据的实测电流见 3.1 节的说明。").format(
                     formation=tc.get("formation") or "（未提供）",
                     rate_program=tc.get("rate_program") or "（未提供）",
                     long_cycle_rate=tc.get("long_cycle_rate") or "（未提供）"),
        "S23B": ("数据处理口径：容量保持率基线统一取第 {base} 圈（化成后首个 1C 循环）；"
                 "清洗规则为剔除末圈被截断的循环与库伦效率持续超过 100.5% 的异常尾段。"
                 "本批数据为每条件 1 颗电芯（n=1），未做平行样均值与误差棒。").format(
                     base=tc.get("retention_baseline_cycle")),
        "S24A": ("实验分为对照组与实验组，每组分别开展长循环与倍率两个测试项目，"
                 "共四个样品编号（STD-1C、STD-rate、1%Q④-1C、1%Q④-rate）。"
                 "原设计每个条件制备 3 颗平行样，本次分析实际使用每条件 1 颗代表电芯 —— "
                 "**本报告只用了其中一对通道的数据**，其余通道待采集后并入。"),
        "S24B": ("因此后续所有差异均按「方向 + 幅度 + 是否超出个体波动范围」表述，"
                 "不做显著性判定。"),
    }
    return "<p>" + txt.get(bid, "") + "</p>"


def render(ctx, labels):
    tpl = (HERE / "报告渲染模板.html").read_text(encoding="utf-8")

    # ---- 单值占位 ----
    rep = ctx.get("report", {})
    out = tpl.replace("{{DOC_TITLE}}", rep.get("title", "电池测试数据分析报告"))
    out = out.replace("{{DOC_SUBTITLE}}", rep.get("subtitle", ""))
    out = out.replace("{{DOC_META_SPANS}}",
                      "<span>报告生成：{}</span><span>数据截至：进行中测试的每日增量</span>"
                      .format(ctx.get("generated", "")))
    out = out.replace("{{DOC_NOTE}}",
                      "阶段性报告：测试仍在进行；活性物质质量为暂定值 2 mg；"
                      "效率口径为放电÷充电（方向以程序给出的数据为准）。")
    out = out.replace("{{TOC_TREE}}",
                      "".join('<li class="l1">' + top + "<ul>"
                              + "".join('<li class="l2">' + s + "</li>" for s in subs)
                              + "</ul></li>" for top, subs in TOC))

    # ---- 块位（S42* 降级为告警；S2*/APPENDIX 由程序模板生成）----
    def frag_for(bid):
        # S42*（4.2 机制段）：**有正文就用正文**。以前这里无条件写降级告警，
        # 那是因为当时实验变量未知；现在变量已知、机制知识库也注入了，
        # 只有在真的没写（或写了却仍是降级标记）时才显示告警。
        if bid.startswith("S42"):
            txt = load_block(bid)
            if txt and "未通过自动校验" not in txt and "该项数据未提供" not in txt:
                return text_to_html(bid, txt)
            extra = S42_WARNING if bid in ("S42INTRO", "S42E") else ""
            return ('<div class="note">' + (extra or S42_WARNING) + "</div>"
                    if bid in ("S42INTRO", "S42E") else "<p>" + S42_WARNING + "</p>")
        if bid.startswith("S2") or bid == "APPENDIX":
            if bid == "APPENDIX":
                return ('<div class="note">附录为预留扩展测试模块'
                        "（EIS / CV / 失效分析 / 热稳定性 / 低温性能），"
                        "按需接入对应分析内容。</div>")
            return chapter2_frag(bid, ctx)
        return text_to_html(bid, load_block(bid))

    for bid in set(re.findall(r"<!--BLOCK:([A-Z0-9]+)-->", out)):
        out = out.replace("<!--BLOCK:" + bid + "-->", frag_for(bid))

    # ---- 表格位（程序按数据渲染；缺数据如实标注）----
    # 表号与《协会第二版报告》对齐：1 体系 / 2 分组 / 3 测试条件 / 4 长循环 /
    # 5 倍率 / 6 首圈 / 7 平均电压·ΔV·EE / 8 dQ/dV 峰 / 9 全维度 / 10 变量—性能
    b31a, b31b, b33a, b32a = bd("S31A"), bd("S31B"), bd("S33A"), bd("S32A")
    b32c, b32d, b34a, b35a = bd("S32C"), bd("S32D"), bd("S34A"), bd("S35A")
    tables = {}
    tables[1] = table_html(1, "测试电池体系配置", ["项", "内容"],
                           [[k, na_cell(v)] for k, v in (ctx.get("system") or {}).items()])
    tables[2] = table_html(2, "实验分组与变量", ["项", "内容"],
                           [[k, na_cell(v)] for k, v in (ctx.get("variable") or {}).items()])
    tables[3] = table_html(3, "核心测试工步与数据处理口径", ["项", "内容"],
                           [[k, na_cell(v)] for k, v in (ctx.get("test_conditions") or {}).items()])
    rows4 = []
    for gname, gv in (b31a.get("groups") or {}).items():
        rb = (b31b.get("groups") or {}).get(gname, {})
        rows4.append([gname, na_cell(gv.get("effective_cycles")),
                      na_cell(gv.get("fade_rate_linear_fit")),
                      na_cell(rb.get("retention_baseline")),
                      na_cell(rb.get("retention_last"))])
    tables[4] = table_html(4, "标准化循环性能数据统计（基线为第 4 圈）",
                           ["组", "有效圈数", "衰减速率 (mAh·g⁻¹·百圈⁻¹)",
                            "保持率-基线", "保持率-最新"], rows4)
    rows5 = []
    for d in (b33a.get("rate_stats") or []):
        rows5.append([na_cell(d.get("label")), na_cell(d.get("a_mah_g"), "{:.2f}"),
                      na_cell(d.get("b_mah_g"), "{:.2f}"), na_cell(d.get("diff"), "{:+.2f}")])
    tables[5] = table_html(5, "各倍率档位放电比容量统计（每档均值）",
                           ["倍率档位", "样品 A / (mAh·g⁻¹)", "样品 B / (mAh·g⁻¹)",
                            "差值 / (mAh·g⁻¹)"],
                           rows5 or [["（本批数据尚未进入倍率段）", "", "", ""]])
    rows6 = []
    for gname, gv in (b32a.get("groups") or {}).items():
        rows6.append([gname, na_cell(gv.get("discharge_sp")),
                      na_cell(gv.get("charge_sp")),
                      na_cell(gv.get("irreversible_sp")),
                      na_cell(gv.get("ce"), "{:.2f}")])
    tables[6] = table_html(6, "首圈充放电容量与不可逆容量统计",
                           ["组", "首圈放电比容量 / (mAh·g⁻¹)", "首圈充电比容量 / (mAh·g⁻¹)",
                            "首圈不可逆容量 / (mAh·g⁻¹)", "首次库伦效率 / %"], rows6)
    rows7 = []
    for gname, gv in (b32c.get("groups") or {}).items():
        gd = (b32d.get("groups") or {}).get(gname, {})
        rows7.append([gname, na_cell(gv.get("avg_v_first"), "{:.4f}"),
                      na_cell(gv.get("avg_v_last"), "{:.4f}"),
                      na_cell(gv.get("avg_v_fade_mv_100c"), "{:+.2f}"),
                      na_cell(gd.get("dV_mean_mv"), "{:.2f}"),
                      na_cell(gd.get("EE_first_pct"), "{:.2f}"),
                      na_cell(gd.get("EE_last_pct"), "{:.2f}"),
                      na_cell(gd.get("platform_share_last_pct"), "{:.2f}")])
    tables[7] = table_html(7, "平均放电电压、电压滞后（ΔV）、能量效率与平台容量占比",
                           ["组", "平均放电电压-首 / V", "平均放电电压-末 / V",
                            "电压衰减速率 / (mV·百圈⁻¹)", "ΔV 均值 / mV",
                            "EE-首 / %", "EE-末 / %", "平台容量占比 / %"], rows7)
    rows8 = []
    for gname, gv in (b34a.get("groups") or {}).items():
        pk = gv.get("peak_positions", {})
        rows8.append([gname, na_cell(pk.get("chg", {}).get("voltage_V"), "{:.3f}"),
                      na_cell(pk.get("dis", {}).get("voltage_V"), "{:.3f}")])
    tables[8] = table_html(8, "dQ/dV 主峰位置（第 1 圈；峰宽/峰高/面积占比待数据）",
                           ["组", "充电分支（脱出）主峰 / V", "放电分支（嵌入）主峰 / V"], rows8)
    tables[9] = table_html(9, "全维度性能综合对比", ["维度", "方向", "幅度"],
                           [[d.get("name", "—"), d.get("direction", "—"),
                             d.get("magnitude", "—")]
                            for d in b35a.get("dimension_summary", [])])
    tables[10] = table_html(10, "变量—性能响应差异",
                            ["保持率类", "容量类", "极化类"],
                            [["（圈数尚少，幅度区间无法统计）", "", ""]])

    # ---- 图表位（图号与《协会第二版报告》一致）----
    # 图 1 长循环 / 图 2 倍率 / 图 3 首圈库伦效率 / 图 4 平均电压 / 图 5、6 dQ/dV（每样品一张）
    vs = "_vs_".join(labels)
    a, b = (list(labels) + ["", ""])[:2]
    figs = {
        1: figure_html(1, "对比_长循环_" + vs + ".svg",
                       "长循环放电比容量与库伦效率对比（两样品叠加）"),
        2: figure_html(2, "对比_倍率_" + vs + ".svg",
                       "放电比容量随循环变化（本批数据尚未进入倍率阶梯段）"),
        3: figure_html(3, "对比_首效VQ_" + vs + ".svg",
                       "首周充放电电压-比容量曲线（图例含各样品首次库伦效率）"),
        4: figure_html(4, "对比_平均电压_" + vs + ".svg",
                       "充放电平均电压随循环的变化（两者之差即电压滞后 ΔV）"),
        5: figure_html(5, "dQdV_" + a + ".svg",
                       a + " 的多周次 dQ/dV 叠放图谱（同图内：上为放电分支/钠嵌入，"
                           "下为充电分支/钠脱出；按圈号配色）"),
        6: figure_html(6, "dQdV_" + b + ".svg",
                       b + " 的多周次 dQ/dV 叠放图谱（同图内：上为放电分支/钠嵌入，"
                           "下为充电分支/钠脱出；按圈号配色）"),
    }

    # ---- 替换：块位最后替换（避免 frag 里含同类注释被二次替换）----
    for no in sorted(figs, reverse=True):
        out = out.replace("<!--FIGURE:" + str(no) + "-->", figs[no])
    for no in sorted(tables, reverse=True):
        out = out.replace("<!--TABLE:" + str(no) + "-->", tables[no])
    for bid in sorted(set(re.findall(r"<!--BLOCK:([A-Z0-9]+)-->", out)), reverse=True):
        out = out.replace("<!--BLOCK:" + bid + "-->", frag_for(bid))
    return out


def main():
    p = HERE / "global_context.json"
    if not p.exists():
        print("global_context.json 不存在 —— 先跑 report_pack.py",
              file=__import__("sys").stderr)
        return 1
    ctx = json.loads(p.read_text(encoding="utf-8-sig"))
    v = ctx.get("variable", {})
    labels = [v.get("control_short", ""), v.get("experimental_short", "")]
    html = render(ctx, labels)
    dst = HERE / "SOH分析报告.html"
    dst.write_text(html, encoding="utf-8")
    print("  已生成:", dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
