"""TOML configuration. Search order: --config, $XDG_CONFIG_HOME/wxd/config.toml, /etc/wxd/config.toml."""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BleConfig:
    device_address: str | None = None
    adapter: str = "hci0"
    poll_interval_s: int = 600


@dataclass
class Config:
    # [location] and [weather] (minus refresh_s) are passed through to weather.WeatherConfig.
    location: dict[str, Any] = field(default_factory=lambda: {"lat": 47.5615, "lon": -52.7126, "name": "St. John's"})
    weather: dict[str, Any] = field(default_factory=dict)
    render: dict[str, Any] = field(default_factory=lambda: {"rotate": 90, "red_invert": False})
    ble: BleConfig = field(default_factory=BleConfig)
    refresh_s: int = 600
    cache_dir: Path = field(default_factory=lambda: default_cache_dir())
    source_path: Path | None = None


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "wxd"


def config_search_paths(explicit: str | os.PathLike | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    paths = []
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    paths.append(Path(xdg) / "wxd" / "config.toml")
    paths.append(Path("/etc/wxd/config.toml"))
    return paths


def parse_config(data: dict[str, Any]) -> Config:
    cfg = Config()
    if "location" in data:
        cfg.location = {**cfg.location, **data["location"]}
    wx = dict(data.get("weather", {}))
    cfg.refresh_s = int(wx.pop("refresh_s", cfg.refresh_s))
    cfg.weather = wx
    if "render" in data:
        cfg.render = {**cfg.render, **data["render"]}
    b = data.get("ble", {})
    known = {f.name for f in dataclasses.fields(BleConfig)}
    unknown = set(b) - known
    if unknown:
        raise ValueError(f"unknown [ble] keys: {sorted(unknown)}")
    cfg.ble = BleConfig(**b)
    if cfg.ble.device_address == "":
        cfg.ble.device_address = None
    if "cache_dir" in data:
        cfg.cache_dir = Path(data["cache_dir"]).expanduser()
    if cfg.refresh_s < 30:
        raise ValueError("weather.refresh_s must be >= 30")
    return cfg


def load_config(explicit: str | os.PathLike | None = None) -> Config:
    """Load first existing config; an explicit path that is missing is an error. No file -> defaults."""
    for p in config_search_paths(explicit):
        if p.is_file():
            with open(p, "rb") as f:
                cfg = parse_config(tomllib.load(f))
            cfg.source_path = p
            return cfg
        if explicit:
            raise FileNotFoundError(f"config not found: {p}")
    return Config()


def _filtered(cls: type, values: dict[str, Any]) -> Any:
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in values.items() if k in names})


def make_weather_config(cfg: Config) -> Any:
    """Build weather.WeatherConfig (lazy import) from [location] + [weather]; unknown keys ignored."""
    from .weather import WeatherConfig

    return _filtered(WeatherConfig, {**cfg.location, **cfg.weather})


def make_render_config(cfg: Config) -> Any:
    from .render import RenderConfig

    return _filtered(RenderConfig, cfg.render)
