# BLE Protocol v1 (device ⇄ host)

Single source of truth for `firmware/src/wx_service.c` and `host/wxd/protocol.py` / `host/wxd/ble.py`.
All multi-byte integers are **little-endian**. Version byte = `1`.

## Roles & timing

- Device: BLE **peripheral**, GATT server. Host: **central**, GATT client.
- Device sleeps; every poll interval (default 600 s) it advertises **connectable, 100 ms interval, for ≤ 30 s**.
- Host scans continuously (passive is enough; name + UUID are in the primary ADV PDU), connects as soon as it sees the device.
- After connect the host should finish within **20 s**. The device disconnects on: successful `COMMIT`, `NO_CHANGE`, or 20 s with no GATT activity.
- Device requests connection interval 7.5–15 ms, latency 0, supervision timeout 4 s, 2M PHY, data length 251. The host should exchange MTU (BlueZ does this automatically; aim for 247).

## Advertising

| Field | Value |
|-------|-------|
| Flags | LE General Discoverable, BR/EDR not supported |
| Complete local name | `WxPaper` |
| Complete list of 128-bit service UUIDs | `7a1e0001-5c3b-4f2a-9e0d-2b6f8c4d1a90` |

## GATT service `7a1e0001-5c3b-4f2a-9e0d-2b6f8c4d1a90`

| Characteristic | UUID | Properties |
|----------------|------|------------|
| Status  | `7a1e0002-5c3b-4f2a-9e0d-2b6f8c4d1a90` | Read |
| Control | `7a1e0003-5c3b-4f2a-9e0d-2b6f8c4d1a90` | Write (with response) |
| Data    | `7a1e0004-5c3b-4f2a-9e0d-2b6f8c4d1a90` | Write Without Response, Write |

### Status (read, 24 bytes)

| Off | Type | Field | Notes |
|-----|------|-------|-------|
| 0 | u8  | proto_version | `1` |
| 1 | u8  | fw_major | |
| 2 | u8  | fw_minor | |
| 3 | u8  | fw_patch | |
| 4 | u16 | battery_mv | VDDH (battery) in mV, 0 = unknown |
| 6 | u8  | battery_pct | 0–100 LiPo estimate, 255 = unknown |
| 7 | u8  | flags | bit0 `STALE_SHOWN`, bit1 `LOW_BATT`, bit2 `NO_IMAGE` (never received one), bit3 `REFRESH_FAILED` (last BUSY timeout), bit4 `CHARGING`/USB present (if detectable) |
| 8 | u32 | displayed_crc | CRC32 of host image on the panel; **0** if none or if the device has drawn an overlay over it |
| 12 | u32 | uptime_s | |
| 16 | u32 | poll_interval_s | current interval |
| 20 | u32 | refresh_count | full refreshes since boot |

Host rule: if `displayed_crc == crc32(new_payload)` → send `NO_CHANGE`; otherwise upload.

### Control (write with response)

First byte is the opcode.

| Op | Name | Args (after opcode) | Effect |
|----|------|---------------------|--------|
| `0x01` | BEGIN | u32 total_len, u32 crc32, u8 format, u32 unix_time | Start an image transfer. `total_len` must be 9472 and `format` must be 0 (raw planes). Resets receive offset to 0. |
| `0x02` | COMMIT | — | Validate length + CRC. On success: device disconnects, then does a full panel refresh. |
| `0x03` | NO_CHANGE | u32 unix_time | Nothing to draw. Device records contact time and disconnects. |
| `0x04` | SET_INTERVAL | u32 seconds | 60 ≤ s ≤ 86400 (60 s is for debugging; production 600). Takes effect from the next sleep. Not persisted across reset in v1. |

Ordering: `SET_INTERVAL` may be written at any point before the final op (the host sends it right after reading Status); the device drops the link ~150 ms after a successful `COMMIT`/`NO_CHANGE`, so anything after the final op is lost. `displayed_crc == 0` means "no known image" — the host never sends `NO_CHANGE` against 0.

