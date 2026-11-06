"""Weather fetching: ECCC (primary, GeoMet OGC API citypageweather) with Open-Meteo fallback.

Stdlib only. Parsing is defensive: missing/odd fields become None; a partially
empty response never raises (only transport / total-garbage failures do).
See docs/weather-sources.md for endpoints, field mapping and cadence.
"""

from __future__ import annotations

import json
import math
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .model import (
    Alert,
    AlertSeverity,
    Current,
    Icon,
    Period,
    WeatherModel,
)

log = logging.getLogger("wxd.weather")

USER_AGENT = "wxd/0.1 (e-paper weather display)"


@dataclass
class WeatherConfig:
    # A1K 1E1 is in Torbay, NL (A1K centroid ~47.646,-52.724; Torbay ~47.65,-52.73).
    lat: float = 47.65
    lon: float = -52.73
    location_name: str = "St. John's"
    eccc_feature_id: str = "nl-24"  # citypage "St. John's" (obs station CYYT)
    eccc_url: str = "https://api.weather.gc.ca/collections/citypageweather-realtime/items/{id}"
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_models: str = "gem_seamless"
    timezone: str = "America/St_Johns"
    timeout_s: float = 15.0
    retries: int = 1  # extra attempts per source
    user_agent: str = USER_AGENT

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


# --------------------------------------------------------------------------- helpers


def _http_get_json(url: str, cfg: WeatherConfig) -> Any:
    last: Exception | None = None
    for _ in range(cfg.retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": cfg.user_agent, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=cfg.timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - retried then re-raised
            last = e
    assert last is not None
    raise last


def _loc(x: Any) -> Any:
    """ECCC wraps values as {"en": v, "fr": v}; unwrap to the English value."""
    if isinstance(x, dict) and ("en" in x or "fr" in x):
        return x.get("en", x.get("fr"))
    return x


def _get(d: Any, *path: str) -> Any:
    """Safe nested get that unwraps the {"en","fr"} wrapper at the end."""
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return _loc(d)


def feels_like(temp_c: float | None, wind_kph: float | None, dewpoint_c: float | None) -> float | None:
    """ECCC-style feels-like: wind chill at <= 10 degC, humidex at >= 20 degC, else None.

    Computed locally because the citypage ``windChill`` field can be stale
    (seen 2026-11-06: windChill -6 reported at +6.9 degC / 28 km/h).
    """
    if temp_c is None:
        return None
    if temp_c <= 10 and wind_kph is not None:
        v = float(wind_kph)
        if v >= 5:
            wc = 13.12 + 0.6215 * temp_c - 11.37 * v**0.16 + 0.3965 * temp_c * v**0.16
        else:
            wc = temp_c + (-1.59 + 0.1345 * temp_c) / 5 * v
        return round(wc, 1)
    if temp_c >= 20 and dewpoint_c is not None:
        e = 6.11 * math.exp(5417.7530 * (1 / 273.16 - 1 / (273.15 + dewpoint_c)))
        hx = temp_c + 0.5555 * (e - 10)
        return round(hx, 1) if hx >= 25 else None
    return None


def _num(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x: Any) -> int | None:
    n = _num(x)
    return None if n is None else int(round(n))


def _parse_dt(s: Any, tz: ZoneInfo) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg: float | None) -> str | None:
    if deg is None:
        return None
    return _COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


# --------------------------------------------------------------------------- icons

