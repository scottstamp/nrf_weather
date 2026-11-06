"""Tide predictions from the Canadian Hydrographic Service IWLS API (no key).

Stdlib only. See docs/tides.md. Events are high/low water times; the high/low
flag is derived by comparing neighbouring heights (the API does not say).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("wxd.tides")

USER_AGENT = "wxd/0.1 (e-paper weather display)"
IWLS_BASE = "https://api-iwls.dfo-mpo.gc.ca/api/v1"
ST_JOHNS_STATION_ID = "5cebf1e33d0f4a073c4bc176"  # station code 00905


class TideError(RuntimeError):
    """Fetch failed or no usable data; caller should keep its last cached model."""


@dataclass
class TideEvent:
    time: datetime  # tz-aware
    height_m: float  # metres above chart datum
    is_high: bool


@dataclass
class TideModel:
    station_name: str
    fetched_at: datetime
    events: list[TideEvent] = field(default_factory=list)


@dataclass
class TideConfig:
    station_id: str = ST_JOHNS_STATION_ID
    station_code: str = "00905"
    station_name: str = "St. John's"
    tz: str = "America/St_Johns"
    timeout_s: float = 15.0
    window_back: timedelta = timedelta(days=1)
    window_fwd: timedelta = timedelta(days=3)
    base_url: str = IWLS_BASE
    user_agent: str = USER_AGENT

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


def _get_json(url: str, cfg: TideConfig) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": cfg.user_agent, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=cfg.timeout_s) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_station_id(cfg: TideConfig) -> str:
    """Look up the station id from cfg.station_code (only needed for a non-default station)."""
    q = urllib.parse.urlencode({"code": cfg.station_code})
    data = _get_json(f"{cfg.base_url}/stations?{q}", cfg)
    if not isinstance(data, list) or not data or not data[0].get("id"):
        raise TideError(f"no IWLS station for code {cfg.station_code}")
    return str(data[0]["id"])


def parse_iwls(data: Any, cfg: TideConfig, fetched_at: datetime) -> TideModel:
    """Pure: IWLS wlp-hilo JSON list -> TideModel (sorted, de-duplicated, classified)."""
    if not isinstance(data, list):
        raise TideError("unexpected IWLS response (not a list)")
    zone = cfg.zone
    pts: dict[datetime, float] = {}
    for e in data:
        try:
            t = datetime.fromisoformat(str(e["eventDate"]).replace("Z", "+00:00"))
            v = float(e["value"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if v != v or v in (float("inf"), float("-inf")):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        pts[t.astimezone(zone)] = v
    items = sorted(pts.items())
    if not items:
        raise TideError("IWLS returned no usable tide events")
    events: list[TideEvent] = []
    n = len(items)
    for i, (t, v) in enumerate(items):
        nb = [items[j][1] for j in (i - 1, i + 1) if 0 <= j < n]
        if not nb:  # single event: unknowable
            is_high = True
        else:
            is_high = all(v > x for x in nb) if len(nb) == 2 else v > nb[0]
        events.append(TideEvent(time=t, height_m=v, is_high=is_high))
    return TideModel(station_name=cfg.station_name, fetched_at=fetched_at, events=events)


def fetch_tides(cfg: TideConfig | None = None, now: datetime | None = None) -> TideModel:
    cfg = cfg or TideConfig()
    now = now or datetime.now(timezone.utc)
    utc = now.astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    q = urllib.parse.urlencode(
        {
            "time-series-code": "wlp-hilo",
            "from": (utc - cfg.window_back).strftime(fmt),
            "to": (utc + cfg.window_fwd).strftime(fmt),
        }
    )
    try:
        data = _get_json(f"{cfg.base_url}/stations/{cfg.station_id}/data?{q}", cfg)
    except Exception as e:  # noqa: BLE001
        raise TideError(f"IWLS fetch failed: {e}") from e
    return parse_iwls(data, cfg, now)
