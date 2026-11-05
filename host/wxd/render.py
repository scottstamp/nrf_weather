"""Image renderer: WeatherModel -> 296x128 "P" image -> panel-native 1-bpp planes.

Palette indices: 0 = white, 1 = black, 2 = red. Everything is drawn without
anti-aliasing so the result is a pure 1-bit-per-plane image. Output depends only on the
inputs (the observation time is shown, never "now"), so identical inputs give identical bytes.

Layout and rationale: docs/layout.md.  CLI: ``python -m wxd.render [--out DIR]``.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .model import (
    Alert,
    AlertSeverity,
    Current,
    Icon,
    Period,
    WeatherModel,
)
from .protocol import NATIVE_H, NATIVE_W, PAYLOAD_BYTES, PLANE_BYTES, ROW_BYTES, DeviceStatus

W, H = 296, 128
WHITE, BLACK, RED = 0, 1, 2
_PALETTE = [255, 255, 255, 0, 0, 0, 255, 0, 0]

ASSETS = Path(__file__).parent / "assets"
MINUS = "−"
STALE_AFTER = timedelta(hours=3)

# Layout grid (px)
COL_X = 206  # forecast column starts here; divider at COL_X - 3
FOOT_Y = 106  # footer strip top
FOOT_H = H - FOOT_Y
ICON_BIG = 56


@dataclass
class RenderConfig:
    rotate: int = 90  # 90 or 270 (connector side / mounting orientation)
    red_invert: bool = False  # True: RED plane bit 0 = red
    font_bold: str = str(ASSETS / "DejaVuSans-Bold.ttf")
    font_regular: str = str(ASSETS / "DejaVuSans.ttf")
    show_battery: bool = True
    show_forecast: bool = True
    max_periods: int = 3
    pop_notable_pct: int = 40
    pop_red_pct: int = 70
    gust_red_kph: int = 70
    battery_low_pct: int = 15
    stale_after: timedelta = field(default_factory=lambda: STALE_AFTER)

    def __post_init__(self) -> None:
        if self.rotate not in (90, 270):
            raise ValueError("rotate must be 90 or 270")


# --------------------------------------------------------------------------- fonts / text


@lru_cache(maxsize=256)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _fit(text: str, path: str, max_w: float, size: int, min_size: int) -> tuple[ImageFont.FreeTypeFont, str]:
    """Shrink font from `size` toward `min_size` until text fits; then ellipsize."""
    for s in range(size, min_size - 1, -1):
        f = _font(path, s)
        if f.getlength(text) <= max_w:
            return f, text
    f = _font(path, min_size)
    t = text
    while len(t) > 1 and f.getlength(t + "…") > max_w:
        t = t[:-1]
    return f, t.rstrip() + "…"


class _Ctx:
    def __init__(self, cfg: RenderConfig) -> None:
        self.cfg = cfg
        self.img = Image.new("P", (W, H), WHITE)
        self.img.putpalette(_PALETTE)
        self.d = ImageDraw.Draw(self.img)
        self.d.fontmode = "1"  # no anti-aliasing

    def text(self, x: float, base: float, s: str, font: ImageFont.FreeTypeFont, fill: int = BLACK, anchor: str = "l") -> float:
        """Draw text at baseline `base`; anchor l/r. Returns width."""
        w = font.getlength(s)
        x0 = x - w if anchor == "r" else x
        self.d.text((round(x0), round(base)), s, font=font, fill=fill, anchor="ls")
        return w

    def fit_text(self, x, base, s, bold, max_w, size, min_size, fill=BLACK, anchor="l") -> float:
        path = self.cfg.font_bold if bold else self.cfg.font_regular
        f, t = _fit(s, path, max_w, size, min_size)
        return self.text(x, base, t, f, fill, anchor)


# --------------------------------------------------------------------------- icons


class _Ink:
    """Draws into an L mask in a 0..100 unit square, supersampled 4x."""

    SS = 4

    def __init__(self, size: int) -> None:
        self.size = size
        self.S = size * self.SS
        self.k = self.S / 100.0
        self.minw = 1.25 * 100.0 / size  # min stroke in units (~1.25 px)
        self.m = Image.new("L", (self.S, self.S), 0)
        self.d = ImageDraw.Draw(self.m)

    def _p(self, v: float) -> float:
        return v * self.k

    def circle(self, cx, cy, r, v=255):
        k = self.k
        self.d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=v)

    def ring(self, cx, cy, r, w, v=255):
        w = max(w, self.minw)
        self.circle(cx, cy, r, v)
        self.circle(cx, cy, r - w, 0 if v else 255)

    def line(self, x1, y1, x2, y2, w, v=255):
        w = max(w, self.minw)
        k = self.k
        self.d.line([x1 * k, y1 * k, x2 * k, y2 * k], fill=v, width=max(1, round(w * k)))
        self.circle(x1, y1, w / 2, v)
        self.circle(x2, y2, w / 2, v)

    def poly(self, pts, v=255):
        self.d.polygon([(x * self.k, y * self.k) for x, y in pts], fill=v)

    def rect(self, x1, y1, x2, y2, v=255):
        k = self.k
        self.d.rectangle([x1 * k, y1 * k, x2 * k, y2 * k], fill=v)

    def arc(self, cx, cy, r, a0, a1, w, v=255):
        w = max(w, self.minw)
        k = self.k
        self.d.arc([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], a0, a1, fill=v, width=max(1, round(w * k)))

    # composite shapes
    def cloud(self, cx, cy, s=1.0, v=255, grow=0.0):
        g = grow
        for (dx, dy, r) in ((-19, 6, 12), (3, -5, 18), (23, 6, 12)):
            self.circle(cx + dx * s, cy + dy * s, r * s + g, v)
        self.rect(cx + (-19) * s, cy + (-6) * s - g, cx + 23 * s, cy + 18 * s + g, v)

    def sun(self, cx, cy, r, ray_in, ray_out, w):
        self.circle(cx, cy, r)
        for i in range(8):
            a = i * math.pi / 4
            c, s = math.cos(a), math.sin(a)
            self.line(cx + c * ray_in, cy + s * ray_in, cx + c * ray_out, cy + s * ray_out, w)

    def moon(self, cx, cy, r):
        self.circle(cx, cy, r)
        self.circle(cx + r * 0.62, cy - r * 0.5, r * 0.82, 0)

    def star(self, cx, cy, r, w=4.5):
        for i in range(3):
            a = i * math.pi / 3 + math.pi / 6
            c, s = math.cos(a) * r, math.sin(a) * r
            self.line(cx - c, cy - s, cx + c, cy + s, w)

    def rain(self, x, y, ln, w=5.5):
        self.line(x, y, x - ln * 0.4, y + ln, w)

    def hook_line(self, x1, x2, y, r):
        self.line(x1, y, x2, y, 5.5)
        self.arc(x2, y - r, r, -90, 100, 5.5)


def _draw_icon(ink: _Ink, icon: Icon) -> None:
    c = ink.cloud
    if icon == Icon.CLEAR_DAY:
        ink.sun(50, 50, 18, 28, 42, 6.5)
    elif icon == Icon.CLEAR_NIGHT:
        ink.moon(46, 54, 34)
    elif icon == Icon.PARTLY_CLOUDY_DAY:
        ink.sun(33, 33, 14, 22, 32, 5.5)
        c(58, 60, 1.0, 0, 5)
        c(58, 60, 1.0, 255)
    elif icon == Icon.PARTLY_CLOUDY_NIGHT:
        ink.moon(34, 34, 25)
        c(58, 60, 1.0, 0, 5)
        c(58, 60, 1.0, 255)
    elif icon == Icon.CLOUDY:
        c(40, 38, 0.8, 255)
        c(56, 58, 1.1, 0, 5)
        c(56, 58, 1.1, 255)
    elif icon == Icon.FOG:
        c(50, 34, 0.95, 255)
        for i, y in enumerate((62, 75, 88)):
            off = 6 if i == 1 else 0
            ink.line(16 + off, y, 84 + off - 6, y, 6)
    elif icon == Icon.DRIZZLE:
        c(50, 38, 1.1, 255)
        for x in (32, 50, 68):
            ink.rain(x + 3, 68, 11)
    elif icon == Icon.RAIN:
        c(50, 36, 1.1, 255)
        for x in (33, 52, 71):
            ink.rain(x + 4, 64, 24)
    elif icon in (Icon.SHOWERS_DAY, Icon.SHOWERS_NIGHT):
        if icon == Icon.SHOWERS_DAY:
            ink.sun(31, 30, 12, 19, 28, 5)
        else:
            ink.moon(31, 31, 22)
        c(56, 48, 1.0, 0, 5)
        c(56, 48, 1.0, 255)
        for x in (44, 62, 80):
            ink.rain(x + 3, 72, 20)
    elif icon == Icon.FREEZING_RAIN:
        c(50, 36, 1.1, 255)
        ink.rain(37, 64, 24)
        ink.rain(73, 64, 24)
        ink.poly([(54, 66), (62, 77), (54, 88), (46, 77)])
    elif icon == Icon.SLEET:
        c(50, 36, 1.1, 255)
        ink.rain(36, 64, 24)
        ink.star(66, 78, 9, 4.5)
        ink.rain(54, 70, 16)
    elif icon == Icon.SNOW:
        c(50, 34, 1.1, 255)
        ink.star(30, 74, 9)
        ink.star(70, 74, 9)
        ink.star(50, 88, 9)
    elif icon == Icon.FLURRIES:
        c(50, 34, 1.1, 255)
        ink.star(36, 78, 11, 5)
        ink.star(66, 80, 9, 4.5)
    elif icon == Icon.BLOWING_SNOW:
        ink.hook_line(10, 62, 30, 9)
        ink.hook_line(10, 76, 50, 10)
        ink.hook_line(10, 54, 70, 8)
        ink.circle(80, 78, 4.5)
        ink.circle(66, 90, 4.5)
        ink.circle(88, 62, 4)
    elif icon == Icon.THUNDERSTORM:
        c(50, 34, 1.1, 255)
        ink.poly([(56, 54), (34, 80), (48, 80), (40, 100), (68, 70), (54, 70), (64, 54)])
    elif icon == Icon.WIND:
        ink.hook_line(8, 66, 32, 10)
        ink.hook_line(8, 80, 52, 11)
        ink.hook_line(8, 56, 72, 9)
    else:  # UNKNOWN
        ink.ring(50, 50, 38, 7)
        f = ImageFont.truetype(_BOLD_FOR_ICONS, int(58 * ink.k))
        ink.d.text((50 * ink.k, 52 * ink.k), "?", font=f, fill=255, anchor="mm")


_BOLD_FOR_ICONS = str(ASSETS / "DejaVuSans-Bold.ttf")


@lru_cache(maxsize=128)
def icon_mask(icon: Icon, size: int) -> Image.Image:
    """1-bit-ish L mask (0 / 255) of `icon` at size x size px."""
    ink = _Ink(size)
    _draw_icon(ink, Icon(icon))
    small = ink.m.resize((size, size), Image.Resampling.BOX)
    return small.point(lambda v: 255 if v >= 112 else 0)


def _paste_icon(ctx: _Ctx, icon: Icon, x: int, y: int, size: int, fill: int = BLACK) -> None:
    ctx.img.paste(fill, (x, y), icon_mask(icon, size))


# --------------------------------------------------------------------------- pieces


def _fmt_t(v: float | int | None) -> str:
    if v is None:
        return "--"
    n = int(round(v))
    if n == 0:
        return "0"
    return f"{MINUS}{-n}" if n < 0 else str(n)


def _draw_big_temp(ctx: _Ctx, x: int, top: int, max_w: int, temp: float | None, digit_h: int = 60) -> None:
    """Huge temperature: digits from font, custom minus bar and degree ring. Digit tops at `top`."""
    bold = ctx.cfg.font_bold
    if temp is None:
        txt, neg = "--", False
    else:
        n = int(round(temp))
        neg = n < 0
        txt = str(abs(n))
    size = 100
    while size > 30:
        f = _font(bold, size)
        asc = -f.getbbox("0", anchor="ls")[1]
        if asc <= digit_h:
            ring_d = max(8, round(size * 0.24))
            minus_w = round(size * 0.30) if neg else 0
            gap = 4
            width = minus_w + (gap if neg else 0) + f.getlength(txt) + 3 + ring_d
            if width <= max_w:
                break
        size -= 1
    f = _font(bold, size)
    asc = -f.getbbox("0", anchor="ls")[1]
    base = top + asc
    cx = x
    if neg:
        th = max(5, round(size * 0.10))
        my = base - round(asc * 0.45)
        ctx.d.rectangle([cx, my - th // 2, cx + minus_w - 1, my - th // 2 + th - 1], fill=BLACK)
        cx += minus_w + gap
    if temp is None:
        # two dashes
        dw = round(size * 0.30)
        th = max(5, round(size * 0.10))
        my = base - round(asc * 0.45)
        for i in range(2):
            x0 = cx + i * (dw + 8)
            ctx.d.rectangle([x0, my - th // 2, x0 + dw - 1, my - th // 2 + th - 1], fill=BLACK)
        return
    w = ctx.text(cx, base, txt, f)
    rx = round(cx + w + 3)
    th = max(4, round(size * 0.075))
    ctx.d.ellipse([rx, top, rx + ring_d - 1, top + ring_d - 1], fill=BLACK)
    ctx.d.ellipse([rx + th, top + th, rx + ring_d - 1 - th, top + ring_d - 1 - th], fill=WHITE)


def _wind_text(cur: Current, cfg: RenderConfig) -> tuple[str, str, bool]:
    """Returns (main, gust_part, gust_is_red)."""
    if cur.wind_kph is None:
        return "Wind --", "", False
    if cur.wind_kph == 0 and not cur.wind_gust_kph:
        return "Calm", "", False
    main = f"{cur.wind_dir + ' ' if cur.wind_dir else ''}{cur.wind_kph}"
    g = cur.wind_gust_kph
    if g and g > cur.wind_kph:
        return main, f" G{g}", g >= cfg.gust_red_kph
    return main, "", False


def _draw_triangle(ctx: _Ctx, x: int, y: int, w: int, up: bool, fill: int = BLACK) -> None:
    h = w - 1
    if up:
        pts = [(x, y + h), (x + w - 1, y + h), (x + (w - 1) / 2, y)]
    else:
        pts = [(x, y), (x + w - 1, y), (x + (w - 1) / 2, y + h)]
    ctx.d.polygon(pts, fill=fill)


def _draw_forecast(ctx: _Ctx, periods: list[Period], x0: int, y0: int, y1: int) -> None:
    cfg = ctx.cfg
    n = min(len(periods), cfg.max_periods)
    if n == 0:
        return
    rh = (y1 - y0) // 3
    for i in range(n):
        p = periods[i]
        y = y0 + i * rh
        if i > 0:
            for xx in range(x0, W - 2, 2):
                ctx.d.point((xx, y - 1), fill=BLACK)
        _paste_icon(ctx, p.icon, x0 + 2, y + (rh - 28) // 2, 28)
        tx = x0 + 34
        mw = W - 2 - tx
        name = p.short_name.upper()
        pop = p.pop_pct if (p.pop_pct is not None and p.pop_pct >= cfg.pop_notable_pct) else None
        shown_pop = False
        if pop is not None:
            ptxt = f"{pop}%"
            pf = _font(cfg.font_bold, 13)
            budget = mw - pf.getlength(ptxt) - 2
            nf, nt = _fit(name, cfg.font_bold, budget, 14, 10)
            if nt == name:
                ctx.text(tx, y + 13, nt, nf)
                ctx.text(W - 2, y + 13, ptxt, pf, RED if pop >= cfg.pop_red_pct else BLACK, "r")
                shown_pop = True
        if not shown_pop:
            ctx.fit_text(tx, y + 13, name, True, mw, 14, 10)
        # line 2: temp + hi/lo hint, or POP
        t = _fmt_t(p.temp_c)
        f = _font(cfg.font_bold, 18)
        tw = ctx.text(tx, y + rh - 3, t, f)
        if p.temp_c is not None:
            _draw_triangle(ctx, round(tx + tw + 3), y + rh - 3 - 9, 8, p.temp_is_high)


def _draw_hilo(ctx: _Ctx, x: int, base: int, hi: int | None, lo: int | None) -> None:
    """Footer: today's forecast high/low as  ▲6°  ▼-3°  (same triangles as the forecast column)."""
    f = _font(ctx.cfg.font_bold, 18)
    tri = 10
    for i, (v, up) in enumerate(((hi, True), (lo, False))):
        if i:
            x += 14
        _draw_triangle(ctx, x, base - 12, tri, up)
        x += tri + 3
        x += round(ctx.text(x, base, f"{_fmt_t(v)}°" if v is not None else "--", f))