# ECCC icon codes: 00-29 day set, 30-48 night set / special.
_ECCC_ICONS: dict[int, Icon] = {
    0: Icon.CLEAR_DAY, 1: Icon.CLEAR_DAY, 2: Icon.PARTLY_CLOUDY_DAY,
    3: Icon.PARTLY_CLOUDY_DAY, 4: Icon.PARTLY_CLOUDY_DAY, 5: Icon.PARTLY_CLOUDY_DAY,
    6: Icon.SHOWERS_DAY, 7: Icon.SLEET, 8: Icon.FLURRIES, 9: Icon.THUNDERSTORM,
    10: Icon.CLOUDY, 11: Icon.RAIN, 12: Icon.RAIN, 13: Icon.RAIN,
    14: Icon.FREEZING_RAIN, 15: Icon.SLEET, 16: Icon.SNOW, 17: Icon.SNOW,
    18: Icon.SNOW, 19: Icon.THUNDERSTORM, 20: Icon.CLOUDY,
    21: Icon.PARTLY_CLOUDY_DAY, 22: Icon.PARTLY_CLOUDY_DAY, 23: Icon.FOG,
    24: Icon.FOG, 25: Icon.BLOWING_SNOW, 26: Icon.FLURRIES, 27: Icon.SLEET,
    28: Icon.DRIZZLE, 29: Icon.UNKNOWN,
    30: Icon.CLEAR_NIGHT, 31: Icon.CLEAR_NIGHT, 32: Icon.PARTLY_CLOUDY_NIGHT,
    33: Icon.PARTLY_CLOUDY_NIGHT, 34: Icon.PARTLY_CLOUDY_NIGHT,
    35: Icon.PARTLY_CLOUDY_NIGHT, 36: Icon.SHOWERS_NIGHT, 37: Icon.SLEET,
    38: Icon.FLURRIES, 39: Icon.THUNDERSTORM, 40: Icon.BLOWING_SNOW,
    41: Icon.WIND, 42: Icon.WIND, 43: Icon.WIND, 44: Icon.FOG, 45: Icon.WIND,
    46: Icon.THUNDERSTORM, 47: Icon.THUNDERSTORM, 48: Icon.THUNDERSTORM,
}


_TO_NIGHT = {Icon.CLEAR_DAY: Icon.CLEAR_NIGHT, Icon.PARTLY_CLOUDY_DAY: Icon.PARTLY_CLOUDY_NIGHT,
             Icon.SHOWERS_DAY: Icon.SHOWERS_NIGHT}
_TO_DAY = {v: k for k, v in _TO_NIGHT.items()}


def set_daylight(icon: Icon, is_day: bool) -> Icon:
    """Swap day/night icon variants (ECCC obs icon can lag sunrise/sunset)."""
    return _TO_DAY.get(icon, icon) if is_day else _TO_NIGHT.get(icon, icon)


def eccc_icon(code: Any) -> Icon:
    c = _int(code)
    return _ECCC_ICONS.get(c, Icon.UNKNOWN) if c is not None else Icon.UNKNOWN


def wmo_icon(code: Any, is_day: bool = True, gust_kph: float | None = None) -> Icon:
    c = _int(code)
    if c is None:
        return Icon.UNKNOWN
    if c in (0, 1):
        return Icon.CLEAR_DAY if is_day else Icon.CLEAR_NIGHT
    if c == 2:
        return Icon.PARTLY_CLOUDY_DAY if is_day else Icon.PARTLY_CLOUDY_NIGHT
    if c == 3:
        return Icon.CLOUDY
    if c in (45, 48):
        return Icon.FOG
    if c in (51, 53, 55):
        return Icon.DRIZZLE
    if c in (56, 57, 66, 67):
        return Icon.FREEZING_RAIN
    if c in (61, 63, 65):
        return Icon.RAIN
    if c in (71, 77, 85):
        return Icon.FLURRIES
    if c in (73, 75, 86):
        if gust_kph is not None and gust_kph >= 60:
            return Icon.BLOWING_SNOW
        return Icon.SNOW
    if c in (80, 81, 82):
        return Icon.SHOWERS_DAY if is_day else Icon.SHOWERS_NIGHT
    if c in (95, 96, 99):
        return Icon.THUNDERSTORM
    return Icon.UNKNOWN


_WMO_TEXT = {
    0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Cloudy", 45: "Fog", 48: "Freezing Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle", 56: "Freezing Drizzle",
    57: "Freezing Drizzle", 61: "Light Rain", 63: "Rain", 65: "Heavy Rain",
    66: "Freezing Rain", 67: "Freezing Rain", 71: "Light Snow", 73: "Snow", 75: "Heavy Snow",
    77: "Snow Grains", 80: "Showers", 81: "Showers", 82: "Heavy Showers",
    85: "Snow Showers", 86: "Heavy Snow Showers", 95: "Thunderstorm", 96: "Thunderstorm",
    99: "Thunderstorm",
}

