"""BLE central side: scanner loop + one upload session per device appearance.

Spec: docs/protocol.md. Transport is abstracted behind `GattClient` so the session logic
runs against a fake device in tests; `BleakGattClient` is the real (BlueZ) implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, Union

from . import protocol as P

log = logging.getLogger("wxd.ble")

CONNECT_TIMEOUT_S = 10.0
SESSION_TIMEOUT_S = 20.0  # device drops the link after 20 s of inactivity
MAX_CHUNK = 244  # bytes per Data write (ATT_MTU 247 - 3)
RESCAN_INTERVAL_S = 30 * 60
# After a session the device stops advertising, but BlueZ can still report its last
# advert for a few seconds; ignore it briefly. Must stay well below the shortest poll
# interval (60 s in debug builds).
SUCCESS_DEBOUNCE_S = 20.0
FAILURE_DEBOUNCE_S = 2.0
MAX_BACKOFF_S = 300.0


# ---------------------------------------------------------------- errors


class BleError(Exception):
    """Base for session-level errors."""


class AttError(BleError):
    """ATT application error returned by the device (0x80..0x84)."""

    def __init__(self, code: int):
        super().__init__(f"ATT error 0x{code:02x}")
        self.code = code


class DisconnectedError(BleError):
    """Link went away (expected after COMMIT / NO_CHANGE)."""


class SessionError(BleError):
    """Session failed for a non-transport reason (bad status, retries exhausted)."""


# ---------------------------------------------------------------- transport


class GattClient(Protocol):
    @property
    def mtu(self) -> int:
        """Negotiated ATT_MTU."""

    async def read_status(self) -> bytes: ...

    async def write_control(self, data: bytes) -> None:
        """Write-with-response. Raises AttError / DisconnectedError."""

    async def write_data(self, data: bytes, response: bool) -> None:
        """Raises AttError / DisconnectedError."""


# Returns the weather payload (bytes), None (nothing to send), or {slot: payload | None}.
Payloads = Union[bytes, None, dict[int, Union[bytes, None]]]
GetPayload = Callable[[P.DeviceStatus], Union[Payloads, Awaitable[Payloads]]]
Connector = Callable[[Any], "contextlib.AbstractAsyncContextManager[GattClient]"]

_ATT_RE = re.compile(r"ATT error:?\s*0x([0-9a-fA-F]{2})|Application error:?\s*0x([0-9a-fA-F]{2})", re.I)


def map_bleak_exception(exc: BaseException, connected: bool) -> BleError | BaseException:
    """Translate a bleak exception into AttError / DisconnectedError where recognisable.

    BlueZ reports ATT application errors as D-Bus errors whose text contains
    "Operation failed with ATT error: 0x81". Anything else while the link is
    down is treated as a disconnect.
    """
    text = " ".join(
        str(x) for x in (exc, getattr(exc, "dbus_error", ""), getattr(exc, "dbus_error_details", "")) if x
    )
    m = _ATT_RE.search(text)
    if m:
        return AttError(int(m.group(1) or m.group(2), 16))
    if not connected or isinstance(exc, EOFError) or "not connected" in text.lower() or "disconnect" in text.lower():
        return DisconnectedError(text or type(exc).__name__)
    return exc


class BleakGattClient:
    """GattClient over a connected bleak.BleakClient."""

    def __init__(self, client: Any):
        self._c = client

    @property
    def mtu(self) -> int:
        return int(getattr(self._c, "mtu_size", 23) or 23)

    async def _call(self, coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except (asyncio.CancelledError, asyncio.TimeoutError):
            raise
        except Exception as e:  # noqa: BLE001
            mapped = map_bleak_exception(e, self._c.is_connected)
            if mapped is e:
                raise
            raise mapped from e

    async def read_status(self) -> bytes:
        return bytes(await self._call(self._c.read_gatt_char(P.STATUS_UUID)))

    async def write_control(self, data: bytes) -> None:
        await self._call(self._c.write_gatt_char(P.CONTROL_UUID, data, response=True))

    async def write_data(self, data: bytes, response: bool) -> None:
        await self._call(self._c.write_gatt_char(P.DATA_UUID, data, response=response))


def bleak_connector(adapter: str | None = None, timeout: float = CONNECT_TIMEOUT_S) -> Connector:
    @contextlib.asynccontextmanager
    async def connect(device: Any) -> AsyncIterator[GattClient]:
        from bleak import BleakClient

        kwargs: dict[str, Any] = {"timeout": timeout}
        if adapter:
            kwargs["adapter"] = adapter
        client = BleakClient(device, **kwargs)
        await client.connect()
        # BlueZ: bleak reports MTU 23 unless it is acquired explicitly (AcquireWrite on
        # the write-without-response Data characteristic). Without this every Data
        # write is 20 bytes -> ~530 writes instead of ~40.
        acquire = getattr(getattr(client, "_backend", None), "_acquire_mtu", None)
        if acquire is not None:
            try:
                await acquire()
            except Exception as e:  # noqa: BLE001
                log.warning("MTU acquire failed (%s); using %d", e, getattr(client, "mtu_size", 23))
        try:
            yield BleakGattClient(client)
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except Exception as e:  # noqa: BLE001 - device usually already dropped the link
                log.debug("disconnect: %r", e)

    return connect


# ---------------------------------------------------------------- session


@dataclass
class SessionResult:
    outcome: str  # "uploaded" | "no_change"
    status: P.DeviceStatus
    crc: int | None  # weather (slot 0) payload CRC, if one was rendered
    duration_s: float
    attempts: int = 0  # max attempts used by any uploaded slot
    reason: str = ""
    link_dropped_on_final: bool = False  # device disconnected during DONE (treated as success)
    uploaded_slots: tuple[int, ...] = ()


def chunk_size_for_mtu(mtu: int) -> int:
    return max(4, min(MAX_CHUNK, mtu - 3))


async def _final_write(client: GattClient, data: bytes) -> bool:
    """Write DONE. Returns True if the link dropped instead of acking.

    The device disconnects right after DONE, so depending on timing the write response
    may be lost and the stack reports a disconnect. That is success. (A real failure
    arrives as an AttError, which propagates.) Slots were already committed (and acked)
    before DONE, so nothing is lost either way.
    """
    try:
        await client.write_control(data)
    except DisconnectedError:
        return True
    return False


async def _upload(client: GattClient, slot: int, payload: bytes, crc: int, now: int, response: bool) -> None:
    mtu = client.mtu
    chunk = chunk_size_for_mtu(mtu)
    t0 = time.monotonic()
    await client.write_control(P.op_begin(crc, now, slot))
    chunks = P.data_chunks(payload, chunk)
    for c in chunks:
        await client.write_data(c, response=response)
    t1 = time.monotonic()
    await client.write_control(P.op_commit())
    log.info(
        "upload slot %d: mtu=%d chunk=%d writes=%d rsp=%s data=%.2fs commit=%.2fs",
        slot, mtu, chunk, len(chunks), response, t1 - t0, time.monotonic() - t1,
    )


def _normalize_payloads(res: Any) -> dict[int, bytes]:
    """get_payload may return bytes (weather slot only), None, or {slot: bytes | None}."""
    if res is None:
        return {}
    if isinstance(res, (bytes, bytearray)):
        return {P.SLOT_WEATHER: bytes(res)} if res else {}
    return {int(k): bytes(v) for k, v in dict(res).items() if v}


async def run_session(
    client: GattClient,
    get_payload: GetPayload,
    *,
    interval_s: int | None = None,
    clock: Callable[[], float] = time.time,
) -> SessionResult:
    t0 = time.monotonic()
    status = P.DeviceStatus.parse(await client.read_status())
    if status.proto_version != P.PROTO_VERSION:
        raise SessionError(f"unsupported protocol version {status.proto_version}")

    if interval_s and interval_s != status.poll_interval_s and P.MIN_INTERVAL_S <= interval_s <= P.MAX_INTERVAL_S:
        try:
            await client.write_control(P.op_set_interval(interval_s))
        except AttError as e:
            log.warning("SET_INTERVAL rejected: %s", e)

    res = get_payload(status)
    if inspect.isawaitable(res):
        res = await res
    payloads = _normalize_payloads(res)
    now = int(clock())

    for slot, pl in payloads.items():
        if len(pl) != P.PAYLOAD_BYTES:
            raise SessionError(f"slot {slot} payload is {len(pl)} bytes, want {P.PAYLOAD_BYTES}")
    weather_crc = P.crc32(payloads[P.SLOT_WEATHER]) if P.SLOT_WEATHER in payloads else None

    uploaded: list[int] = []
    max_attempts = 0
    for slot in sorted(payloads):
        if slot >= status.num_slots:
            log.debug("device has no slot %d; skipped", slot)
            continue
        pl = payloads[slot]
        crc = P.crc32(pl)
        if status.slot_crc(slot) == crc:
            continue
        last: AttError | None = None
        for attempt, response in ((1, False), (2, True)):
            try:
                await _upload(client, slot, pl, crc, now, response)
                break
            except AttError as e:
                last = e
                log.warning("slot %d upload attempt %d failed: %s", slot, attempt, e)
        else:
            raise SessionError(f"slot {slot} upload failed after retry: {last}")
        uploaded.append(slot)
        max_attempts = max(max_attempts, attempt)

    dropped = await _final_write(client, P.op_done(now))
    dt = time.monotonic() - t0
    if uploaded:
        return SessionResult("uploaded", status, weather_crc, dt, attempts=max_attempts,
                             link_dropped_on_final=dropped, uploaded_slots=tuple(uploaded))
    reason = "crc match" if payloads else "no payload"
    return SessionResult("no_change", status, weather_crc, dt, reason=reason, link_dropped_on_final=dropped)


# ---------------------------------------------------------------- scanner / daemon


@dataclass
class BleSettings:
    device_address: str | None = None
    adapter: str | None = "hci0"
    poll_interval_s: int | None = 600
    rescan_interval_s: float = RESCAN_INTERVAL_S
    success_debounce_s: float = SUCCESS_DEBOUNCE_S
    connect_timeout_s: float = CONNECT_TIMEOUT_S


def _norm_addr(a: str | None) -> str:
    return (a or "").upper()


def advert_matches(name: str | None, uuids: list[str] | None, address: str, pinned: str | None) -> bool:
    if pinned:
        return _norm_addr(address) == _norm_addr(pinned)
    if uuids and any(u.lower() == P.SERVICE_UUID for u in uuids):
        return True
    return name == P.DEVICE_NAME


def _default_scanner_factory(adapter: str | None):
    def factory(callback):
        from bleak import BleakScanner

        kwargs: dict[str, Any] = {"detection_callback": callback, "service_uuids": [P.SERVICE_UUID]}
        if adapter:
            kwargs["adapter"] = adapter
        return BleakScanner(**kwargs)

    return factory


class WxBle:
    """Scan forever; run one session per device appearance. Never raises on BLE errors."""

    def __init__(
        self,
        settings: BleSettings,
        get_payload: GetPayload,
        *,
        connector: Connector | None = None,
        scanner_factory: Callable[[Callable], Any] | None = None,
        on_result: Callable[[SessionResult], None] | None = None,
        poll_s: float = 1.0,
    ):
        self.s = settings
        self.get_payload = get_payload
        self._connector = connector or bleak_connector(settings.adapter, settings.connect_timeout_s)
        self._scanner_factory = scanner_factory or _default_scanner_factory(settings.adapter)
        self.on_result = on_result
        self._poll_s = poll_s
        self._ignore_until: dict[str, float] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._busy = False
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    # scanner callback (runs in the event loop)
    def _on_adv(self, device: Any, adv: Any) -> None:
        name = getattr(adv, "local_name", None) or getattr(device, "name", None)
        uuids = getattr(adv, "service_uuids", None)
        addr = device.address
        if not advert_matches(name, uuids, addr, self.s.device_address):
            return
        if self._busy or time.monotonic() < self._ignore_until.get(_norm_addr(addr), 0.0):
            return
        self._busy = True  # one pending/active session at a time
        self._queue.put_nowait(device)

    async def _session(self, device: Any) -> SessionResult:
        async with self._connector(device) as client:
            async with asyncio.timeout(SESSION_TIMEOUT_S):
                return await run_session(client, self.get_payload, interval_s=self.s.poll_interval_s)

    async def _handle(self, device: Any) -> SessionResult | None:
        addr = _norm_addr(device.address)
        t0 = time.monotonic()
        result: SessionResult | None = None
        try:
            result = await asyncio.wait_for(self._session(device), self.s.connect_timeout_s + SESSION_TIMEOUT_S + 5)
            st = result.status
            log.info(
                "session %s: %s%s crc=%s batt=%dmV/%s%% view=%d attempts=%d%s %.1fs",
                addr,
                result.outcome,
                f" slots={list(result.uploaded_slots)}" if result.uploaded_slots else "",
                f"{result.crc:08x}" if result.crc is not None else "-",
                st.battery_mv,
                st.battery_pct if st.battery_pct is not None else "?",
                st.view,
                result.attempts,
                " (link dropped on final write)" if result.link_dropped_on_final else "",
                time.monotonic() - t0,
            )
            self._ignore_until[addr] = time.monotonic() + self.s.success_debounce_s
            if self.on_result:
                self.on_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("session %s failed after %.1fs: %s", addr, time.monotonic() - t0, e or type(e).__name__)
            self._ignore_until[addr] = time.monotonic() + FAILURE_DEBOUNCE_S
        finally:
            self._busy = False
            while not self._queue.empty():
                self._queue.get_nowait()
        return result

    async def _sleep(self, secs: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), secs)

    async def run(self, once: bool = False) -> SessionResult | None:
        """Run until stop() (or, with once=True, until the first successful session)."""
        backoff = 5.0
        while not self._stop.is_set():
            try:
                scanner = self._scanner_factory(self._on_adv)
                async with scanner:
                    log.info("scanning for %s", self.s.device_address or P.DEVICE_NAME)
                    backoff = 5.0
                    started = time.monotonic()
                    while not self._stop.is_set() and time.monotonic() - started < self.s.rescan_interval_s:
                        try:
                            device = await asyncio.wait_for(self._queue.get(), self._poll_s)
                        except asyncio.TimeoutError:
                            continue
                        res = await self._handle(device)
                        if res is not None and once:
                            return res
                    if not self._stop.is_set():
                        log.info("periodic scanner restart")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("BLE scanner error: %s: %s (retry in %.0fs; is the adapter powered? `bluetoothctl power on`)", type(e).__name__, e, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
        return None


async def scan_devices(adapter: str | None, seconds: float = 15.0) -> list[dict]:
    """One-shot scan; returns WxPaper-looking devices. Raises on adapter problems."""
    from bleak import BleakScanner

    kwargs: dict[str, Any] = {}
    if adapter:
        kwargs["adapter"] = adapter
    found = await BleakScanner.discover(timeout=seconds, return_adv=True, **kwargs)
    out = []
    for addr, (dev, adv) in found.items():
        if advert_matches(adv.local_name or dev.name, adv.service_uuids, addr, None):
            out.append({"address": addr, "name": adv.local_name or dev.name, "rssi": adv.rssi})
    return out