def _alert_banner(ctx: _Ctx, alerts: list[Alert], x0: int, y0: int, x1: int, y1: int) -> None:
    top = alerts[0]
    for a in alerts:
        if a.severity == AlertSeverity.WARNING:
            top = a
            break
    solid = top.severity == AlertSeverity.WARNING
    d = ctx.d
    d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=RED if solid else WHITE)
    if not solid:
        d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=RED, width=2)
    fg = WHITE if solid else RED
    # warning triangle
    ty = y0 + (y1 - y0 - 15) // 2
    tx = x0 + 5
    d.polygon([(tx, ty + 14), (tx + 16, ty + 14), (tx + 8, ty)], fill=fg)
    d.rectangle([tx + 7, ty + 5, tx + 8, ty + 9], fill=RED if solid else WHITE)
    d.rectangle([tx + 7, ty + 11, tx + 8, ty + 12], fill=RED if solid else WHITE)
    title = (top.title or "ALERT").upper()
    if len(alerts) > 1:
        title += f" +{len(alerts) - 1}"
    ctx.fit_text(tx + 22, y1 - 6, title, True, x1 - 4 - (tx + 22), 17, 12, fg)


def _draw_battery(ctx: _Ctx, x: int, y: int, pct: int | None, low: bool) -> None:
    col = RED if low else BLACK
    d = ctx.d
    w, h = 24, 12
    d.rectangle([x, y, x + w - 1, y + h - 1], outline=col, width=2)
    d.rectangle([x + w, y + 3, x + w + 1, y + h - 4], fill=col)
    if pct is not None:
        fillw = round((w - 6) * max(0, min(100, pct)) / 100)
        if fillw > 0:
            d.rectangle([x + 3, y + 3, x + 2 + fillw, y + h - 4], fill=col)
    elif not low:
        d.line([x + 8, y + h - 4, x + 12, y + 3], fill=col)  # unknown: slash


