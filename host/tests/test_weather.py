"""Offline tests for wxd.weather (fixtures only, no network)."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wxd import weather as w
from wxd.model import AlertSeverity, Icon, WeatherModel

FIX = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("America/St_Johns")
NOW = datetime(2026, 11, 5, 7, 35, tzinfo=TZ)
CFG = w.WeatherConfig()


def load(name):
    return json.loads((FIX / name).read_text())


def test_eccc_fixture():
    m = w.parse_eccc(load("eccc_nl-24.json"), CFG, now=NOW)
    assert m.source == "eccc" and m.location_name == "St. John's"
    c = m.current
    assert c.temp_c == -3.4 and round(c.feels_like_c) == -8  # computed; ECCC says -8
    assert c.wind_kph == 12 and c.wind_gust_kph == 39 and c.wind_dir == "SW"
    assert c.humidity_pct == 82 and c.pressure_kpa == 102.7 and c.pressure_tendency == "rising"
    assert "Airport" in c.station
    assert c.observed_at.utcoffset().total_seconds() == -3.5 * 3600
    # obs 06:30 NST is after 06:50 sunrise? no -> night icon kept; either way a clear icon
    assert c.icon in (Icon.CLEAR_DAY, Icon.CLEAR_NIGHT)
    p = m.periods
    assert [x.short_name for x in p[:4]] == ["DAY", "NIGHT", "FRI", "FRI NT"]
    assert p[0].temp_is_high and p[0].temp_c == 6
    assert not p[1].temp_is_high and p[1].temp_c == 3 and p[1].pop_pct == 40
    assert all(len(x.short_name) <= 6 and x.short_name.isupper() for x in p)
    assert m.today_high_c == 6 and m.today_low_c == 3
    assert m.sunrise < m.sunset and m.alerts == []


def test_eccc_alerts_sorted_and_filtered():
    m = w.parse_eccc(load("eccc_nl-24_warnings_synthetic.json"), CFG, now=NOW)
    assert [(a.title, a.severity) for a in m.alerts] == [
        ("Wind warning", AlertSeverity.WARNING),
        ("Fog advisory", AlertSeverity.ADVISORY),
        ("Special weather statement", AlertSeverity.STATEMENT),
    ]  # "ended" dropped, most severe first
    assert m.alerts[0].expires_at > m.alerts[0].issued_at
    # windChill/iconCode removed in this fixture -> computed from temp/wind, no crash
    assert m.current.feels_like_c is not None
    assert m.current.icon is not Icon.UNKNOWN


def test_open_meteo_fixture():
    m = w.parse_open_meteo(load("open_meteo.json"), CFG, now=NOW)
    assert m.source == "open-meteo" and m.alerts == []
    c = m.current
    assert c.temp_c == -4.3 and c.feels_like_c == -8.6 and c.icon == Icon.CLEAR_DAY
    assert c.wind_kph == 9 and c.wind_dir == "SSW" and c.pressure_kpa == 102.6
    assert [x.short_name for x in m.periods[:4]] == ["DAY", "NIGHT", "FRI", "FRI NT"]
    assert m.periods[1].icon == Icon.CLEAR_NIGHT and not m.periods[1].temp_is_high
    assert m.today_high_c == 6 and m.today_low_c == -5


def test_open_meteo_after_sunset_skips_day_half():
    late = datetime(2026, 11, 5, 20, 0, tzinfo=TZ)
    m = w.parse_open_meteo(load("open_meteo.json"), CFG, now=late)
    assert m.periods[0].short_name == "NIGHT"


@pytest.mark.parametrize("parse", [w.parse_eccc, w.parse_open_meteo])
@pytest.mark.parametrize("data", [{}, {"properties": None, "current": None}, {"properties": {"currentConditions": {}}}])
def test_empty_responses_do_not_crash(parse, data):
    m = parse(data, CFG, now=NOW)
    assert isinstance(m, WeatherModel)
    assert m.current.temp_c is None and m.current.icon == Icon.UNKNOWN
    assert m.periods == [] and m.alerts == []


def test_short_names():
    assert w.short_name("Tonight") == "NIGHT"
    assert w.short_name("Friday night") == "FRI NT"
    assert w.short_name("Wednesday") == "WED"
    assert w.short_name("Some Long Unknown Name") == "SOME L"


def test_icon_maps():
    assert w.eccc_icon(31) == Icon.CLEAR_NIGHT and w.eccc_icon("16") == Icon.SNOW
    assert w.eccc_icon(None) == Icon.UNKNOWN and w.eccc_icon(999) == Icon.UNKNOWN
    assert all(w.eccc_icon(i) in Icon for i in range(49))
    assert w.wmo_icon(2, False) == Icon.PARTLY_CLOUDY_NIGHT
    assert w.wmo_icon(73, True, 80) == Icon.BLOWING_SNOW
    assert w.wmo_icon(95) == Icon.THUNDERSTORM and w.wmo_icon(None) == Icon.UNKNOWN


def test_fetch_falls_back(monkeypatch):
    om = w.parse_open_meteo(load("open_meteo.json"), CFG, now=NOW)

    def boom(cfg):
        raise OSError("down")

    monkeypatch.setattr(w, "fetch_eccc", boom)
    monkeypatch.setattr(w, "fetch_open_meteo", lambda cfg: om)
    assert w.fetch_weather(CFG).source == "open-meteo"
    monkeypatch.setattr(w, "fetch_open_meteo", boom)
    with pytest.raises(RuntimeError):
        w.fetch_weather(CFG)


def test_sample_model():
    m = w.sample_model()
    assert m.alerts[0].severity == AlertSeverity.WARNING
    assert m.current.icon == Icon.SNOW and len(m.periods) >= 3
    assert all(len(p.short_name) <= 6 for p in m.periods)


def test_no_email_in_ua():
    assert "@" not in CFG.user_agent


def test_feels_like_formulas():
    assert round(w.feels_like(-10, 30, None)) == -20  # ECCC wind chill table
    assert round(w.feels_like(6.9, 28, 5.0)) == 3
    assert w.feels_like(15, 30, 10) is None  # neither wind chill nor humidex applies
    assert round(w.feels_like(30, 10, 22)) == 39  # humidex
    assert w.feels_like(None, 10, None) is None


def test_eccc_stale_windchill_ignored():
    # 2026-11-06: ECCC reported windChill -6 at +6.9 degC / 28 km/h
    d = load("eccc_nl-24.json")
    cc = d["properties"]["currentConditions"]
    cc["temperature"]["value"] = {"en": 6.9, "fr": 6.9}
    cc["wind"]["speed"]["value"] = {"en": 28, "fr": 28}
    cc["windChill"] = {"value": {"en": -6, "fr": -6}}
    c = w.parse_eccc(d, CFG, now=NOW).current
    assert round(c.feels_like_c) == 3
