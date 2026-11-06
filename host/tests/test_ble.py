"""BLE session tests against a FakeDevice that emulates the firmware per docs/protocol.md."""

from __future__ import annotations

import asyncio
import contextlib
import os
import struct
import types

import pytest

from wxd import protocol as P
from wxd.ble import (
    AttError,
    BleSettings,
    DisconnectedError,
    SessionError,
    WxBle,
    map_bleak_exception,
    run_session,
)


class FakeDevice:
    """Firmware state machine (GattClient implementation)."""

    def __init__(self, mtu=247, displayed_crc=0, interval=1800):
        self._mtu = mtu
        self.status = P.DeviceStatus(P.PROTO_VERSION, (1, 2, 3), 3950, 80, 0, displayed_crc, 1234, interval, 7,
                                     tide_crc=0, view=0, num_slots=2)
        self.connected = True
        self.rx = bytearray()
        self.began = False
        self.exp_len = 0
        self.exp_crc = 0
        self.seq_err = False
        self.committed: bytes | None = None  # slot 0
        self.slots: dict[int, bytes] = {}
        self.slot = 0
        self.no_change_time: int | None = None
        self.log: list[str] = []
        # fault injection
        self.corrupt_first_chunk = False  # flips a bit in first Data write, first transfer only
        self.skip_chunk_once: int | None = None  # drop the Nth chunk of the first transfer
        self.drop_ack_on_final = False  # raise DisconnectedError from the DONE write
        self.data_responses: list[bool] = []
        self._begins = 0

    @property
    def mtu(self):
        return self._mtu

    def _chk(self):
        if not self.connected:
            raise DisconnectedError("link down")

    async def read_status(self) -> bytes:
        self._chk()
        return self.status.pack()

    async def write_control(self, data: bytes) -> None:
        self._chk()
        op = data[0]
        if op == P.OP_BEGIN:
            if len(data) not in (14, 15):
                raise AttError(P.ERR_BAD_LENGTH)
            total, crc, fmt, _t = struct.unpack_from("<IIBI", data, 1)
            slot = data[14] if len(data) == 15 else 0
            if slot >= self.status.num_slots:
                raise AttError(P.ERR_BAD_VALUE)
            self.slot = slot
            if total != P.PAYLOAD_BYTES:
                raise AttError(P.ERR_BAD_LENGTH)
            if fmt != P.FORMAT_RAW:
                raise AttError(P.ERR_BAD_VALUE)
            self.began, self.exp_len, self.exp_crc = True, total, crc
            self.rx.clear()
            self.seq_err = False
            self._begins += 1
            self.log.append("BEGIN")
        elif op == P.OP_COMMIT:
            if not self.began:
                raise AttError(P.ERR_NO_BEGIN)
            if self.seq_err:
                raise AttError(P.ERR_SEQUENCE)
            if len(self.rx) != self.exp_len:
                raise AttError(P.ERR_BAD_LENGTH)
            if P.crc32(bytes(self.rx)) != self.exp_crc:
                raise AttError(P.ERR_CRC)
            self.slots[self.slot] = bytes(self.rx)
            if self.slot == 0:
                self.committed = bytes(self.rx)
                self.status.displayed_crc = self.exp_crc
            else:
                self.status.tide_crc = self.exp_crc
            self.began = False
            self.log.append("COMMIT" if self.slot == 0 else f"COMMIT {self.slot}")
        elif op == P.OP_DONE:
            if len(data) != 5:
                raise AttError(P.ERR_BAD_LENGTH)
            self.no_change_time = struct.unpack_from("<I", data, 1)[0]
            self.log.append("DONE")
            self.connected = False
            if self.drop_ack_on_final:
                raise DisconnectedError("lost ack")
        elif op == P.OP_SET_INTERVAL:
            (s,) = struct.unpack_from("<I", data, 1)
            if not P.MIN_INTERVAL_S <= s <= P.MAX_INTERVAL_S:
                raise AttError(P.ERR_BAD_VALUE)
            self.status.poll_interval_s = s
            self.log.append(f"INTERVAL {s}")
        else:
            raise AttError(P.ERR_BAD_VALUE)

    async def write_data(self, data: bytes, response: bool) -> None:
        self._chk()
        assert len(data) <= self._mtu - 3, "chunk exceeds MTU"
        self.data_responses.append(response)
        if not self.began:
            if response:
                raise AttError(P.ERR_NO_BEGIN)
            return
        (off,) = struct.unpack_from("<H", data)
        body = data[2:]
        first_transfer = self._begins == 1
        if first_transfer and self.skip_chunk_once is not None and off == self.skip_chunk_once:
            self.skip_chunk_once = None
            return  # lost chunk -> following offsets out of order
        if self.seq_err:
            return
        if off != len(self.rx):
            self.seq_err = True
            return
        if first_transfer and self.corrupt_first_chunk and off == 0:
            body = bytes([body[0] ^ 1]) + body[1:]
        self.rx += body


