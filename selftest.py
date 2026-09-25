#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：用协议文档中的真实回应样例验证命令拼装与解析逻辑。

没有真机时，这是唯一能确认"代码本身没问题"的方式 —— 从而把故障面
收敛到"设备 / 客户端 / 现场配置"这一侧，而不是"是不是我代码写错了"。

    python selftest.py
"""

from __future__ import annotations

import re
from typing import Any

from neware_client import (
    Channel,
    NewareClient,
    NewareError,
    Response,
    Transport,
    parse_bts,
    parse_channel,
    parse_dev_field,
)
from probe import (
    aux_columns,
    check_monotonic,
    check_stepid_within_cycle,
    check_time_unit,
    soh_preview,
)
from probe import _sig


def resp(xml: str) -> Response:
    """把样例 XML 包成 Response，供静态解析方法使用。"""
    root = parse_bts(xml.encode("utf-8"))
    cmd_el = root.find("cmd")
    cmd = cmd_el.text.strip() if cmd_el is not None and cmd_el.text else "?"
    return Response(cmd=cmd, raw=xml, root=root)

# --------------------------------------------------------------------------
# 协议文档 / API 文档截图中的真实回应样例（引号已正規化）
# --------------------------------------------------------------------------

GETDEVINFO_RESP = """<?xml version="1.0" encoding="UTF-8" ?>
<bts version="1.0">
<cmd>getdevinfo_resp</cmd>
<serverip count="2">
<server ip="192.168.1.33" port="3306"/>
<server ip="192.168.1.34" port="3306"/>
</serverip>
<middle count="4">
<channel ip="192.168.1.35" devtype="22" devid="1" subdevid="1" Channelid="1">true</channel>
<channel ip="192.168.1.35" devtype="22" devid="1" subdevid="2" Channelid="2">true</channel>
<channel ip="192.168.1.36" devtype="22" devid="1" subdevid="3" Channelid="3">true</channel>
<channel ip="192.168.1.36" devtype="22" devid="1" subdevid="3" Channelid="1">false</channel>
</middle>
</bts>
"""

GETCHLSTATUS_RESP = """<?xml version="1.0" encoding="UTF-8" ?>
<bts version="1.0">
<cmd>getchlstatus_resp</cmd>
<list count="3">
<status ip="127.0.0.1" devtype="22" devid="3" subdevid="2" chlid="8" reservepause="0" >working</status>
<status ip="127.0.0.1" devtype="22" devid="3" subdevid="1" chlid="6" reservepause="0" >stop</status>
<status ip="127.0.0.1" devtype="22" devid="3" subdevid="1" chlid="3" reservepause="0" >finish</status>
</list>
</bts>
"""

INQUIRE_RESP = """<?xml version="1.0" encoding="UTF-8" ?>
<bts version="1.0">
<cmd>inquire_resp</cmd>
<list count="1">
<inquire dev="22-3-2-1-10" cycle_id="1" step_id="1" step_type="cc" workstatus="stop" barcode="G48100230100" current="1" voltage="2" capacity="100" energy="10" totaltime="3090909" relativetime="7230910" auxtemp="20" auxvol="1" open_or_close="0" log_code="0" V1="0.1103" T1="110.4" t1="10.3" t2="20.3"/>
</list>
</bts>
"""

# download_resp：取自 API 使用操作文档的实测截图（2023-04-21 那组）
DOWNLOAD_RESP = """<?xml version="1.0" encoding="UTF-8"?>
<bts version="1.0">
<cmd>download_resp</cmd>
<download devtype="25" devid="41" subdevid="1" chlid="1" testid="71" startpos="1" count="1000" auxid="0" />
<list count="4">
<data seqid="1" stepid="1" cycleid="0" steptype="rest" testtime="0" atime="2023-04-21 16:07:14" volt="1.226438" curr="1.471726" cap="0" eng="0" V1="0.601604" T1="320.6" Thk1="0.1199899731791" N14="110" />
<data seqid="2" stepid="2" cycleid="0" steptype="rest" testtime="1000" atime="2023-04-21 16:07:15" volt="1.226428" curr="1.471716" cap="0" eng="0" V1="0.601604" T1="320.6" Thk1="0.1199899731791" N14="110" />
<data seqid="3" stepid="3" cycleid="0" steptype="rest" testtime="2000" atime="2023-04-21 16:07:16" volt="1.226418" curr="1.471706" cap="0" eng="0" V1="0.601604" T1="320.6" Thk1="0.1199899731791" N14="110" />
<data seqid="4" stepid="4" cycleid="0" steptype="rest" testtime="3000" atime="2023-04-21 16:07:17" volt="1.226408" curr="1.471696" cap="0" eng="0" V1="0.601604" T1="320.6" Thk1="0.1199899731791" N14="110" />
</list>
</bts>
"""

# 工步层：前半段照抄协议文档样例（只有 cc，没有放电工步）
STEPLAYER_DOC_RESP = """<?xml version="1.0" encoding="UTF-8"?>
<bts version="1.0">
  <cmd>downloadStepLayer_resp</cmd>
  <downloadStepLayer devtype="24" devid="1" subdevid="2" chlid="1" testid="2818580404" />
  <list count="2">
    <data startseqid="1" endseqid="61" stepindex="1" stepid="1" cycleid="1" steptype="rest" steptime="60000" endatime="2020-03-20 14:43:30" startvolt="2.5" endvolt="2.5" startcurr="0" endcurr="0" cap="0" eng="0" dcir="0" />
    <data startseqid="62" endseqid="662" stepindex="2" stepid="2" cycleid="1" steptype="cc" steptime="600000" endatime="2020-03-20 14:43:36" startvolt="2.501" endvolt="3.101" startcurr="1" endcurr="1" cap="0.166666666666667" eng="0.833333333333333" dcir="100" />
  </list>
