#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把河海大学校徽原图处理成「页头 + 背景水印」能用的资源，并写进 console.py。

    python assets/生成校徽.py

做三件事（都在本机跑，盒子不需要装任何东西）：

  1. 抠白底：原图是"白底 + 一种蓝"的平涂徽标，用亮度做阈值把白底变透明。
  2. 单色化 + 硬边：**不保留原始 RGB**。半透明边缘会带一圈灰、还压不住体积；
     统一成取样自校徽本身的蓝（#0D71AD）、边缘取硬边，浏览器缩小时自己做插值，
     反而是又小又干净。
  3. 输出两个东西：
       assets/hohai-seal.png   —— 给人看的成品（1024px，约 11 KB）
       console.py 里的 _SEAL_PNG_B64 —— 真正被页面用到的（内嵌，省掉文件/路由同步）

★ assets/hohai-seal.png 与 console.py 里的常量**都由本脚本生成**，不要手改任何一个；
  要换图就换 assets/校徽原图.png 再重跑本脚本。

为什么内嵌而不是走路由：这个项目吃过太多次"同一个东西存在多份、真正被执行的那份
不是最新的"（见 AGENTS.md 部署纪律）。113 KB 的 base64 换掉一整套同步风险，划算。
"""

import base64
import re
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
CONSOLE = HERE.parent / "console.py"
SRC = HERE / "校徽原图.png"
OUT_PNG = HERE / "hohai-seal.png"

THRESHOLD = 185     # 亮度阈值：低于它算徽标的笔画。185 偏粗，小尺寸下笔画不断
WIDTH = 1024        # 输出宽度（页头显示 58px、水印约 620px，1024 足够清楚）


def build() -> "Image.Image":
    if not SRC.exists():
        sys.exit(f"找不到原图：{SRC}")
    im = Image.open(SRC).convert("RGB")
    g = im.convert("L")

    # ---- 取样徽标本身的蓝：取深色像素的均值 ----
    px, gl = im.load(), g.load()
    tot = [0, 0, 0]
    n = 0
    for y in range(0, im.height, 2):
        for x in range(0, im.width, 2):
            if gl[x, y] < 100:
                r, gg, b = px[x, y]
                tot[0] += r
                tot[1] += gg
                tot[2] += b
                n += 1
    blue = tuple(round(v / n) for v in tot) if n else (0x0D, 0x71, 0xAD)
    print(f"  取样深色像素 {n} 个 → 徽标蓝 #{blue[0]:02X}{blue[1]:02X}{blue[2]:02X}")

    # ---- 硬边两色调色板：索引 0 = 蓝（徽标），索引 1 = 透明（背景）----
    mark = g.point(lambda v: 0 if v < THRESHOLD else 1)          # 0=笔画
    stamp = mark.point(lambda v: 255 if v == 0 else 0)           # 0 → 255（当蒙版用）
    out = Image.new("P", im.size, 1)
    out.putpalette(list(blue) + [255, 255, 255] + [0] * (768 - 6))
    out.paste(0, (0, 0), stamp)                                  # 只在笔画处盖住底色
    out = out.crop(stamp.getbbox())                              # 按内容裁掉四周留白

    h = round(out.height * WIDTH / out.width)
    out = out.resize((WIDTH, h), Image.LANCZOS)
    out.info["transparency"] = 1
    print(f"  裁切并按 {WIDTH}px 缩放 → {out.size}")
    return out


def main() -> int:
    img = build()
    img.save(OUT_PNG, optimize=True, transparency=1)
    kb = OUT_PNG.stat().st_size / 1024
    print(f"  已写 {OUT_PNG.relative_to(HERE.parent)}  ({kb:.1f} KB)")

    b64 = base64.b64encode(OUT_PNG.read_bytes()).decode("ascii")
    src = CONSOLE.read_text(encoding="utf-8")
    new, cnt = re.subn(r"(# SEAL_B64_BEGIN\n)_SEAL_PNG_B64 = \".*?\"(\n# SEAL_B64_END)",
                       lambda m: m.group(1) + f'_SEAL_PNG_B64 = "{b64}"' + m.group(2),
                       src, flags=re.S)
    if cnt != 1:
        sys.exit("在 console.py 里没找到唯一的一对 SEAL_B64_BEGIN/END 标记，未改动任何东西")
    CONSOLE.write_text(new, encoding="utf-8")
    print(f"  已写入 console.py（base64 {len(b64)/1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
