#!/usr/bin/env python3
"""
Render a short explainer video: how consumer and producer surplus change when a
binding price ceiling or price floor pushes a market out of equilibrium.

Model (linear, chosen so every area is a round number):
    Demand   P = 100 - Q
    Supply   P = 10 + 0.5 Q
    Equilibrium  Q* = 60, P* = $40   ->  CS = 1,800 (triangle), PS = 900 (triangle)
    Ceiling $30  ->  40 traded, CS = 2,000 (trapezoid), PS = 400 (triangle), DWL = 300
    Floor   $60  ->  40 traded, CS = 800 (triangle), PS = 1,600 (trapezoid), DWL = 300

Usage:
    python3 make_video.py                         # renders price_controls_surplus.mp4
    python3 make_video.py --stills 12,40,95       # writes PNG stills for those times (s)
    python3 make_video.py --no-audio              # skip the synthesized soundtrack

Requires Pillow and ffmpeg (libx264). No other dependencies.
"""

import argparse
import math
import os
import re
import subprocess
import sys
from multiprocessing import Pool

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
SS = 2          # supersampling factor (draw at 2x, box-downsample -> anti-aliasing)
FPS = 30

# --------------------------------------------------------------------------- palette

BG = (14, 18, 30)
CARD = (24, 30, 47)
CARD_EDGE = (46, 55, 80)
TEXT = (236, 240, 247)
MUTED = (150, 160, 184)
AXIS = (190, 198, 216)
CURVE = (232, 236, 244)
CS_C = (76, 148, 255)
PS_C = (255, 172, 46)
DWL_C = (246, 84, 84)
CTRL_C = (232, 124, 250)
GAIN_C = (84, 224, 140)
LOSS_C = (252, 122, 122)


def mix(a, b, k):
    return tuple(int(round(x + (y - x) * k)) for x, y in zip(a, b))


def rgba(c, a=1.0):
    return (c[0], c[1], c[2], int(round(255 * max(0.0, min(1.0, a)))))


# Region fills are opaque colours pre-blended with the background, so adjacent
# pieces never show seams and colour morphs are a simple interpolation.
CS_FILL = mix(BG, CS_C, 0.60)
PS_FILL = mix(BG, PS_C, 0.60)
DWL_FILL = mix(BG, DWL_C, 0.30)
CS_EDGE = mix(CS_C, (255, 255, 255), 0.35)
PS_EDGE = mix(PS_C, (255, 255, 255), 0.35)

TAG_COLORS = {
    "cs": CS_C, "ps": PS_C, "dwl": DWL_C, "ctrl": CTRL_C,
    "tot": TEXT, "xfer": TEXT, "b": TEXT,
}

# --------------------------------------------------------------------------- economics

QE, PE = 60, 40
QMAX, PMAX = 110, 110

POLY = {
    "cs_eq": [(0, 100), (60, 40), (0, 40)],
    "ps_eq": [(0, 10), (60, 40), (0, 40)],
    "dwl": [(40, 60), (60, 40), (40, 30)],
    "dwl_top": [(40, 60), (60, 40), (40, 40)],   # part of DWL that was consumer surplus
    "dwl_bot": [(40, 40), (60, 40), (40, 30)],   # part of DWL that was producer surplus
    # price ceiling at $30
    "c_cs_keep": [(0, 100), (40, 60), (40, 40), (0, 40)],
    "c_xfer": [(0, 30), (40, 30), (40, 40), (0, 40)],
    "c_ps_keep": [(0, 10), (40, 30), (0, 30)],
    "c_cs_new": [(0, 100), (40, 60), (40, 30), (0, 30)],
    # price floor at $60
    "f_cs_keep": [(0, 100), (40, 60), (0, 60)],
    "f_xfer": [(0, 40), (40, 40), (40, 60), (0, 60)],
    "f_ps_keep": [(0, 10), (40, 30), (40, 40), (0, 40)],
    "f_ps_new": [(0, 10), (40, 30), (40, 60), (0, 60)],
}


def area(poly):
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]))) / 2


def check_model():
    A = {k: area(v) for k, v in POLY.items()}
    assert A["cs_eq"] == 1800 and A["ps_eq"] == 900
    assert A["dwl"] == 300 and A["dwl_top"] == 200 and A["dwl_bot"] == 100
    assert A["c_xfer"] == 400 and A["c_cs_new"] == 2000 and A["c_ps_keep"] == 400
    assert A["c_cs_keep"] + A["dwl_top"] == 1800
    assert A["c_xfer"] + A["c_ps_keep"] + A["dwl_bot"] == 900
    assert A["f_xfer"] == 800 and A["f_cs_keep"] == 800 and A["f_ps_new"] == 1600
    assert A["f_cs_keep"] + A["f_xfer"] + A["dwl_top"] == 1800
    assert A["f_ps_keep"] + A["dwl_bot"] == 900


# --------------------------------------------------------------------------- tweening

def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def smooth(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def prog(t, a, b):
    return smooth((t - a) / (b - a))


def lerp(a, b, k):
    return a + (b - a) * k


def lerp2(p, q, k):
    return (lerp(p[0], q[0], k), lerp(p[1], q[1], k))


def pulse(u, a, end):
    """Outline highlight that fades in at `a`, throbs, and fades out by `end`."""
    on = prog(u, a, a + 0.3) * (1 - prog(u, end - 0.4, end))
    return on * (0.65 + 0.35 * math.cos((u - a) * 2 * math.pi * 1.4))


def ease_out(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def back(x, s=1.7):
    """Ease-out with overshoot: runs past 1, then settles back to exactly 1."""
    x = clamp(x) - 1
    return 1 + x * x * ((s + 1) * x + s)


def pop(a):
    """Scale factor for an element appearing with progress a: overshoots, then settles at 1."""
    return back(a, 2.4) if a < 1 else 1.0


def clip_poly(poly, axis, cut, keep_le=True):
    """Clip a polygon to q <= cut (axis 0) or p <= cut (axis 1); keep_le=False keeps >= instead."""
    def inside(pt):
        return pt[axis] <= cut if keep_le else pt[axis] >= cut

    def cross(a, b):
        t = (cut - a[axis]) / (b[axis] - a[axis])
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))

    out = []
    for i in range(len(poly)):
        a, b = poly[i - 1], poly[i]
        if inside(b):
            if not inside(a):
                out.append(cross(a, b))
            out.append(b)
        elif inside(a):
            out.append(cross(a, b))
    return out


def scale_poly(poly, k):
    cx = sum(q for q, _ in poly) / len(poly)
    cy = sum(p for _, p in poly) / len(poly)
    return [(cx + (q - cx) * k, cy + (p - cy) * k) for q, p in poly]


# --------------------------------------------------------------------------- drawing helpers

_FONTS = {}


def font(size, weight="Regular"):
    key = (round(size * SS), weight)
    if key not in _FONTS:
        _FONTS[key] = ImageFont.truetype(f"/usr/share/fonts/opentype/inter/Inter-{weight}.otf", key[0])
    return _FONTS[key]


def draw_text(d, xy, s, font, fill, anchor="la"):
    """d.text() that honours the fill's alpha (Pillow ignores text alpha on RGB images)."""
    alpha = fill[3] / 255 if len(fill) == 4 else 1.0
    if alpha <= 0.01:
        return
    if alpha >= 0.99:
        d.text(xy, s, font=font, fill=fill[:3], anchor=anchor)
        return
    l, t, r, b = d.textbbox(xy, s, font=font, anchor=anchor)
    l, t, r, b = math.floor(l), math.floor(t), math.ceil(r) + 1, math.ceil(b) + 1
    mask = Image.new("L", (r - l, b - t), 0)
    ImageDraw.Draw(mask).text((xy[0] - l, xy[1] - t), s, font=font, fill=round(255 * alpha), anchor=anchor)
    d._image.paste(fill[:3], (l, t, r, b), mask)


