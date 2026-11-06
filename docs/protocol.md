# BLE Protocol v2 (device ⇄ host)

Single source of truth for `firmware/src/wx_service.c` and `host/wxd/protocol.py` / `host/wxd/ble.py`.
All multi-byte integers are **little-endian**. Version byte = `2`.

**v2 (2026-11-06)** adds image *slots*: the device stores one image per view (slot 0 = weather, slot 1 = tide) and its view buttons switch between them locally. Changes from v1: Status is 32 bytes (adds `slot1_crc`, `view`, `num_slots`), BEGIN takes a slot byte, COMMIT no longer ends the session, and opcode `0x03` (v1 `NO_CHANGE`) is now `DONE`, which ends every session.

## Roles & timing

- Device: BLE **peripheral**, GATT server. Host: **central**, GATT client.
- Device sleeps; every poll interval (default 600 s) it advertises **connectable, 100 ms interval, for ≤ 30 s**.
- Host scans continuously (passive is enough; name + UUID are in the primary ADV PDU), connects as soon as it sees the device.
- After connect the host should finish within **20 s** of inactivity per step. The device disconnects on `DONE` or after 20 s with no GATT activity.
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

### Status (read, 32 bytes)

| Off | Type | Field | Notes |
|-----|------|-------|-------|
| 0 | u8  | proto_version | `2` |
| 1 | u8  | fw_major | |
| 2 | u8  | fw_minor | |
| 3 | u8  | fw_patch | |
| 4 | u16 | battery_mv | VDDH (battery) in mV, 0 = unknown |
| 6 | u8  | battery_pct | 0–100 LiPo estimate, 255 = unknown |
| 7 | u8  | flags | bit0 `STALE_SHOWN`, bit1 `LOW_BATT`, bit2 `NO_IMAGE` (never received one), bit3 `REFRESH_FAILED` (last BUSY timeout), bit4 `CHARGING`/USB present (if detectable) |
| 8 | u32 | slot0_crc | CRC32 of the stored **weather** image; **0** if none (v1: `displayed_crc`) |
| 12 | u32 | uptime_s | |
| 16 | u32 | poll_interval_s | current interval |
| 20 | u32 | refresh_count | full refreshes since boot |
| 24 | u32 | slot1_crc | CRC32 of the stored **tide** image; **0** if none |
| 28 | u8  | view | slot currently selected for display (0 weather, 1 tide) |
| 29 | u8  | num_slots | `2` |
| 30 | u16 | reserved | 0 |

Host rule: for each slot, upload only if `slotN_crc != crc32(new_payload)`; then send `DONE`. The CRCs describe the stored images, not the panel: overlays (OFFLINE, LOW BATTERY) are composed on the device at draw time and never change a slot, so the host never needs to resend to clear one. A slot the host has no data for (e.g. tide fetch failed) is simply not sent; the device keeps its previous image.

### Control (write with response)

First byte is the opcode.

| Op | Name | Args (after opcode) | Effect |
|----|------|---------------------|--------|
| `0x01` | BEGIN | u32 total_len, u32 crc32, u8 format, u32 unix_time, u8 slot | Start an image transfer into `slot` (0 weather, 1 tide; omitted = 0). `total_len` must be 9472 and `format` must be 0 (raw planes). Resets receive offset to 0. |
| `0x02` | COMMIT | — | Validate length + CRC. On success the image replaces the slot's stored image; the session continues (next BEGIN or DONE). |
| `0x03` | DONE | u32 unix_time | End of session (v1 name `NO_CHANGE`). Device records contact time and disconnects; then it refreshes the panel if the shown view's composed image changed. |
| `0x04` | SET_INTERVAL | u32 seconds | 60 ≤ s ≤ 86400 (60 s is for debugging; production 600). Takes effect from the next sleep. Not persisted across reset in v1. |

Ordering: `SET_INTERVAL` may be written at any point before `DONE` (the host sends it right after reading Status); the device drops the link ~150 ms after `DONE`, so anything after it is lost. `slotN_crc == 0` means "no image" — the host always uploads against 0. If the link drops after a COMMIT but before DONE, the committed slot is kept and the device still redraws if needed (it just doesn't count it as host contact).

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

BlueZ/bleak surfaces these as a D-Bus error containing `ATT error: 0x8N` (host maps it in `ble.map_bleak_exception`; to be confirmed on hardware). A device-initiated disconnect racing the DONE response is treated as success by the host (all slots were committed and acked before it).

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
  |                                 | render weather + tide -> payloads, crcs
  |                                 | for each slot whose crc != slotN_crc:
  | <---- BEGIN (slot) -----------  |
  | <---- Data chunks (WwoR) -----  |    ~40 x 244 B
  | <---- COMMIT -----------------  |
  | <---- DONE -------------------  |
  | ---------- disconnect ------->  |
  | compose(view) + overlays; if CRC differs from the panel: refresh (~20 s)
```