# --------------------------------------------------------------------------- short names

_DAYS = {"monday": "MON", "tuesday": "TUE", "wednesday": "WED", "thursday": "THU",
         "friday": "FRI", "saturday": "SAT", "sunday": "SUN"}
_SPECIAL = {"today": "DAY", "tonight": "NIGHT", "this afternoon": "AFTN",
            "this evening": "EVE", "overnight": "NIGHT", "tomorrow": "TMRW",
            "tomorrow night": "TMR NT"}


def short_name(name: str) -> str:
    """'Friday night' -> 'FRI NT', 'Tonight' -> 'NIGHT'; always <= 6 chars, uppercase."""
    n = (name or "").strip().lower()
    if n in _SPECIAL:
        return _SPECIAL[n]
    night = n.endswith(" night")
    base = n[: -len(" night")] if night else n
    if base in _DAYS:
        return _DAYS[base] + (" NT" if night else "")
    return (name or "?").strip().upper()[:6]


# --------------------------------------------------------------------------- ECCC

_ALERT_RANK = {AlertSeverity.WARNING: 0, AlertSeverity.WATCH: 1,
               AlertSeverity.ADVISORY: 2, AlertSeverity.STATEMENT: 3}
_ALERT_TYPES = {"warning": AlertSeverity.WARNING, "watch": AlertSeverity.WATCH,
                "advisory": AlertSeverity.ADVISORY, "statement": AlertSeverity.STATEMENT}


def _alert_title(desc: str, type_word: str) -> str:
    """'YELLOW WARNING - WIND' -> 'Wind warning'; 'SPECIAL WEATHER STATEMENT' -> as is."""
    d = (desc or "").strip()
    if " - " in d:
        hazard = d.split(" - ", 1)[1].strip().lower()
        return f"{hazard.capitalize()} {type_word}"
    return d.capitalize() if d else type_word.capitalize()


def parse_eccc_alerts(warnings: Any, tz: ZoneInfo) -> list[Alert]:
    out: list[Alert] = []
    if not isinstance(warnings, list):
        return out
    for w in warnings:
        if not isinstance(w, dict):
            continue
        t = str(_get(w, "type") or "").lower()
        sev = _ALERT_TYPES.get(t)  # "ended" and unknown types are dropped
        if sev is None:
            continue
        a = Alert(
            title=_alert_title(str(_get(w, "description") or ""), t),
            severity=sev,
            issued_at=_parse_dt(_get(w, "eventIssue"), tz),
            expires_at=_parse_dt(_get(w, "expiryTime"), tz),
        )
        if not any(x.title == a.title for x in out):
            out.append(a)
    out.sort(key=lambda a: (_ALERT_RANK[a.severity],
                            a.issued_at.timestamp() if a.issued_at else 0))
    return out


_POP_RE = re.compile(r"(\d+) percent chance", re.I)


def _eccc_period(f: dict, tz: ZoneInfo) -> Period | None:
    name = _get(f, "period", "textForecastName")
    if not isinstance(name, str) or not name:
        return None
    abbr = f.get("abbreviatedForecast") or {}
    icon = eccc_icon(_get(abbr, "icon", "value"))
    text = _get(f, "textSummary") or ""
    temp_c: int | None = None
    is_high = True
    tl = (f.get("temperatures") or {}).get("temperature")
    if isinstance(tl, list) and tl and isinstance(tl[0], dict):
        temp_c = _int(_get(tl[0], "value"))
        is_high = str(_get(tl[0], "class") or "high").lower() != "low"
    pop = _int(_get(abbr, "pop", "value"))
    if pop is None:
        pops = [int(m) for m in _POP_RE.findall(str(text))]
        pop = max(pops) if pops else None
    amount = None
    acc = (f.get("precipitation") or {}).get("accumulation") if isinstance(
        f.get("precipitation"), dict) else None
    if isinstance(acc, dict):
        v = _num(_get(acc, "amount", "value"))
        u = _get(acc, "amount", "units")
        if v is not None:
            amount = f"{v:g} {u or ''}".strip()
    cond = _get(abbr, "textSummary") or ""
    summary = str(cond)
    if amount:
        summary = f"{summary}. Amount {amount}." if summary else f"Amount {amount}."
    return Period(name=name, short_name=short_name(name), icon=icon, temp_c=temp_c,
                  temp_is_high=is_high, pop_pct=pop, summary=summary, precip_amount=amount)


