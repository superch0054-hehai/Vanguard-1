#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行脚本的"接线"检查。

为什么需要这个测试
------------------
之前 export_review.py 和 reconcile.py 都有同一个 bug：argparse 里少定义了
--timeout，而它们调用的 build_transport() 要用 args.timeout，于是运行到一半
抛 AttributeError。

更糟的是：这两个脚本原有的测试**把 build_transport 打桩替换掉了**，
正好绕过了出问题的那段代码，所以测试全绿但脚本是坏的。

这个文件专门补上这个盲区：用**真实的 argparse + 真实的 build_transport**
跑每个脚本，把一个连不上的地址喂进去，然后断言——

    脚本应该给出一句人话的错误（连接被拒绝），
    而不是抛 AttributeError / TypeError 之类的接线错误。

不需要设备，也不需要网络（连的是本机一个没人监听的端口）。

    python selftest_cli.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 127.0.0.1:1 上没有任何程序在监听 —— 连它会立刻被拒绝，快且确定
DEAD_HOST = "127.0.0.1"
DEAD_PORT = "1"

CASES = [
    ("neware_client.py", ["--transport", "tcp", "--host", DEAD_HOST,
                          "--port", DEAD_PORT, "info"]),
    ("probe.py", ["--transport", "tcp", "--host", DEAD_HOST,
                  "--port", DEAD_PORT, "--report", "_cli_test_report.json"]),
    ("export_review.py", ["--transport", "tcp", "--host", DEAD_HOST,
                          "--port", DEAD_PORT, "--channel", "27-188-10-2",
                          "--outdir", "_cli_test_out"]),
    ("reconcile.py", ["--transport", "tcp", "--host", DEAD_HOST,
                      "--port", DEAD_PORT, "--channel", "27-188-10-2",
                      "--outdir", "_cli_test_out"]),
]

# 出现这些字样说明是"代码接线错了"，不是"环境不通"
WIRING_ERRORS = ["AttributeError", "TypeError", "KeyError",
                 "NameError", "IndexError", "unexpected keyword"]

# 出现这些字样说明脚本正确地把"连不上"讲清楚了
CONN_ERROR_HINTS = ["连接", "拒绝", "refused", "timeout", "超时", "失败"]


def run_case(script: str, args: list[str]) -> tuple[bool, str]:
    here = Path(__file__).parent
    path = here / script
    if not path.exists():
        return False, f"脚本不存在：{script}"

    proc = subprocess.run(
        [sys.executable, str(path), *args],
        capture_output=True, text=True, timeout=90,
        cwd=str(here),
    )
    out = (proc.stdout or "") + (proc.stderr or "")

    for bad in WIRING_ERRORS:
        if bad in out:
            first = [ln.strip() for ln in out.splitlines() if bad in ln]
            return False, f"接线错误 {bad}：{first[0] if first else ''}"
    if "Traceback" in out:
        tail = [ln.strip() for ln in out.splitlines() if ln.strip()][-1]
        return False, f"抛了未捕获异常：{tail}"
    if not any(h in out for h in CONN_ERROR_HINTS):
        return False, f"没给出连接相关的错误说明。输出：{out.strip()[:160]}"
    if proc.returncode == 0:
        return False, "连不上却返回了成功码 0"
    return True, out.strip().splitlines()[-1][:90]


def main() -> int:
    print("=" * 66)
    print("命令行脚本接线检查（真实 argparse + 真实 build_transport）")
    print("=" * 66)
    passed, failed = 0, []
    for script, args in CASES:
        ok, detail = run_case(script, args)
        print(f"  {'PASS' if ok else 'FAIL'}  {script:<20} {detail}")
        if ok:
            passed += 1
        else:
            failed.append(script)

    print()
    print(f"通过 {passed} 个，失败 {len(failed)} 个")
    if failed:
        print("失败：" + "、".join(failed))
        return 1
    print("\n全部通过 ✅ —— 所有脚本的参数解析与传输构造都对得上")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
