#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小可读例子：从头到尾走一次，把电池测试柜的数据取出来。

这个文件故意写得很啰嗦，每一行都解释了"为什么要这么写"。
看懂这 30 行，就看懂了整个通讯过程。

运行（把 IP 换成现场那台跑 BTS Client 的电脑）：
    python example_minimal.py --transport tcp --host 192.168.1.20

如果 BTS Client 和这个脚本在同一台电脑上：
    python example_minimal.py --transport pipe
"""

import argparse
import csv

# 从我们自己的库里拿四个东西。这个库就是 neware_client.py，
# 它把"和 BTS Client 对话"这件事打包成了几个好用的函数。
from neware_client import Channel, NewareClient, PipeTransport, TcpTransport


def main() -> None:
    # ---------------------------------------------------------------
    # 第 0 步：读命令行参数。就是让用户在命令行里指定"连哪台电脑"。
    # ---------------------------------------------------------------
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["pipe", "tcp"], default="tcp")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    # ---------------------------------------------------------------
    # 第 1 步：建立连接。相当于"拨通电话"。
    #
    #   TcpTransport  = 通过网络连到另一台电脑，需要 IP + 端口号
    #   PipeTransport = 两台程序在同一台电脑上，通过"管道"对话
    #
    # 注意：这一步只是"把电话拨通"，还没说任何话。
    # ---------------------------------------------------------------
    if args.transport == "tcp":
        transport = TcpTransport(args.host, 502)   # 502 是 BTS Client 的固定端口
    else:
        transport = PipeTransport()                # 默认管道名 \\.\pipe\NewareBtsAPI

    transport.connect()
    print(f"已连接：{args.transport}")

    # ---------------------------------------------------------------
    # 第 2 步：创建一个"对话器"。它拿着刚才那条连接，负责一问一答。
    # ---------------------------------------------------------------
    client = NewareClient(transport)

    # ---------------------------------------------------------------
    # 第 3 步：问对方"你有哪些设备、哪些通道？"
    #
    # client.getdevinfo() 做的事：
    #   把下面这段文字发给 BTS Client
    #       <?xml version="1.0" encoding="UTF-8" ?>
    #       <bts version="1.0">
    #       <cmd>getdevinfo</cmd>
    #       </bts>
    #   然后等它回一段文字，把回来的文字原样给我们。
    #
    # 注意区别：
    #   getdevinfo()           -> 发命令，拿回"一段文字"（叫 Response）
    #   parse_devinfo(那段文字) -> 把文字切成"能用的数据结构"（字典/列表）
    # 这两件事分开，是因为有时候你会想先看看原始文字长什么样，方便排查问题。
    # ---------------------------------------------------------------
    response = client.getdevinfo()
    print("\n===== BTS Client 原样回给我们的文字 =====")
    print(response.text)          # 打印原始文字，亲眼看一下它长什么样

    # ---------------------------------------------------------------
    # 第 4 步：把那段文字解析成结构化的数据。
    # 解析 = 从一堆文字里把有用的信息"挑"出来，放进字典里。
    # ---------------------------------------------------------------
    info = NewareClient.parse_devinfo(response)
    print(f"\n解析结果：找到 {info['channel_count']} 个通道")
    for c in info["channels"][:3]:        # 只看前 3 个，免得刷屏
        print(f"  通道地址 {c['devtype']}-{c['devid']}-{c['subdevid']}-{c['chlid']}"
              f"  在 {c['ip']} 上")

    # 一个通道都没有就没法继续了（可能是客户端没连上设备）
    if not info["channels"]:
        print("没有找到通道，退出")
        transport.close()
        return

    # ---------------------------------------------------------------
    # 第 5 步：选一个通道，给它一个"地址对象"。
    #
    # 通道地址是四段数字，像"楼栋-楼层-房间-床位"一样逐级定位：
    #   devtype  设备类型（22=BTS80, 23=BTS81, ... 25=BTS83）
    #   devid    设备号
    #   subdevid 单元号（一台设备里的一个机箱）
    #   chlid    通道号（机箱上的第几个通道，一个通道接一颗电池）
    #
    # 后面所有命令都要带上这个地址，否则对方不知道你要操作哪颗电池。
    # ---------------------------------------------------------------
    first = info["channels"][0]
    channel = Channel(first["devtype"], first["devid"],
                      first["subdevid"], first["chlid"], first["ip"])
    print(f"\n选中通道：{channel}")

    # ---------------------------------------------------------------
    # 第 6 步：把这个通道的历史数据全部拉下来。
    #
    # download_all 内部其实是这样干的（因为协议规定一次最多只能要 1000 条）：
    #   第 1 次：给我第 1 条开始的 1000 条
    #   第 2 次：给我第 1001 条开始的 1000 条
    #   ...一直重复，直到对方返回的条数不足 1000，说明到底了
    # 这个"翻页"过程库已经帮我们做掉了，所以这里只写一行。
    #
    # 每条数据的字段（协议文档里的原话）：
    #   seqid     数据序号
    #   stepid    工步号     —— 当前在执行测试程序的第几步
    #   cycleid   循环号     —— 当前是第几次充放电循环
    #   steptype  工步类型   —— cc=恒流充电 dc=恒流放电 rest=搁置 等等
    #   testtime  本工步已运行时间（单位毫秒）
    #   atime     绝对时间（真实世界的时刻）
    #   volt      电压（伏）
    #   curr      电流（安）
    #   cap       容量（安时）
    #   eng       能量（瓦时）
    #   另外还有 V1 / T1 / Thk1 这类"辅助通道"，是温度、辅助电压等
    # ---------------------------------------------------------------
    print("\n正在拉取历史数据（可能会慢，取决于数据量）...")
    rows = client.download_all(channel, progress=False)
    print(f"共拉到 {len(rows)} 条数据")

    if rows:
        print("\n第一条长这样：")
        for key, value in rows[0].items():
            if key != "dbc":          # dbc 是 CAN 总线信号，内容很长，跳过
                print(f"  {key:12} = {value}")

        # -----------------------------------------------------------
        # 第 7 步：存成 CSV 文件。CSV 就是"逗号分隔的表格"，
        # 双击就能用 Excel 打开，方便肉眼检查。
        # -----------------------------------------------------------
        # dbc 字段是嵌套的列表，CSV 放不下，先去掉
        flat = [{k: v for k, v in row.items() if k != "dbc"} for row in rows]
        fieldnames = []
        for row in flat:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        with open("example_output.csv", "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flat)
        print("\n已保存到 example_output.csv，用 Excel 打开看看")

    # ---------------------------------------------------------------
    # 第 8 步：挂断电话。好习惯，不然对方可能一直等。
    # ---------------------------------------------------------------
    transport.close()
    print("已断开连接")


if __name__ == "__main__":
    main()
