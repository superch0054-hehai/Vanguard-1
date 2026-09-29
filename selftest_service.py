#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""service.sh 的启停逻辑测试。

为什么必须测它（而不是只测 collector.py / console.py）：service.sh 是**演示当天的
入口** —— 桌面图标 电池数据平台.desktop 调的就是 `./service.sh start`。
它一旦静默失败，表现是"双击了图标但网页打不开"，而终端里什么都不报。
实际踩到的 bug 就是这一类：采集器是常驻的（演示当天一直在跑），
原来的 start() 看到采集器在跑就整个 return 了 —— 于是控制台**永远起不来**。

这个测试在**临时目录**里放两个假的 collector.py / console.py 来跑，
不动盒子上真正的采集器和控制台，也不碰测试机。

注意：service.sh 用了 setsid，Windows 下没有 —— 那种情况直接跳过（不算通过）。

    python3 selftest_service.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BAR = "=" * 66

# 假的 collector.py / console.py：忽略参数、睡到被杀。
# 这样 service.sh 的 pidfile / 存活判断逻辑是真的，业务是假的。
STUB = "import sys, time\ntime.sleep(600)\n"


class Checker:
    def __init__(self) -> None:
        self.passed, self.failed = 0, []

    def ok(self, cond, label, extra=""):
        if cond:
            self.passed += 1
            print("  PASS  " + label)
        else:
            self.failed.append(label)
            print("  FAIL  " + label + (f"   <- {extra}" if extra else ""))

    def report(self) -> int:
        print(f"\n通过 {self.passed} 项，失败 {len(self.failed)} 项")
        for f in self.failed:
            print("  失败：" + f)
        return 1 if self.failed else 0


def run(d: Path, *args: str) -> tuple[int, str]:
    """在临时目录里执行 service.sh。"""
    p = subprocess.run(["bash", str(d / "service.sh"), *args],
                       cwd=str(d), capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def alive(pidfile: Path) -> bool:
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        return False
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_all(d: Path) -> None:
    """清场：把临时目录里两个假进程都杀掉。"""
    for name in ("collector.pid", "console.pid"):
        f = d / name
        if f.exists():
            try:
                os.kill(int(f.read_text().strip()), 9)
            except (OSError, ValueError):
                pass
            f.unlink()


def main() -> int:
    if os.name == "nt" or not shutil.which("bash"):
        print("跳过：service.sh 依赖 setsid，只能在 Linux 上跑"
              "（盒子是 Debian 12，请在盒子上跑这个测试）。")
        return 0

    c = Checker()
    tmp = Path(tempfile.mkdtemp(prefix="_service_test_"))
    d = tmp
    try:
        shutil.copy(HERE / "service.sh", d / "service.sh")
        (d / "collector.py").write_text(STUB, encoding="utf-8")
        (d / "console.py").write_text(STUB, encoding="utf-8")
        (d / "logs").mkdir(exist_ok=True)
        (d / "data").mkdir(exist_ok=True)

        cpid, xpid = d / "collector.pid", d / "console.pid"

        print(BAR)
        print("① start：两个都该起来")
        print(BAR)
        rc, out = run(d, "start")
        c.ok(rc == 0, "start 返回 0", out[-300:])
        c.ok(alive(cpid), "采集器起来了（collector.pid 指向的进程活着）")
        c.ok(alive(xpid), "★ 控制台也起来了（以前会漏掉）")
        c.ok("控制台已启动" in out, "输出里说了控制台已启动")

        print()
        print(BAR)
        print("② ★ 回归：采集器还在跑时，再 start 必须把控制台带起来")
        print(BAR)
        # 这就是真 bug 的场景 —— 演示当天采集器常驻，用户只会点一次图标。
        # 复现方法：手动杀掉控制台（模拟"盒子重启过 / 有人关了它"），再 start。
        if xpid.exists():
            try:
                os.kill(int(xpid.read_text().strip()), 9)
            except (OSError, ValueError):
                pass
            xpid.unlink()
        time.sleep(0.5)
        collector_pid_before = cpid.read_text().strip()
        rc, out = run(d, "start")
        c.ok(rc == 0, "start 返回 0", out[-300:])
        c.ok("采集服务已经在跑了" in out,
             "★ 认出了采集器在跑（并且不去重启它）")
        c.ok(alive(xpid),
             "★ 控制台被重新拉起来了（旧版在这里静默失败：直接 return）")
        c.ok(cpid.read_text().strip() == collector_pid_before,
             "★ 采集器的 PID 没变（没有被重启 —— 重启会丢测试状态）")

        print()
        print(BAR)
        print("③ ★ 回归：采集器已经没了，stop 仍要收掉控制台")
        print(BAR)
        # 不清掉会留下孤儿进程占着 8090 端口，下次 start 时端口冲突。
        if cpid.exists():
            try:
                os.kill(int(cpid.read_text().strip()), 9)
            except (OSError, ValueError):
                pass
            cpid.unlink()
        time.sleep(0.5)
        rc, out = run(d, "stop")
        c.ok(rc == 0, "stop 返回 0", out[-300:])
        c.ok("采集服务没有在跑" in out, "说明了采集器没在跑")
        c.ok(not alive(xpid),
             "★ 控制台还是被收掉了（旧版在这里直接 return，留下孤儿）")

        print()
        print(BAR)
        print("④ status 两个都要报，别只说一半")
        print(BAR)
        rc, out = run(d, "start")
        rc, out = run(d, "status")
        c.ok("网页控制台" in out, "status 里有控制台那一段")
        c.ok("运行中" in out, "status 报告了运行状态")
        c.ok(alive(cpid) and alive(xpid), "start/status 之后两个都在跑")

        print()
        print(BAR)
        print("⑤ stop 之后必须干净（不留 pidfile、不留进程）")
        print(BAR)
        run(d, "stop")
        time.sleep(0.5)
        c.ok(not alive(cpid), "采集器停了")
        c.ok(not alive(xpid), "控制台停了")
        c.ok(not cpid.exists() and not xpid.exists(), "pid 文件都清掉了")

        print()
        print(BAR)
        print("验证：")
        print(BAR)
        return c.report()
    finally:
        kill_all(d)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