def money(v):
    v = int(round(v))
    return f"−${abs(v):,}" if v < 0 else f"${v:,}"


def signed_money(v):
    v = int(round(v))
    return f"+${v:,}" if v > 0 else f"−${abs(v):,}"


class View:
    """Maps (quantity, price) to supersampled pixel coordinates."""

    def __init__(self, x0, y0, x1, y1, scale=1.0):
        self.x0, self.y0, self.x1, self.y1, self.scale = x0, y0, x1, y1, scale

    def pt(self, q, p):
        x = self.x0 + (self.x1 - self.x0) * q / QMAX
        y = self.y1 - (self.y1 - self.y0) * p / PMAX
        return (x * SS, y * SS)


MAIN = View(170, 150, 1090, 850)

_BG = {}


def background(small=False):
    """Soft radial vignette, built once per process."""
    if not _BG:
        g = Image.radial_gradient("L").resize((W * SS, H * SS), Image.BILINEAR)
        hi = Image.new("RGB", (W * SS, H * SS), (17, 22, 37))
        lo = Image.new("RGB", (W * SS, H * SS), (11, 14, 24))
        _BG["big"] = Image.composite(lo, hi, g)
        _BG["small"] = _BG["big"].reduce(SS)
    return _BG["small" if small else "big"]
PANEL = (1180, 150, 1850, 860)


def dashed(d, p0, p1, fill, width, dash=8, gap=7):
    dash, gap = dash * SS, gap * SS
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    if length < 1:
        return
    ux, uy = dx / length, dy / length
    s = 0.0
    while s < length:
        e = min(s + dash, length)
        d.line([(p0[0] + ux * s, p0[1] + uy * s), (p0[0] + ux * e, p0[1] + uy * e)], fill=fill, width=width)
        s += dash + gap


def arrow_head(d, tip, direction, size, fill):
    ux, uy = direction
    n = math.hypot(ux, uy)
    ux, uy = ux / n, uy / n
    px, py = -uy, ux
    base = (tip[0] - ux * size, tip[1] - uy * size)
    d.polygon([tip, (base[0] + px * size * 0.5, base[1] + py * size * 0.5),
               (base[0] - px * size * 0.5, base[1] - py * size * 0.5)], fill=fill)


def hatch(d, pts, fill, spacing, width, frac=1.0):
    """45-degree hatching clipped to a convex polygon (pixel coords); frac < 1 draws only the first lines."""
    n = len(pts)
    signed = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))
    orient = 1 if signed > 0 else -1
    cs = [x - y for x, y in pts]
    c = math.floor(min(cs) / spacing) * spacing
    c_end = lerp(c, max(cs), frac)
    while c <= c_end:
        tmin, tmax, ok = -1e9, 1e9, True
        for i in range(n):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % n]
            ex, ey = bx - ax, by - ay
            c0 = (ex * (0 - ay) - ey * (c - ax)) * orient
            c1 = (ex - ey) * orient
            if abs(c1) < 1e-9:
                if c0 < 0:
                    ok = False
                    break
            elif c1 > 0:
                tmin = max(tmin, -c0 / c1)
            else:
                tmax = min(tmax, -c0 / c1)
        if ok and tmax > tmin:
            d.line([(c + tmin, tmin), (c + tmax, tmax)], fill=fill, width=width)
        c += spacing


def dot(d, xy, r, fill, a=1.0):
    r *= SS
    d.ellipse([xy[0] - r - SS * 2, xy[1] - r - SS * 2, xy[0] + r + SS * 2, xy[1] + r + SS * 2], fill=rgba(BG, a))
    d.ellipse([xy[0] - r, xy[1] - r, xy[0] + r, xy[1] + r], fill=rgba(fill, a))


def markup_words(s):
    """'{cs:Consumer surplus} rises.' -> words made of (fragment, tag) pieces."""
    runs, pos = [], 0
    for m in re.finditer(r"\{(\w+):([^}]*)\}", s):
        if m.start() > pos:
            runs.append((s[pos:m.start()], None))
        runs.append((m.group(2), m.group(1)))
        pos = m.end()
    if pos < len(s):
        runs.append((s[pos:], None))
    words, cur = [], []
    for text, tag in runs:
        if tag:  # a highlighted phrase never wraps mid-phrase
            cur.append((text, tag))
            continue
        for part in re.split(r"(\s+)", text):
            if not part:
                continue
            if part.isspace():
                if cur:
                    words.append(cur)
                    cur = []
            else:
                cur.append((part, tag))
    if cur:
        words.append(cur)
    return words


def rich_fonts(size, plain="Regular", bold="SemiBold"):
    return font(size, plain), font(size, bold)


def word_width(word, fp, fb):
    return sum((fb if tag else fp).getlength(frag) for frag, tag in word)


def draw_words(d, x, y, word_list, fp, fb, a, base=TEXT):
    space = fp.getlength(" ")
    for word in word_list:
        for frag, tag in word:
            f = fb if tag else fp
            draw_text(d, (x, y), frag, font=f, fill=rgba(TAG_COLORS.get(tag, base), a), anchor="ls")
            x += f.getlength(frag)
        x += space


def draw_rich_line(d, x, y, text, size, a, base=TEXT):
    fp, fb = rich_fonts(size)
    draw_words(d, x * SS, y * SS, markup_words(text), fp, fb, a, base)


def draw_caption(d, text, a, top=920, size=34, maxw=1640, rise=0.0):
    if a <= 0.01:
        return
    fp, fb = rich_fonts(size)
    space = fp.getlength(" ")
    words = [(w, word_width(w, fp, fb)) for w in markup_words(text)]

    def wrap(limit):
        lines, cur, curw = [], [], 0.0
        for w, ww in words:
            if cur and curw + space + ww > limit:
                lines.append((cur, curw))
                cur, curw = [w], ww
            else:
                curw = ww if not cur else curw + space + ww
                cur.append(w)
        if cur:
            lines.append((cur, curw))
        return lines

    # greedy wrap, then shrink the width until just before it needs another line
    # so multi-line captions come out balanced instead of leaving an orphan word
    lines = wrap(maxw * SS)
    limit = maxw * SS
    while len(lines) > 1 and limit > 300 * SS:
        trial = wrap(limit - 10 * SS)
        if len(trial) > len(lines):
            break
        lines, limit = trial, limit - 10 * SS
    lh = size * 1.42 * SS
    y0 = (top + size + rise) * SS + (lh / 2 if len(lines) == 1 else 0)
    for i, (ws, lw) in enumerate(lines):
        draw_words(d, (W * SS - lw) / 2, y0 + i * lh, ws, fp, fb, a)


def draw_footnote(d, text, a):
    if a > 0.01:
        draw_text(d, (W * SS / 2, 1054 * SS), text, font=font(21, "Italic"), fill=rgba(MUTED, a), anchor="ms")


# --------------------------------------------------------------------------- the chart

def new_chart(view):
    return dict(view=view, axes=0.0, d=0.0, s=0.0, d_label=0.0, s_label=0.0, eq=0.0,
                pieces=[], hatch=[], outlines=[], vguides=[], dots=[], qticks=[],
                ctrl=None, bracket=None, rlabels=[], callouts=[], boxlabels=[])


