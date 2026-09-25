#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Neware BTS API 通讯层（对应《新威尔电池测试系统BTSAPI协议》v1.19）

传输方式
--------
管道  \\\\.\\pipe\\NewareBtsAPI     仅本机；BTS Client 须以管理员权限运行
      \\\\.\\pipe\\NewareBtsAPIServer
TCP   <客户端IP>:502               可跨机；客户端版本须 >= 2023/3/17

报文约定
--------
UTF-8 XML，问答式：发出一条必须收到回应后才能发下一条，单条最长等待 30 秒。
发送：管道以两个换行结尾（\\n\\n）；TCP 以 "#\\r\\n" 结尾。
接收：不依赖固定分隔符，累积到出现 </bts> 即视为一帧结束。

用法（命令行）
--------------
    python neware_client.py --transport pipe info
    python neware_client.py --transport tcp --host 192.168.1.20 status 25-41-1-1
    python neware_client.py --transport tcp --host 192.168.1.20 df 25-41-1-1 --out df.csv

通道写法：devtype-devid-subdevid-chlid，可加 @ip，例如 25-41-1-1@127.0.0.1
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# 协议常量
# --------------------------------------------------------------------------

PIPE_CLIENT = r"\\.\pipe\NewareBtsAPI"
PIPE_APISERVER = r"\\.\pipe\NewareBtsAPIServer"
DEFAULT_TCP_PORT = 502
DEFAULT_TIMEOUT = 30.0          # 协议规定单条命令最长 30 秒
MAX_DOWNLOAD_COUNT = 1000       # download 单次最多 1000 条

DEVTYPE_NAMES = {
    20: "BTS78", 21: "BTS79", 22: "BTS80", 23: "BTS81",
    24: "BTS82", 25: "BTS83", 26: "BTS84", 27: "BTS85",
}

STEPTYPE_NAMES = {
    "cc": "恒流充电", "dc": "恒流放电", "cv": "恒压充电", "dv": "恒压放电",
    "cccv": "恒流恒压充电", "cccd": "恒流恒压放电", "pcccv": "电池组恒流恒压",
    "cp": "恒功率充电", "dp": "恒功率放电", "cr": "恒阻充电", "dr": "恒阻放电",
    "rest": "搁置", "pause": "暂停", "sim": "模拟工况",
    "control": "控制工步", "pulse": "脉冲工步", "end": "结束",
}

# 注意：协议里两处状态表不一致 ——
#   2.1.4 getchlstatus_resp 的说明列了：working / stop / finish / protect / pause
#   2.3 的"工作状态定义"总表列了：working / stop / protect / pause /
#        synCtrl / ligth / waitTimeOut / waitStart
# finish 只在 2.1.4 出现；synCtrl 等只在 2.3 出现。两处合并收录，现场核对。
STATUS_NAMES = {
    "working": "测试中", "stop": "停止", "protect": "保护", "pause": "暂停",
    "finish": "完成",
    "synCtrl": "同步控制", "ligth": "点灯", "waitTimeOut": "同步超时",
    "waitStart": "等待加载资源启动",
}

CHARGING_STEPTYPES = {"cc", "cv", "cccv", "pcccv", "cp", "cr", "pulse"}
DISCHARGING_STEPTYPES = {"dc", "dv", "cccd", "dp", "dr"}