def payload(seed=1) -> bytes:
    return bytes((i * seed + 7) & 0xFF for i in range(P.PAYLOAD_BYTES))


def run(coro):
    return asyncio.run(coro)


def test_status_roundtrip():
    s = P.DeviceStatus(2, (1, 2, 3), 3999, None, P.FLAG_LOW_BATT | P.FLAG_NO_IMAGE, 0xDEADBEEF, 99, 1800, 5,
                       tide_crc=0x12345678, view=1, num_slots=2)
    raw = s.pack()
    assert len(raw) == 32
    assert P.DeviceStatus.parse(raw) == s
    assert s.low_battery and s.slot_crc(1) == 0x12345678
    # v1 (24-byte) status still parses: one slot
    v1 = P.DeviceStatus.parse(raw[:24])
    assert v1.num_slots == 1 and v1.tide_crc == 0 and v1.displayed_crc == 0xDEADBEEF


def test_happy_path_upload():
    dev, pl = FakeDevice(), payload()
    r = run(run_session(dev, lambda st: pl, clock=lambda: 1000))
    assert r.outcome == "uploaded" and r.attempts == 1
    assert dev.committed == pl and dev.status.displayed_crc == P.crc32(pl)
    assert dev.log == ["BEGIN", "COMMIT", "DONE"]
    assert set(dev.data_responses) == {False}
    assert len(dev.data_responses) == 40  # 242 B per chunk at MTU 247


def test_async_get_payload_and_status_passed():
    dev, pl = FakeDevice(), payload()
    seen = []

    async def gp(st):
        seen.append(st.battery_mv)
        return pl

    run(run_session(dev, gp))
    assert seen == [3950] and dev.committed == pl


def test_no_change_when_crc_matches():
    pl = payload()
    dev = FakeDevice(displayed_crc=P.crc32(pl))
    r = run(run_session(dev, lambda st: pl, clock=lambda: 4242))
    assert r.outcome == "no_change"
    assert dev.log == ["DONE"] and dev.no_change_time == 4242 and dev.committed is None


def test_no_payload_answers_no_change():
    dev = FakeDevice()
    r = run(run_session(dev, lambda st: None))
    assert r.outcome == "no_change" and dev.log == ["DONE"]


def test_wrong_size_payload_rejected():
    with pytest.raises(SessionError):
        run(run_session(FakeDevice(), lambda st: b"abc"))


def test_crc_error_then_retry_success():
    dev, pl = FakeDevice(), payload()
    dev.corrupt_first_chunk = True
    r = run(run_session(dev, lambda st: pl))
    assert r.outcome == "uploaded" and r.attempts == 2
    assert dev.committed == pl
    assert dev.log == ["BEGIN", "BEGIN", "COMMIT", "DONE"]
    # retry uses write-with-response
    assert dev.data_responses[-1] is True and dev.data_responses[0] is False


def test_out_of_order_chunk_0x84_then_retry():
    dev, pl = FakeDevice(), payload()
    dev.skip_chunk_once = 242 * 3
    errors = []
    orig = dev.write_control

    async def spy(data):
        try:
            await orig(data)
        except AttError as e:
            errors.append(e.code)
            raise

    dev.write_control = spy
    r = run(run_session(dev, lambda st: pl))
    assert errors == [P.ERR_SEQUENCE]
    assert r.attempts == 2 and dev.committed == pl


def test_retry_exhausted():
    dev, pl = FakeDevice(), payload()

    async def always_bad(data, response):
        dev.data_responses.append(response)

    # never accept data -> COMMIT fails with 0x80 both times
    dev.write_data = always_bad
    with pytest.raises(SessionError):
        run(run_session(dev, lambda st: pl))
    assert dev.log == ["BEGIN", "BEGIN"]


@pytest.mark.parametrize("mtu,expected_chunks", [(23, 527), (185, 53), (247, 40), (517, 40)])
def test_mtu_chunking(mtu, expected_chunks):
    dev, pl = FakeDevice(mtu=mtu), payload(3)
    run(run_session(dev, lambda st: pl))
    assert dev.committed == pl
    assert len(dev.data_responses) == expected_chunks


def test_disconnect_on_done_after_upload_is_success():
    dev, pl = FakeDevice(), payload()
    dev.drop_ack_on_final = True
    r = run(run_session(dev, lambda st: pl))
    assert r.outcome == "uploaded" and r.link_dropped_on_final and dev.committed == pl


def test_disconnect_on_done_without_upload_is_success():
    pl = payload()
    dev = FakeDevice(displayed_crc=P.crc32(pl))
    dev.drop_ack_on_final = True
    assert run(run_session(dev, lambda st: pl)).outcome == "no_change"