def draw_chart(d, ch):
    v = ch["view"]
    sc = v.scale
    P = v.pt

    def px(n):
        return max(1, round(n * sc * SS))

    # filled regions
    for poly, col in ch["pieces"]:
        if len(poly) >= 3:
            d.polygon([P(*q) for q in poly], fill=col)
    for poly, a, frac in ch["hatch"]:
        if a > 0.01 and frac > 0.01:
            hatch(d, [P(*q) for q in poly], rgba(DWL_C, 0.9 * a), 11 * sc * SS, px(2), frac)
    def outline(poly, col, a, w):
        if a > 0.01:
            pts = [P(*q) for q in poly]
            d.line(pts + pts[:2], fill=rgba(col, a), width=px(w), joint="curve")

    for poly, col, a in ch["outlines"]:
        outline(poly, col, a, 4)

    # guides
    ea = ch["eq"]

    def vguide(q, p, a, col=MUTED):
        dashed(d, P(q, p), P(q, p * (1 - ease_out(a))), rgba(col, min(1, 2 * a)), px(2), 7 * sc, 6 * sc)

    if ea > 0:
        dashed(d, P(QE, PE), P(QE * (1 - ease_out(ea)), PE), rgba(MUTED, min(1, 2 * ea)), px(2), 7 * sc, 6 * sc)
        vguide(QE, PE, ea)
    for q, p, a, col in ch["vguides"]:
        if a > 0.01:
            vguide(q, p, a, col)

    # axes
    aa = ch["axes"]
    if aa > 0:
        o, xt, yt = P(0, 0), P(QMAX, 0), P(0, PMAX)
        d.line([o, xt], fill=rgba(AXIS, aa), width=px(3))
        d.line([o, yt], fill=rgba(AXIS, aa), width=px(3))
        arrow_head(d, (xt[0] + px(10), xt[1]), (1, 0), px(16), rgba(AXIS, aa))
        arrow_head(d, (yt[0], yt[1] - px(10)), (0, -1), px(16), rgba(AXIS, aa))
        draw_text(d, (xt[0] + px(22), xt[1]), "Q", font=font(26 * sc, "SemiBold"), fill=rgba(AXIS, aa), anchor="lm")
        draw_text(d, (yt[0], yt[1] - px(22)), "P", font=font(26 * sc, "SemiBold"), fill=rgba(AXIS, aa), anchor="mb")

    # curves
    if ch["d"] > 0:
        a_, b_ = P(0, 100), P(100, 0)
        d.line([a_, lerp2(a_, b_, ch["d"])], fill=rgba(CURVE), width=px(5))
    if ch["s"] > 0:
        a_, b_ = P(0, 10), P(QMAX, 65)
        d.line([a_, lerp2(a_, b_, ch["s"])], fill=rgba(CURVE), width=px(5))
    if ch["d_label"] > 0:
        x, y = P(100, 0)
        draw_text(d, (x + px(14), y - px(14)), "D", font=font(34 * sc, "Bold"), fill=rgba(CURVE, ch["d_label"]), anchor="lb")
    if ch["s_label"] > 0:
        x, y = P(QMAX, 65)
        draw_text(d, (x - px(4), y - px(14)), "S", font=font(34 * sc, "Bold"), fill=rgba(CURVE, ch["s_label"]), anchor="mb")

    for poly, col, a in ch.get("shapes", []):
        outline(poly, col, a, 5)

    # price control line
    c = ch["ctrl"]
    if c and c["a"] > 0.01:
        y = P(0, c["p"])[1]
        d.line([P(0, c["p"]), P(QMAX, c["p"])], fill=rgba(CTRL_C, c["a"]), width=px(4))
        if c.get("tag_a", 0) > 0.01:
            x0 = P(0, 0)[0]
            d.line([(x0 - px(8), y), (x0, y)], fill=rgba(CTRL_C, c["tag_a"]), width=px(3))
            draw_text(d, (x0 - px(14), y), c["tag"], font=font(24 * sc, "Bold"), fill=rgba(CTRL_C, c["tag_a"]), anchor="rm")
        if c.get("pill_a", 0) > 0.01:
            f = font(23 * sc, "SemiBold")
            cx, cy = P(*c["pill_at"])
            tw = f.getlength(c["pill"])
            box = [cx - tw / 2 - px(16), cy - px(20), cx + tw / 2 + px(16), cy + px(20)]
            d.rounded_rectangle(box, radius=px(20), fill=rgba(mix(BG, CTRL_C, 0.18), c["pill_a"]),
                                outline=rgba(CTRL_C, c["pill_a"]), width=px(2))
            draw_text(d, (cx, cy), c["pill"], font=f, fill=rgba(mix(CTRL_C, (255, 255, 255), 0.5), c["pill_a"]), anchor="mm")

    # shortage / excess-supply bracket
    b = ch["bracket"]
    if b and b["a"] > 0.01:
        mid, g = (b["q0"] + b["q1"]) / 2, ease_out(b["a"])
        p0, p1 = P(lerp(mid, b["q0"], g), b["p"]), P(lerp(mid, b["q1"], g), b["p"])
        col = rgba(CTRL_C, min(1, 2 * b["a"]))
        d.line([p0, p1], fill=col, width=px(3))
        arrow_head(d, (p0[0] - px(1), p0[1]), (-1, 0), px(14), col)
        arrow_head(d, (p1[0] + px(1), p1[1]), (1, 0), px(14), col)
        for p_ in (p0, p1):
            d.line([(p_[0], p_[1] - px(9)), (p_[0], p_[1] + px(9))], fill=col, width=px(3))
        lx, ly = P((b["q0"] + b["q1"]) / 2, b["lp"])
        f = font(24 * sc, "SemiBold")
        tw = f.getlength(b["label"])
        d.rectangle([lx - tw / 2 - px(8), ly - px(16), lx + tw / 2 + px(8), ly + px(16)], fill=rgba(BG, b["a"]))
        draw_text(d, (lx, ly), b["label"], font=f, fill=col, anchor="mm")

    # points
    if ea > 0:
        e = P(QE, PE)
        dot(d, e, 7 * sc * pop(ea), TEXT, min(1, 3 * ea))
        draw_text(d, (e[0], e[1] - px(14)), "E", font=font(26 * sc, "Bold"), fill=rgba(TEXT, ea), anchor="mb")
    for q, p, col, a in ch["dots"]:
        if a > 0.01:
            dot(d, P(q, p), 7 * sc * pop(a), col, min(1, 3 * a))

    # axis ticks
    def ptick(p, txt, col, a):
        x0, y = P(0, p)
        d.line([(x0 - px(8), y), (x0, y)], fill=rgba(col, a), width=px(3))
        draw_text(d, (x0 - px(14), y), txt, font=font(24 * sc, "SemiBold"), fill=rgba(col, a), anchor="rm")

    def qtick(q, txt, sub, col, a):
        x, y0 = P(q, 0)
        d.line([(x, y0), (x, y0 + px(8))], fill=rgba(col, a), width=px(3))
        draw_text(d, (x, y0 + px(12)), txt, font=font(24 * sc, "SemiBold"), fill=rgba(col, a), anchor="mt")
        if sub:
            draw_text(d, (x, y0 + px(42)), sub, font=font(19 * sc, "Medium"), fill=rgba(MUTED, a), anchor="mt")

    if ea > 0:
        ptick(PE, "$40", MUTED, ea)
        qtick(QE, "60", "Q*" if sc >= 1 else None, MUTED, ea)
    for q, txt, sub, col, a in ch["qticks"]:
        if a > 0.01:
            qtick(q, txt, sub, col, a)

    # labels inside regions
    for L in ch["rlabels"]:
        a = L["a"]
        if a <= 0.01:
            continue
        cx, cy = P(L["q"], L["p"])
        cy += px(16 * (1 - ease_out(a)))
        col = rgba((255, 255, 255), a)
        if L.get("compact"):
            draw_text(d, (cx, cy - px(2)), f"{L['title']} {money(L['value'])}", font=font(23 * sc, "Bold"), fill=col, anchor="ms")
            if L.get("sub_a", 0) > 0.01:
                draw_text(d, (cx, cy + px(4)), L["sub"], font=font(19 * sc, "MediumItalic"),
                       fill=rgba((255, 255, 255), L["sub_a"] * 0.85), anchor="mt")
        else:
            draw_text(d, (cx, cy - px(16)), L["title"], font=font(24 * sc, "Bold"), fill=col, anchor="ms")
            draw_text(d, (cx, cy + px(18)), money(L["value"]), font=font(32 * sc, "Bold"), fill=col, anchor="ms")
            if L.get("sub_a", 0) > 0.01:
                draw_text(d, (cx, cy + px(26)), L["sub"], font=font(21 * sc, "MediumItalic"),
                       fill=rgba((255, 255, 255), L["sub_a"] * 0.85), anchor="mt")

    for L in ch["boxlabels"]:
        if L["a"] > 0.01:
            cx, cy = P(L["q"], L["p"])
            draw_text(d, (cx, cy), L["text"], font=font(21 * sc, "SemiBold"), fill=rgba((255, 255, 255), L["a"]), anchor="mm")

    # callouts with leader lines
    for L in ch["callouts"]:
        a = L["a"]
        if a <= 0.01:
            continue
        tx, ty = P(L["tq"], L["tp"])
        sx, sy = P(L["q"], L["p"])
        end = lerp2((sx, sy), (tx, ty + px(L.get("lead_dy", 0))), ease_out(min(1, 1.7 * a)))
        d.line([(sx, sy), end], fill=rgba(DWL_C, min(1, 3 * a)), width=px(2))
        dot(d, (sx, sy), 4.5 * sc * pop(min(1, 2 * a)), DWL_C, min(1, 3 * a))
        a = clamp(2 * a - 1)
        if a <= 0.01:
            continue
        tw = font(23 * sc, "SemiBold").getlength(L["title"])
        d.rounded_rectangle([tx - tw / 2 - px(10), ty - px(34), tx + tw / 2 + px(10), ty + px(38)],
                            radius=px(10), fill=rgba(BG, a), outline=rgba(mix(BG, DWL_C, 0.5), a), width=px(2))
        draw_text(d, (tx, ty - px(4)), L["title"], font=font(23 * sc, "SemiBold"), fill=rgba(DWL_C, a), anchor="mb")
        draw_text(d, (tx, ty + px(2)), money(L["value"]), font=font(30 * sc, "Bold"), fill=rgba(mix(DWL_C, (255, 255, 255), 0.25), a), anchor="mt")


