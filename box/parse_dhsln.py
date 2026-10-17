# -*- coding: utf-8 -*-
"""
东华 .dhsln 解析器（原格式，UTF-8 XML）—— 只读，不修改任何原文件。

提取：实验类型/方法名、信号定义、全部中文参数、MachineId/FileName（数据文件名、
      与通道号相关）、以及"配方"里的循环/圈数等信息。

用法：
  python parse_dhsln.py <文件或文件夹> [...]            # 打印概览
  python parse_dhsln.py <文件.dhsln> --params           # 连全部参数一起打印
  python parse_dhsln.py <文件.dhsln> --json out.json    # 导出结构化结果
"""
import os
import sys
import json
import glob
import xml.etree.ElementTree as ET


def parse(path):
    tree = ET.parse(path)
    root = tree.getroot()
    out = {
        "file": os.path.basename(path),
        "project_version": root.get("Version"),
        "machine_id": None,
        "data_file": None,
        "experiments": [],
    }
    ai = root.find("AnalysisItem")
    if ai is not None:
        out["machine_id"] = (ai.findtext("MachineId") or "").strip()
        out["data_file"] = (ai.findtext("FileName") or "").strip()

    for ep in root.iter("ExperimentParam"):
        e = {"index": ep.get("ExperimentIndex"), "name": None, "type": None,
             "signals": None, "support_signals": None, "params": {}}
        info = ep.find("ExperimentInfo")
        if info is not None:
            bv = info.find("BaseView")
            if bv is not None:
                e["type"] = bv.get("ExperimentType")
                e["name"] = bv.get("ExperimentName")
                e["signals"] = bv.get("DefaultSignals")
                e["support_signals"] = bv.get("SupportSignals")
            for p in info.iter("UIParam"):
                nm, pv = p.get("ParamName"), p.get("ParamValue")
                if nm and pv not in (None, ""):
                    e["params"][nm] = pv
        out["experiments"].append(e)
    return out


def brief(r):
    e0 = r["experiments"][0] if r["experiments"] else {}
    return "%-46s 数据文件=%-4s 实验类型=%-5s 方法=%-16s 参数段=%d 信号=%s" % (
        r["file"][:46], r["data_file"], e0.get("type"), (e0.get("name") or "")[:16],
        len(r["experiments"]), e0.get("signals"))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_params = "--params" in sys.argv
    json_out = None
    if "--json" in sys.argv:
        json_out = sys.argv[sys.argv.index("--json") + 1]
    if not args:
        print(__doc__)
        return
    files = []
    for a in args:
        if os.path.isdir(a):
            files += sorted(glob.glob(os.path.join(a, "**", "*.dhsln"), recursive=True))
        elif os.path.isfile(a):
            files.append(a)
    results = []
    for f in files:
        try:
            r = parse(f)
        except Exception as exc:
            print("!! 解析失败 %s: %s" % (f, exc))
            continue
        results.append(r)
        print(brief(r))
        if show_params:
            for e in r["experiments"]:
                print("     [段 %s] %s | 信号 %s" % (e["index"], e["name"], e["signals"]))
                for k, v in list(e["params"].items())[:40]:
                    print("        %-24s = %s" % (k, v))
    if json_out and results:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
        print("已写出 %s（%d 个文件）" % (json_out, len(results)))


if __name__ == "__main__":
    main()
