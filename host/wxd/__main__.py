"""wxd CLI: run | preview | scan | push | testpattern."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import os
import dataclasses
import json
import logging
import signal
import sys
import types
import typing
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from . import protocol as P
from .config import Config, load_config, make_render_config, make_tide_config, make_weather_config

log = logging.getLogger("wxd")

TIDE_STALE_AFTER = timedelta(hours=24)


# ---------------------------------------------------------------- model JSON cache


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    return obj


def from_jsonable(tp: Any, val: Any) -> Any:
    if val is None:
        return None
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return from_jsonable(args[0], val) if len(args) == 1 else val
    if origin in (list, typing.List):
        (inner,) = typing.get_args(tp)
        return [from_jsonable(inner, v) for v in val]
    if isinstance(tp, type):
        if dataclasses.is_dataclass(tp):
            hints = typing.get_type_hints(tp)
            return tp(**{k: from_jsonable(hints[k], v) for k, v in val.items() if k in hints})
        if issubclass(tp, Enum):
            return tp(val)
        if tp is datetime:
            return datetime.fromisoformat(val)
    return val


def save_model(model: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(to_jsonable(model)))
    tmp.replace(path)


def load_model(path: Path, tp: Any = None) -> Any | None:
    try:
        if tp is None:
            from .model import WeatherModel as tp

        return from_jsonable(tp, json.loads(path.read_text()))
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("ignoring unreadable cache %s: %s", path, e)
        return None


# ---------------------------------------------------------------- state


class State:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model: Any | None = None
        self.cache_path = cfg.cache_dir / "model.json"
        self.tides: Any | None = None
        self.tide_cache_path = cfg.cache_dir / "tides.json"

    def load_cache(self) -> None:
        self.model = load_model(self.cache_path)
        if self.model is not None:
            log.info("loaded cached weather from %s (fetched %s)", self.cache_path, self.model.fetched_at)
        if self.cfg.tides_enabled:
            from .tides import TideModel

            self.tides = load_model(self.tide_cache_path, TideModel)
            if self.tides is not None:
                log.info("loaded cached tides from %s (fetched %s)", self.tide_cache_path, self.tides.fetched_at)

    def fetch_sync(self) -> Any:
        from .weather import fetch_weather

        return fetch_weather(make_weather_config(self.cfg))

    async def refresh_once(self) -> bool:
        try:
            model = await asyncio.to_thread(self.fetch_sync)
        except Exception as e:  # noqa: BLE001
            log.warning("weather refresh failed: %s: %s", type(e).__name__, e)
            return False
        self.model = model
        try:
            save_model(model, self.cache_path)
        except OSError as e:
            log.warning("cannot write cache: %s", e)
        log.info("weather refreshed (%s, %s)", model.location_name, model.source)
        return True

    async def refresh_loop(self) -> None:
        while True:
            await self.refresh_once()
            await asyncio.sleep(self.cfg.refresh_s)

    async def tide_refresh_once(self) -> bool:
        from .tides import fetch_tides

        try:
            tides = await asyncio.to_thread(fetch_tides, make_tide_config(self.cfg))
        except Exception as e:  # noqa: BLE001
            log.warning("tide refresh failed: %s: %s", type(e).__name__, e)
            return False
        self.tides = tides
        try:
            save_model(tides, self.tide_cache_path)
        except OSError as e:
            log.warning("cannot write tide cache: %s", e)
        log.info("tides refreshed (%s, %d events)", tides.station_name, len(tides.events))
        return True

    async def tide_refresh_loop(self) -> None:
        while True:
            ok = await self.tide_refresh_once()
            # predictions are static: refetch rarely, retry sooner after a failure
            await asyncio.sleep(self.cfg.tides_refresh_s if ok else min(600, self.cfg.tides_refresh_s))

    def weather_payload(self, status: P.DeviceStatus) -> bytes | None:
        if self.model is None:
            log.warning("no weather yet; weather slot not sent")
            return None
        from .render import render, to_planes

        rcfg = make_render_config(self.cfg)
        return to_planes(render(self.model, status, rcfg), rcfg)

    def tide_payload(self, status: P.DeviceStatus) -> bytes | None:
        if not self.cfg.tides_enabled or self.tides is None:
            return None
        from .render import to_planes
        from .render_tide import render_tide

        rcfg = make_render_config(self.cfg)
        # Predictions stay valid for days: only flag them stale after a day without a refetch.
        tcfg = dataclasses.replace(rcfg, stale_after=TIDE_STALE_AFTER)
        return to_planes(render_tide(self.tides, status, tcfg, tz=make_tide_config(self.cfg).tz), rcfg)

    def payload_sync(self, status: P.DeviceStatus) -> dict[int, bytes | None]:
        out: dict[int, bytes | None] = {}
        for slot, fn in ((P.SLOT_WEATHER, self.weather_payload), (P.SLOT_TIDE, self.tide_payload)):
            try:
                out[slot] = fn(status)
            except Exception:  # noqa: BLE001 - one broken view must not block the other
                log.exception("render of slot %d failed; slot not sent", slot)
                out[slot] = None
        return out

    async def get_payload(self, status: P.DeviceStatus) -> dict[int, bytes | None]:
        return await asyncio.to_thread(self.payload_sync, status)


def _settings(cfg: Config):
    from .ble import BleSettings

    return BleSettings(
        device_address=cfg.ble.device_address,
        adapter=cfg.ble.adapter,
        poll_interval_s=cfg.ble.poll_interval_s,
    )


# ---------------------------------------------------------------- commands


@contextlib.contextmanager
def ble_lock(cfg: Config):
    """Only one wxd process may talk to the device: two would race for the same
    connection and BlueZ fails both (org.bluez.Error.InProgress)."""
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.cache_dir / "wxd.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
            raise SystemExit(f"another wxd (pid {holder}) is using the device; lock {path}")
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        yield
    finally:
        os.close(fd)


async def cmd_run(cfg: Config, once: bool) -> int:
    from .ble import WxBle

    state = State(cfg)
    state.load_cache()
    ble = WxBle(_settings(cfg), state.get_payload)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, ble.stop)

    if once:
        # one-shot push: make sure we have fresh data if the cache is empty
        if state.model is None:
            await state.refresh_once()
        if cfg.tides_enabled and state.tides is None:
            await state.tide_refresh_once()
        res = await ble.run(once=True)
        return 0 if res is not None else 1

    tasks = [asyncio.create_task(state.refresh_loop())]
    if cfg.tides_enabled:
        tasks.append(asyncio.create_task(state.tide_refresh_loop()))
    try:
        await ble.run()
    finally:
        for t in tasks:
            t.cancel()
    return 0


def cmd_preview(cfg: Config, out: str, view: str = "weather") -> int:
    from .render import render

    state = State(cfg)
    status = P.DeviceStatus(P.PROTO_VERSION, (0, 0, 0), 4000, 85, 0, 0, 0, cfg.ble.poll_interval_s, 0)
    if view == "tide":
        from .render_tide import render_tide
        from .tides import fetch_tides

        tcfg = make_tide_config(cfg)
        rcfg = dataclasses.replace(make_render_config(cfg), stale_after=TIDE_STALE_AFTER)
        img = render_tide(fetch_tides(tcfg), status, rcfg, tz=tcfg.tz)
    else:
        img = render(state.fetch_sync(), status, make_render_config(cfg))
    img.save(out)
    print(f"wrote {out}")
    return 0


def test_pattern_image():
    """Bring-up pattern: RED | BLACK | WHITE thirds, labelled, plus a corner marker.

    Verifies red polarity (CONFIG_WX_EPD_RED_INVERT) and orientation at a glance.
    """
    from importlib.resources import files

    from PIL import Image, ImageDraw, ImageFont

    from .render import BLACK, RED, WHITE, _PALETTE

    img = Image.new("P", (296, 128), WHITE)
    img.putpalette(_PALETTE)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    font = ImageFont.truetype(str(files("wxd") / "assets" / "DejaVuSans-Bold.ttf"), 22)
    w = 296 // 3
    for i, (bg, fg, label) in enumerate([(RED, WHITE, "RED"), (BLACK, WHITE, "BLACK"), (WHITE, BLACK, "WHITE")]):
        x0 = i * w
        d.rectangle([x0, 24, x0 + w - 1, 103], fill=bg)
        d.text((x0 + w // 2, 64), label, fill=fg, font=font, anchor="mm")
    d.rectangle([0, 0, 295, 127], outline=BLACK)
    d.polygon([(0, 0), (20, 0), (0, 20)], fill=RED)  # top-left marker
    d.text((24, 2), "TOP LEFT", fill=BLACK, font=ImageFont.truetype(str(files("wxd") / "assets" / "DejaVuSans-Bold.ttf"), 16))
    return img


async def cmd_testpattern(cfg: Config, out: str | None) -> int:
    from .ble import WxBle
    from .render import to_planes

    img = test_pattern_image()
    if out:
        img.save(out)
        print(f"wrote {out}")
        return 0
    payload = to_planes(img, make_render_config(cfg))

    def get_payload(_status: P.DeviceStatus) -> bytes:
        return payload

    ble = WxBle(_settings(cfg), get_payload)
    res = await ble.run(once=True)
    return 0 if res is not None else 1


async def cmd_scan(cfg: Config, seconds: float) -> int:
    from .ble import scan_devices

    try:
        devs = await scan_devices(cfg.ble.adapter, seconds)
    except Exception as e:  # noqa: BLE001
        print(
            f"BLE scan failed: {type(e).__name__}: {e}\n"
            f"Check the adapter ({cfg.ble.adapter}) exists and is powered: `bluetoothctl power on`, `rfkill list`.",
            file=sys.stderr,
        )
        return 2
    if not devs:
        print(f"no {P.DEVICE_NAME} devices seen in {seconds:.0f}s (device advertises ~30 s every poll interval)")
        return 1
    for d in devs:
        print(f"{d['address']}  {d['name']}  rssi={d['rssi']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wxd", description="nRF52840 e-paper weather host daemon")
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the daemon")
    pv = sub.add_parser("preview", help="fetch + render to a PNG (no BLE)")
    pv.add_argument("--out", required=True)
    pv.add_argument("--view", choices=("weather", "tide"), default="weather")
    sc = sub.add_parser("scan", help="list nearby WxPaper devices")
    sc.add_argument("--seconds", type=float, default=15.0)
    sub.add_parser("push", help="wait for the device, push once, exit")
    tp = sub.add_parser("testpattern", help="push a red/black/white bring-up pattern once (or --out PNG)")
    tp.add_argument("--out", help="write the pattern to a PNG instead of pushing")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"config error: {e}", file=sys.stderr)
        return 2
    log.info("config: %s", cfg.source_path or "defaults")

    try:
        if args.cmd == "run":
            with ble_lock(cfg):
                return asyncio.run(cmd_run(cfg, once=False))
        if args.cmd == "push":
            with ble_lock(cfg):
                return asyncio.run(cmd_run(cfg, once=True))
        if args.cmd == "preview":
            return cmd_preview(cfg, args.out, args.view)
        if args.cmd == "testpattern":
            if args.out:
                return asyncio.run(cmd_testpattern(cfg, args.out))
            with ble_lock(cfg):
                return asyncio.run(cmd_testpattern(cfg, None))
        if args.cmd == "scan":
            return asyncio.run(cmd_scan(cfg, args.seconds))
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