def _draw_hourglass(ctx: _Ctx, x: int, y: int) -> None:
    d = ctx.d
    d.polygon([(x, y), (x + 8, y), (x + 4, y + 5)], fill=BLACK)
    d.polygon([(x + 4, y + 6), (x, y + 12), (x + 8, y + 12)], fill=BLACK)


def _is_stale(model: WeatherModel, now: datetime | None, cfg: RenderConfig) -> bool:
    obs = model.current.observed_at
    if now is None:
        return False
    if obs is None:
        return True
    try:
        return now - obs > cfg.stale_after
    except TypeError:  # naive vs aware
        return False


# --------------------------------------------------------------------------- render


def render(
    model: WeatherModel,
    status: DeviceStatus | None,
    cfg: RenderConfig | None = None,
    now: datetime | None = None,
) -> Image.Image:
    """Render landscape 296x128 "P" image (0 white, 1 black, 2 red)."""
    cfg = cfg or RenderConfig()
    ctx = _Ctx(cfg)
    d = ctx.d
    cur = model.current
    left_w = COL_X - 6
    has_fc = cfg.show_forecast and bool(model.periods)

    # big icon (top right of the left region) and huge temperature
    icon_x = left_w - ICON_BIG + 2
    _paste_icon(ctx, cur.icon, icon_x, 2, ICON_BIG)
    _draw_big_temp(ctx, 4, 4, icon_x - 6, cur.temp_c)

    # wind row
    main, gust, gust_red = _wind_text(cur, cfg)
    wf = _font(cfg.font_bold, 22)
    avail = left_w - 4
    if wf.getlength(main + gust) > avail:
        wf = _font(cfg.font_bold, 18)
    w = ctx.text(4, 88, main, wf)
    if gust:
        ctx.text(4 + w, 88, gust, wf, RED if gust_red else BLACK)

    # condition + feels row
    feels = cur.feels_like_c
    show_feels = feels is not None and (cur.temp_c is None or round(feels) != round(cur.temp_c))
    fw = 0.0
    if show_feels:
        ff = _font(cfg.font_bold, 16)
        ft = f"Feels {_fmt_t(feels)}"
        fw = ctx.text(left_w, 104, ft, ff, RED if (feels <= -30 or feels >= 35) else BLACK, "r")
    ctx.fit_text(4, 104, cur.condition or "", False, left_w - 4 - (fw + 10 if show_feels else 0), 17, 13)

    # divider + forecast
    if has_fc:
        d.line([COL_X - 4, 4, COL_X - 4, FOOT_Y - 4], fill=BLACK)
        _draw_forecast(ctx, model.periods, COL_X, 2, FOOT_Y - 2)

    # footer: alert banner, else today's high/low; right: obs time, stale marker, battery
    right_x = COL_X - 2
    if model.alerts:
        _alert_banner(ctx, model.alerts, 0, FOOT_Y, right_x - 4, H)
    elif model.today_high_c is not None or model.today_low_c is not None:
        _draw_hilo(ctx, 4, H - 5, model.today_high_c, model.today_low_c)

    obs = cur.observed_at
    ts = obs.strftime("%H:%M") if obs else "--:--"
    stale = _is_stale(model, now, cfg)
    tf = _font(cfg.font_regular, 14)
    bx = W - 4
    low = False
    if cfg.show_battery and status is not None:
        pct = status.battery_pct
        low = status.low_battery or (pct is not None and pct <= cfg.battery_low_pct)
        bx = W - 4 - 26
        _draw_battery(ctx, bx, H - 6 - 12 + 1, pct, low)
        bx -= 6
    tw = ctx.text(bx, H - 6, ts, tf, BLACK, "r")
    if stale:
        _draw_hourglass(ctx, round(bx - tw - 12), H - 6 - 11)
    return ctx.img


