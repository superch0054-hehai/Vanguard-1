#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制类命令（写操作）的离线测试 —— **全程不碰设备**。

为什么单独一套：写操作是唯一能毁掉正在跑的测试的动作（数据无法追溯恢复），
所以动手之前必须先把"报文拼得对不对""回包读不读得出来""安全闸拦不拦得住"
这三件事在离线上钉死。

回包样例的来源：Empa 的 aurora-neware（MIT，github.com/empaeconversion/aurora-neware）
里的 tests/mocks.py —— 那是他们在自己实验室的真实硬件上跑出来的回应，
其中 devtype="27" 与我们的 BTS85 一致。我们的实现不发 connect 登录、
报文末尾少两个换行，所以样例只用于**校验解析**，不当作我们报文的黄金标准；
我们报文的黄金标准是协议文档（见 selftest.py 的做法）。

依赖 selftest.py 里的 FakeTransport / Checker（同一个目录）。

    python3 selftest_control.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from neware_client import (STARTABLE_STATUSES, Channel, NewareClient,
                           NewareError, parse_bts, Response)
from selftest import Checker, FakeTransport

BAR = "=" * 66

# 用真实通道：27-188-10-2（BTS85，devtype=27，和我们现场一致）
CH = Channel(devtype=27, devid=188, subdevid=10, chlid=2)
CH_B = Channel(devtype=27, devid=188, subdevid=10, chlid=3)

# --------------------------------------------------------------------------
# 回包样例（来源：aurora-neware tests/mocks.py 的真实回应，引号已正规化）
# --------------------------------------------------------------------------

ACK_START_OK = ('<bts version="1.0"><cmd>start_resp</cmd><list count="1">'
                '<start ip="127.0.0.1" devtype="27" devid="21" subdevid="1" '
                'chlid="1">ok</start></list></bts>')
ACK_START_FALSE = ('<bts version="1.0"><cmd>start_resp</cmd><list count="1">'
                   '<start ip="127.0.0.1" devtype="27" devid="21" subdevid="1" '
                   'chlid="2">false</start></list></bts>')
ACK_STOP_OK = ('<bts version="1.0"><cmd>stop_resp</cmd><list count="1">'
               '<stop ip="127.0.0.1" devtype="27" devid="21" subdevid="1" '
               'chlid="1">ok</stop></list></bts>')
ACK_STOP_FALSE = ('<bts version="1.0"><cmd>stop_resp</cmd><list count="1">'
                  '<stop ip="127.0.0.1" devtype="27" devid="21" subdevid="1" '
                  'chlid="4">false</stop></list></bts>')
ACK_LIGHT_OK = ('<bts version="1.0"><cmd>light_resp</cmd><list count="1">'
                '<light ip="127.0.0.1" devtype="27" devid="21" subdevid="1" '
                'chlid="1">ok</light></list></bts>')
ACK_CLEARFLAG_FALSE = ('<bts version="1.0"><cmd>clearflag_resp</cmd><list count="1">'
                       '<clearflag ip="127.0.0.1" devtype="27" devid="21" '
                       'subdevid="1" chlid="1">false</clearflag></list></bts>')
# reset_resp 没有 <list> 包裹（协议 2.1.19 的请求格式也是这样）
ACK_RESET_OK = ('<bts version="1.0"><cmd>reset_resp</cmd>'
                '<reset ip="127.0.0.1" devtype="27" devid="188" subdevid="10" '
                'chlid="2">ok</reset></bts>')


def status_resp(entries) -> str:
    """按 [(通道, 状态)] 生成 getchlstatus 回应。"""
    rows = "\n".join(
        f'<status ip="127.0.0.1" devtype="{c.devtype}" devid="{c.devid}" '
        f'subdevid="{c.subdevid}" chlid="{c.chlid}" reservepause="0">{s}</status>'
        for c, s in entries)
    return ('<?xml version="1.0" encoding="UTF-8" ?>\n<bts version="1.0">\n'
            f'<cmd>getchlstatus_resp</cmd>\n<list count="{len(entries)}">\n'
            f'{rows}\n</list>\n</bts>')


def ack(xml: str) -> list[dict]:
    return NewareClient.parse_ack(Response(cmd="x", raw=xml,
                                           root=parse_bts(xml.encode())))


