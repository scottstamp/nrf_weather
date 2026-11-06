import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wxd.protocol import DeviceStatus
from wxd.render import RenderConfig, to_planes
from wxd.render_tide import render_tide, upcoming
from wxd.tides import TideConfig, TideError, parse_iwls

FIX = Path(__file__).parent / "fixtures" / "iwls_00905_wlp-hilo.json"
CFG = TideConfig()
UTC = timezone.utc


def _model():
    return parse_iwls(json.loads(FIX.read_text()), CFG, datetime(2026, 11, 6, 12, 0, tzinfo=UTC))


def _ev(t, v):
    return {"eventDate": t, "value": v}


def test_fixture_alternates_and_tz():
    m = _model()
    assert len(m.events) == 20
    assert all(a.is_high != b.is_high for a, b in zip(m.events, m.events[1:]))
    e = next(e for e in m.events if e.time.astimezone(UTC) == datetime(2026, 11, 6, 2, 28, tzinfo=UTC))
    assert e.height_m == pytest.approx(0.551)
    assert not e.is_high
    assert e.time.utcoffset() == timedelta(hours=-3, minutes=-30)  # NST in November
    assert (e.time.hour, e.time.minute) == (22, 58)


def test_classification_endpoints():
    data = [_ev("2026-11-06T01:00:00Z", 1.2), _ev("2026-11-06T07:00:00Z", 0.4), _ev("2026-11-06T13:00:00Z", 1.0)]
    m = parse_iwls(data, CFG, datetime.now(UTC))
    assert [e.is_high for e in m.events] == [True, False, True]


def test_bad_entries_skipped_and_sorted():
    data = [
        _ev("2026-11-06T13:00:00Z", 1.0),
        {"value": 1.0},
        _ev("garbage", 1.0),
        _ev("2026-11-06T07:00:00Z", None),
        _ev("2026-11-06T07:00:00Z", 0.4),
        "junk",
        _ev("2026-11-06T01:00:00Z", 1.2),
    ]
    m = parse_iwls(data, CFG, datetime.now(UTC))
    assert [e.height_m for e in m.events] == [1.2, 0.4, 1.0]


def test_empty_raises():
    with pytest.raises(TideError):
        parse_iwls([], CFG, datetime.now(UTC))
    with pytest.raises(TideError):
        parse_iwls({"error": 1}, CFG, datetime.now(UTC))


def test_upcoming_excludes_past():
    m = _model()
    now = datetime(2026, 11, 6, 15, 0, tzinfo=CFG.zone)
    ev = upcoming(m, now)
    assert ev and all(e.time >= now for e in ev)
    assert len(ev) <= 5


def test_render_size_mode_planes():
    m = _model()
    now = datetime(2026, 11, 6, 15, 0, tzinfo=CFG.zone)
    st = DeviceStatus(1, (0, 1, 0), 3700, 70, 0, 0, 0, 0, 0)
    img = render_tide(m, st, RenderConfig(), now)
    assert img.size == (296, 128) and img.mode == "P"
    assert len(to_planes(img, RenderConfig())) == 9472
    render_tide(m, None, RenderConfig(), datetime(2030, 1, 1, tzinfo=UTC))  # no events: no crash


def test_render_deterministic_between_events():
    m = _model()
    cfg = RenderConfig()
    st = DeviceStatus(1, (0, 1, 0), 3700, 70, 0, 0, 0, 0, 0)
    t0 = datetime(2026, 11, 6, 15, 0, tzinfo=CFG.zone)
    a = to_planes(render_tide(m, st, cfg, t0), cfg)
    b = to_planes(render_tide(m, st, cfg, t0 + timedelta(minutes=10)), cfg)
    assert a == b
    # an event passing changes the image
    nxt = upcoming(m, t0)[0].time
    c = to_planes(render_tide(m, st, cfg, nxt + timedelta(minutes=1)), cfg)
    assert c != a
