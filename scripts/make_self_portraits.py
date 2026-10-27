# -*- coding: utf-8 -*-
"""生成「蓝色大肥鱼·鲸鱼娘」自画像（程序化绘制，无版权素材）。

用 Pillow 在 768×768 逻辑画布上 2 倍超采样绘制，输出到 data/self/：
    01-normal.png   日常：睁眼微笑
    02-shy.png      害羞：脸红、闭眼、尾巴绕过来
    03-angry.png    生气：鼓脸“是蓝鲸不是肥！”
    04-eating.png   干饭：抱着白饭碗
    05-wave.png     挥手：浮上水面打招呼
    06-heart.png    比心：双手比心

画法要点：所有部件用「形状并集 + 自动描边」的 blob 方式拼接，避免方块感；
先画背景/尾巴/后发，再画身体/脸/五官/刘海/头顶鳍，最后加暗角输出。

跑法：
    python scripts/make_self_portraits.py
可选再跑 `python scripts/describe_self_portraits.py`，让视觉模型看过自己一次，
把描述写进 data/self/portrait.json。
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "self"

W = H = 768
S = 3  # 超采样倍数（更平滑的边缘）
CW, CH = W * S, H * S

# ---------------- 配色 ----------------
BG_TOP = (12, 32, 62)
BG_BOTTOM = (4, 10, 24)
GLOW = (32, 96, 146)
SEAWEED = (16, 58, 78)
HAIR = (47, 111, 181)
HAIR_LIGHT = (111, 182, 232)
HAIR_DARK = (24, 58, 98)
HAIR_TOP = (38, 70, 126)      # 发根深蓝
HAIR_BOTTOM = (108, 172, 216)  # 发尾浅蓝（参考图渐变）
LACE = (250, 252, 255)         # 女仆头饰白
LACE_SHADOW = (206, 220, 238)
NAVY = (30, 52, 96)            # 深蓝蝴蝶结
SAPPHIRE = (74, 164, 222)      # 蓝宝石胸针
SKIN = (255, 231, 219)
SKIN_SHADOW = (236, 192, 178)
EYE_IRIS = (46, 142, 216)
EYE_IRIS_DARK = (22, 82, 144)
EYE_LASH = (22, 34, 58)
MOUTH = (192, 104, 114)
BLUSH = (255, 148, 170)
DRESS = (240, 247, 255)
DRESS_SHADOW = (196, 220, 242)
FIN = (60, 130, 198)
FIN_LIGHT = (140, 206, 244)
WHITE = (255, 255, 255)


# ---------------- 基础工具 ----------------
def sc(value: float) -> int:
    return int(round(value * S))


def pt(x: float, y: float) -> tuple[int, int]:
    return sc(x), sc(y)


def box(x0, y0, x1, y1):  # noqa: ANN001
    return [sc(x0), sc(y0), sc(x1), sc(y1)]


def bezier(p0, p1, p2, p3, steps: int = 26):  # noqa: ANN001
    points = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1]
        points.append((x, y))
    return points


def lerp(a, b, t):  # noqa: ANN001, ANN201
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def capsule(x0: float, y0: float, x1: float, y1: float, r: float) -> list[tuple[float, float]]:
    """胶囊形状（两端圆头），用来做发束、手臂。"""
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length * r, dx / length * r
    points = []
    steps = 18
    for i in range(steps + 1):
        t = i / steps
        points.append((x0 + dx * t + nx, y0 + dy * t + ny))
    for i in range(steps + 1):
        t = i / steps
        points.append((x1 + dx * t * 0 - nx * math.cos(math.pi * t) - ny * math.sin(math.pi * t),
                       y1 - ny * math.cos(math.pi * t) + nx * math.sin(math.pi * t)))
    return points


def shape_mask(shapes) -> Image.Image:  # noqa: ANN001
    mask = Image.new("L", (CW, CH), 0)
    m = ImageDraw.Draw(mask)
    for shape in shapes:
        kind, data = shape[0], shape[1]
        if kind == "ellipse":
            m.ellipse(box(*data), fill=255)
        else:
            m.polygon([pt(x, y) for x, y in data], fill=255)
    return mask


def blob(canvas: Image.Image, shapes, fill, outline=None, width: float = 3.0) -> Image.Image:  # noqa: ANN001
    """把若干椭圆/多边形合并成一个圆润形状，并沿外轮廓描边。"""
    mask = shape_mask(shapes)
    blur = max(1.2, width * S * 0.6)
    grown = mask.filter(ImageFilter.GaussianBlur(blur)).point(lambda p: 255 if p > 110 else 0)
    if outline is not None:
        edge = ImageChops.subtract(grown, mask)
        canvas.paste(Image.new("RGBA", (CW, CH), outline), (0, 0), edge)
    canvas.paste(Image.new("RGBA", (CW, CH), fill), (0, 0), mask)
    return canvas


def gradient_blob(canvas: Image.Image, shapes, top, bottom, y0: float, y1: float,
                  outline=None, width: float = 3.0) -> Image.Image:  # noqa: ANN001
    """同 blob，但填充是竖直渐变（发根深、发尾浅），用来做参考图的渐变蓝发。"""
    mask = shape_mask(shapes)
    blur = max(1.2, width * S * 0.6)
    grown = mask.filter(ImageFilter.GaussianBlur(blur)).point(lambda p: 255 if p > 110 else 0)
    if outline is not None:
        edge = ImageChops.subtract(grown, mask)
        canvas.paste(Image.new("RGBA", (CW, CH), outline), (0, 0), edge)
    grad = Image.new("RGBA", (CW, CH))
    gd = ImageDraw.Draw(grad)
    span = max(1.0, y1 - y0)
    for y in range(CH):
        t = min(1.0, max(0.0, (y / S - y0) / span))
        gd.line([(0, y), (CW, y)], fill=lerp(top, bottom, t) + (255,))
    canvas.paste(grad, (0, 0), mask)
    return canvas


def bar(canvas: Image.Image, cx: float, cy: float, w: float, h: float, r: float,
        fill, outline=None, width: float = 0.0) -> None:
    """圆角矩形（用 blob 实现）。"""
    half = w / 2
    shapes = [
        ("ellipse", (cx - half, cy - h / 2, cx - half + 2 * r, cy + h / 2)),
        ("ellipse", (cx + half - 2 * r, cy - h / 2, cx + half, cy + h / 2)),
        ("poly", [(cx - half + r, cy - h / 2), (cx + half - r, cy - h / 2),
                  (cx + half - r, cy + h / 2), (cx - half + r, cy + h / 2)]),
    ]
    blob(canvas, shapes, fill, outline, width)


def ellipse(canvas: Image.Image, cx, cy, rx, ry, fill, outline=None, width: float = 0.0):  # noqa: ANN001
    d = ImageDraw.Draw(canvas)
    d.ellipse(box(cx - rx, cy - ry, cx + rx, cy + ry), fill=fill,
              outline=outline, width=sc(max(0.5, width)))


def arc(canvas: Image.Image, cx, cy, rx, ry, start, end, fill, width):  # noqa: ANN001
    ImageDraw.Draw(canvas).arc(box(cx - rx, cy - ry, cx + rx, cy + ry), start, end,
                               fill=fill, width=sc(max(1.0, width)))


def strip(canvas: Image.Image, points, width: float, fill):  # noqa: ANN001
    d = ImageDraw.Draw(canvas)
    d.line([pt(x, y) for x, y in points], fill=fill, width=sc(width), joint="curve")
    for x, y in (points[0], points[-1]):
        d.ellipse(box(x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=fill)


def soft(canvas: Image.Image, draw_fn) -> None:  # noqa: ANN001
    layer = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    canvas.alpha_composite(layer)


# ---------------- 背景 ----------------
def draw_background(seed: int) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", (CW, CH), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(CH):
        d.line([(0, y), (CW, y)], fill=lerp(BG_TOP, BG_BOTTOM, y / CH))

    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse(
        [W / 2 - W * 0.5, -H * 0.1, W / 2 + W * 0.5, H * 0.95], fill=255
    )
    glow = glow.filter(ImageFilter.GaussianBlur(120)).resize((CW, CH), Image.LANCZOS)
    img.paste(Image.new("RGB", (CW, CH), GLOW), (0, 0), glow)

    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).ellipse([-W * 0.4, H * 0.82, W * 1.4, H * 1.4], fill=255)
    shadow = shadow.filter(ImageFilter.GaussianBlur(90)).resize((CW, CH), Image.LANCZOS)
    img.paste(Image.new("RGB", (CW, CH), (2, 6, 15)), (0, 0), shadow)
    canvas = img.convert("RGBA")

    # 深海光柱
    light = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    ld = ImageDraw.Draw(light)
    for i in range(3):
        x = W * (0.4 + i * 0.1)
        ld.polygon(
            [pt(x - 46, -20), pt(x + 46, -20), pt(x + 150, H + 20), pt(x - 26, H + 20)],
            fill=(122, 202, 244, 15),
        )
    canvas.alpha_composite(light.filter(ImageFilter.GaussianBlur(34 * S)))

    # 底部海草剪影
    weeds = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    wd = ImageDraw.Draw(weeds)
    for base_x in (70, 150, 230, 540, 620, 700):
        height = rng.uniform(90, 170)
        sway = rng.uniform(-40, 40)
        curve = bezier(
            (base_x, H + 10), (base_x + sway * 0.3, H - height * 0.4),
            (base_x + sway, H - height * 0.75), (base_x + sway * 1.3, H - height), 16,
        )
        wd.line([pt(x, y) for x, y in curve], fill=SEAWEED + (200,),
                width=sc(rng.uniform(6, 11)), joint="curve")
    canvas.alpha_composite(weeds.filter(ImageFilter.GaussianBlur(2 * S)))

    # 气泡
    bubbles = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bubbles)
    for _ in range(28):
        r = rng.uniform(3, 13)
        x = rng.uniform(20, W - 20)
        y = rng.uniform(20, H - 20)
        alpha = rng.randint(40, 115)
        bd.ellipse(box(x - r, y - r, x + r, y + r),
                   outline=(205, 240, 255, alpha), width=sc(1.2))
        bd.ellipse(box(x - r * 0.5, y - r * 0.6, x - r * 0.1, y - r * 0.2),
                   fill=(255, 255, 255, min(170, alpha + 50)))
    canvas.alpha_composite(bubbles.filter(ImageFilter.GaussianBlur(0.5 * S)))
    return canvas


# ---------------- 角色 ----------------
def draw_tail(canvas: Image.Image) -> None:
    stem = bezier((566, 700), (626, 656), (676, 588), (662, 500), 26)
    strip(canvas, stem, 30, FIN)
    fin1 = [(660, 512), (742, 442), (756, 528)] + bezier(
        (756, 528), (716, 574), (680, 550), (660, 512), 14
    )[::-1]
    blob(canvas, [("poly", fin1)], FIN, FIN_LIGHT, 2.4)
    fin2 = [(656, 520), (612, 430), (566, 494)] + bezier(
        (566, 494), (606, 528), (634, 536), (656, 520), 14
    )[::-1]
    blob(canvas, [("poly", fin2)], FIN, FIN_LIGHT, 2.4)


def draw_back_hair(canvas: Image.Image) -> None:
    gradient_blob(
        canvas,
        [
            ("ellipse", (150, 190, 618, 660)),
            ("ellipse", (140, 380, 300, 730)),
            ("ellipse", (470, 380, 630, 730)),
            ("ellipse", (238, 176, 530, 380)),
        ],
        HAIR_TOP,
        HAIR_BOTTOM,
        190,
        730,
        HAIR_DARK,
        4.2,
    )
    for offset, width in ((22, 8), (42, 5)):
        strand = bezier((214, 336), (176, 452), (186, 604), (216, 690), 26)
        strip(canvas, [(x + offset, y) for x, y in strand], width, HAIR_LIGHT)
    strand_r = bezier((554, 336), (592, 452), (582, 604), (552, 690), 26)
    strip(canvas, strand_r, 5, HAIR_LIGHT)


def draw_body(canvas: Image.Image) -> None:
    # 脖子
    blob(canvas, [("ellipse", (352, 462, 416, 534))], SKIN_SHADOW)
    # 肩膀 + 裙子
    blob(
        canvas,
        [
            ("ellipse", (230, 500, 538, 612)),
            ("poly", [(240, 548), (528, 548), (600, H + 20), (168, H + 20)]),
        ],
        DRESS,
        (168, 198, 228),
        3.6,
    )
    soft(
        canvas,
        lambda ld: ld.ellipse(box(196, 700, W - 196, H + 40), fill=DRESS_SHADOW + (150,)),
    )
    # V 领（肤色的三角，位置压低一点）
    blob(canvas, [("poly", [(W / 2 - 44, 528), (W / 2 + 44, 528), (W / 2, 574)])], SKIN_SHADOW)
    # 领口白色荷叶边（参考图的女仆装领口）
    for dx in (-78, -46, 46, 78):
        blob(canvas, [("ellipse", (W / 2 + dx - 19, 518, W / 2 + dx + 19, 566))], LACE, LACE_SHADOW, 1.4)
    # 深蓝大蝴蝶结 + 蓝宝石胸针
    for sign in (-1, 1):
        blob(
            canvas,
            [("poly", [
                (W / 2, 594), (W / 2 + sign * 36, 560),
                (W / 2 + sign * 76, 578), (W / 2 + sign * 42, 606),
            ])],
            NAVY,
            (10, 20, 44),
            1.6,
        )
    ellipse(canvas, W / 2, 592, 15, 15, NAVY, (10, 20, 44), 1.4)
    ellipse(canvas, W / 2, 592, 9, 9, SAPPHIRE)
    ellipse(canvas, W / 2 - 3, 589, 3, 3, (226, 246, 255))


def draw_face(canvas: Image.Image) -> None:
    blob(canvas, [("ellipse", (266, 234, 502, 516))], SKIN, SKIN_SHADOW, 3.2)


def draw_eyes(canvas: Image.Image, *, style: str = "open", look=(0, 0)) -> None:  # noqa: ANN001
    left_x, right_x = W / 2 - 57, W / 2 + 57
    eye_y = 398
    for cx in (left_x, right_x):
        if style == "closed":
            arc(canvas, cx, eye_y + 6, 30, 17, 200, 340, EYE_LASH, 5)
            continue
        if style == "happy":
            arc(canvas, cx, eye_y - 4, 28, 21, 200, 340, EYE_LASH, 6)
            continue
        ellipse(canvas, cx, eye_y, 30, 34, WHITE, EYE_LASH, 2.6)
        ellipse(canvas, cx + look[0], eye_y + 2 + look[1], 22, 26, EYE_IRIS)
        ellipse(canvas, cx + look[0], eye_y + 9 + look[1], 14, 14, EYE_IRIS_DARK)
        ellipse(canvas, cx + look[0], eye_y + 10 + look[1], 8, 9, (12, 32, 62))
        ellipse(canvas, cx + look[0] - 8, eye_y - 9 + look[1], 8, 9, WHITE)
        ellipse(canvas, cx + look[0] + 9, eye_y + 13 + look[1], 5, 5, (208, 238, 255))
        arc(canvas, cx, eye_y - 3, 31, 30, 196, 344, EYE_LASH, 7)
        arc(canvas, cx, eye_y + 9, 25, 22, 25, 145, (92, 122, 158), 2.4)


def draw_brows(canvas: Image.Image, *, angry: bool = False) -> None:
    left_x, right_x = W / 2 - 54, W / 2 + 54
    eye_y = 398
    if angry:
        arc(canvas, left_x + 4, eye_y - 42, 26, 15, 205, 320, HAIR_DARK, 5)
        arc(canvas, right_x - 4, eye_y - 42, 26, 15, 220, 335, HAIR_DARK, 5)
    else:
        arc(canvas, left_x, eye_y - 40, 24, 13, 205, 335, (112, 142, 178), 4)
        arc(canvas, right_x, eye_y - 40, 24, 13, 205, 335, (112, 142, 178), 4)


def draw_mouth(canvas: Image.Image, *, style: str = "smile", y: float = 454) -> None:
    cx = W / 2
    if style == "smile":
        arc(canvas, cx, y, 20, 14, 25, 155, MOUTH, 5)
    elif style == "shy":
        d = ImageDraw.Draw(canvas)
        d.line([pt(cx - 14, y + 2), pt(cx - 4, y + 8), pt(cx + 6, y + 2), pt(cx + 16, y + 8)],
               fill=MOUTH, width=sc(4), joint="curve")
    elif style == "angry":
        arc(canvas, cx, y + 14, 18, 14, 205, 335, MOUTH, 5)
    elif style == "open":
        ellipse(canvas, cx, y + 6, 18, 16, (148, 60, 72), MOUTH, 3)
        ellipse(canvas, cx, y + 12, 12, 7, (255, 168, 178))


def draw_blush(canvas: Image.Image, alpha: int = 90) -> None:
    def _paint(ld: ImageDraw.ImageDraw) -> None:
        for cx in (W / 2 - 92, W / 2 + 92):
            ld.ellipse(box(cx - 48, 424, cx + 48, 470), fill=BLUSH + (alpha,))

    soft(canvas, _paint)


def draw_front_hair(canvas: Image.Image) -> None:
    # 刘海帘：盖住额头，底部做成不规则发梢（不描边，避免在额头上压一条硬线）
    gradient_blob(
        canvas,
        [
            ("ellipse", (264, 222, 496, 386)),
            (
                "poly",
                [
                    (262, 306), (282, 390), (310, 350), (336, 394), (364, 352),
                    (392, 396), (420, 354), (448, 392), (476, 350), (500, 306),
                ],
            ),
        ],
        HAIR_TOP,
        HAIR_LIGHT,
        226,
        392,
        None,
        3.2,
    )
    # 两侧垂发（画在身体前面，发梢收尖）
    left = (
        bezier((256, 392), (214, 520), (238, 650), (286, 712), 24)
        + bezier((286, 712), (262, 640), (250, 520), (286, 392), 24)[::-1]
    )
    gradient_blob(canvas, [("poly", left)], HAIR_TOP, HAIR_BOTTOM, 380, 720, HAIR_DARK, 3.4)
    right = (
        bezier((512, 392), (554, 520), (530, 650), (482, 712), 24)
        + bezier((482, 712), (506, 640), (518, 520), (482, 392), 24)[::-1]
    )
    gradient_blob(canvas, [("poly", right)], HAIR_TOP, HAIR_BOTTOM, 380, 720, HAIR_DARK, 3.4)
    # 高光
    strip(canvas, bezier((296, 268), (268, 360), (276, 486), (292, 590), 20), 7, HAIR_LIGHT)
    strip(canvas, bezier((470, 260), (500, 350), (496, 470), (484, 560), 20), 5, HAIR_LIGHT)
    strip(canvas, bezier((318, 286), (356, 276), (398, 292), (424, 322), 16), 5, (186, 222, 248))
    strip(canvas, bezier((262, 430), (240, 512), (250, 578), (264, 616), 18), 5, HAIR_LIGHT)


def draw_headpiece(canvas: Image.Image) -> None:
    """白色蕾丝女仆头饰：贴着头顶的一圈荷叶边发箍。"""
    arc(canvas, W / 2, 336, 172, 122, 188, 352, LACE, 26)
    arc(canvas, W / 2, 336, 172, 122, 188, 352, LACE_SHADOW, 2)
    # 荷叶边小圆（沿同一段弧线）
    for i in range(9):
        theta = math.pi * (0.12 + 0.76 * i / 8)
        cx = W / 2 - 168 * math.cos(theta)
        cy = 332 - 118 * math.sin(theta)
        ellipse(canvas, cx, cy, 20, 18, LACE, LACE_SHADOW, 1.3)
    # 头饰中央的蓝色蝴蝶结
    for sign in (-1, 1):
        blob(canvas, [("poly", [(W / 2, 252), (W / 2 + sign * 40, 228), (W / 2 + sign * 40, 272)])],
             (86, 162, 222), (40, 96, 160), 1.4)
    ellipse(canvas, W / 2, 250, 11, 11, (86, 162, 222), (40, 96, 160), 1.2)


def draw_ear_fins(canvas: Image.Image) -> None:
    """头侧的一对小鲸鳍（参考图里像耳朵的位置，朝后上方）。"""
    for sign in (-1, 1):
        base_x = W / 2 + sign * 136
        fin = (
            bezier((base_x - sign * 8, 470), (base_x + sign * 40, 446), (base_x + sign * 70, 408), (base_x + sign * 66, 372), 16)
            + bezier((base_x + sign * 66, 372), (base_x + sign * 34, 400), (base_x + sign * 6, 438), (base_x - sign * 8, 470), 16)
        )
        blob(canvas, [("poly", fin)], FIN, FIN_LIGHT, 2.0)
        for i in range(2):
            p0 = (base_x - sign * 2, 462 - i * 24)
            p1 = (base_x + sign * (44 - i * 8), 448 - 26 - i * 24)
            strip(canvas, bezier(p0, (p0[0] + sign * 16, p0[1] - 8),
                                 (p1[0] - sign * 12, p1[1] + 2), p1, 10), 3, (226, 244, 255))


def draw_hair_bow(canvas: Image.Image) -> None:
    """头发上的蓝色小蝴蝶结发饰。"""
    for sign in (-1, 1):
        blob(canvas, [("poly", [(500, 330), (500 + sign * 40, 306), (500 + sign * 40, 352)])],
             (86, 162, 222), (40, 96, 160), 1.4)
    ellipse(canvas, 500, 328, 10, 10, (86, 162, 222), (40, 96, 160), 1.2)


def draw_ahoge(canvas: Image.Image) -> None:
    ahoge = bezier((W / 2, 240), (W / 2 + 16, 176), (W / 2 + 56, 174), (W / 2 + 46, 128), 18)
    strip(canvas, ahoge, 7, HAIR_LIGHT)


def draw_bowl(canvas: Image.Image) -> None:
    cx, cy = W / 2, 624
    blob(canvas, [("ellipse", (cx - 122, cy - 52, cx + 122, cy + 54))], DRESS, (196, 214, 236), 2.4)
    ellipse(canvas, cx, cy - 14, 104, 30, (224, 236, 250), (196, 214, 236), 2)

    def _rice(ld: ImageDraw.ImageDraw) -> None:
        for i in range(9):
            ld.ellipse(box(cx - 86 + i * 20, cy - 78 - (i % 3) * 8, cx - 42 + i * 20, cy - 30),
                       fill=(255, 255, 252, 238))

    soft(canvas, _rice)
    d = ImageDraw.Draw(canvas)
    d.line([pt(cx + 34, cy - 26), pt(cx + 122, cy - 96)], fill=(196, 150, 104), width=sc(7))
    d.line([pt(cx + 52, cy - 18), pt(cx + 138, cy - 86)], fill=(214, 170, 122), width=sc(7))


def draw_wave_hand(canvas: Image.Image) -> None:
    arm = capsule(516, 604, 566, 396, 15)
    blob(canvas, [("poly", arm)], SKIN, SKIN_SHADOW, 2.2)
    blob(canvas, [("ellipse", (536, 318, 608, 394))], SKIN, SKIN_SHADOW, 2.2)
    d = ImageDraw.Draw(canvas)
    for i in range(4):
        d.line([pt(552 + i * 12, 340), pt(546 + i * 12, 302)],
               fill=SKIN_SHADOW, width=sc(6))


def draw_heart_hands(canvas: Image.Image) -> None:
    for sign in (-1, 1):
        arm = capsule(W / 2 + sign * 132, 660, W / 2 + sign * 66, 596, 14)
        blob(canvas, [("poly", arm)], SKIN, SKIN_SHADOW, 2.2)
    heart = []
    for i in range(120):
        t = math.pi * 2 * i / 120
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        heart.append((W / 2 + x * 2.6, 566 + y * 2.6))
    blob(canvas, [("poly", heart)], (255, 130, 156), (255, 192, 206), 2.6)


def draw_effect(canvas: Image.Image, kind: str) -> None:
    d = ImageDraw.Draw(canvas)
    if kind == "angry":
        for dx in (-1, 1):
            bx, by = W / 2 + dx * 100, 226
            d.line([pt(bx - dx * 18, by - 20), pt(bx + dx * 18, by - 6)],
                   fill=(255, 118, 118), width=sc(6))
            d.line([pt(bx + dx * 18, by - 20), pt(bx - dx * 18, by - 6)],
                   fill=(255, 118, 118), width=sc(6))
    elif kind == "shy":
        for dx, dy in ((-126, -30), (130, -20)):
            heat = bezier(
                (W / 2 + dx, 300 + dy), (W / 2 + dx + 18, 280 + dy),
                (W / 2 + dx - 14, 260 + dy), (W / 2 + dx + 6, 236 + dy), 16,
            )
            strip(canvas, heat, 5, (255, 172, 192))


def draw_finish(canvas: Image.Image) -> Image.Image:
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse([-120, -70, W + 120, H + 70], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(150)).resize((CW, CH), Image.LANCZOS)
    dark = Image.new("RGBA", (CW, CH), (2, 6, 14, 255))
    out = Image.composite(canvas, dark, mask)
    return out.convert("RGB").resize((1024, 1024), Image.LANCZOS)


STYLES = {
    "01-normal": dict(eyes="open", mouth="smile", blush=80, look=(0, 0), effect=None, props=()),
    "02-shy": dict(eyes="closed", mouth="shy", blush=150, look=(-4, 2), effect="shy", props=()),
    "03-angry": dict(eyes="open", mouth="angry", blush=60, look=(0, 0), effect="angry", props=()),
    "04-eating": dict(eyes="happy", mouth="open", blush=100, look=(0, 0), effect=None, props=("bowl",)),
    "05-wave": dict(eyes="happy", mouth="smile", blush=70, look=(0, 0), effect=None, props=("wave",)),
    "06-heart": dict(eyes="happy", mouth="smile", blush=140, look=(0, 0), effect=None, props=("heart",)),
}


def render(style: dict, seed: int) -> Image.Image:
    canvas = draw_background(seed)
    draw_tail(canvas)
    draw_back_hair(canvas)
    draw_body(canvas)
    draw_face(canvas)
    draw_eyes(canvas, style=style["eyes"], look=style["look"])
    draw_brows(canvas, angry=style["effect"] == "angry")
    draw_mouth(canvas, style=style["mouth"])
    draw_blush(canvas, style["blush"])
    draw_front_hair(canvas)
    draw_ear_fins(canvas)
    draw_headpiece(canvas)
    draw_hair_bow(canvas)
    draw_ahoge(canvas)
    for prop in style["props"]:
        if prop == "bowl":
            draw_bowl(canvas)
        elif prop == "wave":
            draw_wave_hand(canvas)
        elif prop == "heart":
            draw_heart_hands(canvas)
    if style["effect"]:
        draw_effect(canvas, style["effect"])
    return draw_finish(canvas)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for index, (name, style) in enumerate(STYLES.items()):
        image = render(style, seed=100 + index)
        path = OUT_DIR / f"{name}.png"
        image.save(path, "PNG", optimize=True)
        print(f"已生成 {path.name}（{path.stat().st_size // 1024} KB）")

    manifest_path = OUT_DIR / "portrait.json"
    manifest: dict = {"appearance": "", "items": []}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    existing = {item.get("file"): item for item in manifest.get("items", [])}
    manifest["items"] = [
        existing.get(f"{name}.png")
        or {"file": f"{name}.png", "mood": name.split("-", 1)[-1], "description": "", "tags": ""}
        for name in STYLES
    ]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"清单已更新：{manifest_path}")


if __name__ == "__main__":
    main()