# --------------------------------------------------------------------------- scorecard panel

def draw_panel(d, pv, a):
    if a <= 0.01:
        return
    x0, y0, x1, y1 = PANEL
    d.rounded_rectangle([x0 * SS, y0 * SS, x1 * SS, y1 * SS], radius=18 * SS,
                        fill=rgba(CARD, a), outline=rgba(CARD_EDGE, a), width=2 * SS)
    draw_text(d, ((x0 + 32) * SS, (y0 + 50) * SS), "Surplus scorecard", font=font(28, "Bold"), fill=rgba(TEXT, a), anchor="ls")
    if pv["delta"]:
        draw_text(d, ((x1 - 32) * SS, (y0 + 50) * SS), "change vs. equilibrium", font=font(19, "Medium"),
               fill=rgba(MUTED, a * pv["delta"]), anchor="rs")

    rows = [
        ("Consumer surplus", CS_C, pv["cs"], 1800),
        ("Producer surplus", PS_C, pv["ps"], 900),
        ("Deadweight loss", DWL_C, pv["dwl"], 0),
        ("Total surplus", TEXT, pv["cs"] + pv["ps"], 2700),
    ]
    bx0, bx1 = (x0 + 32) * SS, (x1 - 32) * SS
    for i, (label, col, val, eqv) in enumerate(rows):
        y = y0 + 84 + i * 124
        d.ellipse([(x0 + 34) * SS, (y + 14) * SS, (x0 + 50) * SS, (y + 30) * SS], fill=rgba(col, a))
        draw_text(d, ((x0 + 62) * SS, (y + 22) * SS), label, font=font(26, "Medium"), fill=rgba(TEXT, a), anchor="lm")
        bump = pv.get("pop", {}).get(("cs", "ps", "dwl", "tot")[i], 0.0)
        draw_text(d, ((x1 - 32) * SS, (y + 22) * SS), money(val), font=font(34 * (1 + 0.16 * bump), "Bold"),
                  fill=rgba(mix(col, (255, 255, 255), 0.35 * bump), a), anchor="rm")
        delta = val - eqv if label != "Deadweight loss" else 0
        if pv["delta"] and abs(delta) >= 0.5:
            dc = GAIN_C if delta > 0 else LOSS_C
            draw_text(d, ((x1 - 32) * SS, (y + 58) * SS), signed_money(delta), font=font(22, "SemiBold"),
                   fill=rgba(dc, a * pv["delta"]), anchor="rm")
        by0, by1 = (y + 78) * SS, (y + 92) * SS
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=7 * SS, fill=rgba(CARD_EDGE, a))
        full = bx1 - bx0
        if label == "Total surplus":
            x = bx0
            for v_, c_ in ((pv["cs"], CS_C), (pv["ps"], PS_C)):
                w_ = full * v_ / 2700
                if w_ > 1:
                    d.rectangle([x, by0, x + w_, by1], fill=rgba(c_, a))
                x += w_
            w_ = full * pv["dwl"] / 2700
            if w_ > 1:
                d.rectangle([x, by0, x + w_, by1], fill=rgba(DWL_FILL, a))
                hatch(d, [(x, by0), (x + w_, by0), (x + w_, by1), (x, by1)], rgba(DWL_C, a), 8 * SS, 2 * SS)
        else:
            w_ = full * val / 2700
            if w_ > 1:
                d.rounded_rectangle([bx0, by0, bx0 + w_, by1], radius=7 * SS, fill=rgba(col, a))

    # units traded
    y = y0 + 84 + 4 * 124 + 14
    d.line([bx0, (y - 16) * SS, bx1, (y - 16) * SS], fill=rgba(CARD_EDGE, a), width=2 * SS)
    draw_text(d, ((x0 + 34) * SS, (y + 26) * SS), "Units traded", font=font(26, "Medium"), fill=rgba(TEXT, a), anchor="lm")
    q = pv["qty"]
    draw_text(d, ((x1 - 32) * SS, (y + 26) * SS), "—" if q is None else f"{int(round(q))}",
              font=font(34 * (1 + 0.16 * pv.get("pop", {}).get("qty", 0.0)), "Bold"), fill=rgba(TEXT, a), anchor="rm")
    if pv["delta"] and q is not None and abs(q - QE) >= 0.5:
        draw_text(d, ((x1 - 32) * SS, (y + 62) * SS), f"−{int(round(QE - q))} units", font=font(22, "SemiBold"),
               fill=rgba(LOSS_C, a * pv["delta"]), anchor="rm")