# --------------------------------------------------------------------------- planes


def _transpose_for(cfg: RenderConfig, inverse: bool = False) -> Image.Transpose:
    fwd = Image.Transpose.ROTATE_90 if cfg.rotate == 90 else Image.Transpose.ROTATE_270
    if not inverse:
        return fwd
    return Image.Transpose.ROTATE_270 if cfg.rotate == 90 else Image.Transpose.ROTATE_90


def to_planes(img: Image.Image, cfg: RenderConfig | None = None) -> bytes:
    """Pack landscape 296x128 P image into 9472-byte payload: BW plane || RED plane.

    Rotation (PIL transpose; landscape (x, y), native (nx, ny), native 128 wide x 296 tall):
      rotate=90  (counter-clockwise 90): native (0,0) = landscape TOP-RIGHT corner
                 (x=295,y=0). nx = y, ny = 295 - x. Landscape top-left -> native bottom-left.
      rotate=270 (clockwise 90): native (0,0) = landscape BOTTOM-LEFT corner
                 (x=0,y=127). nx = 127 - y, ny = x. Landscape top-left -> native top-right.
    Row ny holds 16 bytes; MSB of byte 0 is native x=0.
    BW plane: 1 = white, 0 = black. RED plane: 1 = red (0 = red if cfg.red_invert).
    Red pixels get BW=1 (white) underneath.
    """
    cfg = cfg or RenderConfig()
    if img.size != (W, H):
        raise ValueError(f"expected {W}x{H}, got {img.size}")
    p = img if img.mode == "P" else img.convert("P")
    nat = p.transpose(_transpose_for(cfg))
    assert nat.size == (NATIVE_W, NATIVE_H)
    black = nat.point(lambda v: 255 if v == BLACK else 0, "L")
    red = nat.point(lambda v: 255 if v == RED else 0, "L")
    bw = ImageChops.invert(black)  # white (255) where not black; red -> white
    red_bits = ImageChops.invert(red) if cfg.red_invert else red
    nodither = Image.Dither.NONE
    bw_b = bw.convert("1", dither=nodither).tobytes()
    red_b = red_bits.convert("1", dither=nodither).tobytes()
    out = bw_b + red_b
    assert len(out) == PAYLOAD_BYTES and len(bw_b) == PLANE_BYTES and ROW_BYTES == 16
    return out


