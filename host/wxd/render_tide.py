"""Tide view: 296x128 landscape B/W/R image of the next few high/low tides.

Deterministic by design: nothing depends on wall-clock time except which events
are still in the future, the first-row day label, and the stale marker, so the
image (and its CRC) only changes when an event passes or the data changes.
Previews: `python -m wxd.render_tide` (writes docs/img/tide-preview.png).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from .protocol import DeviceStatus
from .render import (
    BLACK,
    FOOT_Y,
    RED,
    W,
    H,
    RenderConfig,
    _Ctx,
    _draw_battery,
    _draw_hourglass,
    _draw_triangle,
    _font,
    preview,
)
from .tides import TideConfig, TideEvent, TideModel, parse_iwls

ROW_H = 21
ROWS_Y = 2  # top of first row
MAX_ROWS = 5
_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "iwls_00905_wlp-hilo.json"


def fmt_time(t: datetime) -> str:
    return f"{t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"


def upcoming(model: TideModel, now: datetime, n: int = MAX_ROWS) -> list[TideEvent]:
    return [e for e in sorted(model.events, key=lambda e: e.time) if e.time >= now][:n]


def render_tide(
    model: TideModel,
    status: DeviceStatus | None,
    cfg: RenderConfig | None = None,
    now: datetime | None = None,
    tz: str = "America/St_Johns",
) -> Image.Image:
    cfg = cfg or RenderConfig()
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    now = now or datetime.now(timezone.utc)
    ctx = _Ctx(cfg)
    d = ctx.d
    bold = cfg.font_bold
    f_day = _font(bold, 17)
    f_word = _font(bold, 15)
    f_time = _font(bold, 19)
    f_ht = _font(bold, 19)

    events = upcoming(model, now)
    if not events:
        f = _font(bold, 22)
        ctx.text((W - f.getlength("NO TIDE DATA")) / 2, 62, "NO TIDE DATA", f)
    prev_day = None
    for i, e in enumerate(events):
        lt = e.time.astimezone(zone)
        base = ROWS_Y + i * ROW_H + 17
        day = lt.date()
        if day != prev_day:
            ctx.text(4, base, _DAYS[lt.weekday()], f_day)
            if i > 0:
                d.line([4, ROWS_Y + i * ROW_H - 1, W - 5, ROWS_Y + i * ROW_H - 1], fill=BLACK)
            prev_day = day
        col = RED if e.is_high else BLACK
        _draw_triangle(ctx, 48, base - 13, 14, e.is_high, col)
        ctx.text(66, base, "HIGH" if e.is_high else "LOW", f_word)
        ctx.text(224, base, fmt_time(lt), f_time, BLACK, "r")
        ctx.text(W - 5, base, f"{e.height_m:.1f}m", f_ht, BLACK, "r")

    # footer: title/station left, stale marker + battery right
    d.line([4, FOOT_Y, W - 5, FOOT_Y], fill=BLACK)
    bx = W - 4
    if cfg.show_battery and status is not None:
        pct = status.battery_pct
        low = status.low_battery or (pct is not None and pct <= cfg.battery_low_pct)
        bx = W - 4 - 26
        _draw_battery(ctx, bx, H - 6 - 12 + 1, pct, low)
        bx -= 6
    stale = now - model.fetched_at > cfg.stale_after
    if stale:
        bx -= 8
        _draw_hourglass(ctx, round(bx), H - 6 - 11)
        bx -= 6
    f_t = _font(bold, 17)
    title = f"TIDES {model.station_name}"
    while f_t.getlength(title) > bx - 8 - 4 and len(title) > 6:
        title = title[:-1]
    ctx.text(4, H - 6, title, f_t)
    return ctx.img


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Write tide preview PNG from the test fixture.")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "docs" / "img" / "tide-preview.png"))
    ap.add_argument("--scale", type=int, default=3)
    args = ap.parse_args(argv)
    cfg = TideConfig()
    data = json.loads(FIXTURE.read_text())
    now = datetime(2026, 11, 6, 15, 0, tzinfo=cfg.zone)
    model = parse_iwls(data, cfg, now)
    from .protocol import DeviceStatus as DS

    st = DS(1, (0, 1, 0), 3700, 70, 0, 0, 0, 0, 0)
    img = render_tide(model, st, RenderConfig(), now, cfg.tz)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    preview(img, args.scale).save(out)
    print(out)


if __name__ == "__main__":
    main()