def draw_header(d, title, a):
    if a <= 0.01:
        return
    x = (170 - 28 * (1 - ease_out(a))) * SS
    draw_text(d, (170 * SS, 54 * SS), "PRICE CONTROLS & ECONOMIC SURPLUS", font=font(19, "SemiBold"), fill=rgba(MUTED, a), anchor="ls")
    draw_text(d, (x, 100 * SS), title, font=font(40, "Bold"), fill=rgba(TEXT, a), anchor="ls")


# --------------------------------------------------------------------------- scenes

T_TITLE, T_EQ, T_CEIL, T_FLOOR, T_SUM = 5.0, 20.0, 33.0, 32.5, 15.0
S_EQ = T_TITLE
S_CEIL = S_EQ + T_EQ
S_FLOOR = S_CEIL + T_CEIL
S_SUM = S_FLOOR + T_FLOOR
DURATION = S_SUM + T_SUM


def eq_base(ch):
    ch.update(axes=1.0, d=1.0, s=1.0, d_label=1.0, s_label=1.0, eq=1.0)


def scene_title(d, u):
    out = 1 - prog(u, T_TITLE - 0.6, T_TITLE)
    cx = W * SS / 2

    def stagger(i):
        k = prog(u, 0.15 + 0.18 * i, 0.95 + 0.18 * i)
        return k * out, (30 * (1 - ease_out(k)) - 14 * (1 - out)) * SS

    a, dy = stagger(0)
    draw_text(d, (cx, 372 * SS + dy), "ECONOMICS EXPLAINER", font=font(24, "SemiBold"), fill=rgba(MUTED, a), anchor="ms")
    a, dy = stagger(1)
    draw_text(d, (cx, 470 * SS + dy), "How Price Controls Change Surplus", font=font(80, "Bold"), fill=rgba(TEXT, a), anchor="ms")
    a, dy = stagger(2)
    draw_text(d, (cx, 540 * SS + dy), "Price ceilings, price floors, and deadweight loss",
              font=font(36, "Regular"), fill=rgba(MUTED, a), anchor="ms")
    chips = [("Consumer surplus", CS_FILL, CS_C, False), ("Producer surplus", PS_FILL, PS_C, False),
             ("Deadweight loss", DWL_FILL, DWL_C, True)]
    f = font(28, "Medium")
    gap, sq = 64 * SS, 30 * SS
    widths = [sq + 14 * SS + f.getlength(t) for t, *_ in chips]
    x = cx - (sum(widths) + gap * (len(chips) - 1)) / 2
    for i, ((t, fill, edge, hat), w_) in enumerate(zip(chips, widths)):
        a2, dy = stagger(3 + 0.6 * i)
        y = 650 * SS + dy
        box = [x, y - sq / 2, x + sq, y + sq / 2]
        d.rectangle(box, fill=rgba(fill, a2), outline=rgba(edge, a2), width=2 * SS)
        if hat:
            hatch(d, [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])], rgba(DWL_C, a2), 8 * SS, 2 * SS)
        draw_text(d, (x + sq + 14 * SS, y), t, font=f, fill=rgba(TEXT, a2), anchor="lm")
        x += w_ + gap


EQ_CAPTIONS = [
    (0.6, 4.6, "{b:Demand} slopes down, {b:supply} slopes up. They cross at the equilibrium {b:E}: price P* = $40, quantity Q* = 60."),
    (4.8, 9.4, "{cs:Consumer surplus} is the area under demand and above the price: what buyers would pay, minus what they actually pay."),
    (9.6, 14.2, "{ps:Producer surplus} is the area above supply and below the price: what sellers receive, minus the least they'd accept."),
    (14.4, 19.6, "At equilibrium every worthwhile trade happens, so total surplus is as large as it can be: {tot:$1,800 + $900 = $2,700}."),
]


def scene_eq(d, u):
    ch = new_chart(MAIN)
    ch["axes"] = prog(u, 0.0, 0.8)
    ch["d"] = ease_out((u - 0.6) / 1.4)
    ch["s"] = ease_out((u - 1.6) / 1.4)
    ch["d_label"] = prog(u, 1.8, 2.3)
    ch["s_label"] = prog(u, 2.8, 3.3)
    ch["eq"] = prog(u, 3.0, 3.6)
    kcs, kps = prog(u, 5.0, 6.2), prog(u, 9.8, 11.0)
    # surplus areas sweep in from the price axis toward E
    if kcs > 0:
        ch["pieces"].append((clip_poly(POLY["cs_eq"], 0, QE * ease_out(kcs)), CS_FILL))
    if kps > 0:
        ch["pieces"].append((clip_poly(POLY["ps_eq"], 0, QE * ease_out(kps)), PS_FILL))
    ch["shapes"] = [(POLY["cs_eq"], CS_EDGE, pulse(u, 5.0, 8.8)), (POLY["ps_eq"], PS_EDGE, pulse(u, 9.8, 13.6))]
    ch["rlabels"].append(dict(q=20, p=58, title="CS", value=1800, a=prog(u, 5.6, 6.2)))
    ch["rlabels"].append(dict(q=20, p=30, title="PS", value=900, a=prog(u, 10.4, 11.0)))
    draw_chart(d, ch)
    bump = lambda k: math.sin(math.pi * k)  # noqa: E731
    pv = dict(cs=1800 * kcs, ps=900 * kps, dwl=0, qty=QE if ch["eq"] > 0.5 else None, delta=0,
              pop=dict(cs=bump(kcs), ps=bump(kps), tot=max(bump(kcs), bump(kps))))
    draw_panel(d, pv, prog(u, 0.4, 1.4))
    draw_header(d, "Market equilibrium", prog(u, 0.0, 0.6) * (1 - prog(u, T_EQ - 0.6, T_EQ)))
    return EQ_CAPTIONS, None


# Timeline shared by the ceiling and floor scenes (seconds into the scene).
T_LINE = (1.0, 2.5)
T_DOTS = (5.0, 5.8)
T_BRACKET = (5.6, 6.4)
T_TRADE = (7.4, 8.2)
T_XFER_PULSE = 10.4
T_XFER = (11.2, 12.7)
T_DWL_PULSE = 15.8
T_DWL = (16.6, 17.9)
T_SHAPES = 21.2