def parse_eccc(data: dict, cfg: WeatherConfig, now: datetime | None = None) -> WeatherModel:
    tz = cfg.tz
    now = now or datetime.now(tz)
    props = (data or {}).get("properties") or {}
    cc = props.get("currentConditions") or {}
    forecasts = (props.get("forecastGroup") or {}).get("forecasts") or []
    periods = [p for p in (_eccc_period(f, tz) for f in forecasts if isinstance(f, dict)) if p]

    # icon: current obs often has none (no icon when condition not reported) -> fall back
    icon = eccc_icon(_get(cc, "iconCode", "value"))
    if icon is Icon.UNKNOWN:
        hf = (props.get("hourlyForecastGroup") or {}).get("hourlyForecasts") or []
        if hf and isinstance(hf[0], dict):
            icon = eccc_icon(_get(hf[0], "iconCode", "value"))
    if icon is Icon.UNKNOWN and periods:
        icon = periods[0].icon

    rs = props.get("riseSet") or {}
    sunrise, sunset = _parse_dt(_get(rs, "sunrise"), tz), _parse_dt(_get(rs, "sunset"), tz)
    obs_t = _parse_dt(_get(cc, "timestamp"), tz)
    if obs_t and sunrise and sunset:
        icon = set_daylight(icon, sunrise <= obs_t < sunset)

    temp = _num(_get(cc, "temperature", "value"))
    feels = feels_like(temp, _num(_get(cc, "wind", "speed", "value")),
                       _num(_get(cc, "dewpoint", "value")))
    if feels is None and (temp is None or temp <= 0):
        # no wind/temp to compute from: trust ECCC's own value only where it is plausible
        feels = _num(_get(cc, "windChill", "value"))
    if feels is None and (temp is None or temp >= 20):
        feels = _num(_get(cc, "humidex", "value"))
    cond = _get(cc, "condition")
    if not cond and periods:
        cond = periods[0].summary.split(".")[0]

    current = Current(
        observed_at=_parse_dt(_get(cc, "timestamp"), tz),
        temp_c=temp,
        condition=str(cond or ""),
        icon=icon,
        feels_like_c=feels,
        wind_kph=_int(_get(cc, "wind", "speed", "value")),
        wind_gust_kph=_int(_get(cc, "wind", "gust", "value")),
        wind_dir=(str(_get(cc, "wind", "direction", "value")) if
                  _get(cc, "wind", "direction", "value") else None),
        humidity_pct=_int(_get(cc, "relativeHumidity", "value")),
        pressure_kpa=_num(_get(cc, "pressure", "value")),
        pressure_tendency=(str(_get(cc, "pressure", "tendency")).lower()
                           if _get(cc, "pressure", "tendency") else None),
        station=_get(cc, "station", "value"),
    )

    high = low = None
    for p in periods[:2]:
        if p.temp_is_high and high is None and p.temp_c is not None:
            high = p.temp_c
        elif not p.temp_is_high and low is None and p.temp_c is not None:
            low = p.temp_c

    name = _get(props, "name") or cfg.location_name
    return WeatherModel(
        location_name=str(name),
        source="eccc",
        fetched_at=now,
        current=current,
        periods=periods,
        alerts=parse_eccc_alerts(props.get("warnings"), tz),
        today_high_c=high,
        today_low_c=low,
        sunrise=sunrise,
        sunset=sunset,
    )