def planes_to_image(payload: bytes, cfg: RenderConfig | None = None) -> Image.Image:
    """Inverse of to_planes: payload -> landscape 296x128 "P" image (0 white, 1 black, 2 red)."""
    cfg = cfg or RenderConfig()
    if len(payload) != PAYLOAD_BYTES:
        raise ValueError(f"payload must be {PAYLOAD_BYTES} bytes, got {len(payload)}")
    bw = Image.frombytes("1", (NATIVE_W, NATIVE_H), payload[:PLANE_BYTES]).convert("L")
    rd = Image.frombytes("1", (NATIVE_W, NATIVE_H), payload[PLANE_BYTES:]).convert("L")
    if cfg.red_invert:
        rd = ImageChops.invert(rd)
    nat = Image.new("P", (NATIVE_W, NATIVE_H), WHITE)
    nat.putpalette(_PALETTE)
    nat.paste(BLACK, mask=ImageChops.invert(bw))
    nat.paste(RED, mask=rd)
    return nat.transpose(_transpose_for(cfg, inverse=True))


def preview(img: Image.Image, scale: int = 3) -> Image.Image:
    """RGB preview, nearest-neighbor scaled."""
    rgb = img.convert("RGB")
    return rgb.resize((img.width * scale, img.height * scale), Image.Resampling.NEAREST)