CONTROL = {
    "ceiling": dict(
        header="Price ceiling: a legal maximum price",
        price=30, tag="$30", pill="Price ceiling = $30", pill_at=(93, 35.5),
        qticks=[(40, "40", "Qs"), (70, "70", "Qd")], dots=[(40, 30), (70, 30)], extra_dot=(40, 60),
        bracket=dict(q0=40, q1=70, p=24, label="Shortage = 30 units", lp=17.5),
        pieces=[("c_cs_keep", CS_FILL, None), ("dwl_top", CS_FILL, "dwl"), ("c_xfer", PS_FILL, "xfer"),
                ("c_ps_keep", PS_FILL, None), ("dwl_bot", PS_FILL, "dwl")],
        xfer_poly="c_xfer", xfer_to=CS_FILL, xfer_wipe=(40, 30), xfer_text="$400 transfer", xfer_at=(20, 35),
        dcs=(400, -200), dps=(-400, -100),
        cs_final=(15, 63), ps_final=(12, 23.5), ps_compact=True,
        cs_poly="c_cs_new", cs_shape="trapezoid", ps_poly="c_ps_keep", ps_shape="triangle",
        callout=dict(q=47, p=47, tq=82, tp=82, lead_dy=0),
        captions=[
            (0.4, 4.8, "A {ctrl:price ceiling} is a legal maximum price. It only binds if it is set {ctrl:below} equilibrium: here, $30."),
            (5.0, 10.2, "At $30, buyers want 70 units but sellers supply only 40: a {ctrl:shortage} of 30. Only 40 units actually trade."),
            (10.4, 15.6, "Sellers get $10 less on each of those 40 units, so this {xfer:$400 rectangle} moves from {ps:producers} to {cs:consumers}."),
            (15.8, 21.0, "Units 40 to 60 are no longer traded, so their surplus simply vanishes. This triangle is the {dwl:deadweight loss: $300}."),
            (21.2, 26.6, "New {cs:consumer surplus} is a trapezoid: $1,800 + $400 − $200 = $2,000. {ps:Producer surplus} shrinks to a triangle: $400."),
            (26.8, 31.3, "Consumers who get the good gain, producers lose more, and total surplus falls from $2,700 to {tot:$2,400}."),
        ],
        footnote=(T_SHAPES, 31.3, "*Assumes the 40 available units go to the buyers who value them most."),
    ),
    "floor": dict(
        header="Price floor: a legal minimum price",
        price=60, tag="$60", pill="Price floor = $60", pill_at=(72, 80),
        qticks=[(40, "40", "Qd"), (100, "100", "Qs")], dots=[(40, 60), (100, 60)], extra_dot=(40, 30),
        bracket=dict(q0=40, q1=100, p=65, label="Excess supply = 60 units", lp=70.5),
        pieces=[("f_cs_keep", CS_FILL, None), ("f_xfer", CS_FILL, "xfer"), ("dwl_top", CS_FILL, "dwl"),
                ("f_ps_keep", PS_FILL, None), ("dwl_bot", PS_FILL, "dwl")],
        xfer_poly="f_xfer", xfer_to=PS_FILL, xfer_wipe=(40, 60), xfer_text="$800 transfer", xfer_at=(20, 50),
        dcs=(-800, -200), dps=(800, -100),
        cs_final=(13, 75.5), ps_final=(15, 30), ps_compact=False,
        cs_poly="f_cs_keep", cs_shape="triangle", ps_poly="f_ps_new", ps_shape="trapezoid",
        callout=dict(q=50, p=38, tq=72, tp=12, lead_dy=0),
        captions=[
            (0.4, 4.8, "A {ctrl:price floor} is a legal minimum price (think minimum wage). It only binds if set {ctrl:above} equilibrium: here, $60."),
            (5.0, 10.2, "At $60, sellers offer 100 units but buyers want only 40: an {ctrl:excess supply} of 60. Only 40 units actually trade."),
            (10.4, 15.6, "Buyers pay $20 more on each of those 40 units, so this {xfer:$800 rectangle} moves from {cs:consumers} to {ps:producers}."),
            (15.8, 21.0, "Again, units 40 to 60 are never traded. The surplus they would have created is the {dwl:deadweight loss: $300}."),
            (21.2, 26.6, "{cs:Consumer surplus} shrinks to a triangle: $800. {ps:Producer surplus} becomes a trapezoid: $900 + $800 − $100 = $1,600."),
            (26.8, 31.3, "Producers who make a sale gain, consumers lose more, and total surplus again falls to {tot:$2,400}."),
        ],
        footnote=(T_SHAPES, 31.3, "*Assumes only the 40 units demanded get produced, by the lowest-cost sellers."),
    ),
}


def build_control_chart(view, cfg, u, out, mini=False):
    """Chart state for a price-control scene at local time u (mini=True: final state, no animation)."""
    ch = new_chart(view)
    eq_base(ch)
    a_line = prog(u, T_LINE[0], T_LINE[0] + 0.4) * out
    k_line = back((u - T_LINE[0]) / (T_LINE[1] - T_LINE[0]), 2.2)  # drops in and settles
    a_dots = prog(u, *T_DOTS) * out
    a_trade = prog(u, *T_TRADE) * out
    k1 = prog(u, *T_XFER) * out
    k2 = prog(u, *T_DWL) * out
    a_shape_cs = prog(u, T_SHAPES, T_SHAPES + 0.6) * out
    a_shape_ps = prog(u, T_SHAPES + 1.0, T_SHAPES + 1.6) * out

    # spotlight: dim everything except the region the caption is talking about
    dim_x = 0 if mini else prog(u, T_XFER_PULSE, T_XFER_PULSE + 0.5) * (1 - prog(u, 15.2, 15.7)) * out
    dim_d = 0 if mini else prog(u, T_DWL_PULSE, T_DWL_PULSE + 0.5) * (1 - prog(u, 20.6, 21.1)) * out

    def lit(col, kind):
        dim = (dim_x if kind != "xfer" else 0) + (dim_d if kind != "dwl" else 0)
        return mix(col, BG, 0.55 * dim)

    for key, base, kind in cfg["pieces"]:
        col = mix(base, DWL_FILL, k2) if kind == "dwl" else base
        ch["pieces"].append((POLY[key], lit(col, kind)))
        if kind == "xfer" and k1 > 0:
            # the new owner's colour sweeps across the rectangle from their side
            start, end = cfg["xfer_wipe"]
            part = clip_poly(POLY[key], 1, lerp(start, end, ease_out(k1)), keep_le=end > start)
            ch["pieces"].append((part, lit(cfg["xfer_to"], kind)))
    ch["hatch"].append((POLY["dwl"], min(1.0, 3 * k2), ease_out(k2)))
    if not mini:
        # a ring that collapses onto the deadweight-loss triangle as it appears
        kp = clamp((u - T_DWL[0]) / 0.8) * out
        if 0 < kp < 1:
            ch.setdefault("shapes", []).append((scale_poly(POLY["dwl"], lerp(1.6, 1.0, ease_out(kp))), DWL_C, 1 - kp))

    if not mini:
        ch["outlines"].append((POLY[cfg["xfer_poly"]], TEXT, pulse(u, T_XFER_PULSE, T_XFER[1] + 0.3) * out))
        ch["outlines"].append((POLY["dwl"], TEXT, pulse(u, T_DWL_PULSE, T_DWL[1] + 0.3) * out))
    ch.setdefault("shapes", []).extend([(POLY[cfg["cs_poly"]], CS_EDGE, a_shape_cs), (POLY[cfg["ps_poly"]], PS_EDGE, a_shape_ps)])

    ch["ctrl"] = dict(p=lerp(PE, cfg["price"], k_line), a=a_line, tag=cfg["tag"],
                      tag_a=prog(u, T_LINE[1] - 0.3, T_LINE[1] + 0.2) * out,
                      pill=cfg["pill"], pill_at=cfg["pill_at"],
                      pill_a=0 if mini else prog(u, T_LINE[1] - 0.2, T_LINE[1] + 0.4) * out)

    for (q, txt, sub), (dq, dp) in zip(cfg["qticks"], cfg["dots"]):
        ch["qticks"].append((q, txt, None if mini else sub, MUTED, a_dots))
        ch["dots"].append((dq, dp, CTRL_C, a_dots))
    if not mini:
        b = dict(cfg["bracket"])
        b["a"] = prog(u, *T_BRACKET) * out
        ch["bracket"] = b
    # the quantity actually traded
    ch["vguides"].append((40, 60, a_trade, TEXT))
    ch["dots"].append((*cfg["extra_dot"], TEXT, a_trade))

    cs_val = 1800 + cfg["dcs"][0] * k1 + cfg["dcs"][1] * k2
    ps_val = 900 + cfg["dps"][0] * k1 + cfg["dps"][1] * k2
    ch["rlabels"].append(dict(q=lerp(20, cfg["cs_final"][0], k1), p=lerp(58, cfg["cs_final"][1], k1),
                              title="CS", value=cs_val, a=1.0, sub=cfg["cs_shape"], sub_a=a_shape_cs,
                              compact=mini))
    ch["rlabels"].append(dict(q=lerp(20, cfg["ps_final"][0], k1), p=lerp(30, cfg["ps_final"][1], k1),
                              title="PS", value=ps_val, a=1.0, sub=cfg["ps_shape"], sub_a=a_shape_ps,
                              compact=mini or (cfg["ps_compact"] and k1 > 0.5)))
    if not mini:
        xa = prog(u, T_XFER[1], T_XFER[1] + 0.5) * out
        ch["boxlabels"].append(dict(q=cfg["xfer_at"][0], p=cfg["xfer_at"][1], text=cfg["xfer_text"], a=xa))
        c = dict(cfg["callout"])
        c.update(title="Deadweight loss", value=300, a=prog(u, T_DWL[1] - 0.3, T_DWL[1] + 0.3) * out)
        ch["callouts"].append(c)
    b1, b2 = math.sin(math.pi * k1), math.sin(math.pi * k2)
    return ch, dict(cs=cs_val, ps=ps_val, dwl=300 * k2, qty=lerp(QE, 40, a_trade), delta=1.0,
                    pop=dict(cs=max(b1, b2), ps=max(b1, b2), dwl=b2, tot=b2, qty=math.sin(math.pi * a_trade)))


