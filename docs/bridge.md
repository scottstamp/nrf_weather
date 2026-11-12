# Wi-Fi ⇄ BLE bridge (ESP32-C3 / C6)

When the host PC is out of BLE range of the display, a small **ESP32-C3 bridge** (any C3 board with 4 MB flash, e.g. a "C3 SuperMini"; an ESP32-C6 such as the nanoESP32-C6 works too, same code) sits near the display. It does all BLE work, and gets the images from the host over Wi-Fi/LAN:

```
 ┌──── host PC ("tiny", any room) ────┐   HTTP (LAN)    ┌──── ESP32-C3 bridge ────┐   BLE   ┌── WxPaper ──┐
 │ wxd serve                          │◀──────────────▶│ scan, connect, read      │◀──────▶│ nRF52840 +  │
 │  weather + tide fetch (as before)  │  POST status    │ Status, upload slots,    │        │ e-paper     │
 │  render both views                 │  ← slot images  │ DONE                     │        └─────────────┘
 └────────────────────────────────────┘                 └─────────────────────────┘
```

- The host keeps **all** fetching, rendering and CRC logic (one renderer; layout changes stay in Python).
- The bridge is a radio proxy. It holds no weather logic and no cached images.
- The BLE protocol to the display is unchanged (`protocol.md` v2). Display firmware needs no change.

## HTTP API (host side, `wxd serve`)

Plain HTTP on the LAN, default port **8787**. Binary bodies, all integers **little-endian**. No authentication: it only renders public weather into images (bind it to the LAN interface, not the internet).

### `POST /v1/session`

Called by the bridge once per display contact, right after it has read the display's Status characteristic.

Request: `Content-Type: application/octet-stream`. Body = the raw Status value as read from the display (32 bytes, protocol v2). A display reporting another protocol version gets `400`. Optional header `X-Wx-Device: <BLE address>` (for logs).

Response `200`, `Content-Type: application/octet-stream`:

| Off | Type | Field | Notes |
|-----|------|-------|-------|
| 0 | 4 B | magic | ASCII `WXB1` |
| 4 | u32 | interval_s | Poll interval the display should use (`[ble] poll_interval_s`); 0 = leave as is. The bridge sends `SET_INTERVAL` only if it differs from Status `poll_interval_s`. |
| 8 | u8 | count | Number of slot images that follow (0, 1 or 2) |
| 9 | — | slots | `count` × { u8 slot, u32 crc32, 9472 B payload } |

The host includes a slot only if its freshly rendered image's CRC differs from the slot CRC in the posted Status (same rule as the direct BLE daemon). A slot with no data (fetch failed, no cache) is left out. `count = 0` means "nothing to upload": the bridge just sends `DONE`.

Errors: `400` bad body (too short, wrong proto version), `503` host has nothing at all to serve yet (no weather and no tides; the bridge then disconnects **without** DONE, so the display counts it as no contact). Any non-200, timeout, or connection failure is handled the same way as 503.

### `POST /v1/result`

Optional; called by the bridge after each session so the outcome shows up in the host's journal.

Request: `Content-Type: application/json`, e.g.

```json
{"device": "EB:11:79:5D:39:62", "outcome": "uploaded", "slots": [0, 1], "rssi": -61,
 "ms": 2350, "batt_mv": 4050, "error": ""}
```

`outcome` is `uploaded`, `no_change`, or `failed` (with `error` text). Response `204`. The host logs one line per result.

### `GET /v1/health`

`200` with a small JSON: `{"weather_fetched_at": "...", "tides_fetched_at": "...", "last_result": {...}}`. Handy for `curl`.

## Bridge session (ESP32 firmware, `bridge/`)

1. Passive scan continuously; match service UUID `7a1e0001-…` or name `WxPaper` (optional pinned address).
2. Connect, exchange MTU (247), discover the service and its 3 characteristics.
3. Read Status → `POST /v1/session` (timeout 8 s).
4. If the response's `interval_s` ≠ Status `poll_interval_s` → `SET_INTERVAL`.
5. For each returned slot: `BEGIN(slot)` → Data chunks (write without response, MTU−3 bytes) → `COMMIT`; on an ATT error, retry once using write-with-response chunks.
6. `DONE` → the display disconnects (a disconnect during DONE counts as success).
7. `POST /v1/result`; ignore the same display for 20 s (stale advertising reports).

On any failure (HTTP or BLE) the bridge disconnects without `DONE`; the display's own retry (5 min) and stale handling (OFFLINE after 3 h) apply as with the PC daemon.

## Build and flash

Toolchain: ESP-IDF v6.1 (`source ~/.espressif/tools/activate_idf_v6.1.sh`).

```sh
cd bridge
cp sdkconfig.defaults.local.example sdkconfig.defaults.local   # Wi-Fi SSID/password, host URL (git-ignored)
rm -f sdkconfig && idf.py build                                 # ESP32-C3 (default); re-run `rm -f sdkconfig` after editing the .local file
# for an ESP32-C6 instead: rm -rf build sdkconfig && idf.py set-target esp32c6 && idf.py build
idf.py -p /dev/serial/by-id/<the board> flash monitor
```

- USB: most C3 boards (e.g. SuperMini) have one USB-C wired to the chip's own USB Serial/JTAG; it flashes and shows the log (secondary console, `/dev/ttyACM*`, "Espressif USB JTAG/serial debug unit"). Boards with a CH340/CP210x UART chip show up as `/dev/ttyUSB*` (UART0 console). The nanoESP32-C6 has both ports. Use `/dev/serial/by-id/...` so you don't hit the nRF display's `/dev/ttyACM0` by mistake.
- If flashing fails to connect, hold BOOT (GPIO9), tap RESET, release BOOT, and flash again.
- Antenna: cheap C3 boards often have a small PCB antenna. Keep the bridge in the same room as the display, and check the `rssi` in the session log (better than about −80 dBm is fine).
- Power: any USB charger. The bridge scans all the time (~30–40 mA), so it can't run on a battery.
- Placement: within easy BLE range of the display (same room) and in Wi-Fi range.
- Host: run `wxd serve` (not `wxd run`) on the PC; see `host.md` "Bridge mode". `CONFIG_WXB_HOST_URL` must resolve from the ESP32. Use the PC's IP if your router doesn't resolve its hostname.
- Partition table: single app, 1.5 MB (the image is ~1.0 MB on the C3, ~1.3 MB on the C6). Needs a 4 MB flash board; 2 MB C3 variants are too small.

Log of a good session (115200 baud):

```
wx_ble: session EB:11:79:5D:39:62 rssi -58
wx_ble: slot 0: 40 writes of 244 B, rsp=0, 1900 ms, commit rc=0
wx_ble: session EB:11:79:5D:39:62: uploaded slots=[0] 3100 ms
```

Status: firmware builds clean, and the host side was tested end-to-end with a fake status. **Not yet tested on hardware** (ESP32 ⇄ display). Builds checked for esp32c3 (default) and esp32c6.
