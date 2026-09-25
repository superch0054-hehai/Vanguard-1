#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export_review.py 的离线端到端测试（不需要真机）。

用现场实测到的真实回应形态（含尾部 # 标识）模拟一台客户端，
验证审查包能被正确生成。不需要真机。

    python selftest_export.py
"""

import re
import sys
from pathlib import Path

import export_review
import selftest
from neware_client import NewareError, Transport

GETDEVINFO = """<?xml version="1.0" encoding="UTF-8"?>
<bts version="1.0">
  <cmd>getdevinfo_resp</cmd>
  <client version="BTS Client 2025.11.24(R3) (64)" />
  <serverip count="1">
    <server ip="127.0.0.1" port="3306" version="8.0.4.546 (2025.11.24)" devCount="6">
      <dev ip="192.168.1.188" devid="188" zwjVersion="4S_8.0.19.0" xwjCount="10">
        <xwj id="10" xwjVersion="M04310702" xwjGuid="AABB" />
      </dev>
    </server>
  </serverip>
  <middle count="2">
    <channel ip="127.0.0.1" devtype="27" devid="188" subdevid="10" Channelid="1">true</channel>
    <channel ip="127.0.0.1" devtype="27" devid="188" subdevid="10" Channelid="2">true</channel>
  </middle>
</bts>
"""

STATUS = """<?xml version="1.0" encoding="UTF-8"?>
<bts version="1.0">
  <cmd>getchlstatus_resp</cmd>
  <list count="1">
    <status ip="127.0.0.1" devtype="27" devid="188" subdevid="10" chlid="2" reservepause="1">working</status>
  </list>
</bts>
"""

INQUIRE = """<?xml version="1.0" encoding="UTF-8"?>
<bts version="1.0">
  <cmd>inquire_resp</cmd>
  <list count="1">
    <inquire dev="27-188-10-2-0" cycle_id="55" step_id="10" step_type="rest"
      workstatus="working" barcode="D672035TAA112" current="0" voltage="1.4961"
      capacity="0" energy="0" totaltime="43726" relativetime="143.9"
      auxtemp="0" auxvol="0" open_or_close="0" log_code="102000" CPU1="34.73" />
  </list>
</bts>
"""

INQUIREDF = """<?xml version="1.0" encoding="UTF-8"?>
<bts version="1.0">
  <cmd>inquiredf_resp</cmd>
  <list count="1">
    <chl devtype="27" devid="188" subdevid="10" chlid="2" testid="244" count="12092">false</chl>
  </list>