def scene_control(d, u, kind):
    cfg = CONTROL[kind]
    duration = T_CEIL if kind == "ceiling" else T_FLOOR
    # The ceiling scene rewinds to equilibrium; the floor scene fades out to the summary.
    out = 1 - prog(u, 31.4, 32.4) if kind == "ceiling" else 1.0
    ch, pv = build_control_chart(MAIN, cfg, u, out)
    draw_chart(d, ch)
    draw_panel(d, pv, 1.0)
    draw_header(d, cfg["header"], prog(u, 0.0, 0.6) * (1 - prog(u, duration - 0.6, duration)))
    return cfg["captions"], cfg["footnote"]


SUM_CAPTIONS = [
    (0.9, 7.2, "Same pattern, opposite directions: a {xfer:rectangle} of surplus shifts toward the side the control is meant to help..."),
    (7.4, 14.0, "...while a {dwl:triangle} of surplus disappears. Trade falls to the short side of the market, and society loses the {dwl:deadweight loss}."),
]

SUM_CARDS = [
    ("ceiling", "Price ceiling at $30", (110, 140, 930, 860), [
        "{cs:CS}  $1,800 → $2,000  (triangle → trapezoid)",
        "{ps:PS}  $900 → $400  (smaller triangle)",
        "{dwl:DWL}  $300  ·  {ctrl:shortage} of 30 units",
    ]),
    ("floor", "Price floor at $60", (990, 140, 1810, 860), [
        "{cs:CS}  $1,800 → $800  (smaller triangle)",
        "{ps:PS}  $900 → $1,600  (triangle → trapezoid)",
        "{dwl:DWL}  $300  ·  {ctrl:excess supply} of 60 units",
    ]),
]


def scene_summary(d, u):
    target = d._image
    for i, (kind, title, (x0, y0, x1, y1), lines) in enumerate(SUM_CARDS):
        a = prog(u, 0.2 + 0.5 * i, 1.0 + 0.5 * i)
        if a <= 0.01:
            continue
        ty0 = y0
        rise = round(44 * (1 - ease_out(a)))
        y0, y1 = y0 + rise, y1 + rise
        # draw the card (with its chart) on a copy, then blend it in, so the whole card fades as one
        layer = target.copy() if a < 0.999 else target
        ld = ImageDraw.Draw(layer, "RGBA")
        ld.rounded_rectangle([x0 * SS, y0 * SS, x1 * SS, y1 * SS], radius=18 * SS,
                             fill=rgba(CARD), outline=rgba(CARD_EDGE), width=2 * SS)
        draw_text(ld, ((x0 + 36) * SS, (y0 + 50) * SS), title, font=font(30, "Bold"), fill=rgba(CTRL_C), anchor="ls")
        view = View(x0 + 90, y0 + 105, x1 - 60, y0 + 505, scale=0.72)
        ch, _ = build_control_chart(view, CONTROL[kind], 99.0, 1.0, mini=True)
        draw_chart(ld, ch)
        if layer is not target:
            box = ((x0 - 4) * SS, (ty0 - 4) * SS, (x1 + 4) * SS, (y1 + 4) * SS)
            target.paste(Image.blend(target.crop(box), layer.crop(box), a), box[:2])
        for j, line in enumerate(lines):
            la = prog(u, 1.6 + 0.35 * j + 0.5 * i, 2.2 + 0.35 * j + 0.5 * i)
            draw_rich_line(d, x0 + 40, ty0 + 600 + j * 47 + 14 * (1 - ease_out(la)), line, 26, la)
    draw_header(d, "Summary", prog(u, 0.0, 0.6))
    return SUM_CAPTIONS, None


# --------------------------------------------------------------------------- frame assembly

def caption_alpha(u, a, b):
    return prog(u, a, a + 0.35) * (1 - prog(u, b - 0.35, b))


def render(t):
    im = background().copy()
    d = ImageDraw.Draw(im, "RGBA")
    fade = 1.0
    caps, foot = [], None
    if t < S_EQ:
        scene_title(d, t)
    elif t < S_CEIL:
        u = t - S_EQ
        caps, foot = scene_eq(d, u)
    elif t < S_FLOOR:
        u = t - S_CEIL
        caps, foot = scene_control(d, u, "ceiling")
    elif t < S_SUM:
        u = t - S_FLOOR
        caps, foot = scene_control(d, u, "floor")
        fade = 1 - prog(u, T_FLOOR - 0.9, T_FLOOR - 0.1)
    else:
        u = t - S_SUM
        caps, foot = scene_summary(d, u)
        fade = prog(u, 0.0, 0.5) * (1 - prog(u, T_SUM - 0.8, T_SUM))
    for a, b, text in caps:
        draw_caption(d, text, caption_alpha(u, a, b), rise=18 * (1 - ease_out((u - a) / 0.5)))
    if foot:
        draw_footnote(d, foot[2], caption_alpha(u, foot[0], foot[1]))
    im = im.reduce(SS)
    if fade < 0.999:
        im = Image.blend(background(small=True), im, max(0.0, fade))
    return im


def render_bytes(i):
    return render(i / FPS).tobytes()


# --------------------------------------------------------------------------- soundtrack

SR = 32000
BPM = 90