</bts>
"""

# 工步层：人工合成 3 圈充放电，用于校验 SOH 算法本身
STEPLAYER_SYNTH_RESP = """<?xml version="1.0" encoding="UTF-8"?>
<bts version="1.0">
  <cmd>downloadStepLayer_resp</cmd>
  <downloadStepLayer devtype="25" devid="41" subdevid="1" chlid="1" testid="7" />
  <list count="6">
    <data startseqid="1" endseqid="10" stepindex="1" stepid="1" cycleid="1" steptype="cc" steptime="1000" endatime="2024-01-01 10:00:00" startvolt="3.0" endvolt="4.2" startcurr="0.2" endcurr="0.2" cap="0.200" eng="0.800" dcir="0" />
    <data startseqid="11" endseqid="20" stepindex="2" stepid="2" cycleid="1" steptype="dc" steptime="1000" endatime="2024-01-01 11:00:00" startvolt="4.2" endvolt="3.0" startcurr="-0.2" endcurr="-0.2" cap="0.190" eng="0.750" dcir="100" />
    <data startseqid="21" endseqid="30" stepindex="3" stepid="1" cycleid="2" steptype="cc" steptime="1000" endatime="2024-01-02 10:00:00" startvolt="3.0" endvolt="4.2" startcurr="0.2" endcurr="0.2" cap="0.200" eng="0.798" dcir="0" />
    <data startseqid="31" endseqid="40" stepindex="4" stepid="2" cycleid="2" steptype="dc" steptime="1000" endatime="2024-01-02 11:00:00" startvolt="4.2" endvolt="3.0" startcurr="-0.2" endcurr="-0.2" cap="0.185" eng="0.730" dcir="110" />
    <data startseqid="41" endseqid="50" stepindex="5" stepid="1" cycleid="3" steptype="cc" steptime="1000" endatime="2024-01-03 10:00:00" startvolt="3.0" endvolt="4.2" startcurr="0.2" endcurr="0.2" cap="0.200" eng="0.795" dcir="0" />
    <data startseqid="51" endseqid="60" stepindex="6" stepid="2" cycleid="3" steptype="dc" steptime="1000" endatime="2024-01-03 11:00:00" startvolt="4.2" endvolt="3.0" startcurr="-0.2" endcurr="-0.2" cap="0.180" eng="0.710" dcir="120" />
  </list>