</bts>
"""

# 按现场实测的形态造几个循环：rest / cc / rest / dc / rest
_STEPS = [
    (1, "rest", "0.277095971679688", "0.27730244140625", "0", "0", "0", "0", "0"),
    (2, "cc", "0.294440112304687", "3.299961328125", "0.2", "0.2",
     "2.11492042581085E-05", "4.04964375775307E-05", "1208499.9972422"),
    (3, "rest", "3.237841796875", "2.089097265625", "0", "0", "0", "0",
     "4374836.02070782"),
    (4, "dc", "2.05503671875", "0.499961474609375", "-0.2", "-0.2",
     "2.8777731131413E-06", "2.59706439464935E-06", "2400277.43569898"),
    (1, "rest", "0.52266923828125", "1.02011845703125", "0", "0", "0", "0",
     "1599194.1932233"),
    (2, "cc", "1.03866025390625", "3.2999546875", "0.2", "0.2",
     "3.19450555252843E-06", "7.37252094040741E-06", "1306782.51322715"),
    (3, "rest", "3.2463908203125", "2.1234390625", "0", "0", "0", "0",
     "3772246.16639628"),
    (4, "dc", "2.0865359375", "0.500048095703125", "-0.2", "-0.2",
     "2.4466553441016E-06", "2.27197574531601E-06", "2600995.90966926"),
]


def _step_layer_resp() -> str:
    rows = []
    seq = 1
    stepindex = 0
    for cycle in (1, 2, 3):
        for stepid, stype, sv, ev, sc, ec, cap, eng, dcir in _STEPS:
            stepindex += 1
            rows.append(
                f'<data startseqid="{seq}" endseqid="{seq + 9}" stepindex="{stepindex}" '
                f'stepid="{stepid}" cycleid="{cycle}" steptype="{stype}" steptime="60000" '
                f'endatime="2026-09-23 10:{stepindex:02d}:00" startvolt="{sv}" '
                f'endvolt="{ev}" startcurr="{sc}" endcurr="{ec}" cap="{cap}" '
                f'eng="{eng}" dcir="{dcir}" />')
            seq += 10
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<bts version=\"1.0\">\n"
            "<cmd>downloadStepLayer_resp</cmd>\n"
            "<downloadStepLayer devtype=\"27\" devid=\"188\" subdevid=\"10\" "
            "chlid=\"2\" testid=\"244\" />\n"
            f"<list count=\"{len(rows)}\">" + "".join(rows) + "</list>\n</bts>\n")


def _detail_resp(startpos: int, count: int) -> str:
    total = 60
    rows = []
    end = min(startpos + count, total + 1)
    for i in range(startpos, end):
        rows.append(
            f'<data seqid="{i}" stepid="{(i % 4) + 1}" cycleid="{(i // 12) + 1}" '
            f'steptype="cc" testtime="{(i % 12) * 1000}" '
            f'atime="2026-09-23 10:{(i // 60):02d}:{i % 60:02d}" '
            f'volt="{1.5 + (i % 20) * 0.08:.4f}" curr="0.2" cap="{i * 1.5e-7:.9f}" '
            f'eng="{i * 3e-7:.9f}" CPU="34.73" />')
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<bts version=\"1.0\">\n"
            "<cmd>download_resp</cmd>\n"
            f"<list count=\"{len(rows)}\">" + "".join(rows) + "</list>\n</bts>\n")


class Dispatch(Transport):
    """按命令分发固定回应，并在末尾加上现场实测的 # 标识。"""

    name = "dispatch"

    def __init__(self, page_size: int = 25) -> None:
        self.page_size = page_size
        self.sent: list[str] = []

    def connect(self) -> None:
        pass

    def send_recv(self, payload: bytes, timeout=None) -> bytes:
        text = payload.decode("utf-8")
        self.sent.append(text)
        if "<cmd>getdevinfo</cmd>" in text:
            body = GETDEVINFO
        elif "<cmd>getchlstatus</cmd>" in text:
            body = STATUS
        elif "<cmd>inquire</cmd>" in text:
            body = INQUIRE
        elif "<cmd>inquiredf</cmd>" in text:
            body = INQUIREDF
        elif "<cmd>downloadStepLayer</cmd>" in text:
            body = _step_layer_resp()
        elif "<cmd>download</cmd>" in text:
            startpos = int(re.search(r'startpos="(\d+)"', text).group(1))
            body = _detail_resp(startpos, self.page_size)
        else:
            raise NewareError(f"测试未准备该命令：{text[:80]}")
        return body.encode("utf-8") + b"\n\n#\r\n"


def main() -> int:
    outdir = Path("_review_out")
    if outdir.exists():
        for f in outdir.iterdir():
            f.unlink()

    export_review.build_transport = lambda args: Dispatch(page_size=25)
    sys.argv = [
        "export_review.py",
        "--host", "10.201.47.169",
        "--channel", "27-188-10-2",
        "--outdir", str(outdir),
    ]
    rc = export_review.main()
    if rc != 0:
        print("main() 返回非零", rc, file=sys.stderr)
        return rc

    print("\n" + "=" * 68)
    readme = (outdir / "说明.md").read_text(encoding="utf-8")
    print("说明.md 前 60 行：")
    print("\n".join(readme.splitlines()[:60]))
    print("..." + "\n")
    print("=" * 68)

    checks = [
        ("说明.md 生成", (outdir / "说明.md").exists()),
        ("明细.csv 生成", (outdir / "明细.csv").exists()),
        ("工步层.csv 生成", (outdir / "工步层.csv").exists()),
        ("含客户端版本", "2025.11.24" in readme),
        ("含通道地址", "27-188-10-2" in readme),
        ("含条码", "D672035TAA112" in readme),
        ("含 testid", "244" in readme),
        ("含辅助通道提示", "CPU" in readme),
        ("含 10 个审查问题", readme.count("**1.") == 1 and "**10." in readme),
        ("口径存疑已标注", "待确认" in readme),
        ("完整性提示正确", "完整=否" in readme or "是否完整：否" in readme),
    ]
    print("验证：")
    ok = True
    for label, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
        ok = ok and passed

    rows = (outdir / "明细.csv").read_text(encoding="utf-8-sig").strip().splitlines()
    print(f"\n明细.csv 行数（含表头）: {len(rows)}")
    print(f"明细.csv 表头: {rows[0]}")
    step_rows = (outdir / "工步层.csv").read_text(encoding="utf-8-sig").strip().splitlines()
    print(f"工步层.csv 行数（含表头）: {len(step_rows)}")

    print(f"\n{'全部通过 ✅' if ok else '有失败项 ❌'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