def sfx_events():
    """(time, kind) for every sound effect, derived from the same timeline constants as the picture."""
    ev = [(0.35, "chime_title"), (S_EQ + 0.0, "whoosh"), (S_EQ + 0.6, "draw"), (S_EQ + 1.6, "draw"),
          (S_EQ + 3.05, "pop"), (S_EQ + 5.0, "sweep"), (S_EQ + 5.7, "tick"), (S_EQ + 9.8, "sweep"),
          (S_EQ + 10.5, "tick"), (S_EQ + 14.5, "chime")]
    for s0 in (S_CEIL, S_FLOOR):
        ev += [(s0 + T_LINE[0] - 0.15, "whoosh"), (s0 + T_LINE[0] + 0.75, "thud"),
               (s0 + T_DOTS[0] + 0.05, "pop"), (s0 + T_DOTS[0] + 0.2, "pop"), (s0 + T_BRACKET[0] + 0.1, "stretch"),
               (s0 + T_TRADE[0] + 0.05, "tick"), (s0 + T_XFER[0], "sweep"), (s0 + T_XFER[1] + 0.05, "pop"),
               (s0 + T_DWL[0] - 0.05, "loss"), (s0 + T_DWL[1] - 0.2, "pop"),
               (s0 + T_SHAPES + 0.05, "chime"), (s0 + T_SHAPES + 1.05, "chime_low")]
    ev += [(S_CEIL + 31.3, "whoosh"), (S_FLOOR + T_FLOOR - 1.0, "whoosh"),
           (S_SUM + 0.15, "whoosh"), (S_SUM + 0.65, "whoosh"), (S_SUM + 1.6, "tick"), (S_SUM + 2.1, "tick"),
           (S_SUM + 12.8, "chime_title")]
    return ev


def synth_audio(path):
    """Write a mono WAV: a soft four-chord pad with a plucked arpeggio, plus sound effects on the beats."""
    import array
    import random
    import wave

    rnd = random.Random(7)
    tau = 2 * math.pi
    n = int((DURATION + 1) * SR)

    def hz(m):
        return 440.0 * 2 ** ((m - 69) / 12)

    # ---- music: one 4-chord loop (D, Bm, G, A), synthesized once and tiled
    beat = 60 / BPM
    chord_len = 8 * beat
    chords = [[50, 57, 62, 66, 69], [47, 54, 62, 66, 69], [43, 50, 59, 62, 66], [45, 52, 57, 61, 64]]
    loop_n = int(4 * chord_len * SR)
    loop = [0.0] * loop_n
    for ci, notes in enumerate(chords):
        s0 = int(ci * chord_len * SR)
        span = chord_len + 1.6
        for m in notes:
            amp = 0.05 if m < 55 else 0.035
            for det in (-0.07, 0.07):
                w = tau * hz(m + det) / SR
                for k in range(int(span * SR)):
                    t = k / SR
                    env = min(1.0, t / 1.4) * min(1.0, max(0.0, (span - t) / 1.6))
                    loop[(s0 + k) % loop_n] += amp * env * math.sin(w * k)
    lp = 0.0  # gentle low-pass to keep the pad warm
    for i in range(loop_n):
        lp += 0.18 * (loop[i] - lp)
        loop[i] = lp
    pattern = [2, 3, 4, 3, 2, 4, 3, 4]
    for ci, notes in enumerate(chords):
        for b in range(8):
            f = hz(notes[pattern[b]] + 12)
            s0 = int((ci * chord_len + b * beat) * SR)
            for k in range(int(0.7 * SR)):
                t = k / SR
                v = math.exp(-t * 6.5) * (math.sin(tau * f * t) + 0.25 * math.sin(2 * tau * f * t))
                loop[(s0 + k) % loop_n] += 0.045 * v
    music = [loop[i % loop_n] for i in range(n)]
    for i in range(n):
        t = i / SR
        music[i] *= min(1.0, t / 2.5) * min(1.0, max(0.0, (DURATION - t) / 3.0))

    # ---- sound effects
    fx = [0.0] * n

    def add(t0, gen, length):
        s0 = int(t0 * SR)
        for k in range(int(length * SR)):
            if 0 <= s0 + k < n:
                fx[s0 + k] += gen(k / SR)

    def chirp(f0, f1, rate, decay, amp, length, harm=0.0):
        ph = [0.0]

        def g(t):
            f = f1 + (f0 - f1) * math.exp(-t * rate)
            ph[0] += tau * f / SR
            return amp * math.exp(-t * decay) * (math.sin(ph[0]) + harm * math.sin(2 * ph[0]))
        return g, length

    def noise(dur, amp, bright, shape=lambda u: math.sin(math.pi * u) ** 2):
        y = [0.0, 0.0]

        def g(t):
            u = t / dur
            c = 0.03 + bright * math.sin(math.pi * u)
            y[0] += c * (rnd.uniform(-1, 1) - y[0])
            y[1] += c * (y[0] - y[1])
            return amp * shape(u) * y[1] * 4
        return g, dur

    def bell(notes, amp, decay):
        def g(t):
            return amp * sum(math.sin(tau * hz(m) * t) * math.exp(-t * decay * (1 + 0.4 * j))
                             for j, m in enumerate(notes)) * min(1.0, t * 200)
        return g, 2.2

    kinds = {
        "pop": lambda: chirp(1100, 520, 35, 26, 0.30, 0.2, 0.2),
        "tick": lambda: chirp(2600, 2200, 60, 70, 0.12, 0.06),
        "thud": lambda: chirp(150, 48, 12, 7, 0.32, 0.5, 0.15),
        "loss": lambda: chirp(220, 70, 3.5, 3.2, 0.24, 1.2, 0.3),
        "draw": lambda: noise(1.4, 0.10, 0.10),
        "whoosh": lambda: noise(0.75, 0.16, 0.22),
        "sweep": lambda: noise(1.3, 0.18, 0.16),
        "stretch": lambda: chirp(500, 1400, -1.0, 4, 0.0, 0.01),
        "chime": lambda: bell([81, 86, 88], 0.075, 2.4),
        "chime_low": lambda: bell([74, 78, 81], 0.075, 2.4),
        "chime_title": lambda: bell([74, 78, 81, 86], 0.07, 1.6),
    }
    for t0, kind in sfx_events():
        if kind == "stretch":  # rising zip for the shortage / excess-supply arrow
            ph = [0.0]

            def g(t, ph=ph):
                ph[0] += tau * (500 + 2200 * t) / SR
                return 0.10 * math.sin(math.pi * t / 0.35) * math.sin(ph[0])
            add(t0, g, 0.35)
            continue
        gen, length = kinds[kind]()
        add(t0, gen, length)

    out = array.array("h", (int(32767 * math.tanh(0.8 * music[i] + fx[i])) for i in range(n)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(out.tobytes())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output", nargs="?", default="price_controls_surplus.mp4")
    ap.add_argument("--stills", help="comma-separated times (s) to export as PNGs instead of a video")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--no-audio", action="store_true", help="render a silent video")
    args = ap.parse_args()
    check_model()

    if args.stills:
        for s in args.stills.split(","):
            t = float(s)
            path = f"still_{t:06.2f}.png"
            render(t).save(path)
            print("wrote", path)
        return

    n = int(round(DURATION * FPS))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
    if not args.no_audio:
        wav = os.path.splitext(args.output)[0] + ".soundtrack.wav"
        print("synthesizing soundtrack...", file=sys.stderr)
        synth_audio(wav)
        cmd += ["-i", wav, "-map", "0:v", "-map", "1:a",
                "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000", "-ac", "2",
                "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", args.output]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    with Pool(args.jobs) as pool:
        for i, frame in enumerate(pool.imap(render_bytes, range(n), chunksize=6)):
            enc.stdin.write(frame)
            if i % (FPS * 5) == 0:
                print(f"\rframe {i}/{n}", end="", file=sys.stderr, flush=True)
    enc.stdin.close()
    if enc.wait() != 0:
        sys.exit("ffmpeg failed")
    if not args.no_audio:
        os.remove(wav)
    print(f"\nwrote {args.output} ({n} frames, {DURATION:.1f}s)", file=sys.stderr)


if __name__ == "__main__":
    main()