`unix_time` = host's UTC seconds; the device uses it only to track "time since last contact" (stale detection is uptime-based, so it doesn't need a calendar).

Errors are returned as ATT application errors on the Control write:

| Code | Meaning |
|------|---------|
| `0x80` | Bad length (BEGIN args, total_len ≠ 9472, or COMMIT with received bytes ≠ total_len) |
| `0x81` | CRC mismatch on COMMIT |
| `0x82` | COMMIT/Data without a prior BEGIN |
| `0x83` | Unknown opcode / bad format / value out of range |
| `0x84` | Data written out of order (sequence error seen since BEGIN; reported at COMMIT) |

On any error the host may retry once from BEGIN, using write-*with*-response for the Data chunks. Data writes never fail, with or without response: problems (no BEGIN, out-of-order offset, overflow past 9472) are latched and reported by COMMIT as `0x82` / `0x84`.

BlueZ/bleak surfaces these as a D-Bus error containing `ATT error: 0x8N` (host maps it in `ble.map_bleak_exception`; to be confirmed on hardware). A device-initiated disconnect racing the COMMIT response is treated as success by the host; if the commit had actually failed, the next poll's `displayed_crc` mismatch causes a resend.

### Data (write without response)

`u16 offset` + `payload`; the whole write is ≤ ATT_MTU − 3 bytes (so payload ≤ ATT_MTU − 5: 242 B at MTU 247 → 40 writes). Chunks must arrive in order: `offset` must equal the number of bytes received so far since BEGIN; otherwise the device latches error `0x84` and ignores further data until the next BEGIN.

## Image payload (format 0, 9472 bytes)

Two 1-bpp planes in **panel-native orientation**, concatenated: `BW[4736] || RED[4736]`.

- Native panel geometry: 128 px wide (source lines, "x") × 296 px tall (gate lines, "y").
- Each plane: 296 rows × 16 bytes. Row `y` starts at byte `y * 16`. In each byte, **bit 7 (MSB) is the leftmost pixel** (`x = 8 * byte_index + (7 - bit)`).
- BW plane: `1 = white`, `0 = black`.
- RED plane: `1 = red`, `0 = not red`. Red wins over BW where both are set.
- The host renders landscape 296x128 and rotates into native orientation (`rotate` = 90 or 270, i.e. connector left/right). The firmware's `CONFIG_WX_ROTATION` must match, because the device draws its own splash/OFFLINE/LOW BATTERY overlays in landscape.
  - rotate=90 (landscape turned 90° CCW): `nx = y`, `ny = 295 - x` → native (0,0) = landscape top-right.
  - rotate=270 (90° CW): `nx = 127 - y`, `ny = x` → native (0,0) = landscape bottom-left.
- Payload red polarity is always semantic (`1 = red`). If a panel needs inverted RED RAM, set firmware `CONFIG_WX_EPD_RED_INVERT=y`; leave host `red_invert = false`.
- CRC: CRC-32/IEEE (same as Python `zlib.crc32`, Zephyr `crc32_ieee()`) over all 9472 bytes.

The firmware writes the BW plane verbatim to SSD1680 RAM `0x24` and the RED plane to RAM `0x26` with data-entry mode `0x03` (X inc, Y inc), RAM X window 0..15, Y window 0..295.

## Sequence

```
device                             host
  | -- ADV_IND (WxPaper, UUID) -->  |
  | <------- CONNECT_IND ---------  |
  | <-- MTU / DLE / PHY exchange -> |
  | <------ read Status ----------  |
  |                                 | render(model, status) -> payload, crc
  |                                 | if crc == displayed_crc:
  | <---- write Control NO_CHANGE - |
  | ---------- disconnect ------->  |
  |                                 | else:
  | <---- write Control BEGIN ----  |
  | <---- Data chunks (WwoR) -----  |  ~40 x 244 B
  | <---- write Control COMMIT ---  |
  | ---------- disconnect ------->  |
  | (refresh panel ~15-25 s)        |
```