class NewareError(RuntimeError):
    """通讯或协议层面的错误。"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def _attr(value: Any) -> str:
    """转义 XML 属性值。"""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@dataclass(frozen=True)
class Channel:
    """Neware 通道四元组。取数时另加 auxid / testid。"""
    devtype: int
    devid: int
    subdevid: int
    chlid: int
    ip: str = "127.0.0.1"

    @property
    def key(self) -> str:
        """稳定标识，也是 inquire 回应里 dev 字段去掉辅助通道号后的形态。"""
        return f"{self.devtype}-{self.devid}-{self.subdevid}-{self.chlid}"

    def attrs(self, extra: dict[str, Any] | None = None) -> str:
        parts = [
            f'ip="{_attr(self.ip)}"',
            f'devtype="{self.devtype}"',
            f'devid="{self.devid}"',
            f'subdevid="{self.subdevid}"',
            f'chlid="{self.chlid}"',
        ]
        for name, value in (extra or {}).items():
            parts.append(f'{name}="{_attr(value)}"')
        return " ".join(parts)

    def __str__(self) -> str:
        dev = DEVTYPE_NAMES.get(self.devtype, f"devtype{self.devtype}")
        return f"{self.key}@{self.ip}({dev})"


def parse_channel(spec: str) -> Channel:
    """解析 '25-41-1-1' 或 '25-41-1-1@127.0.0.1'。"""
    ip = "127.0.0.1"
    if "@" in spec:
        spec, ip = spec.rsplit("@", 1)
    parts = spec.split("-")
    if len(parts) != 4:
        raise ValueError(
            f"通道格式应为 devtype-devid-subdevid-chlid，收到 {spec!r}")
    try:
        devtype, devid, subdevid, chlid = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"通道各段必须是整数：{spec!r}") from exc
    return Channel(devtype, devid, subdevid, chlid, ip)


def parse_dev_field(dev: str) -> tuple[Channel, int]:
    """解析 inquire 回应里的 dev 字段 '22-3-2-1-10' -> (Channel, auxid)。"""
    parts = dev.split("-")
    if len(parts) < 4:
        raise ValueError(f"dev 字段格式异常：{dev!r}")
    channel = Channel(*(int(p) for p in parts[:4]))
    auxid = int(parts[4]) if len(parts) > 4 else 0
    return channel, auxid


# --------------------------------------------------------------------------
# 传输层
# --------------------------------------------------------------------------

class Transport:
    name = "abstract"

    def connect(self) -> None:
        raise NotImplementedError

    def send_recv(self, payload: bytes, timeout: float | None = None) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "Transport":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class PipeTransport(Transport):
    r"""Windows 命名管道。

    两个硬约束（协议 2.1）：
      * 必须以管理员权限运行对端程序，否则打开管道失败；
      * 不支持操作远程电脑上的客户端，只能连本机。
    """

    name = "pipe"

    def __init__(self, pipe_name: str = PIPE_CLIENT, timeout: float = DEFAULT_TIMEOUT):
        self.pipe_name = pipe_name
        self.timeout = timeout
        self._fh = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        # 命名管道是 Windows 特有的内核对象。在 Linux/macOS 上（例如边缘算力盒子
        # 跑的是 Linux）根本不存在，而且管道本身也不支持跨机器。
        if sys.platform != "win32":
            raise NewareError(
                f"当前系统是 {sys.platform}，不支持 Windows 命名管道。\n"
                f"管道是本机内核对象，无法跨机器使用；\n"
                f"边缘盒子与 BTS Client 是两台机器时，请改用 --transport tcp。"
            )
        try:
            self._fh = open(self.pipe_name, "r+b", buffering=0)
        except PermissionError as exc:
            raise NewareError(
                f"打开管道 {self.pipe_name} 被拒绝（错误码 5，内核完整性标签拦下了“写”请求）：\n"
                f"  * 需以【管理员权限】运行本脚本；\n"
                f"  * BTS Client 也必须以管理员权限运行。"
            ) from exc
        except FileNotFoundError as exc:
            raise NewareError(
                f"管道 {self.pipe_name} 不存在（错误码 2，内核里没有这个对象）：\n"
                f"  * BTS Client 未启动；\n"
                f"  * 若对接的是 BTSAPIServer 请改用 {PIPE_APISERVER}。"
            ) from exc
        except OSError as exc:
            raise NewareError(f"打开管道 {self.pipe_name} 失败：{exc}") from exc

    def _abort(self) -> None:
        """超时兜底：关闭句柄，让阻塞中的 read 抛错。"""
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass

    def send_recv(self, payload: bytes, timeout: float | None = None) -> bytes:
        if self._fh is None:
            raise NewareError("管道尚未连接")
        timeout = timeout or self.timeout
        with self._lock:
            self._fh.write(payload)
            buf = bytearray()
            timer = threading.Timer(timeout, self._abort)
            timer.start()
            try:
                while b"</bts>" not in buf:
                    chunk = self._fh.read(4096)
                    if not chunk:
                        break
                    buf += chunk
            except (OSError, ValueError) as exc:
                raise NewareError(
                    f"等待回应超时（{timeout:g}s）或管道已断开。"
                    f"注意：管道一旦超时即失效，需重新连接。"
                ) from exc
            finally:
                timer.cancel()
        if b"</bts>" not in buf:
            raise NewareError(f"回应不完整（未见到 </bts>）：{bytes(buf[:400])!r}")
        return _one_frame(buf)

    def close(self) -> None:
        self._abort()
        self._fh = None


class TcpTransport(Transport):
    """TCP 直连 BTS Client，默认端口 502。"""

    name = "tcp"

    def __init__(self, host: str, port: int = DEFAULT_TCP_PORT,
                 timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout)
        except ConnectionRefusedError as exc:
            # 对方的 TCP 栈回了 RST：包到了，但那个端口没有程序在监听。
            raise NewareError(
                f"{self.host}:{self.port} 连接被拒绝（收到 RST）——"
                f"包到达了对方电脑，但该端口无人监听。可能原因：\n"
                f"  1) BTS Client 没启动\n"
                f"  2) API 模式没激活（或激活后没重启客户端）\n"
                f"  3) 客户端版本早于 2023/3/17，没有 TCP 监听功能\n"
                f"  4) {self.port} 端口被别的程序占用，客户端 bind 失败\n"
                f"  网络本身是通的，问题在对方电脑上的客户端。"
            ) from exc
        except socket.timeout as exc:
            # 包被静默丢弃：网络不通或防火墙拦截
            raise NewareError(
                f"连接 {self.host}:{self.port} 超时（无任何响应）——"
                f"数据包被静默丢弃。可能原因：\n"
                f"  1) IP 地址写错，或对方不在同一网段\n"
                f"  2) 对方防火墙拦了 {self.port} 端口\n"
                f"  3) 网线/交换机/路由问题\n"
                f"  先在本机执行 ping {self.host} 确认网络可达。"
            ) from exc
        except OSError as exc:
            raise NewareError(
                f"连接 {self.host}:{self.port} 失败：{exc}"
            ) from exc
        self._sock.settimeout(self.timeout)

    def send_recv(self, payload: bytes, timeout: float | None = None) -> bytes:
        if self._sock is None:
            raise NewareError("TCP 尚未连接")
        self._sock.settimeout(timeout or self.timeout)
        self._sock.sendall(payload + b"#\r\n")
        buf = bytearray()
        while b"</bts>" not in buf:
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise NewareError(
                    f"等待回应超时（{timeout or self.timeout:g}s）："
                    f"已收到 {len(buf)} 字节"
                ) from exc
            if not chunk:
                break
            buf += chunk
        if b"</bts>" not in buf:
            raise NewareError(f"回应不完整（未见到 </bts>）：{bytes(buf[:400])!r}")
        return _one_frame(buf)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


# --------------------------------------------------------------------------
# 回应解析
# --------------------------------------------------------------------------

@dataclass
class Response:
    cmd: str
    raw: str
    root: ET.Element

    @property
    def text(self) -> str:
        return self.raw

    def dump(self) -> str:
        return self.raw


def _one_frame(buf: bytes | bytearray) -> bytes:
    """从接收缓冲区里截出恰好一帧。

    真实客户端在 </bts> 之后还会追加自己的结束标识（实测是一个井号加回车换行）。
    如果把这些尾部字节一起交给 XML 解析器，会报
    "junk after document element"。分帧是传输层的职责，所以在这里截掉。
    """
    end = buf.find(b"</bts>")
    if end < 0:
        return bytes(buf)
    return bytes(buf[:end + len(b"</bts>")])


def parse_bts(raw: bytes) -> ET.Element:
    """宽容解析。厂商文档里出现过大写引号，真实客户端输出正常，这里都兜住。"""
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        pass
    text = raw.decode("utf-8", "replace")
    text = (text.replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2018", "'").replace("\u2019", "'"))
    text = re.sub(r"<\?xml[^>]*\?>", "", text).strip()
    # 防御：万一尾部标识混进来了，从最后一个 </bts> 处截断
    tail_at = text.rfind("</bts>")
    if tail_at >= 0:
        text = text[:tail_at + len("</bts>")]
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        # 截断通常发生在结尾，所以头尾都要给，否则只看开头发现不了问题
        head = text[:400]
        if len(text) > 800:
            extra = f"\n...（中间省略 {len(text) - 800} 字符）...\n{text[-400:]}"
        else:
            extra = ""
        raise NewareError(
            f"回应不是合法 XML：{exc}\n"
            f"总长度 {len(text)} 字符。开头：\n{head}{extra}\n"
            f"提示：若报 unclosed token，多半是回应被截断 —— "
            f"用 dump_raw.py 抓原始字节看清全貌"
        ) from exc


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

class NewareClient:
    """问答式封装。所有命令返回 Response，另有若干便捷解析方法。"""

    def __init__(self, transport: Transport, timeout: float = DEFAULT_TIMEOUT):
        self.transport = transport
        self.timeout = timeout
        self.history: list[Response] = []

    # -- 底层 -------------------------------------------------------------

    def request(self, body: str, timeout: float | None = None) -> Response:
        payload = (
            '<?xml version="1.0" encoding="UTF-8" ?>\n'
            '<bts version="1.0">\n' + body + "\n</bts>"
        ).encode("utf-8")
        raw = self.transport.send_recv(payload, timeout)
        root = parse_bts(raw)
        cmd_el = root.find("cmd")
        resp = Response(cmd_el.text.strip() if cmd_el is not None and cmd_el.text else "?",
                        raw.decode("utf-8", "replace"), root)
        self.history.append(resp)
        return resp

    def raw(self, body: str, timeout: float | None = None) -> Response:
        """发送任意命令体，用于试探协议未文档化的命令（例如传说中的“联机”）。"""
        return self.request(body, timeout)

    # -- 2.1.1 获取设备信息 ------------------------------------------------

    def getdevinfo(self) -> Response:
        return self.request("<cmd>getdevinfo</cmd>")

    @staticmethod
    def parse_devinfo(resp: Response) -> dict[str, Any]:
        """解析 getdevinfo_resp。

        注意：现场客户端（2025 版）的回应比协议文档（2023 版）丰富 ——
        多了 <client> 元素，以及 <server> 下的 <dev>/<xwj> 层级。
        文档里描述的 <middle>/<channel> 依然存在，所以两套都解析。
        """
        root = resp.root

        client_el = root.find("client")
        client_version = client_el.get("version") if client_el is not None else None

        servers = [
            {
                "ip": el.get("ip"),
                "port": el.get("port"),
                "version": el.get("version"),
                "dev_count": int(el.get("devCount", 0) or 0),
            }
            for el in root.findall("./serverip/server")
        ]

        # 现场新增的层级：server > dev（设备）> xwj（小位机）
        devices = [
            {
                "ip": el.get("ip"),
                "devid": int(el.get("devid", 0) or 0),
                "firmware": el.get("zwjVersion"),
                "xwj_count": int(el.get("xwjCount", 0) or 0),
            }
            for el in root.findall("./serverip/server/dev")
        ]

        middle = root.find("middle")
        channels = [
            {
                "ip": el.get("ip"),
                "devtype": int(el.get("devtype", 0)),
                "devid": int(el.get("devid", 0)),
                "subdevid": int(el.get("subdevid", 0)),
                "chlid": int(el.get("Channelid") or el.get("chlid") or 0),
                "can_light": (el.text or "").strip().lower() == "true",
            }
            for el in root.findall("./middle/channel")
        ]
        return {
            "client_version": client_version,
            "servers": servers,
            "devices": devices,
            "channel_count": int(middle.get("count", 0)) if middle is not None else 0,
            "channels": channels,
        }

    # -- 2.1.4 通道状态 ----------------------------------------------------

    def getchlstatus(self, channels: Sequence[Channel]) -> Response:
        items = "\n".join(f'<status {c.attrs()}>true</status>' for c in channels)
        return self.request(
            f'<cmd>getchlstatus</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    @staticmethod
    def parse_status(resp: Response) -> list[dict[str, Any]]:
        out = []
        for el in resp.root.iter("status"):
            raw = (el.text or "").strip()
            out.append({
                "ip": el.get("ip"),
                "devtype": int(el.get("devtype", 0)),
                "devid": int(el.get("devid", 0)),
                "subdevid": int(el.get("subdevid", 0)),
                "chlid": int(el.get("chlid", 0)),
                "status": raw,
                "status_cn": STATUS_NAMES.get(raw, ""),
                "reservepause": el.get("reservepause"),
            })
        return out

    # -- 2.1.7 实时数据 ----------------------------------------------------

    def inquire(self, channels: Sequence[Channel], aux: int = 0,
                barcode: bool = True) -> Response:
        items = "\n".join(
            f'<inquire {c.attrs({"aux": aux, "barcode": 1 if barcode else 0})}>true</inquire>'
            for c in channels)
        return self.request(
            f'<cmd>inquire</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    @staticmethod
    def parse_inquire(resp: Response) -> list[dict[str, Any]]:
        out = []
        for el in resp.root.iter("inquire"):
            if el.get("dev") is None:
                continue
            row: dict[str, Any] = dict(el.attrib)
            row["devtype"] = int(row["dev"].split("-")[0])
            row["auxid"] = int(row["dev"].split("-")[4]) if len(
                row["dev"].split("-")) > 4 else 0
            row["aux"] = {k: v for k, v in el.attrib.items()
                          if k not in ("dev", "cycle_id", "step_id", "step_type",
                                       "workstatus", "barcode", "current", "voltage",
                                       "capacity", "energy", "totaltime",
                                       "relativetime", "auxtemp", "auxvol",
                                       "open_or_close", "log_code")}
            row["dbc"] = [
                {"message_name": d.get("message_name"),
                 "signal_name": d.get("signal_name"),
                 "value": d.get("value"), "unit": d.get("unit")}
                for d in el.findall("DBC")
            ]
            out.append(row)
        return out

    # -- 2.1.6 下载 DF 数据 ------------------------------------------------

    def download(self, channel: Channel, auxid: int = 0, testid: int = 0,
                 startpos: int = 1, count: int = MAX_DOWNLOAD_COUNT) -> Response:
        if count > MAX_DOWNLOAD_COUNT:
            raise ValueError(f"count 不能超过 {MAX_DOWNLOAD_COUNT}")
        return self.request(
            '<cmd>download</cmd>\n'
            f'<download {channel.attrs({"auxid": auxid, "testid": testid, "startpos": startpos, "count": count})}/>')

    def iter_download(self, channel: Channel, auxid: int = 0, testid: int = 0,
                      count: int = MAX_DOWNLOAD_COUNT,
                      max_rows: int | None = None,
                      progress: bool = True):
        """按批产出 DF 数据（生成器），不会把全部数据堆在内存里。

        内存受限的机器（例如边缘算力盒子，还要同时跑大模型）应该用这个，
        而不是 download_all —— 可以边拉边写盘：

            with open("detail.parquet", "wb") as fh:
                for batch in client.iter_download(channel):
                    append_batch(fh, batch)

        注意内存里同时只留一批（默认 1000 条）。
        """
        got = 0
        startpos = 1
        while True:
            if max_rows is not None and got >= max_rows:
                return
            batch = self.parse_data(
                self.download(channel, auxid, testid, startpos, count))
            if not batch:
                return
            if max_rows is not None:
                batch = batch[:max_rows - got]
                if not batch:
                    return
            yield batch
            got += len(batch)
            if progress:
                print(f"  已拉取 {got} 条（本批 {len(batch)} 条）", file=sys.stderr)
            if len(batch) < count:
                return
            startpos = max(int(r.get("seqid", 0)) for r in batch) + 1

    def download_all(self, channel: Channel, auxid: int = 0, testid: int = 0,
                     count: int = MAX_DOWNLOAD_COUNT,
                     max_rows: int | None = None,
                     progress: bool = True) -> list[dict[str, Any]]:
        """分页拉全量 DF 数据，返回列表。

        startpos 是数据序号（1-based），不是偏移量。
        数据量大时请改用 iter_download，避免内存被占满。
        """
        rows: list[dict[str, Any]] = []
        for batch in self.iter_download(channel, auxid, testid, count,
                                        max_rows, progress):
            rows.extend(batch)
        return rows

    @staticmethod
    def parse_data(resp: Response) -> list[dict[str, Any]]:
        """解析 download_resp / downloadStepLayer_resp 里的 data 节点。"""
        rows = []
        for el in resp.root.iter("data"):
            row: dict[str, Any] = dict(el.attrib)
            row["dbc"] = [
                {"message_name": d.get("message_name"),
                 "signal_name": d.get("signal_name"),
                 "value": d.get("value"), "unit": d.get("unit")}
                for d in el.findall("DBC")
            ]
            rows.append(row)
        return rows

    # -- 2.1.13 工步层数据 -------------------------------------------------

    def download_steplayer(self, channel: Channel, testid: int = 0,
                           dcir: int = 1, v1i1: dict | None = None,
                           v2i2: dict | None = None) -> Response:
        v1i1 = v1i1 or {"previousstep": 1, "type": 1, "value": ""}
        v2i2 = v2i2 or {"previousstep": 0, "type": 0, "value": ""}
        return self.request(
            '<cmd>downloadStepLayer</cmd>\n'
            f'<downloadStepLayer {channel.attrs({"testid": testid, "dcir": dcir})}/>\n'
            f'<V1I1 {_attr_dict(v1i1)}/>\n'
            f'<V2I2 {_attr_dict(v2i2)}/>')

    # -- 2.1.12 查询 DF 数据上传情况 ---------------------------------------

    def inquiredf(self, channels: Sequence[Channel], testid: int = 0) -> Response:
        items = "\n".join(f'<chl {c.attrs({"testid": testid})}/>' for c in channels)
        return self.request(
            f'<cmd>inquiredf</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    @staticmethod
    def parse_inquiredf(resp: Response) -> list[dict[str, Any]]:
        return [
            {
                "ip": el.get("ip"),
                "devtype": int(el.get("devtype", 0)),
                "devid": int(el.get("devid", 0)),
                "subdevid": int(el.get("subdevid", 0)),
                "chlid": int(el.get("chlid", 0)),
                "testid": el.get("testid"),
                "uploaded": int(el.get("count", 0)),
                "complete": (el.text or "").strip().lower() == "true",
            }
            for el in resp.root.iter("chl")
        ]

    # -- 2.1.14 日志数据 ---------------------------------------------------

    def downloadlog(self, channel: Channel, testid: int = 0,
                    log_level: int = 0) -> Response:
        return self.request(
            '<cmd>downloadlog</cmd>\n'
            f'<download {channel.attrs({"testid": testid, "log_lever": log_level})}/>')

    # -- 2.1.3 启动 --------------------------------------------------------

    def start(self, items: Sequence[tuple],
              dbc_can: int | None = None, backup: dict | None = None) -> Response:
        """启动测试。

        items 的每一项可以是三元组或四元组：
            (channel, 工步文件路径, 条码)
            (channel, 工步文件路径, 条码, 备注)   # 备注见 QL-165829

        remark 是同一个工步下发给多个通道时，给每个通道单独标注的备注，
        会显示在客户端界面的通道信息里，便于区分"这几颗电池在跑同一套工步"。
        backup 非空时，客户端会在测试结束后自动把数据文件导出到指定目录
        （filetype=0 导出 NDA，filetype=1 导出 Excel）。
        """
        attr = f' count="{len(items)}"'
        if dbc_can is not None:
            attr += f' DBC_CAN="{dbc_can}"'

        lines = []
        for item in items:
            if len(item) == 3:
                ch, path, barcode = item
                remark = None
            elif len(item) == 4:
                ch, path, barcode, remark = item
            else:
                raise ValueError(f"start 每一项应为 3 或 4 元组，收到 {item!r}")
            extra = {"barcode": barcode}
            if remark:
                extra["remark"] = remark
            lines.append(f'<start {ch.attrs(extra)}>{path}</start>')

        if backup:
            lines.append(f"<backup {_attr_dict(backup)} />")
        return self.request(f'<cmd>start</cmd>\n<list{attr}>\n' + "\n".join(lines) + "\n</list>")

    # -- 控制辅助：等待测试结束 -------------------------------------------

    def wait_until_status(self, channels: Sequence[Channel],
                          targets: Iterable[str] = ("finish", "stop", "protect"),
                          timeout: float = 3600.0, interval: float = 10.0,
                          progress: bool = True) -> dict[str, str]:
        """轮询通道状态，直到全部落入 targets 中的某一状态。

        返回 {通道地址: 最终状态}。超时则返回最后一次读到的状态。
        注意：协议规定实时性不优于 5 秒，interval 不要设得比 5 秒还小。
        """
        wanted = {t.lower() for t in targets}
        # 只关心自己请求的通道：若回应里混入了别的通道，不能让它影响判断
        wanted_keys = {c.key for c in channels}
        deadline = time.monotonic() + timeout
        last: dict[str, str] = {}
        while True:
            rows = self.parse_status(self.getchlstatus(channels))
            last = {f"{r['devtype']}-{r['devid']}-{r['subdevid']}-{r['chlid']}":
                    r["status"] for r in rows
                    if f"{r['devtype']}-{r['devid']}-{r['subdevid']}-{r['chlid']}"
                    in wanted_keys}
            if progress:
                summary = ", ".join(f"{k}={v}" for k, v in last.items())
                print(f"  [{datetime.now():%H:%M:%S}] {summary}", file=sys.stderr)
            if last and all(v.lower() in wanted for v in last.values()):
                return last
            if time.monotonic() >= deadline:
                if progress:
                    print(f"  等待超时（{timeout:g}s），返回最后读到的状态", file=sys.stderr)
                return last
            time.sleep(interval)

    # -- 2.1.5 停止 --------------------------------------------------------

    def stop(self, channels: Sequence[Channel]) -> Response:
        items = "\n".join(f'<stop {c.attrs()}>true</stop>' for c in channels)
        return self.request(
            f'<cmd>stop</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    # -- 2.1.8 广播停止 ----------------------------------------------------

    def broadcaststop(self, devtype: int, devid: int, ip: str = "127.0.0.1") -> Response:
        return self.request(
            '<cmd>broadcaststop</cmd>\n<list count="1">\n'
            f'<stop ip="{_attr(ip)}" devtype="{devtype}" devid="{devid}">true</stop>\n'
            '</list>')

    # -- 2.1.9 接续 --------------------------------------------------------

    def continue_run(self, channels: Sequence[Channel]) -> Response:
        items = "\n".join(f'<continue {c.attrs()}>true</continue>' for c in channels)
        return self.request(
            f'<cmd>continue</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    # -- 2.1.10 通道开关控制（IGBT） ---------------------------------------

    def chl_ctrl(self, channel: Channel, on: bool) -> Response:
        return self.request(
            '<cmd>chl_ctrl</cmd>\n<list count="1">\n'
            f'<chl_ctrl {channel.attrs()}>{1 if on else 0}</chl_ctrl>\n</list>')

    # -- 2.1.11 跳转 -------------------------------------------------------

    def goto(self, channel: Channel, step: int) -> Response:
        return self.request(
            '<cmd>goto</cmd>\n<list count="1">\n'
            f'<goto {channel.attrs({"step": step})}>true</goto>\n</list>')

    # -- 2.1.2 点灯 --------------------------------------------------------

    def light(self, channels: Sequence[Channel], on: bool = True) -> Response:
        items = "\n".join(
            f'<light {c.attrs()}>{"true" if on else "false"}</light>' for c in channels)
        return self.request(
            f'<cmd>light</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    # -- 2.1.15~2.1.18 并联 / 报警 / 标记 ----------------------------------

    def parallel(self, channels: Sequence[Channel], paralleltype: int = 0) -> Response:
        items = "\n".join(f'<parallel {c.attrs()} />' for c in channels)
        return self.request(
            f'<cmd>parallel</cmd>\n<list count="{len(channels)}" paralleltype="{paralleltype}">\n'
            f'{items}\n</list>')

    def getparallel(self, channels: Sequence[Channel]) -> Response:
        items = "\n".join(f'<parallel {c.attrs()} />' for c in channels)
        return self.request(
            f'<cmd>getparallel</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    def resetalarm(self, channels: Sequence[Channel]) -> Response:
        items = "\n".join(f'<resetalarm {c.attrs()} />' for c in channels)
        return self.request(
            f'<cmd>resetalarm</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    def clearflag(self, channels: Sequence[Channel]) -> Response:
        items = "\n".join(f'<clearflag {c.attrs()} />' for c in channels)
        return self.request(
            f'<cmd>clearflag</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    # -- 2.1.19 重置工步 ---------------------------------------------------

    def reset(self, channel: Channel, step_file: str, dbc_can: int = 0) -> Response:
        return self.request(
            '<cmd>reset</cmd>\n'
            f'<reset {channel.attrs({"DBC_CAN": dbc_can})}>{step_file}</reset>')

    # -- 2.1.20~2.1.22 预约暂停 --------------------------------------------

    def setpause(self, channels: Sequence[Channel], cycleid: int = -1,
                 stepid: int = -1, timeout: int | str = "") -> Response:
        items = "\n".join(
            f'<setpause {c.attrs({"cycleid": cycleid, "stepid": stepid, "timeout": timeout})} />'
            for c in channels)
        return self.request(
            f'<cmd>setpause</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    def cancelpause(self, channels: Sequence[Channel], cycleid: int = -1,
                    stepid: int = -1) -> Response:
        items = "\n".join(
            f'<cancelpause {c.attrs({"cycleid": cycleid, "stepid": stepid})} />'
            for c in channels)
        return self.request(
            f'<cmd>cancelpause</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')

    def getpause(self, channels: Sequence[Channel]) -> Response:
        items = "\n".join(f'<getpause {c.attrs()} />' for c in channels)
        return self.request(
            f'<cmd>getpause</cmd>\n<list count="{len(channels)}">\n{items}\n</list>')


def _attr_dict(d: dict[str, Any]) -> str:
    return " ".join(f'{k}="{_attr(v)}"' for k, v in d.items())


# --------------------------------------------------------------------------
# 便捷构造
# --------------------------------------------------------------------------

def build_transport(args: argparse.Namespace) -> Transport:
    if args.transport == "pipe":
        return PipeTransport(args.pipe or PIPE_CLIENT, args.timeout)
    if args.transport == "tcp":
        if not args.host:
            raise NewareError("TCP 方式必须用 --host 指定运行 BTS Client 的电脑 IP")
        return TcpTransport(args.host, args.port, args.timeout)
    raise NewareError(f"未知传输方式：{args.transport}")


def write_rows(rows: list[dict[str, Any]], path: str | None,
               fmt: str = "csv") -> None:
    """落地数据行。CSV 用标准库；Parquet 需 pandas + pyarrow，缺失时回落 CSV。"""
    if not rows:
        print("没有数据可写出", file=sys.stderr)
        return
    if not path:
        for row in rows[:20]:
            print(json.dumps(row, ensure_ascii=False))
        if len(rows) > 20:
            print(f"...（共 {len(rows)} 行，仅显示前 20 行）", file=sys.stderr)
        return

    flat = [{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
             for k, v in row.items()} for row in rows]
    if fmt == "parquet" or path.lower().endswith(".parquet"):
        try:
            import pandas as pd
            pd.DataFrame(flat).to_parquet(path, index=False)
            print(f"已写出 {len(flat)} 行 -> {path}")
            return
        except ImportError:
            print("未安装 pandas/pyarrow，回落为 CSV", file=sys.stderr)
            path = re.sub(r"\.parquet$", ".csv", path, flags=re.I)

    fieldnames: list[str] = []
    for row in flat:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flat)
    print(f"已写出 {len(flat)} 行 -> {path}")


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Neware BTS API 通讯工具（BTSAPI 协议 v1.19）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--transport", choices=["pipe", "tcp"], default="tcp",
                   help="pipe=本机命名管道（需管理员）；tcp=跨机 502 端口")
    p.add_argument("--host", help="运行 BTS Client 的电脑 IP（tcp 方式必填）")
    p.add_argument("--port", type=int, default=DEFAULT_TCP_PORT)
    p.add_argument("--pipe", help=f"管道名，默认 {PIPE_CLIENT}")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help="单条命令超时秒数，协议上限 30")

    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("info", help="getdevinfo 获取设备信息并列出全部通道")

    sp = sub.add_parser("status", help="getchlstatus 获取通道状态")
    sp.add_argument("channels", nargs="*", help="通道，缺省则用 info 自动发现")

    sp = sub.add_parser("rt", help="inquire 查询实时数据")
    sp.add_argument("channels", nargs="*")
    sp.add_argument("--aux", type=int, default=0)
    sp.add_argument("--no-barcode", action="store_true")

    sp = sub.add_parser("df", help="download 下载 DF 明细数据")
    sp.add_argument("channel")
    sp.add_argument("--testid", type=int, default=0)
    sp.add_argument("--auxid", type=int, default=0)
    sp.add_argument("--limit", type=int, default=0, help="最多拉多少条，0=全量")
    sp.add_argument("--out", help="输出文件，支持 .csv / .parquet")
    sp.add_argument("--count", type=int, default=MAX_DOWNLOAD_COUNT)

    sp = sub.add_parser("step", help="downloadStepLayer 下载工步层数据（含 DCIR）")
    sp.add_argument("channel")
    sp.add_argument("--testid", type=int, default=0)
    sp.add_argument("--no-dcir", action="store_true")
    sp.add_argument("--out")

    sp = sub.add_parser("dfcheck", help="inquiredf 检查 DF 数据是否上传完整")
    sp.add_argument("channels", nargs="*")
    sp.add_argument("--testid", type=int, default=0)

    sp = sub.add_parser("log", help="downloadlog 下载日志（需 LogCode.csv 才能解读）")
    sp.add_argument("channel")
    sp.add_argument("--testid", type=int, default=0)
    sp.add_argument("--level", type=int, default=0)
    sp.add_argument("--out")

    sp = sub.add_parser("start", help="start 启动测试。格式 通道:工步路径:条码[:备注]")
    sp.add_argument("items", nargs="+")
    sp.add_argument("--dbc-can", type=int)
    sp.add_argument("--backup-dir", help="同时下发自动备份任务，测试结束后自动导出到该目录")
    sp.add_argument("--backup-type", choices=["nda", "excel"], default="nda",
                    help="自动备份的文件类型，默认 nda")

    sp = sub.add_parser("wait", help="轮询通道状态直到测试结束（默认等 finish/stop/protect）")
    sp.add_argument("channels", nargs="+")
    sp.add_argument("--timeout", type=float, default=3600.0, help="最长等待秒数")
    sp.add_argument("--interval", type=float, default=10.0,
                    help="轮询间隔秒数，协议实时性不优于 5 秒，不要设得比 5 小")

    sp = sub.add_parser("stop", help="stop 停止测试")
    sp.add_argument("channels", nargs="+")

    sp = sub.add_parser("light", help="light 点灯")
    sp.add_argument("channels", nargs="+")
    sp.add_argument("--off", action="store_true")

    sp = sub.add_parser("raw", help="发送任意命令体，用于试探未文档化的命令")
    sp.add_argument("body", help="例如 '<cmd>login</cmd>'")

    return p


def _channels_or_discover(client: NewareClient, specs: Sequence[str]) -> list[Channel]:
    if specs:
        return [parse_channel(s) for s in specs]
    info = NewareClient.parse_devinfo(client.getdevinfo())
    channels = [
        Channel(c["devtype"], c["devid"], c["subdevid"], c["chlid"], c["ip"])
        for c in info["channels"]
    ]
    if not channels:
        raise NewareError("getdevinfo 未返回任何通道，请显式指定通道参数")
    print(f"自动发现 {len(channels)} 个通道", file=sys.stderr)
    return channels


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        transport = build_transport(args)
    except NewareError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        with transport:
            client = NewareClient(transport, args.timeout)
            act = args.action

            if act == "info":
                info = NewareClient.parse_devinfo(client.getdevinfo())
                if info.get("client_version"):
                    print(f"客户端版本：{info['client_version']}")
                print(f"服务器 {len(info['servers'])} 台，"
                      f"设备 {len(info['devices'])} 台，"
                      f"通道 {info['channel_count']} 个")
                for srv in info["servers"]:
                    extra = f"  server版本 {srv['version']}" if srv.get("version") else ""
                    print(f"  server {srv['ip']}:{srv['port']}"
                          f"  （设备数 {srv['dev_count']}）{extra}")
                for dev in info["devices"]:
                    print(f"  dev   devid={dev['devid']}  ip={dev['ip']}"
                          f"  固件 {dev['firmware']}  小位机 {dev['xwj_count']} 个")
                for c in info["channels"]:
                    name = DEVTYPE_NAMES.get(c["devtype"], f"devtype{c['devtype']}")
                    print(f"  {c['devtype']}-{c['devid']}-{c['subdevid']}-{c['chlid']}"
                          f"@{c['ip']}  {name}  可点灯={c['can_light']}")

            elif act == "status":
                chans = _channels_or_discover(client, args.channels)
                rows = NewareClient.parse_status(client.getchlstatus(chans))
                for r in rows:
                    print(f"  {r['devtype']}-{r['devid']}-{r['subdevid']}-{r['chlid']}"
                          f"  {r['status']:<12} {r['status_cn']}"
                          f"  reservepause={r['reservepause']}")

            elif act == "rt":
                chans = _channels_or_discover(client, args.channels)
                rows = NewareClient.parse_inquire(
                    client.inquire(chans, args.aux, not args.no_barcode))
                write_rows(rows, None)

            elif act == "df":
                ch = parse_channel(args.channel)
                rows = client.download_all(ch, auxid=args.auxid, testid=args.testid,
                                           count=args.count,
                                           max_rows=args.limit or None)
                print(f"共 {len(rows)} 条")
                write_rows(rows, args.out)

            elif act == "step":
                ch = parse_channel(args.channel)
                resp = client.download_steplayer(ch, args.testid,
                                                 dcir=0 if args.no_dcir else 1)
                rows = NewareClient.parse_data(resp)
                for r in rows:
                    print(f"  工步{str(r.get('stepindex', '')):>4} "
                          f"循环{str(r.get('cycleid', '')):>4} "
                          f"{str(r.get('steptype', '')):<6} "
                          f"{r.get('startvolt')}→{r.get('endvolt')}V  "
                          f"cap={str(r.get('cap', '')):<23} "
                          f"eng={str(r.get('eng', '')):<23} "
                          f"dcir={r.get('dcir')}")
                write_rows(rows, args.out)

            elif act == "dfcheck":
                chans = _channels_or_discover(client, args.channels)
                rows = NewareClient.parse_inquiredf(
                    client.inquiredf(chans, args.testid))
                for r in rows:
                    print(f"  {r['devtype']}-{r['devid']}-{r['subdevid']}-{r['chlid']}"
                          f"  testid={r['testid']}  已上传={r['uploaded']}"
                          f"  完整={'是' if r['complete'] else '否'}")

            elif act == "log":
                ch = parse_channel(args.channel)
                rows = NewareClient.parse_data(
                    client.downloadlog(ch, args.testid, args.level))
                print(f"共 {len(rows)} 条（协议上限 5000）")
                write_rows(rows, args.out)

            elif act == "start":
                items = []
                for spec in args.items:
                    parts = spec.split(":")
                    if len(parts) not in (3, 4):
                        raise NewareError(
                            f"start 参数格式应为 通道:工步文件:条码[:备注]，收到 {spec!r}")
                    channel, path, barcode = parts[0], parts[1], parts[2]
                    remark = parts[3] if len(parts) == 4 else None
                    items.append((parse_channel(channel), path, barcode, remark))
                backup = None
                if args.backup_dir:
                    backup = {
                        "backupdir": args.backup_dir,
                        "filetype": 0 if args.backup_type == "nda" else 1,
                        "filenametype": 1,      # 1 = 用条码命名，便于和电芯对应
                    }
                print(client.start(items, args.dbc_can, backup).dump())

            elif act == "wait":
                chans = [parse_channel(s) for s in args.channels]
                final = client.wait_until_status(chans, timeout=args.timeout,
                                                interval=args.interval)
                print("最终状态：")
                for key, value in final.items():
                    print(f"  {key}  {value}  {STATUS_NAMES.get(value, '')}")

            elif act == "stop":
                chans = [parse_channel(s) for s in args.channels]
                print(client.stop(chans).dump())

            elif act == "light":
                chans = [parse_channel(s) for s in args.channels]
                print(client.light(chans, not args.off).dump())

            elif act == "raw":
                print(client.raw(args.body).dump())

        return 0

    except NewareError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
