"""HTTP server for the ESP32-C6 Wi-Fi<->BLE bridge. Contract: docs/bridge.md.

stdlib only. `build_session_response` is pure (no sockets) so it is unit-tested directly.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import protocol as P

log = logging.getLogger("wxd.bridge")

MAGIC = b"WXB1"
MAX_BODY = 4096
SOCKET_TIMEOUT_S = 15.0


class BadRequest(ValueError):
    """Client sent an unusable body (-> HTTP 400)."""


class NoData(Exception):
    """Host has nothing to serve at all (-> HTTP 503)."""


def build_session_response(
    status_bytes: bytes, payloads: dict[int, bytes | None], interval_s: int | None
) -> bytes:
    status = parse_status(status_bytes)
    return _build(status, payloads, interval_s)[0]


def parse_status(status_bytes: bytes) -> P.DeviceStatus:
    if len(status_bytes) < P.STATUS_LEN_V1:
        raise BadRequest(f"status too short: {len(status_bytes)} < {P.STATUS_LEN_V1}")
    try:
        status = P.DeviceStatus.parse(status_bytes)
    except Exception as e:  # noqa: BLE001
        raise BadRequest(str(e)) from e
    if status.proto_version != P.PROTO_VERSION:
        raise BadRequest(f"unsupported protocol version {status.proto_version}")
    return status


def _build(
    status: P.DeviceStatus, payloads: dict[int, bytes | None], interval_s: int | None
) -> tuple[bytes, list[int]]:
    if not any(payloads.values()):
        raise NoData("no weather and no tide data")
    slots: list[int] = []
    parts: list[bytes] = []
    for slot in sorted(payloads):
        pl = payloads[slot]
        if not pl or len(pl) != P.PAYLOAD_BYTES:
            continue
        if not 0 <= slot < status.num_slots:
            continue
        crc = P.crc32(pl)
        if crc == status.slot_crc(slot):
            continue
        slots.append(slot)
        parts.append(struct.pack("<BI", slot, crc) + pl)
    head = MAGIC + struct.pack("<IB", interval_s or 0, len(slots))
    return head + b"".join(parts), slots


class BridgeServer:
    """Runs the HTTP server in a daemon thread. `state` needs payload_sync(status), model, tides."""

    def __init__(self, state: Any, listen: str, port: int, interval_s: int | None):
        self.state = state
        self.interval_s = interval_s
        self.last_result: dict | None = None
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            timeout = SOCKET_TIMEOUT_S  # applied to the connection socket
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:  # silence default stderr logging
                log.debug("%s " + fmt, self.address_string(), *args)

            def _send(self, code: int, body: bytes = b"", ctype: str = "application/octet-stream") -> None:
                self.send_response(code)
                if body or code != 204:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _body(self) -> bytes | None:
                try:
                    n = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._send(411)
                    return None
                if n < 0 or n > MAX_BODY:
                    self._send(413)
                    return None
                return self.rfile.read(n)

            def do_POST(self) -> None:  # noqa: N802
                try:
                    if self.path == "/v1/session":
                        self._session()
                    elif self.path == "/v1/result":
                        self._result()
                    else:
                        self._send(404)
                except (TimeoutError, ConnectionError, OSError) as e:
                    log.warning("bridge request %s aborted: %s", self.path, e)
                except Exception:  # noqa: BLE001
                    log.exception("bridge request %s failed", self.path)
                    try:
                        self._send(500)
                    except OSError:
                        pass

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/v1/health":
                    self._send(200, json.dumps(outer.health()).encode(), "application/json")
                else:
                    self._send(404)

            def _session(self) -> None:
                body = self._body()
                if body is None:
                    return
                dev = self.headers.get("X-Wx-Device", "?")
                try:
                    status = parse_status(body)
                except BadRequest as e:
                    log.warning("bridge session %s: bad request: %s", dev, e)
                    self._send(400, str(e).encode(), "text/plain")
                    return
                t0 = time.monotonic()
                payloads = outer.state.payload_sync(status)
                ms = int((time.monotonic() - t0) * 1000)
                try:
                    resp, slots = _build(status, payloads, outer.interval_s)
                except NoData:
                    log.warning("bridge session %s: no weather or tide data yet; 503", dev)
                    self._send(503, b"no data", "text/plain")
                    return
                log.info(
                    "bridge session %s: batt=%dmV/%s%% view=%d slots=%s crc=%08x/%08x render=%dms",
                    dev, status.battery_mv,
                    status.battery_pct if status.battery_pct is not None else "?",
                    status.view, slots, status.displayed_crc, status.tide_crc, ms,
                )
                self._send(200, resp)

            def _result(self) -> None:
                body = self._body()
                if body is None:
                    return
                try:
                    r = json.loads(body)
                    if not isinstance(r, dict):
                        raise ValueError("not an object")
                except ValueError as e:
                    self._send(400, f"bad json: {e}".encode(), "text/plain")
                    return
                with outer._lock:
                    outer.last_result = r
                slots = r.get("slots") or []
                err = r.get("error") or ""
                log.info(
                    "bridge result %s: %s%s rssi=%s batt=%smV %sms%s",
                    r.get("device", "?"), r.get("outcome", "?"),
                    f" slots={list(slots)}" if slots else "",
                    r.get("rssi", "?"), r.get("batt_mv", "?"), r.get("ms", "?"),
                    f" error={err}" if err else "",
                )
                self._send(204)

        self.httpd = ThreadingHTTPServer((listen, port), Handler)
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def health(self) -> dict:
        def ts(m: Any) -> str | None:
            f = getattr(m, "fetched_at", None)
            return f.isoformat() if f is not None else None

        with self._lock:
            last = self.last_result
        return {
            "weather_fetched_at": ts(self.state.model),
            "tides_fetched_at": ts(self.state.tides),
            "last_result": last,
        }

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="wxd-bridge", daemon=True)
        self._thread.start()
        log.info("bridge HTTP listening on %s:%d", *self.httpd.server_address[:2])

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(5)