def test_disconnect_mid_transfer_fails():
    dev, pl = FakeDevice(), payload()
    orig = dev.write_data
    n = 0

    async def flaky(data, response):
        nonlocal n
        n += 1
        if n == 5:
            dev.connected = False
        await orig(data, response)

    dev.write_data = flaky
    with pytest.raises(DisconnectedError):
        run(run_session(dev, lambda st: pl))


def test_two_slots_only_changed_ones_sent():
    w, t = payload(1), payload(5)
    dev = FakeDevice()
    r = run(run_session(dev, lambda st: {P.SLOT_WEATHER: w, P.SLOT_TIDE: t}))
    assert r.outcome == "uploaded" and r.uploaded_slots == (0, 1)
    assert dev.log == ["BEGIN", "COMMIT", "BEGIN", "COMMIT 1", "DONE"]
    assert dev.slots == {0: w, 1: t}
    # next contact: only the tide image changed
    t2 = payload(7)
    dev.connected, dev.log = True, []
    r = run(run_session(dev, lambda st: {P.SLOT_WEATHER: w, P.SLOT_TIDE: t2}))
    assert r.uploaded_slots == (1,) and dev.log == ["BEGIN", "COMMIT 1", "DONE"] and dev.slots[1] == t2
    # nothing changed; a missing tide payload (fetch failed) leaves the stored one alone
    dev.connected, dev.log = True, []
    r = run(run_session(dev, lambda st: {P.SLOT_WEATHER: w, P.SLOT_TIDE: None}))
    assert r.outcome == "no_change" and dev.log == ["DONE"] and dev.slots[1] == t2


def test_single_slot_device_skips_tide():
    dev = FakeDevice()
    dev.status.num_slots = 1
    r = run(run_session(dev, lambda st: {P.SLOT_WEATHER: payload(1), P.SLOT_TIDE: payload(2)}))
    assert r.uploaded_slots == (0,) and dev.log == ["BEGIN", "COMMIT", "DONE"]


def test_set_interval_only_when_different():
    dev, pl = FakeDevice(interval=1800), payload()
    run(run_session(dev, lambda st: pl, interval_s=1800))
    assert not any(x.startswith("INTERVAL") for x in dev.log)
    dev = FakeDevice(interval=900)
    run(run_session(dev, lambda st: pl, interval_s=1800))
    assert dev.log[0] == "INTERVAL 1800" and dev.status.poll_interval_s == 1800


def test_map_bleak_exception():
    e = Exception("Operation failed with ATT error: 0x81")
    m = map_bleak_exception(e, True)
    assert isinstance(m, AttError) and m.code == 0x81
    assert isinstance(map_bleak_exception(Exception("boom"), False), DisconnectedError)
    boom = Exception("boom")
    assert map_bleak_exception(boom, True) is boom


def test_daemon_debounce_and_once():
    pl = payload()
    dev = FakeDevice()
    devices = []

    @contextlib.asynccontextmanager
    async def connector(d):
        devices.append(d)
        dev.connected = True
        yield dev

    class FakeScanner:
        def __init__(self, cb):
            self.cb = cb

        async def __aenter__(self):
            ble_dev = types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="WxPaper")
            adv = types.SimpleNamespace(local_name="WxPaper", service_uuids=[P.SERVICE_UUID])
            other = types.SimpleNamespace(address="11:22:33:44:55:66", name="Other")
            asyncio.get_running_loop().call_soon(self.cb, other, types.SimpleNamespace(local_name="Other", service_uuids=[]))
            asyncio.get_running_loop().call_soon(self.cb, ble_dev, adv)
            return self

        async def __aexit__(self, *a):
            return False

    w = WxBle(BleSettings(), lambda st: pl, connector=connector, scanner_factory=FakeScanner, poll_s=0.05)
    res = run(asyncio.wait_for(w.run(once=True), 5))
    assert res.outcome == "uploaded" and len(devices) == 1
    # debounce: same device ignored right after success
    w._on_adv(types.SimpleNamespace(address="aa:bb:cc:dd:ee:ff"), types.SimpleNamespace(local_name="WxPaper", service_uuids=[]))
    assert w._queue.empty()


def test_daemon_survives_scanner_error():
    calls = []

    class Boom:
        def __init__(self, cb):
            calls.append(1)

        async def __aenter__(self):
            raise OSError("adapter missing")

        async def __aexit__(self, *a):
            return False

    async def go():
        w = WxBle(BleSettings(), lambda st: None, scanner_factory=Boom, poll_s=0.05)
        t = asyncio.create_task(w.run())
        await asyncio.sleep(0.2)
        w.stop()
        await asyncio.wait_for(t, 10)

    run(go())
    assert calls
