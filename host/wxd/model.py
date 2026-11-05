"""Normalized weather data model shared by weather.py (producer) and render.py (consumer).

Every source adapter (ECCC, Open-Meteo, ...) must map into these types. Units are fixed:
temperatures in degC, wind in km/h, pressure in kPa, times timezone-aware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Icon(str, Enum):
    """Normalized condition icons. Renderer must draw every member."""

    CLEAR_DAY = "clear-day"
    CLEAR_NIGHT = "clear-night"
    PARTLY_CLOUDY_DAY = "partly-cloudy-day"
    PARTLY_CLOUDY_NIGHT = "partly-cloudy-night"
    CLOUDY = "cloudy"
    FOG = "fog"
    DRIZZLE = "drizzle"
    RAIN = "rain"
    SHOWERS_DAY = "showers-day"
    SHOWERS_NIGHT = "showers-night"
    FREEZING_RAIN = "freezing-rain"
    SLEET = "sleet"  # ice pellets / rain-snow mix
    SNOW = "snow"
    FLURRIES = "flurries"
    BLOWING_SNOW = "blowing-snow"
    THUNDERSTORM = "thunderstorm"
    WIND = "wind"
    UNKNOWN = "unknown"


class AlertSeverity(str, Enum):
    WARNING = "warning"  # red, most severe
    WATCH = "watch"
    ADVISORY = "advisory"
    STATEMENT = "statement"  # special weather statement


@dataclass
class Current:
    observed_at: datetime | None
    temp_c: float | None
    condition: str  # short human text, e.g. "Light Snow"
    icon: Icon
    feels_like_c: float | None = None  # wind chill or humidex if reported/meaningful
    wind_kph: int | None = None
    wind_gust_kph: int | None = None
    wind_dir: str | None = None  # 16-point compass, e.g. "NW"
    humidity_pct: int | None = None
    pressure_kpa: float | None = None
    pressure_tendency: str | None = None  # "rising" | "falling" | "steady"
    station: str | None = None


@dataclass
class Period:
    """A forecast period. ECCC style is day/night halves ("Tonight", "Friday", "Friday night")."""

    name: str  # long name, e.g. "Friday night"
    short_name: str  # <= 6 chars for the display, e.g. "FRI", "NIGHT", "FRI NT"
    icon: Icon
    temp_c: int | None
    temp_is_high: bool  # True = daytime high, False = overnight low
    pop_pct: int | None = None  # probability of precipitation
    summary: str = ""  # short text, e.g. "Snow. Amount 10 cm."
    precip_amount: str | None = None  # e.g. "10 cm", "5 mm"


@dataclass
class Alert:
    title: str  # e.g. "Wind warning"
    severity: AlertSeverity
    issued_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class WeatherModel:
    location_name: str  # e.g. "St. John's"
    source: str  # "eccc" | "open-meteo"
    fetched_at: datetime
    current: Current
    periods: list[Period] = field(default_factory=list)  # chronological, first = current/next
    alerts: list[Alert] = field(default_factory=list)  # most severe first
    today_high_c: int | None = None
    today_low_c: int | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None
