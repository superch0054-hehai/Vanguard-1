#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""console.py 的离线测试。

重点不是"页面能打开"，而是三件事：
  ① 界面上必须明确写清"这是只读的、不会操作测试机"
  ② 数据能正确渲染出来（指标、条码、完整性）
  ③ 脏数据不会把界面搞崩，也不会被当成 HTML 执行

    python selftest_console.py
"""

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA = Path("_console_test_data")
BAR = "=" * 66


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_fake_data() -> None:
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)

    recs = [
        {
            "key": "27-188-10-1|247", "channel": "27-188-10-1", "devtype": 27,
            "devtype_name": "BTS85", "devid": 188, "subdevid": 10, "chlid": 1,
            "testid": "247", "barcode": "D:测试数据黄坤1",
            "collected_at": "2026-09-25T13:52:55", "upload_complete": True,
            "step_count": 3, "detail_count": 1241, "dir": "channel=27-188-10-1/testid=247",
            "parser_version": "collector 1.0",
            "soh": {"available": True, "cycle_count": 55, "cycle_range": [1, 55],
                    "capacity_retention_pct": 57.06,
                    "coulomb_efficiency_last_pct": 98.7,
                    "energy_efficiency_last_pct": 88.1,
                    "dcir_growth_pct": 173.55},
        },
        {
            # 条码里塞 HTML —— 用来验证界面做了转义，不会被当标签执行
            "key": "27-188-10-2|244", "channel": "27-188-10-2", "devtype": 27,
            "devtype_name": "BTS85", "devid": 188, "subdevid": 10, "chlid": 2,
            "testid": "244", "barcode": "<script>alert(1)</script>",
            "collected_at": "2026-09-25T13:53:01", "upload_complete": False,
            "step_count": 9, "detail_count": 59253, "dir": "channel=27-188-10-2/testid=244",
            "parser_version": "collector 1.0",
            "soh": {"available": True, "cycle_count": 185, "cycle_range": [1, 185],
                    "capacity_retention_pct": 17.24},
        },
    ]
    lines = [json.dumps(r, ensure_ascii=False) for r in recs]
    lines.append("{ 这不是合法 JSON —— 坏行应该被跳过，而不是让界面崩掉")
    lines.append("")
    (DATA / "manifest.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    d = DATA / "channel=27-188-10-1" / "testid=247"
    d.mkdir(parents=True)
    (d / "steps.csv").write_text(
        "startseqid,endseqid,stepid,cycleid,steptype,cap,eng,dcir\n"
        "1,781,1,1,cc,0.00166,0.00234,0\n"
        "799,1241,3,1,dc,0.000947,0.00112,169166.9\n", encoding="utf-8")
    (d / "detail.csv").write_text("seqid,volt,curr\n1,0.4932,0.000284\n", encoding="utf-8")

    (DATA / "status.json").write_text(json.dumps({
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "service": "collector 1.0", "uptime_seconds": 15.0,
        "watched_channels": 2, "collected_datasets": 2, "working_now": 0,
        "statuses": {"27-188-10-1": "finish"},
    }, ensure_ascii=False), encoding="utf-8")


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


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
            print("  -", f)
        return 1 if self.failed else 0


def main() -> int:
    make_fake_data()
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        [sys.executable, "console.py", "--data-dir", str(DATA), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # 等它起来
        up = False
        for _ in range(50):
            try:
                if get(base + "/health")[0] == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.2)

        c = Checker()
        print(BAR)
        print("① 服务能起来吗")
        print(BAR)
        c.ok(up, "控制台起来了（/health 返回 200）")
        if not up:
            return c.report()

        code, home = get(base + "/")

        print()
        print(BAR)
        print("② ★ 界面上必须写清「这是只读的」")
        print(BAR)
        c.ok(code == 200, "首页返回 200")
        c.ok("只读界面" in home, "首页明确写着「这是只读界面」")
        c.ok("不会对测试机发任何命令" in home, "首页说明不会向测试机发命令")
        c.ok("刷新共享数据" in home, "提供了刷新共享数据的入口")

        print()
        print(BAR)
        print("③ 数据渲染对不对")
        print(BAR)
        c.ok("BatteryLab 数据控制台" in home, "标题正确")
        c.ok("27-188-10-1" in home and "27-188-10-2" in home, "两个通道都列出来了")
        c.ok("D:测试数据黄坤1" in home, "条码显示出来了")
        c.ok("57.06" in home, "容量保持率显示出来了")
        c.ok("17.24" in home, "第二个数据集的保持率也在")
        c.ok("未传完" in home, "未传完的数据集有警示")
        c.ok("心跳" in home, "显示心跳")

        print()
        print(BAR)
        print("④ ★ 脏数据与安全")
        print(BAR)
        c.ok("<script>alert(1)</script>" not in home,
             "条码里的 <script> 被转义了（没有当标签输出）")
        c.ok("&lt;script&gt;" in home, "转义成了 HTML 实体")
        c.ok(home.count("27-188-10-1") >= 1, "坏 manifest 行没让页面崩掉")

        print()
        print(BAR)
        print("⑤ 数据集详情页")
        print(BAR)
        code, det = get(base + "/dataset?channel=27-188-10-1&testid=247")
        c.ok(code == 200, "详情页返回 200")
        c.ok("数据集详情" in det, "到了详情页")
        c.ok("容量保持率" in det, "有关键指标")
        c.ok("DCIR 增长率" in det, "有 DCIR 增长率")
        c.ok("这个测试做了什么" in det, "有工步预览小节")
        c.ok("dd" in det and "cc" in det, "工步内容渲染出来了")
        c.ok("不要用别的口径重算" in det, "提醒了口径问题")

        print()
        print(BAR)
        print("⑥ 出错路径")
        print(BAR)
        c.ok(get(base + "/dataset?channel=x&testid=y")[0] == 404, "不存在的数据集 → 404")
        c.ok(get(base + "/nope")[0] == 404, "乱路径 → 404")
        code, js = get(base + "/api/overview")
        c.ok(code == 200, "/api/overview 返回 200")
        try:
            obj = json.loads(js)
            c.ok(isinstance(obj.get("datasets"), list) and len(obj["datasets"]) == 2,
                 "API 里正好 2 个数据集（坏行被跳过）")
        except json.JSONDecodeError:
            c.ok(False, "API 返回的是合法 JSON")

        print()
        print(BAR)
        print("验证：")
        print(BAR)
        return c.report()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
