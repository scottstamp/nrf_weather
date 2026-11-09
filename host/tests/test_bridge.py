import json
import struct
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace

import pytest

from wxd import protocol as P
from wxd.bridge import MAGIC, BadRequest, BridgeServer, NoData, build_session_response

PL0 = bytes([1]) * P.PAYLOAD_BYTES
PL1 = bytes([2]) * P.PAYLOAD_BYTES


def status(crc0=0, crc1=0, num_slots=2, ver=P.PROTO_VERSION) -> bytes:
    return P.DeviceStatus(ver, (1, 0, 0), 4000, 80, 0, crc0, 10, 600, 1, crc1, 0, num_slots).pack()


def parse(resp: bytes):
    assert resp[:4] == MAGIC
    interval, count = struct.unpack_from("<IB", resp, 4)
    off, slots = 9, {}
    for _ in range(count):
        slot, crc = struct.unpack_from("<BI", resp, off)
        pl = resp[off + 5 : off + 5 + P.PAYLOAD_BYTES]
        assert P.crc32(pl) == crc
        slots[slot] = pl
        off += 5 + P.PAYLOAD_BYTES
    assert off == len(resp)
    return interval, slots


def test_both_differ():
    iv, slots = parse(build_session_response(status(), {0: PL0, 1: PL1}, 600))
    assert iv == 600 and slots == {0: PL0, 1: PL1}


def test_one_matches():
    _, slots = parse(build_session_response(status(crc0=P.crc32(PL0)), {0: PL0, 1: PL1}, None))
    assert slots == {1: PL1}


def test_none_differ():
    iv, slots = parse(build_session_response(status(P.crc32(PL0), P.crc32(PL1)), {0: PL0, 1: PL1}, None))
    assert iv == 0 and slots == {}


def test_slot_beyond_num_slots():
    _, slots = parse(build_session_response(status(num_slots=1), {0: PL0, 1: PL1}, 600))
    assert slots == {0: PL0}


def test_missing_and_wrong_size_skipped():
    _, slots = parse(build_session_response(status(), {0: PL0, 1: b"short"}, 1))
    assert slots == {0: PL0}
    _, slots = parse(build_session_response(status(), {0: None, 1: PL1}, 1))
    assert slots == {1: PL1}


def test_all_none_is_nodata():
    with pytest.raises(NoData):
        build_session_response(status(), {0: None, 1: None}, 600)


def test_wrong_proto_and_short():
    with pytest.raises(BadRequest):
        build_session_response(status(ver=P.PROTO_VERSION + 1), {0: PL0}, 600)
    with pytest.raises(BadRequest):
        build_session_response(status()[:23], {0: PL0}, 600)


def test_v1_status_accepted():
    _, slots = parse(build_session_response(status()[: P.STATUS_LEN_V1], {0: PL0, 1: PL1}, 600))
    assert slots == {0: PL0}  # v1 has one slot


class FakeState:
    def __init__(self, payloads):
        self.payloads = payloads
        self.model = SimpleNamespace(fetched_at=datetime(2026, 1, 2, 3, 4, 5))
        self.tides = None
        self.seen = []

    def payload_sync(self, st):
        self.seen.append(st)
        return self.payloads


def post(url, data, ctype="application/octet-stream", headers=None):
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": ctype, **(headers or {})})
    return urllib.request.urlopen(req, timeout=5)


@pytest.fixture
def server():
    state = FakeState({0: PL0, 1: PL1})
    srv = BridgeServer(state, "127.0.0.1", 0, 600)
    srv.start()
    yield srv, state, f"http://127.0.0.1:{srv.port}"
    srv.stop()


def test_end_to_end(server):
    srv, state, base = server
    with post(base + "/v1/session", status(crc0=P.crc32(PL0)), headers={"X-Wx-Device": "AA:BB"}) as r:
        assert r.status == 200
        iv, slots = parse(r.read())
    assert iv == 600 and slots == {1: PL1}
    assert state.seen[0].battery_mv == 4000

    res = {"device": "AA:BB", "outcome": "uploaded", "slots": [1], "rssi": -60, "ms": 100, "batt_mv": 4000, "error": ""}
    with post(base + "/v1/result", json.dumps(res).encode(), "application/json") as r:
        assert r.status == 204

    with urllib.request.urlopen(base + "/v1/health", timeout=5) as r:
        h = json.loads(r.read())
    assert h["last_result"] == res
    assert h["weather_fetched_at"].startswith("2026-01-02")
    assert h["tides_fetched_at"] is None


def test_errors(server):
    _, state, base = server
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/v1/session", b"short")
    assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/v1/session", b"x" * 5000)
    assert e.value.code == 413
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/v1/result", b"not json")
    assert e.value.code == 400
    state.payloads = {0: None, 1: None}
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/v1/session", status())
    assert e.value.code == 503
