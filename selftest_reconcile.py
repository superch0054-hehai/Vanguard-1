#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reconcile.py 的离线测试：生成对账表 + 回填后自动核对。不需要真机。

    python selftest_reconcile.py
"""

import csv
import sys
from pathlib import Path

import reconcile
import selftest_export as SE

BAR = "=" * 66


def main() -> int:
    outdir = Path("_rec_out")
    if outdir.exists():
        for f in outdir.iterdir():
            f.unlink()
    outdir.mkdir(parents=True, exist_ok=True)

    print(BAR)
    print("① 生成对账表（模拟设备）")
    print(BAR)
    reconcile.build_transport = lambda args: SE.Dispatch(page_size=25)
    sys.argv = ["reconcile.py", "--host", "10.201.47.169",
                "--channel", "27-188-10-2", "--outdir", str(outdir)]
    if reconcile.main() != 0:
        print("生成失败", file=sys.stderr)
        return 1

    sheets = list(outdir.glob("对账表_*.csv"))
    notes = list(outdir.glob("对账说明_*.md"))
    print()
    print("产出：" + str([p.name for p in sheets + notes]))

    checks = [
        ("对账表已生成", len(sheets) == 1),
        ("对账说明已生成", len(notes) == 1),
    ]

    # ---- ② 未回填的情形 ------------------------------------------------
    print()
    print(BAR)
    print("② 未回填时核对（应提示待填，不应误报失败）")
    print(BAR)
    rc = reconcile.do_check(sheets[0])
    checks.append(("未填时返回码为 0", rc == 0))

    # ---- ③ 回填成全部一致 ----------------------------------------------
    print()
    print(BAR)
    print("③ 回填成与我们的值一致后核对")
    print(BAR)
    rows = list(csv.DictReader(open(sheets[0], encoding="utf-8-sig")))
    for row in rows:
        row["BTSDA 的值"] = row["我们的值"]
    with open(sheets[0], "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    rc = reconcile.do_check(sheets[0])
    checks.append(("全部一致时返回码为 0", rc == 0))

    # ---- ④ 制造一处不一致 ----------------------------------------------
    print()
    print(BAR)
    print("④ 把首圈库仑效率改成 95（模拟口径不一致）")
    print(BAR)
    rows = list(csv.DictReader(open(sheets[0], encoding="utf-8-sig")))
    for row in rows:
        if row["项目"] == "首圈库仑效率":
            row["BTSDA 的值"] = "95"
    with open(sheets[0], "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    rc = reconcile.do_check(sheets[0])
    checks.append(("不一致时返回码为 1，能被发现", rc == 1))

    print()
    print(BAR)
    print("验证：")
    ok = True
    for label, passed in checks:
        print("  " + ("PASS  " if passed else "FAIL  ") + label)
        ok = ok and passed
    print()
    print("全部通过 ✅" if ok else "有失败项 ❌")

    text = notes[0].read_text(encoding="utf-8")
    print()
    print("--- 对账说明.md 前 50 行 ---")
    print("\n".join(text.splitlines()[:50]))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