def ack_resp(tag: str, count: int = 1, inner: str | None = None) -> str:
    """生成一个 `<cmd>{tag}_resp</cmd>` 回包，仅供 FakeTransport 应答用。"""
    if inner is None:
        inner = (f'<{tag} ip="127.0.0.1" devtype="27" devid="188" '
                 f'subdevid="10" chlid="2">ok</{tag}>')
    return (f'<bts version="1.0"><cmd>{tag}_resp</cmd>'
            f'<list count="{count}">{inner}</list></bts>')


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    print(BAR)
    print("① 写命令的报文拼装（黄金标准 = 协议文档；与 aurora 真机实现逐字对照过）")
    print(BAR)
    fake = FakeTransport({
        "getchlstatus": status_resp([(CH, "finish")]),
        "stop": ACK_STOP_OK, "light": ACK_LIGHT_OK, "start": ACK_START_OK,
        "reset": ACK_RESET_OK,
        "setpause": ack_resp("setpause"), "cancelpause": ack_resp("cancelpause"),
        "chl_ctrl": ack_resp("chl_ctrl"), "goto": ack_resp("goto"),
        "continue": ack_resp("continue"), "broadcaststop": ack_resp("broadcaststop"),
        "parallel": ack_resp("parallel"), "clearflag": ack_resp("clearflag"),
        "resetalarm": ack_resp("resetalarm"),
    })
    fake.connect()
    cli = NewareClient(fake, 5.0)

    # 帧头帧尾：和读命令同一套（selftest.py 里已验过，这里复核写命令也走同一条路）
    cli.stop([CH])
    sent = fake.sent[-1]
    c.ok(sent.startswith('<?xml version="1.0" encoding="UTF-8" ?>'),
         "报文以 XML 声明开头")
    c.ok('<bts version="1.0">' in sent, "有 <bts version=\"1.0\"> 根元素")
    c.ok(sent.rstrip().endswith("</bts>"), "报文以 </bts> 结尾")
    # 注意：尾部 #\r\n 由 TcpTransport 追加，FakeTransport 记录的是追加前的 payload，
    # 所以这里没法断言它 —— 那一段靠真机 45 小时的读流量已经验证过了。

    c.ok('<cmd>stop</cmd>' in sent, "stop 命令名")
    c.ok('<list count="1">' in sent, "stop 的 list count 正确")
    c.ok('<stop ip="127.0.0.1" devtype="27" devid="188" subdevid="10" '
         'chlid="2">true</stop>' in sent, "★ stop 通道节点与协议格式一致")

    fake.sent.clear()
    cli.light([CH], on=True)
    c.ok('<light ip="127.0.0.1" devtype="27" devid="188" subdevid="10" '
         'chlid="2">true</light>' in fake.sent[-1], "light 开 → true")
    cli.light([CH], on=False)
    c.ok('>false</light>' in fake.sent[-1], "light 关 → false")

    fake.sent.clear()
    cli.start([(CH, "c:/工步.xml", "D672035TAA112")])
    sent = fake.sent[-1]
    c.ok('<cmd>start</cmd>' in sent, "start 命令名")
    c.ok('<list count="1">' in sent, "start 的 list count 正确")
    c.ok('barcode="D672035TAA112"' in sent, "★ start 带 barcode（电池条码）")
    c.ok('>c:/工步.xml</start>' in sent, "★ start 把工步文件路径放在元素文本里")
    c.ok("remark" not in sent, "没给备注时不出现 remark 属性")
    cli.start([(CH, "c:/工步.xml", "D672035TAA112", "同批 A")])
    c.ok('remark="同批 A"' in fake.sent[-1], "给了备注时出现 remark 属性")

    # reset / setpause / cancelpause / chl_ctrl / goto / parallel / continue /
    # broadcaststop / clearflag / resetalarm —— 全部 13 个写命令在这里过一遍
    fake.sent.clear()
    cli.reset(CH, "c:/工步.xml", dbc_can=0)
    sent = fake.sent[-1]
    c.ok('<cmd>reset</cmd>' in sent, "reset 命令名")
    c.ok('DBC_CAN="0"' in sent, "reset 带 DBC_CAN（协议 2.1.19 要求）")
    c.ok('>c:/工步.xml</reset>' in sent, "reset 把工步文件路径放在元素文本里")
    # 协议 2.1.19 的 reset 请求**没有** <list> 包裹 —— 我一度以为这是缺陷，
    # 查了协议原文才确认我们是对的。这条断言就是防止以后有人"顺手补上"。
    c.ok("<list" not in sent.split("<cmd>reset</cmd>")[1],
         "★ reset 不带 <list> 包裹（协议原文如此）")

    cli.setpause([CH], cycleid=-1, stepid=-1, timeout="")
    sent = fake.sent[-1]
    c.ok('<cmd>setpause</cmd>' in sent and "<list count=\"1\">" in sent,
         "setpause 有 list 包裹")
    c.ok('cycleid="-1"' in sent and 'stepid="-1"' in sent, "setpause 带循环/工步号")
    # 协议 2.1.20 的样例就是自闭合、且带 timeout 属性：
    #   <setpause … cycleid="5" stepid="1" timeout="" />
    c.ok('timeout="" />' in sent, "★ setpause 自闭合且带 timeout 属性（协议样例如此）")

    cli.cancelpause([CH], cycleid=5, stepid=1)
    sent = fake.sent[-1]
    c.ok('<cmd>cancelpause</cmd>' in sent and 'cycleid="5"' in sent,
         "cancelpause 带循环/工步号")
    # 协议 2.1.21 的样例**没有** timeout 属性：<cancelpause … cycleid="5" stepid="1" />
    c.ok('stepid="1" />' in sent and 'timeout' not in sent,
         "★ cancelpause 自闭合且不带 timeout（与 setpause 的差别就在这里）")

    # -- 剩下 5 个写命令（chl_ctrl / goto / parallel / continue / broadcaststop）--
    # 这些是我 2026-09-29 对照协议原文逐字核过的（chl_ctrl 的 0/1、goto 的 step
    # 属性、parallel 的 paralleltype 挂在 list 上 —— 都是协议样例的原样）。
    fake.sent.clear()
    cli.chl_ctrl(CH, on=True)
    sent = fake.sent[-1]
    # 协议 2.1.10：内容 0=关闭 1=打开，**不是 true/false**，而且注明
    # "IGBT通道模块控制，需要中位机和硬件支持"
    c.ok('<chl_ctrl ip="127.0.0.1" devtype="27" devid="188" subdevid="10" '
         'chlid="2">1</chl_ctrl>' in sent, "★ chl_ctrl 开 → 1（不是 true）")
    cli.chl_ctrl(CH, on=False)
    c.ok('>0</chl_ctrl>' in fake.sent[-1], "★ chl_ctrl 关 → 0")

    fake.sent.clear()
    cli.goto(CH, step=42)
    sent = fake.sent[-1]
    # 协议 2.1.11：<goto … step="3">true</goto>
    c.ok('<goto ip="127.0.0.1" devtype="27" devid="188" subdevid="10" '
         'chlid="2" step="42">true</goto>' in sent, "★ goto 的 step 是属性、文本是 true")

    fake.sent.clear()
    cli.continue_run([CH])
    sent = fake.sent[-1]
    # 协议 2.1.9：<continue …>true</continue>
    c.ok('<continue ip="127.0.0.1" devtype="27" devid="188" subdevid="10" '
         'chlid="2">true</continue>' in sent, "★ continue 的文本是 true")

    fake.sent.clear()
    cli.broadcaststop(27, 188)
    sent = fake.sent[-1]
    # 协议 2.1.8：元素名是 **stop**（不是 broadcaststop），且只有设备级三元组
    c.ok('<stop ip="127.0.0.1" devtype="27" devid="188">true</stop>' in sent,
         "★ broadcaststop 用 <stop> 节点、只到设备级（没有通道号）")
    c.ok("chlid" not in sent, "broadcaststop 不该带通道号")

    fake.sent.clear()
    cli.parallel([CH, CH_B], paralleltype=1)
    sent = fake.sent[-1]
    # 协议 2.1.15：paralleltype 挂在 <list> 上，节点自闭合
    c.ok('<list count="2" paralleltype="1">' in sent,
         "★ parallel 的 paralleltype 挂在 <list> 属性上")
    c.ok(sent.count("<parallel ") == 2 and 'chlid="3" />' in sent,
         "parallel 每通道一个自闭合节点")

    # -- 2 处曾与协议不符、2026-09-29 已修正的（防回归）--
    fake.sent.clear()
    cli.clearflag([CH])
    sent = fake.sent[-1]
    # 协议 2.1.18：<clearflag … chlid="1">true</clearflag> —— 不是自闭合
    c.ok('<clearflag ip="127.0.0.1" devtype="27" devid="188" subdevid="10" '
         'chlid="2">true</clearflag>' in sent,
         "★ clearflag 的文本是 true（之前误发自闭合，已修）")
    c.ok("/> " not in sent.split("<clearflag")[1],
         "clearflag 不再是自闭合形态")

    fake.sent.clear()
    cli.resetalarm([CH, CH_B])
    sent = fake.sent[-1]
    # 协议 2.1.17：**设备级**（只有 ip/devtype/devid）+ 文本 true。
    # 同一台设备的重复通道要去重 —— 这里两个通道同属 188，应只发一条。
    c.ok('<resetalarm ip="127.0.0.1" devtype="27" devid="188">true</resetalarm>' in sent,
         "★ resetalarm 是设备级、文本 true（之前误发通道级自闭合，已修）")
    c.ok("subdevid" not in sent and "chlid" not in sent,
         "resetalarm 不带 subdevid/chlid")
    c.ok('<list count="1">' in sent, "resetalarm 同一设备去重后 count=1")

    # ------------------------------------------------------------------
    print()
    print(BAR)
    print("② 写命令的回包解析（以前一个解析器都没有 —— 写方法从没被调用过）")
    print(BAR)
    for name, xml, want_ok, want_tag in [
        ("start ok", ACK_START_OK, True, "start"),
        ("start false", ACK_START_FALSE, False, "start"),
        ("stop ok", ACK_STOP_OK, True, "stop"),
        ("stop false", ACK_STOP_FALSE, False, "stop"),
        ("light ok", ACK_LIGHT_OK, True, "light"),
        ("clearflag false", ACK_CLEARFLAG_FALSE, False, "clearflag"),
        ("reset 无 list", ACK_RESET_OK, True, "reset"),
    ]:
        rows = ack(xml)
        c.ok(len(rows) == 1, f"{name}：解析出 1 条", f"得到 {len(rows)} 条")
        r = rows[0]
        c.ok(r["tag"] == want_tag, f"{name}：元素名 = {want_tag}", r.get("tag"))
        c.ok(r["ok"] is want_ok, f"{name}：ok = {want_ok}", r.get("ack"))
    c.ok(ack(ACK_STOP_FALSE)[0]["chlid"] == 4, "回包里读得到是哪个通道（chlid=4）")
    c.ok(ack(ACK_STOP_OK)[0]["devtype"] == 27, "回包里读得到 devtype（=27）")

    # ------------------------------------------------------------------
    print()
    print(BAR)
    print("③ 启动安全闸：不能顶掉正在跑的测试")
    print(BAR)
    for st_name, should_pass in [("finish", True), ("stop", True),
                                 ("protect", True), ("working", False),
                                 ("pause", False)]:
        f = FakeTransport({"getchlstatus": status_resp([(CH, st_name)])})
        f.connect()
        n = NewareClient(f, 5.0)
        try:
            seen = n.assert_startable([CH])
            passed = True
            msg = f"放行，读到 {seen}"
        except NewareError as e:
            passed = False
            msg = str(e)[:70]
        c.ok(passed is should_pass,
             f"通道状态 {st_name:8s} → {'放行' if should_pass else '拒绝'}",
             msg)

    # fail-closed：读不到状态也要拒绝
    f = FakeTransport({"getchlstatus": status_resp([(CH_B, "finish")])})
    f.connect()
    n = NewareClient(f, 5.0)
    try:
        n.assert_startable([CH])
        c.ok(False, "★ 目标通道读不到状态 → 拒绝（fail-closed）", "竟然放行了")
    except NewareError as e:
        c.ok("读不到状态" in str(e), "★ 目标通道读不到状态 → 拒绝（fail-closed）",
             str(e)[:70])
        c.ok("27-188-10-2" in str(e) or f"{CH.key}" in str(e),
             "错误信息里点名了是哪个通道", str(e)[:80])

    # 混入没请求的通道：不能影响判断
    f = FakeTransport({"getchlstatus": status_resp([(CH, "finish"), (CH_B, "working")])})
    f.connect()
    n = NewareClient(f, 5.0)
    try:
        n.assert_startable([CH])
        c.ok(True, "★ 回应里混入别的通道（在跑）不影响判断")
    except NewareError as e:
        c.ok(False, "★ 回应里混入别的通道不影响判断", str(e)[:70])

    # 多个通道里有一个在跑 → 全部拒绝
    f = FakeTransport({"getchlstatus": status_resp([(CH, "finish"), (CH_B, "working")])})
    f.connect()
    n = NewareClient(f, 5.0)
    try:
        n.assert_startable([CH, CH_B])
        c.ok(False, "两个通道里有一个在跑 → 整体拒绝", "竟然放行了")
    except NewareError as e:
        c.ok("working" in str(e), "★ 两个通道里有一个在跑 → 整体拒绝", str(e)[:70])

    # ------------------------------------------------------------------
    print()
    print(BAR)
    print("④ 最强的一条性质：被拒绝时**一个字节都不发**")
    print(BAR)
    # FakeTransport 对没准备回包的命令会直接抛错，所以这里只准备 getchlstatus。
    # 如果 start 真的发出去了，就会撞上"自测未准备 start 的回应" —— 双重保险。
    for st_name in ("working", "pause"):
        f = FakeTransport({"getchlstatus": status_resp([(CH, st_name)])})
        f.connect()
        n = NewareClient(f, 5.0)
        try:
            n.start([(CH, "c:/工步.xml", "BARCODE")])
            c.ok(False, f"状态 {st_name} 时 start 应当抛错", "竟然返回了")
        except NewareError:
            c.ok(True, f"状态 {st_name} 时 start 抛错")
        c.ok(len(f.sent) == 1 and "<cmd>start</cmd>" not in "".join(f.sent),
             f"★ 状态 {st_name} 时 start 一个字节都没发（只发了查状态那一条）",
             f"实际发了 {len(f.sent)} 条")

    # force=True 才放行（唯一的逃生口，必须显式）
    f = FakeTransport({"getchlstatus": status_resp([(CH, "working")]),
                       "start": ACK_START_OK})
    f.connect()
    n = NewareClient(f, 5.0)
    n.start([(CH, "c:/工步.xml", "BARCODE")], force=True)
    c.ok(any("<cmd>start</cmd>" in s for s in f.sent),
         "force=True 时才真的发出 start（显式逃生口）")

    # 状态正常时当然要能发出去
    f = FakeTransport({"getchlstatus": status_resp([(CH, "finish")]),
                       "start": ACK_START_OK})
    f.connect()
    n = NewareClient(f, 5.0)
    n.start([(CH, "c:/工步.xml", "BARCODE")])
    c.ok(any("<cmd>start</cmd>" in s for s in f.sent),
         "通道空闲时 start 正常发出")

    # ------------------------------------------------------------------
    print()
    print(BAR)
    print("⑤ 政策锚点：可启动状态与 aurora-neware 真机实现一致")
    print(BAR)
    c.ok(tuple(STARTABLE_STATUSES) == ("finish", "stop", "protect"),
         "★ STARTABLE_STATUSES = finish/stop/protect（与 aurora 的 allowed_states 相同）",
         str(STARTABLE_STATUSES))
    c.ok("working" not in STARTABLE_STATUSES, "working（测试中）绝不在可启动之列")
    c.ok("pause" not in STARTABLE_STATUSES,
         "pause（有测试挂着）也不在可启动之列 —— 对它 start 会顶掉那份测试")

    print()
    print(BAR)
    print("⑥ control.py 的四道闸（**只测拒绝路径** —— 会通过闸的测试都会连真机，"
          "那不是离线测试该干的）")
    print(BAR)
    import control as ctl
    import tempfile
    c.ok(ctl.CONTROL_CHANNELS == {"10-6", "10-7", "10-8"},
         "控制通道 = 10-6 / 10-7 / 10-8（2026-09-29 需求方指定）",
         str(ctl.CONTROL_CHANNELS))

    # 闸① 白名单：10-6/-7/-8 放行，10-1/-2/-3（正在跑老化的）必须拒
    got = ctl.resolve_channel("10-6")
    c.ok(got.key == "27-188-10-6", "简称 10-6 展开成完整通道", got.key)
    c.ok(ctl.resolve_channel("27-188-10-7").chlid == 7, "全称也认")
    for bad in ("10-1", "10-2", "10-3", "10-9", "11-6", "27-190-10-6", "abc", "10"):
        try:
            ctl.resolve_channel(bad)
            c.ok(False, f"白名单拒绝 {bad!r}", "竟然放行了")
        except ctl.ControlError:
            c.ok(True, f"白名单拒绝 {bad!r}")
    # 10-1/-2/-3 的拒绝理由要提到白名单（这三条正是采集器在盯的老化通道）
    try:
        ctl.resolve_channel("10-1")
    except ctl.ControlError as e:
        c.ok("白名单" in str(e), "拒绝理由里说明了是白名单拦的", str(e)[:60])

    # 闸② 使能文件：不存在 → 整个工具拒绝工作
    with tempfile.TemporaryDirectory() as td:
        no_enable = Path(td) / "nope.control_enabled"
        try:
            ctl.require_enabled(no_enable)
            c.ok(False, "使能文件不存在 → 拒绝工作", "竟然放行了")
        except ctl.ControlError as e:
            c.ok("控制功能未使能" in str(e), "★ 使能文件不存在 → 整个工具拒绝")
            c.ok("总闸" in str(e), "拒绝文案说明了这是总闸", str(e)[:60])
        (Path(td) / "ok.control_enabled").write_text("ok", encoding="utf-8")
        ctl.require_enabled(Path(td) / "ok.control_enabled")
        c.ok(True, "使能文件存在 → 放行")

    # 闸③/命令黑名单 + --yes：走 main() 的完整入口（带临时使能文件，避免误连真机）
    with tempfile.TemporaryDirectory() as td:
        enable = Path(td) / "e.control_enabled"
        enable.write_text("ok", encoding="utf-8")
        audit_path = Path(td) / "control.log"

        rc = ctl.main(["broadcaststop", "10-6"], enable_file=enable, audit_path=audit_path)
        c.ok(rc == 2, "broadcaststop → 拒绝退出（故意不实现）")
        rc = ctl.main(["goto", "10-6"], enable_file=enable, audit_path=audit_path)
        c.ok(rc == 2, "goto → 拒绝（数据缺口不可逆）")
        rc = ctl.main(["reset", "10-6"], enable_file=enable, audit_path=audit_path)
        c.ok(rc == 2, "reset → 拒绝")
        rc = ctl.main(["parallel", "10-6"], enable_file=enable, audit_path=audit_path)
        c.ok(rc == 2, "parallel → 拒绝（协议里参数表是空的）")
        rc = ctl.main(["chl_ctrl", "10-6"], enable_file=enable, audit_path=audit_path)
        c.ok(rc == 2, "chl_ctrl → 拒绝（需要中位机和硬件支持）")

        rc = ctl.main(["stop", "10-6"], enable_file=enable, audit_path=audit_path)
        c.ok(rc == 2, "★ stop 不带 --yes → 拒绝")
        rc = ctl.main(["start", "10-6", "--step-file", "x.xml", "--barcode", "B"],
                      enable_file=enable, audit_path=audit_path)
        c.ok(rc == 2, "★ start 不带 --yes → 拒绝")

        # 被拒的也要留痕（审计日志）
        c.ok(audit_path.exists(), "被拒绝的操作写了审计日志")
        if audit_path.exists():
            lines = [json.loads(l) for l in
                     audit_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            c.ok(len(lines) >= 7, f"审计日志有 {len(lines)} 条记录（每条拒绝一行）")
            blocked = [r for r in lines if r.get("result") == "blocked"]
            c.ok(any(r["command"] == "broadcaststop" for r in blocked),
                 "★ broadcaststop 的拒绝也留了痕")
            c.ok(all(r.get("time") and r.get("user") is not None for r in lines),
                 "每条审计都有时间和用户")

    # light 的参数：默认开灯，--off 才关灯
    a = ctl.build_parser().parse_args(["light", "10-6"])
    c.ok(a.on is True, "light 不带参数 = 开灯")
    a = ctl.build_parser().parse_args(["light", "10-6", "--off"])
    c.ok(a.on is False, "light --off = 关灯")

    print()
    print(BAR)
    print("验证：")
    print(BAR)
    return c.report()


if __name__ == "__main__":
    raise SystemExit(main())