# --------------------------------------------------------------------------- sample scenarios / CLI

_NST = timezone(timedelta(hours=-3, minutes=-30))


def _scenarios() -> dict[str, tuple[WeatherModel, DeviceStatus | None, datetime]]:
    def st(pct: int, flags: int = 0) -> DeviceStatus:
        return DeviceStatus(1, (0, 1, 0), 3700, pct, flags, 0, 1000, 3600, 5)

    now = datetime(2026, 1, 15, 14, 20, tzinfo=_NST)
    obs = datetime(2026, 1, 15, 14, 0, tzinfo=_NST)
    winter = WeatherModel(
        "St. John's", "eccc", now,
        Current(obs, -12.4, "Light Snow", Icon.SNOW, feels_like_c=-22, wind_kph=40,
                wind_gust_kph=60, wind_dir="NW", humidity_pct=85, station="St. John's A"),
        [
            Period("Tonight", "NIGHT", Icon.CLOUDY, -15, False, 20),
            Period("Friday", "FRI", Icon.SNOW, -8, True, 70, "Snow. Amount 10 cm."),
            Period("Friday night", "FRI NT", Icon.FLURRIES, -14, False, 40),
        ],
        [Alert("Wind warning", AlertSeverity.WARNING)],
        -8, -15,
    )
    summer = WeatherModel(
        "St. John's", "eccc", datetime(2026, 7, 20, 13, 10, tzinfo=_NST),
        Current(datetime(2026, 7, 20, 13, 0, tzinfo=_NST), 24.2, "Sunny", Icon.CLEAR_DAY,
                feels_like_c=27, wind_kph=15, wind_dir="SW"),
        [
            Period("Tonight", "NIGHT", Icon.CLEAR_NIGHT, 14, False, 0),
            Period("Wednesday", "WED", Icon.PARTLY_CLOUDY_DAY, 22, True, 10),
            Period("Wednesday night", "WED NT", Icon.SHOWERS_NIGHT, 13, False, 60),
        ],
    )
    longtxt = WeatherModel(
        "St. John's", "eccc", now,
        Current(obs, -3.6, "Freezing Drizzle Mixed With Ice Pellets", Icon.FREEZING_RAIN,
                feels_like_c=-11, wind_kph=100, wind_gust_kph=135, wind_dir="NNW"),
        [
            Period("Tonight", "TONIGHT", Icon.THUNDERSTORM, -5, False, 100),
            Period("Friday", "FRIDAY", Icon.BLOWING_SNOW, 0, True, 90),
            Period("Friday night", "FRI NIGHT", Icon.FOG, -22, False, None),
        ],
        [Alert("Freezing rain warning in effect for St. John's and vicinity", AlertSeverity.WARNING),
         Alert("Wind warning", AlertSeverity.WARNING)],
    )
    missing = WeatherModel(
        "St. John's", "open-meteo", now,
        Current(None, None, "", Icon.UNKNOWN),
        [Period("Tonight", "NIGHT", Icon.UNKNOWN, None, False)],
    )
    lowbatt = WeatherModel(
        "St. John's", "eccc", now,
        Current(obs, 2.0, "Mainly Cloudy", Icon.PARTLY_CLOUDY_NIGHT, feels_like_c=-3,
                wind_kph=25, wind_gust_kph=45, wind_dir="E"),
        [
            Period("Tonight", "NIGHT", Icon.RAIN, 1, False, 80),
            Period("Friday", "FRI", Icon.SHOWERS_DAY, 4, True, 50),
            Period("Friday night", "FRI NT", Icon.DRIZZLE, -1, False, 30),
        ],
        [Alert("Rainfall watch", AlertSeverity.WATCH)],
    )
    stale_now = obs + timedelta(hours=5)
    return {
        "winter_warning": (winter, st(80), now),
        "summer_clear": (summer, st(95), summer.fetched_at),
        "long_text": (longtxt, st(60), now),
        "missing_data": (missing, None, now),
        "low_battery_stale": (lowbatt, st(8, 1 << 1), stale_now),
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Write preview PNGs of sample scenarios.")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "out"))
    ap.add_argument("--scale", type=int, default=3)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = RenderConfig()
    for name, (m, s, now) in _scenarios().items():
        img = render(m, s, cfg, now)
        preview(img, args.scale).save(out / f"{name}.png")
        print(out / f"{name}.png")
    # icon sheet
    icons = list(Icon)
    sheet = Image.new("P", (len(icons) * 64, 64 + 30), WHITE)
    sheet.putpalette(_PALETTE)
    for i, ic in enumerate(icons):
        sheet.paste(BLACK, (i * 64 + 4, 2), icon_mask(ic, 56))
        sheet.paste(BLACK, (i * 64 + 18, 62), icon_mask(ic, 28))
    preview(sheet, 2).save(out / "icons.png")


if __name__ == "__main__":
    main()