</bts>
"""

INQUIREDF_RESP = """<?xml version="1.0" encoding="UTF-8" ?>
<bts version="1.0">
<cmd>inquiredf_resp</cmd>
<list count="2">
<chl devtype="22" devid="3" subdevid="2" chlid="8" testid="11" count="10">true</chl>
<chl devtype="22" devid="3" subdevid="1" chlid="6" testid="10" count="11">false</chl>
</list>
</bts>
"""

DOWNLOADLOG_RESP = """<?xml version="1.0" encoding="UTF-8"?>
<bts version="1.0">
  <cmd>downloadlog_resp</cmd>
  <download devtype="24" devid="1" subdevid="1" chlid="1" testid="2818592789" log_lever="0" />
  <list count="2">
    <data seqid="1" log_code="8" atime="2022-04-06 09:48:38" />
    <data seqid="1202" log_code="5" atime="2022-04-06 09:48:51" />
  </list>
</bts>
"""

START_RESP = """<?xml version="1.0" encoding="UTF-8" ?>
<bts version="1.0">
<cmd>start_resp</cmd>
<list count="2">
<start ip="127.0.0.1" devtype="25" devid="41" subdevid="1" chlid="1">ok</start>
<start ip="127.0.0.1" devtype="25" devid="41" subdevid="1" chlid="2">false</start>
</list>
</bts>
"""

# 故意使用厂商文档里出现过的中文引号，验证宽容解析
CURLY_QUOTE_RESP = """<?xml version="1.0" encoding="UTF-8" ?>
<bts version="1.0">
<cmd>getdevinfo_resp</cmd>
<middle count=”1”>
<channel ip=”127.0.0.1” devtype=”22” devid=”3” subdevid=”1” Channelid=”1”>true</channel>
</middle>
</bts>
"""


class FakeTransport(Transport):
    """把 <cmd> 映射到固定回应，并记录所有发出的报文。"""

    name = "fake"

    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.sent: list[str] = []
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def send_recv(self, payload: bytes, timeout: float | None = None) -> bytes:
        text = payload.decode("utf-8")
        self.sent.append(text)
        match = re.search(r"<cmd>([^<]+)</cmd>", text)
        cmd = match.group(1).strip() if match else ""
        if cmd not in self.responses:
            raise NewareError(f"自测未准备 {cmd} 的回应")
        return self.responses[cmd].encode("utf-8")


# --------------------------------------------------------------------------
# 断言
# --------------------------------------------------------------------------

class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, cond: bool, label: str, extra: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}" + (f"  <- {extra}" if extra else ""))

    def eq(self, got: Any, want: Any, label: str) -> None:
        self.ok(got == want, label, f"得到 {got!r}，期望 {want!r}")

    def report(self) -> int:
        print(f"\n通过 {self.passed} 项，失败 {len(self.failed)} 项")
        for name in self.failed:
            print(f"  - {name}")
        return 1 if self.failed else 0


def test_channel(c: Checker) -> None:
    print("\n[1] 通道四元组解析")
    ch = parse_channel("25-41-1-1@192.168.1.20")
    c.eq((ch.devtype, ch.devid, ch.subdevid, ch.chlid, ch.ip),
         (25, 41, 1, 1, "192.168.1.20"), "解析 25-41-1-1@192.168.1.20")
    c.eq(ch.key, "25-41-1-1", "key 生成")
    c.eq(ch.attrs(), 'ip="192.168.1.20" devtype="25" devid="41" subdevid="1" chlid="1"',
         "属性串拼装")
    c.eq(ch.attrs({"barcode": "D672035TAA11"}),
         'ip="192.168.1.20" devtype="25" devid="41" subdevid="1" chlid="1" barcode="D672035TAA11"',
         "带条码的属性串")
    dev_ch, aux = parse_dev_field("22-3-2-1-10")
    c.eq((dev_ch.devtype, dev_ch.devid, dev_ch.subdevid, dev_ch.chlid, aux),
         (22, 3, 2, 1, 10), "解析 inquire 的 dev 字段（含辅助通道号）")
    try:
        parse_channel("25-41-1")
        c.ok(False, "非法通道应抛错")
    except ValueError:
        c.ok(True, "非法通道正确抛错")


def test_commands(c: Checker) -> None:
    print("\n[2] 命令拼装（对照协议文档）")
    channels = [Channel(22, 3, 2, 1), Channel(22, 3, 2, 12)]
    fake = FakeTransport({
        "getdevinfo": GETDEVINFO_RESP,
        "getchlstatus": GETCHLSTATUS_RESP,
        "inquire": INQUIRE_RESP,
        "download": DOWNLOAD_RESP,
        "downloadStepLayer": STEPLAYER_DOC_RESP,
        "inquiredf": INQUIREDF_RESP,
        "downloadlog": DOWNLOADLOG_RESP,
        "start": START_RESP,
    })
    fake.connect()
    client = NewareClient(fake, 5.0)

    client.getdevinfo()
    c.ok('<cmd>getdevinfo</cmd>' in fake.sent[-1], "getdevinfo 报文")
    c.ok(fake.sent[-1].startswith('<?xml version="1.0" encoding="UTF-8" ?>'),
         "报文以 XML 声明开头")
    c.ok(fake.sent[-1].rstrip().endswith("</bts>"), "报文以 </bts> 结尾")

    client.getchlstatus(channels)
    sent = fake.sent[-1]
    c.ok('<cmd>getchlstatus</cmd>' in sent, "getchlstatus 命令名")
    c.ok('<list count="2">' in sent, "getchlstatus count 正确")
    c.ok('<status ip="127.0.0.1" devtype="22" devid="3" subdevid="2" chlid="12">true</status>' in sent,
         "getchlstatus 通道节点格式")

    client.inquire(channels, aux=0, barcode=True)
    sent = fake.sent[-1]
    c.ok('aux="0" barcode="1"' in sent, "inquire 的 aux / barcode 属性")

    client.download(Channel(25, 41, 1, 1), auxid=0, testid=71, startpos=1, count=1000)
    sent = fake.sent[-1]
    c.ok('<download ip="127.0.0.1" devtype="25" devid="41" subdevid="1" chlid="1" '
         'auxid="0" testid="71" startpos="1" count="1000"/>' in sent,
         "download 单标签自闭合格式（与实测截图一致）")

    try:
        client.download(Channel(25, 41, 1, 1), count=2000)
        c.ok(False, "count 超过 1000 应抛错")
    except ValueError:
        c.ok(True, "count 超过 1000 正确抛错")

    client.download_steplayer(Channel(24, 1, 2, 1), testid=0, dcir=1)
    sent = fake.sent[-1]
    c.ok('<downloadStepLayer ip="127.0.0.1" devtype="24" devid="1" subdevid="2" chlid="1" '
         'testid="0" dcir="1"/>' in sent, "downloadStepLayer 节点")
    c.ok('<V1I1 previousstep="1" type="1" value=""/>' in sent, "V1I1 默认取值方式")
    c.ok('<V2I2 previousstep="0" type="0" value=""/>' in sent, "V2I2 默认取值方式")

    client.start([(Channel(25, 41, 1, 1), r"D:\stepManager\test\1.xml", "D672035TAA11")],
                 backup={"backupdir": r"D:\temp\backup", "filetype": 0, "filenametype": 1})
    sent = fake.sent[-1]
    c.ok('<list count="1">' in sent, "start count")
    c.ok('barcode="D672035TAA11">D:\\stepManager\\test\\1.xml</start>' in sent,
         "start 路径作为元素文本、条码作为属性")
    c.ok('<backup backupdir="D:\\temp\\backup" filetype="0" filenametype="1" />' in sent,
         "backup 自动备份节点（filetype=0 表示备份 NDA）")

    # QL-165829：同一工步下发多通道时，各通道可自定义备注
    client.start([
        (Channel(25, 41, 1, 1), r"D:\s\1.xml", "BAR001", "SC20-第3批"),
        (Channel(25, 41, 1, 2), r"D:\s\1.xml", "BAR002", "SC20-第3批"),
    ])
    sent = fake.sent[-1]
    c.ok('<list count="2">' in sent, "start 多通道 count")
    c.ok('barcode="BAR001" remark="SC20-第3批">D:\\s\\1.xml</start>' in sent,
         "remark 备注作为 start 元素的属性（QL-165829）")
    c.ok(sent.count('remark="SC20-第3批"') == 2, "两个通道各自带备注")

    client.start([(Channel(25, 41, 1, 1), r"D:\s\2.xml", "BAR003")])
    c.ok("remark" not in fake.sent[-1], "不带备注时不应出现 remark 属性")

    before = sum(1 for s in fake.sent if "<cmd>download</cmd>" in s)
    client.download_all(Channel(25, 41, 1, 1), testid=71)
    after = sum(1 for s in fake.sent if "<cmd>download</cmd>" in s)
    c.eq(after - before, 1, "download_all 遇到不足 1000 条即停止分页")
    c.ok('startpos="1"' in fake.sent[-1], "download_all 首次请求 startpos=1")


def test_parsing(c: Checker) -> None:
    print("\n[3] 回应解析")
    info = NewareClient.parse_devinfo(resp(GETDEVINFO_RESP))
    c.eq(len(info["servers"]), 2, "解析服务器列表")
    c.eq(info["channel_count"], 4, "解析 middle count")
    c.eq(len(info["channels"]), 4, "解析通道列表")
    c.eq(info["channels"][0]["chlid"], 1, "解析 Channelid 属性（非 chlid）")
    c.eq(info["channels"][3]["can_light"], False, "解析可点灯标志")

    statuses = NewareClient.parse_status(resp(GETCHLSTATUS_RESP))
    c.eq([s["status"] for s in statuses], ["working", "stop", "finish"],
         "解析通道状态（含只在 2.1.4 出现的 finish）")
    c.eq(statuses[2]["status_cn"], "完成", "finish 中文映射")
    c.eq(statuses[0]["reservepause"], "0", "解析 reservepause 属性")

    rt = NewareClient.parse_inquire(resp(INQUIRE_RESP))
    c.eq(rt[0]["devtype"], 22, "从 dev 字段取 devtype")
    c.eq(rt[0]["auxid"], 10, "从 dev 字段取 auxid")
    c.eq(rt[0]["voltage"], "2", "解析 voltage")
    c.eq(rt[0]["barcode"], "G48100230100", "解析 barcode")
    c.ok("V1" in rt[0]["aux"] and "T1" in rt[0]["aux"], "辅助通道被归入 aux 子字典")

    rows = NewareClient.parse_data(resp(DOWNLOAD_RESP))
    c.eq(len(rows), 4, "解析 DF 明细条数")
    c.eq(rows[0]["seqid"], "1", "解析 seqid")
    c.eq(rows[0]["V1"], "0.601604", "解析辅助通道 V1")

    checks = NewareClient.parse_inquiredf(resp(INQUIREDF_RESP))
    c.eq(checks[0]["complete"], True, "inquiredf 完整标记 true")
    c.eq(checks[1]["complete"], False, "inquiredf 完整标记 false")
    c.eq(checks[1]["uploaded"], 11, "inquiredf 已上传条数")

    logs = NewareClient.parse_data(resp(DOWNLOADLOG_RESP))
    c.eq([r["log_code"] for r in logs], ["8", "5"], "解析日志码")

    name_map = {"cc": "恒流充电", "dc": "恒流放电", "rest": "搁置"}

    def _steptype_cn(value: str) -> str:
        return name_map.get(value, "未定义")

    start_root = parse_bts(START_RESP.encode("utf-8"))
    c.eq(start_root.find("cmd").text, "start_resp", "start_resp 命令名")
    c.eq([el.text.strip() for el in start_root.iter("start")], ["ok", "false"],
         "start_resp 逐通道的 ok / false 结果")
    c.eq(_steptype_cn("cc"), "恒流充电", "工步类型中文映射")

    try:
        parse_bts(CURLY_QUOTE_RESP.encode("utf-8"))
        c.ok(True, "宽容解析厂商文档中的中文引号")
    except NewareError as exc:
        c.ok(False, "宽容解析中文引号", str(exc))


def test_time_unit(c: Checker) -> None:
    print("\n[4] 时间单位校验（ms / s 陷阱）")
    rows = NewareClient.parse_data(resp(DOWNLOAD_RESP))
    unit = check_time_unit(rows)
    c.eq(unit["unit"], "ms", "从真实样本判定 testtime 单位为毫秒")
    c.ok(300 <= (unit["ratio"] or 0) <= 3000, "比值落在毫秒区间", str(unit))

    seconds_rows = [
        {"testtime": "0", "atime": "2024-01-01 10:00:00"},
        {"testtime": "10", "atime": "2024-01-01 10:00:10"},
    ]
    c.eq(check_time_unit(seconds_rows)["unit"], "s", "识别出以秒为单位的异常样本")
    c.eq(check_time_unit([{"testtime": "0", "atime": ""}])["unit"], "unknown",
         "样本不足时返回 unknown")

    c.eq(check_monotonic(rows, "seqid"), True, "seqid 单调递增")
    c.eq(check_monotonic([{"seqid": "5"}, {"seqid": "3"}], "seqid"), False,
         "识别 seqid 非单调")

    cols = aux_columns(rows)
    c.ok(set(cols) >= {"V1", "T1", "Thk1", "N14"}, "自动发现辅助通道列名", str(cols))
    c.ok(not (set(cols) & {"volt", "curr", "cap", "eng", "seqid", "atime"}),
         "标准字段未被误判为辅助通道", str(cols))


def test_soh(c: Checker) -> None:
    print("\n[5] SOH 指标计算")
    synth = NewareClient.parse_data(resp(STEPLAYER_SYNTH_RESP))
    soh = soh_preview(synth)
    c.ok(soh["available"], "合成数据可算出 SOH")
    c.eq(soh["cycle_count"], 3, "识别循环数")
    c.eq(soh["cycle_range"], [1, 3], "循环号范围")
    c.eq(soh["first_discharge_cap_mah"], 0.19, "首圈放电容量")
    c.eq(soh["last_discharge_cap_mah"], 0.18, "末圈放电容量")
    c.eq(soh["capacity_retention_pct"], 94.74, "容量保持率 = 0.18/0.19")
    c.eq(soh["coulomb_efficiency_first_pct"], 95.0, "首圈库仑效率 = 0.19/0.20")
    c.eq(soh["coulomb_efficiency_last_pct"], 90.0, "末圈库仑效率 = 0.18/0.20")
    c.eq(soh["energy_efficiency_first_pct"], 93.75, "首圈能量效率 = 0.750/0.800")
    c.eq(soh["dcir_first_mohm"], 100.0, "首圈 DCIR")
    c.eq(soh["dcir_last_mohm"], 120.0, "末圈 DCIR")
    c.eq(soh["dcir_growth_pct"], 120.0, "DCIR 增长")
    c.eq(len(soh["retention_curve"]), 3, "容量保持率曲线点数")
    c.ok(all("cycle" in p for p in soh["retention_curve"]),
         "曲线点数没超过上限时，不带截断提示的 note")

    # 曲线超长时要截断到 CURVE_CAP，并且**必须**在 note 里说清截断到多少 ——
    # 否则读的人会以为下面是完整曲线（旧版就是这么误导的）。
    from probe import CURVE_CAP
    many = [{"cycleid": str(i), "steptype": "dc", "cap": str(0.2 - 0.0001 * i),
             "eng": "0.7", "dcir": str(100 + i)} for i in range(1, CURVE_CAP + 61)]
    many_soh = soh_preview(many)
    c.eq(len(many_soh["retention_curve"]), CURVE_CAP + 1,
         f"超长曲线截断到 {CURVE_CAP} 点 + 1 条 note")
    note = many_soh["retention_curve"][-1].get("note", "")
    c.ok("仅列前" in note and str(CURVE_CAP) in note,
         "截断时的 note 说清了「只列前多少圈」", note)

    print("\n[5b] ★ 小容量不能被取整抹掉（真实数据上踩过）")
    # 原来用 round(v, 6)：4.89e-07 → 0，5.05e-07 → 1e-06。既丢数据又歪曲数值，
    # 而且从结果上看不出来 —— 一份真实数据的逐圈放电容量整列被抹成了 0。
    c.eq(_sig(4.89e-07), 4.89e-07, "4.89e-07 不被抹成 0")
    c.eq(_sig(5.05e-07), 5.05e-07, "5.05e-07 不被放大成 1e-06")
    c.eq(_sig(0.000947), 0.000947, "常规量级（mAh 级）不受影响")
    c.eq(_sig(0.0), 0.0, "0 仍然是 0")
    c.eq(_sig(None), None, "非数字原样返回，不抛异常")
    tiny = [{"cycleid": str(i), "steptype": "dc", "cap": str(5.0e-07 - i * 1e-9),
             "eng": "1e-07", "dcir": "100"} for i in range(1, 6)]
    tiny_soh = soh_preview(tiny)
    c.ok(tiny_soh["first_discharge_cap_mah"] != 0,
         "1e-07 量级的容量不会被算成 0",
         str(tiny_soh["first_discharge_cap_mah"]))
    c.ok(all(p["dis_cap"] != 0 for p in tiny_soh["retention_curve"]),
         "逐圈曲线里的小容量也都保住了")

    doc = NewareClient.parse_data(resp(STEPLAYER_DOC_RESP))
    doc_soh = soh_preview(doc)
    c.eq(doc_soh["available"], False,
         "协议文档样例（无放电工步）正确报不可用而非出错")
    c.ok("放电容量" in doc_soh["reason"], "不可用原因说明清楚", doc_soh["reason"])


class PaginatingTransport(Transport):
    """模拟真实的分页：每次只返回 count 条，直到数据发完。

    分页是最容易"静默丢数据"的地方，必须专门测。
    """

    name = "paginating"

    def __init__(self, total_rows: int, page_size: int = 3) -> None:
        self.total_rows = total_rows
        self.page_size = page_size
        self.sent: list[str] = []
        self.startpos_seen: list[int] = []

    def connect(self) -> None:
        pass

    def send_recv(self, payload: bytes, timeout: float | None = None) -> bytes:
        text = payload.decode("utf-8")
        self.sent.append(text)
        match = re.search(r'startpos="(\d+)"', text)
        if match is None:
            raise NewareError("自测未准备该命令的回应")
        startpos = int(match.group(1))
        self.startpos_seen.append(startpos)
        end = min(startpos + self.page_size, self.total_rows + 1)
        rows = "".join(
            f'<data seqid="{i}" stepid="1" cycleid="1" steptype="cc" '
            f'testtime="0" atime="2024-01-01 10:00:00" volt="3.5" curr="1" '
            f'cap="0.1" eng="0.4" />'
            for i in range(startpos, end)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n<bts version="1.0">\n'
            f'<cmd>download_resp</cmd>\n<list count="{end - startpos}">{rows}</list>'
            '\n</bts>'
        ).encode("utf-8")


def test_pagination(c: Checker) -> None:
    print("\n[6] 分页拉取（最容易静默丢数据的地方）")
    ch = Channel(25, 41, 1, 1)

    # 7 条数据，每页 3 条 -> 3 次请求：[1-3] [4-6] [7]
    t = PaginatingTransport(total_rows=7, page_size=3)
    t.connect()
    batches = list(NewareClient(t, 5.0).iter_download(ch, count=3, progress=False))
    seqids = [r["seqid"] for b in batches for r in b]
    c.eq(seqids, ["1", "2", "3", "4", "5", "6", "7"], "分页拉全，无缺号无重复")
    c.eq(t.startpos_seen, [1, 4, 7],
         "startpos 按数据序号递增（不是按偏移量递增）")
    c.eq(len(batches), 3, "批次数正确（最后一批不足一页即停）")

    # 正好整除：6 条 / 每页 3
    t2 = PaginatingTransport(total_rows=6, page_size=3)
    t2.connect()
    seqids2 = [r["seqid"] for r in NewareClient(t2, 5.0).download_all(
        ch, count=3, progress=False)]
    c.eq(seqids2, ["1", "2", "3", "4", "5", "6"], "整页边界不丢数据也不重复")

    # 空数据
    t3 = PaginatingTransport(total_rows=0, page_size=3)
    t3.connect()
    c.eq(NewareClient(t3, 5.0).download_all(ch, count=3, progress=False), [],
         "无数据时返回空列表且不报错")

    # max_rows 截断
    t4 = PaginatingTransport(total_rows=100, page_size=3)
    t4.connect()
    c.eq(len(NewareClient(t4, 5.0).download_all(
        ch, count=3, max_rows=5, progress=False)), 5, "max_rows 正确截断")

    # 生成器惰性：只取一批不该触发全部请求（内存友好性的关键）
    t5 = PaginatingTransport(total_rows=100, page_size=3)
    t5.connect()
    gen = NewareClient(t5, 5.0).iter_download(ch, count=3, progress=False)
    next(gen)
    c.eq(len(t5.startpos_seen), 1, "生成器惰性求值：取一批只发一次请求")


class TraileredTransport(Transport):
    """模拟真实客户端：在 </bts> 之后追加自己的结束标识。

    现场实测这个标识是「一个井号 + 回车换行」。如果分帧没做对，
    这些尾部字节会被一起交给 XML 解析器，报 "junk after document element"，
    于是每一条命令都会失败。
    """

    name = "trailered"

    def __init__(self, response: str, trailer: bytes = b"\n\n#\r\n") -> None:
        self.response = response
        self.trailer = trailer
        self.sent: list[str] = []

    def connect(self) -> None:
        pass

    def send_recv(self, payload: bytes, timeout: float | None = None) -> bytes:
        self.sent.append(payload.decode("utf-8"))
        return self.response.encode("utf-8") + self.trailer


def test_frame_extraction(c: Checker) -> None:
    print("\n[7] 分帧：尾部标识不能带进 XML 解析器")

    for label, trailer in [
        ("井号加回车换行（现场实测的形态）", b"\n\n#\r\n"),
        ("两个换行（协议文档对管道的约定）", b"\n\n"),
        ("无尾部标识", b""),
    ]:
        t = TraileredTransport(GETDEVINFO_RESP, trailer)
        t.connect()
        client = NewareClient(t, 5.0)
        try:
            info = NewareClient.parse_devinfo(client.getdevinfo())
            c.eq(info["channel_count"], 4, f"{label}：能正确解析")
        except NewareError as exc:
            c.ok(False, f"{label}：能正确解析", str(exc)[:140])

    from neware_client import _one_frame

    raw = b'<?xml version="1.0"?><bts><cmd>x</cmd></bts>\n\n#\r\n'
    c.eq(_one_frame(raw), b'<?xml version="1.0"?><bts><cmd>x</cmd></bts>',
         "_one_frame 恰好截到 </bts> 为止")
    c.eq(_one_frame(b"<bts>no end tag"), b"<bts>no end tag",
         "没有 </bts> 时原样返回（由调用方负责报错）")
    # 尾部标识里出现 "<" 是最坏情况，也必须被截掉
    c.ok(not _one_frame(raw).endswith(b"#\r\n"),
         "尾部标识不会残留")


def test_multistep_traps(c: Checker) -> None:
    """多循环 / 多工步数据上的两个陷阱。

    这两个都是现场实测踩出来的假警报，会白浪费现场时间：
      ① testtime 是"当前工步已运行时间"，每个工步归零 ——
         拿整段数据的首尾相比毫无意义（现场给出了 66 这种废数字）。
      ② stepid 是循环内的工步号，每个循环归零 ——
         全局看必然不单调，那是正常现象，不是数据错误。
    """
    print("\n[8] 多循环/多工步数据的两个陷阱")

    rows: list[dict[str, str]] = []
    base = 0
    seq = 0
    for cycle in (1, 2, 3):
        for step in range(1, 5):
            for k in range(3):
                seq += 1
                base += 1000
                rows.append({
                    "seqid": str(seq),
                    "cycleid": str(cycle),
                    "stepid": str(step),
                    "testtime": str(k * 1000),          # 工步内从 0 起，单位毫秒
                    "atime": f"2026-01-01 10:{base // 60000:02d}:"
                             f"{(base // 1000) % 60:02d}",
                })

    unit = check_time_unit(rows)
    c.eq(unit["unit"], "ms", "跨工步的长数据仍能正确判定单位为 ms")
    c.eq(unit["scope"], "同工步内", "在工步内部比较，而非全样本首尾相比")
    c.ok(unit["samples"] >= 2, "有多个工步参与判定", str(unit))

    c.eq(check_monotonic(rows, "stepid"), False,
         "stepid 全局不单调（正常现象，不该报错）")
    c.eq(check_stepid_within_cycle(rows), True, "stepid 在循环内递增（正确的检查方式）")
    c.eq(check_monotonic(rows, "seqid"), True, "seqid 全局单调")
    c.eq(check_monotonic(rows, "cycleid"), True, "cycleid 全局单调不减")


def main() -> int:
    c = Checker()
    print("=" * 62)
    print("Neware 通讯层离线自测（数据来自协议文档 / API 文档实测截图）")
    print("=" * 62)
    test_channel(c)
    test_commands(c)
    test_parsing(c)
    test_time_unit(c)
    test_soh(c)
    test_pagination(c)
    test_frame_extraction(c)
    test_multistep_traps(c)
    return c.report()


if __name__ == "__main__":
    raise SystemExit(main())