def fetch_eccc(cfg: WeatherConfig) -> WeatherModel:
    url = cfg.eccc_url.format(id=cfg.eccc_feature_id) + "?f=json&lang=en"
    m = parse_eccc(_http_get_json(url, cfg), cfg)
    if m.current.temp_c is None and not m.periods:
        raise ValueError("ECCC response empty")
    return m


# --------------------------------------------------------------------------- Open-Meteo


def open_meteo_url(cfg: WeatherConfig) -> str:
    q = {
        "latitude": f"{cfg.lat:.4f}",
        "longitude": f"{cfg.lon:.4f}",
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
                   "weather_code,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,precipitation_sum,snowfall_sum,sunrise,sunset",
        "models": cfg.open_meteo_models,
        "wind_speed_unit": "kmh",
        "timezone": cfg.timezone,
        "forecast_days": "5",
    }
    return cfg.open_meteo_url + "?" + urllib.parse.urlencode(q)


def _idx(lst: Any, i: int) -> Any:
    return lst[i] if isinstance(lst, list) and 0 <= i < len(lst) else None


def parse_open_meteo(data: dict, cfg: WeatherConfig, now: datetime | None = None) -> WeatherModel:
    tz = cfg.tz
    now = now or datetime.now(tz)
    cur = (data or {}).get("current") or {}
    daily = (data or {}).get("daily") or {}
    is_day = bool(cur.get("is_day", 1))
    code = cur.get("weather_code")
    gust = _num(cur.get("wind_gusts_10m"))
    press = _num(cur.get("pressure_msl"))
    current = Current(
        observed_at=_parse_dt(cur.get("time"), tz),
        temp_c=_num(cur.get("temperature_2m")),
        condition=_WMO_TEXT.get(_int(code), "") if code is not None else "",
        icon=wmo_icon(code, is_day, gust),
        feels_like_c=_num(cur.get("apparent_temperature")),
        wind_kph=_int(cur.get("wind_speed_10m")),
        wind_gust_kph=_int(gust),
        wind_dir=compass(_num(cur.get("wind_direction_10m"))),
        humidity_pct=_int(cur.get("relative_humidity_2m")),
        pressure_kpa=None if press is None else round(press / 10.0, 1),
        pressure_tendency=None,
        station=None,
    )

    days = daily.get("time") or []
    sunrises = [_parse_dt(s, tz) for s in (daily.get("sunrise") or [])]
    sunsets = [_parse_dt(s, tz) for s in (daily.get("sunset") or [])]
    periods: list[Period] = []
    for i, ds in enumerate(days):
        try:
            d = date.fromisoformat(ds)
        except (TypeError, ValueError):
            continue
        dname = d.strftime("%A")
        wcode = _idx(daily.get("weather_code"), i)
        pop = _int(_idx(daily.get("precipitation_probability_max"), i))
        snow = _num(_idx(daily.get("snowfall_sum"), i))
        rain = _num(_idx(daily.get("precipitation_sum"), i))
        amount = None
        if snow and snow >= 1:
            amount = f"{snow:g} cm"
        elif rain and rain >= 1 and not snow:
            amount = f"{rain:g} mm"
        text = _WMO_TEXT.get(_int(wcode), "")
        summ = text + (f". Amount {amount}." if amount and text else "")
        tmax = _int(_idx(daily.get("temperature_2m_max"), i))
        tmin = _int(_idx(daily.get("temperature_2m_min"), i))
        sset = _idx(sunsets, i)
        is_today = i == 0
        if is_today:
            # skip the day half once the sun has set
            if sset is None or now < sset:
                periods.append(Period("Today", "DAY", wmo_icon(wcode, True), tmax, True,
                                      pop, summ, amount))
            periods.append(Period("Tonight", "NIGHT", wmo_icon(wcode, False), tmin, False,
                                  pop, summ, amount))
        else:
            periods.append(Period(dname, short_name(dname), wmo_icon(wcode, True), tmax, True,
                                  pop, summ, amount))
            periods.append(Period(f"{dname} night", short_name(f"{dname} night"),
                                  wmo_icon(wcode, False), tmin, False, pop, summ, amount))
    return WeatherModel(
        location_name=cfg.location_name,
        source="open-meteo",
        fetched_at=now,
        current=current,
        periods=periods[:8],
        alerts=[],
        today_high_c=_int(_idx(daily.get("temperature_2m_max"), 0)),
        today_low_c=_int(_idx(daily.get("temperature_2m_min"), 0)),
        sunrise=_idx(sunrises, 0),
        sunset=_idx(sunsets, 0),
    )


