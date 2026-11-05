from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from wxd.model import Alert, AlertSeverity, Current, Icon, Period, WeatherModel
from wxd.protocol import PAYLOAD_BYTES, PLANE_BYTES
from wxd.render import RenderConfig, _scenarios, icon_mask, planes_to_image, render, to_planes

TZ = timezone(timedelta(hours=-3, minutes=-30))


@pytest.mark.parametrize("rot", [90, 270])
@pytest.mark.parametrize("inv", [False, True])
def test_roundtrip(rot, inv):
    cfg = RenderConfig(rotate=rot, red_invert=inv)
    for m, s, now in _scenarios().values():
        img = render(m, s, cfg, now)
        assert img.size == (296, 128) and img.mode == "P"
        payload = to_planes(img, cfg)
        assert len(payload) == PAYLOAD_BYTES == 9472
        back = planes_to_image(payload, cfg)
        assert back.tobytes() == img.tobytes()


def test_corner_mapping():
    for rot, landscape_px, native_row in ((90, (295, 0), 0), (270, (0, 127), 0)):
        cfg = RenderConfig(rotate=rot)
        img = Image.new("P", (296, 128), 0)
        img.putpalette([255, 255, 255, 0, 0, 0, 255, 0, 0])
        img.putpixel(landscape_px, 1)
        p = to_planes(img, cfg)
        assert p[0] == 0x7F  # native (0,0) black, rest of row white
        assert sum(bin(b).count("1") for b in p[:PLANE_BYTES]) == PLANE_BYTES * 8 - 1


def test_red_plane_polarity():
    img = Image.new("P", (296, 128), 0)
    img.putpalette([255, 255, 255, 0, 0, 0, 255, 0, 0])
    img.putpixel((295, 0), 2)
    p = to_planes(img, RenderConfig(rotate=90))
    assert p[PLANE_BYTES] == 0x80 and p[0] == 0xFF
    q = to_planes(img, RenderConfig(rotate=90, red_invert=True))
    assert q[PLANE_BYTES] == 0x7F


def test_deterministic():
    cfg = RenderConfig()
    for m, s, now in _scenarios().values():
        assert to_planes(render(m, s, cfg, now), cfg) == to_planes(render(m, s, cfg, now), cfg)


def test_now_does_not_change_output_when_fresh():
    m, s, now = _scenarios()["winter_warning"]
    a = to_planes(render(m, s, RenderConfig(), now))
    b = to_planes(render(m, s, RenderConfig(), now + timedelta(minutes=30)))
    assert a == b


@pytest.mark.parametrize("size", [24, 28, 56, 64])
def test_every_icon_renders(size):
    for ic in Icon:
        assert icon_mask(ic, size).getbbox() is not None, ic


def test_all_none():
    m = WeatherModel("", "x", datetime(2026, 1, 1, tzinfo=TZ), Current(None, None, "", Icon.UNKNOWN))
    img = render(m, None, RenderConfig(), None)
    assert len(to_planes(img)) == 9472
    m.periods = [Period("x", "", Icon.UNKNOWN, None, True, None)]
    m.alerts = [Alert("", AlertSeverity.STATEMENT)]
    render(m, None, RenderConfig(rotate=270), datetime(2026, 1, 1, tzinfo=TZ))


def test_pixels_only_palette():
    for m, s, now in _scenarios().values():
        assert set(render(m, s, RenderConfig(), now).tobytes()) <= {0, 1, 2}


def test_bad_rotate():
    with pytest.raises(ValueError):
        RenderConfig(rotate=0)
