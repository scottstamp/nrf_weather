"""BLE protocol v1 constants and codecs. Spec: docs/protocol.md (keep in sync with firmware)."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

PROTO_VERSION = 1

DEVICE_NAME = "WxPaper"
SERVICE_UUID = "7a1e0001-5c3b-4f2a-9e0d-2b6f8c4d1a90"
STATUS_UUID = "7a1e0002-5c3b-4f2a-9e0d-2b6f8c4d1a90"
CONTROL_UUID = "7a1e0003-5c3b-4f2a-9e0d-2b6f8c4d1a90"
DATA_UUID = "7a1e0004-5c3b-4f2a-9e0d-2b6f8c4d1a90"

# Panel-native geometry (see docs/protocol.md "Image payload").
NATIVE_W = 128
NATIVE_H = 296
ROW_BYTES = NATIVE_W // 8
PLANE_BYTES = ROW_BYTES * NATIVE_H  # 4736
PAYLOAD_BYTES = 2 * PLANE_BYTES  # 9472
FORMAT_RAW = 0

MIN_INTERVAL_S = 60  # debug-friendly floor; production default is 1800
MAX_INTERVAL_S = 86400

OP_BEGIN = 0x01
OP_COMMIT = 0x02
OP_NO_CHANGE = 0x03
OP_SET_INTERVAL = 0x04

ERR_BAD_LENGTH = 0x80
ERR_CRC = 0x81
ERR_NO_BEGIN = 0x82
ERR_BAD_VALUE = 0x83
ERR_SEQUENCE = 0x84

FLAG_STALE_SHOWN = 1 << 0
FLAG_LOW_BATT = 1 << 1
FLAG_NO_IMAGE = 1 << 2
FLAG_REFRESH_FAILED = 1 << 3
FLAG_CHARGING = 1 << 4

_STATUS_FMT = "<BBBBHBBIIII"
STATUS_LEN = struct.calcsize(_STATUS_FMT)  # 24


@dataclass
class DeviceStatus:
    proto_version: int
    fw_version: tuple[int, int, int]
    battery_mv: int  # 0 = unknown
    battery_pct: int | None  # None = unknown
    flags: int
    displayed_crc: int
    uptime_s: int
    poll_interval_s: int
    refresh_count: int

    @property
    def low_battery(self) -> bool:
        return bool(self.flags & FLAG_LOW_BATT)

    @classmethod
    def parse(cls, data: bytes) -> "DeviceStatus":
        if len(data) < STATUS_LEN:
            raise ValueError(f"status too short: {len(data)} < {STATUS_LEN}")
        (ver, fmaj, fmin, fpat, mv, pct, flags, crc, up, interval, refreshes) = struct.unpack_from(
            _STATUS_FMT, data
        )
        return cls(
            proto_version=ver,
            fw_version=(fmaj, fmin, fpat),
            battery_mv=mv,
            battery_pct=None if pct == 255 else pct,
            flags=flags,
            displayed_crc=crc,
            uptime_s=up,
            poll_interval_s=interval,
            refresh_count=refreshes,
        )

    def pack(self) -> bytes:
        """Inverse of parse (used by tests / fake device)."""
        return struct.pack(
            _STATUS_FMT,
            self.proto_version,
            *self.fw_version,
            self.battery_mv,
            255 if self.battery_pct is None else self.battery_pct,
            self.flags,
            self.displayed_crc,
            self.uptime_s,
            self.poll_interval_s,
            self.refresh_count,
        )


def crc32(payload: bytes) -> int:
    return zlib.crc32(payload) & 0xFFFFFFFF


def op_begin(crc: int, unix_time: int, total_len: int = PAYLOAD_BYTES, fmt: int = FORMAT_RAW) -> bytes:
    return struct.pack("<BIIBI", OP_BEGIN, total_len, crc, fmt, unix_time)


def op_commit() -> bytes:
    return bytes([OP_COMMIT])


def op_no_change(unix_time: int) -> bytes:
    return struct.pack("<BI", OP_NO_CHANGE, unix_time)


def op_set_interval(seconds: int) -> bytes:
    if not MIN_INTERVAL_S <= seconds <= MAX_INTERVAL_S:
        raise ValueError(f"interval must be {MIN_INTERVAL_S}..{MAX_INTERVAL_S} s")
    return struct.pack("<BI", OP_SET_INTERVAL, seconds)


def data_chunks(payload: bytes, max_chunk: int) -> list[bytes]:
    """Split payload into Data writes: u16 offset + bytes. max_chunk = ATT_MTU - 3 (bytes per write)."""
    step = max_chunk - 2
    if step <= 0:
        raise ValueError("max_chunk too small")
    return [struct.pack("<H", off) + payload[off : off + step] for off in range(0, len(payload), step)]