def fetch_open_meteo(cfg: WeatherConfig) -> WeatherModel:
    m = parse_open_meteo(_http_get_json(open_meteo_url(cfg), cfg), cfg)
    if m.current.temp_c is None and not m.periods:
        raise ValueError("Open-Meteo response empty")
    return m


# --------------------------------------------------------------------------- entry point


def fetch_weather(cfg: WeatherConfig | None = None) -> WeatherModel:
    """ECCC first, Open-Meteo on failure. Raises RuntimeError only if both fail."""
    cfg = cfg or WeatherConfig()
    errors: list[str] = []
    try:
        m = fetch_eccc(cfg)
        if m.current.temp_c is None:  # station obs missing: borrow current from Open-Meteo
            try:
                om = fetch_open_meteo(cfg)
                m.current = om.current
                m.current.station = None
            except Exception as e:  # noqa: BLE001
                log.warning("current-conditions backfill failed: %s", e)
        return m
    except Exception as e:  # noqa: BLE001
        log.warning("ECCC failed: %s", e)
        errors.append(f"eccc: {e!r}")
    try:
        return fetch_open_meteo(cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("Open-Meteo failed: %s", e)
        errors.append(f"open-meteo: {e!r}")
    raise RuntimeError("all weather sources failed: " + "; ".join(errors))


# --------------------------------------------------------------------------- sample


def sample_model(now: datetime | None = None) -> WeatherModel:
    """Realistic hard-coded snowy, windy NL winter day with a wind warning (renderer tests)."""
    tz = ZoneInfo("America/St_Johns")
    now = now or datetime(2026, 1, 15, 14, 5, tzinfo=tz)
    obs = now.replace(minute=0, second=0, microsecond=0)
    cur = Current(
        observed_at=obs, temp_c=-4.2, condition="Light Snow", icon=Icon.SNOW,
        feels_like_c=-13.0, wind_kph=48, wind_gust_kph=72, wind_dir="NW",
        humidity_pct=88, pressure_kpa=98.9, pressure_tendency="falling",
        station="St. John's Int'l Airport",
    )
    periods = [
        Period("Tonight", "NIGHT", Icon.BLOWING_SNOW, -7, False, 80,
               "Snow at times heavy.", "10 cm"),
        Period("Friday", "FRI", Icon.SNOW, -3, True, 70, "Snow. Amount 5 cm.", "5 cm"),
        Period("Friday night", "FRI NT", Icon.CLOUDY, -9, False, 20, "Cloudy.", None),
        Period("Saturday", "SAT", Icon.PARTLY_CLOUDY_DAY, -2, True, 10, "Mainly cloudy.", None),
        Period("Saturday night", "SAT NT", Icon.FLURRIES, -6, False, 40, "Flurries.", None),
    ]
    alerts = [
        Alert("Wind warning", AlertSeverity.WARNING,
              issued_at=now - timedelta(hours=3), expires_at=now + timedelta(hours=9)),
        Alert("Snowfall advisory", AlertSeverity.ADVISORY,
              issued_at=now - timedelta(hours=5), expires_at=now + timedelta(hours=12)),
    ]
    return WeatherModel(
        location_name="St. John's", source="eccc", fetched_at=now, current=cur,
        periods=periods, alerts=alerts, today_high_c=-3, today_low_c=-7,
        sunrise=obs.replace(hour=7, minute=38), sunset=obs.replace(hour=16, minute=44),
    )


if __name__ == "__main__":  # manual check: python -m wxd.weather
    import pprint
    pprint.pprint(fetch_weather(WeatherConfig()))
