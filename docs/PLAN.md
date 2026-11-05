# nRF Weather Display — Project Plan

Battery-powered windowsill weather display for **A1K 1E1 (St. John's area, Newfoundland)**.

- MCU: nRF52840 on a **nice!nano / ProMicro nRF52840** board (Adafruit UF2 bootloader, S140 v6 layout).
- Display: 2.9" black/white/red e-paper, 128x296, part **YRD0290RWS800F6**, controller **SSD1680(Z8)**, on an SPI breakout (VCC, GND, DIN, CLK, CS, DC, RST, BUSY).
- Firmware: Zephyr via nRF Connect SDK **v3.4.2** (Zephyr 4.4.2), board target `promicro_nrf52840/nrf52840/uf2`.
- Host: a Linux PC running a Python BLE daemon (BlueZ + `bleak`) that fetches weather and pushes it to the device.

## 1. Architecture (decided)

```
 ┌──────────── Linux host (always on) ─────────────┐         ┌──────── nice!nano + e-paper ────────┐
 │ weather.py  ── ECCC (primary) / Open-Meteo (fb) │         │ sleeps in System ON idle (~3 µA)    │
 │     │ WeatherModel                               │  BLE    │ every 30 min: wake, advertise ≤30 s │
 │ render.py  ── 296x128 B/W/R image (PIL)         │◀──────▶│ host connects, reads Status,        │
 │     │ two 1-bpp planes (BW + RED), CRC32         │         │ writes image (or NO_CHANGE)         │
 │ ble.py     ── passive scan, connect, upload     │         │ disconnect → power panel, refresh,  │
 │ wxd main   ── config, cache, systemd service    │         │ deep-sleep panel, cut panel power   │
 └─────────────────────────────────────────────────┘         └─────────────────────────────────────┘
```

### Key decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Host renders the full image**; device is a "smart frame". | Rich fonts/icons without bloating firmware; layout iterated on PC with PNG previews (no hardware needed); firmware small and robust. 9472 bytes per frame is < 1 s over BLE. |
| D2 | **Device = BLE peripheral that advertises only when it wants data** ("poll" = 30 s advertising window every 30 min). Host scans continuously. | Advertising is far cheaper than scanning on the battery side; the mains-powered host does the expensive listening. |
| D3 | **Custom SSD1680 tri-color driver** in the app instead of Zephyr's `ssd16xx`. | Zephyr's `ssd16xx` driver is mono-only (`PIXEL_FORMAT_MONO10`) and uses the RED RAM (0x26) as the partial-refresh "old" buffer, so it cannot drive the red plane. |
| D4 | **Panel power is switched off between updates** using the nice!nano's VCC switch (P0.13, high = on), with all panel GPIOs set to disconnected while off. | Removes breakout quiescent current (booster, regulator, level shifter); e-paper is bistable. |
| D5 | Image only re-sent/refreshed when its CRC32 differs from what the device shows. Image shows **observation time** (hourly), not fetch time. | A tri-color full refresh is ~15–25 s of panel current and visible flashing; avoid needless refreshes (~24/day max). |
| D6 | Weather source: **Environment and Climate Change Canada (ECCC)** official data (St. John's, NL citypage / CYYT obs + warnings), **Open-Meteo** as fallback. | ECCC is authoritative for NL incl. warnings; Open-Meteo is free, keyless, reliable JSON. Details in `weather-sources.md`. |
| D7 | Release build: no UART/USB/logging; debug build overlay enables USB CDC logging. | Logging/USB keep clocks & peripherals alive. |
| D8 | v1 has **no BLE bonding**; integrity via CRC32. Authentication (bonding or HMAC) is a later hardening phase. | BlueZ Just Works pairing from a headless daemon is fragile; threat (neighbor writes a bogus image) is low. Revisit in Phase 6. |

## 2. Display layout (landscape 296x128, readable at ~1–2 m)

Pixel pitch ≈ 0.227 mm (active area ≈ 67 x 29 mm). Text < 14 px is not readable from a few feet, so the layout keeps few, large elements:

```
┌─────────────────────────────────────────────────┬──────────────┐
│  [ICON 64px]   -12°            (huge, ~64 px)   │ TONIGHT  ☁ -15│
│                feels -20  ·  NW 40 g60          │ FRI      ❄  -8│
│  Light Snow                                     │ FRI NT   ☁ -14│
├─────────────────────────────────────────────────┴──────────────┤
│ ▲ WIND WARNING (red banner, only when an alert is active)  14:00│
└────────────────────────────────────────────────────────────────┘
```
Red is used for: active warnings, the low-battery icon, and optionally high-impact values (gusts ≥ 70 km/h, POP ≥ 70 %). Final layout is owned by `host/wxd/render.py`; previews committed to `docs/img/`.

## 3. Power budget (target ≥ 6 months on a 1000 mAh LiPo)

| State | Current | Duration / day | Charge / day |
|-------|---------|----------------|--------------|
| System ON idle, RTC, RAM retained, panel unpowered | ~3–5 µA (nRF) + board LDO/charger quiescent | 24 h | ~0.1 mAh |
| Advertising 100 ms interval, until connect (~1–3 s typ., ≤ 30 s) | ~ 0.15 mA avg in window | 48 × ~3 s | ~0.01 mAh |
| Connection + 9.5 kB transfer (2M PHY, DLE, 7.5–15 ms CI) | ~3–5 mA | ≤ 24 × 1 s | ~0.03 mAh |
| Panel full B/W/R refresh | ~5–8 mA (panel) + MCU idle | ≤ 24 × ~20 s | ~0.9 mAh |
| **Total** | | | **≈ 1–1.2 mAh/day → 800+ days theoretical; LiPo self-discharge dominates** |

Panel refresh dominates → D5 matters most. Measure with a PPK2 in Phase 5 and record in `power.md`.

## 4. Work breakdown & phases

| Phase | Work | Owner | Parallel? |
|-------|------|-------|-----------|
| 0 | Repo scaffold, toolchain env script (`scripts/ncs-env.sh`), plan, contracts (`protocol.md`, `host/wxd/model.py`) | lead | — |
| 1a | Weather source research + `host/wxd/weather.py` (ECCC + Open-Meteo → `WeatherModel`), `docs/weather-sources.md` | Sonnet agent | ✔ |
| 1b | Renderer `host/wxd/render.py` + icons + plane packing + PNG previews, `docs/layout.md` | Sonnet agent | ✔ |
| 1c | Host BLE client `host/wxd/ble.py`, daemon main loop, config, systemd unit | Sonnet agent | ✔ |
| 1d | Firmware: SSD1680 tri-color driver, BLE peripheral + GATT service, power/sleep state machine, battery ADC, stale overlay | lead | ✔ |
| 2 | Integration review: protocol conformance (firmware ⇄ ble.py), end-to-end dry run with a fake device, docs pass | lead | — |
| 3 | Hardware bring-up (needs board): flash UF2, verify panel orientation/colors, BUSY timing, BLE link with host | user + lead | — |
| 4 | Tuning: layout readability at distance, cold-temperature refresh behaviour, alert rendering | — | — |
| 5 | Power measurement (PPK2), DCDC enablement if inductor present, sleep current verification | — | — |
| 6 | Hardening: authentication (bonding or HMAC), OTA/DFU over BLE (optional), host watchdog | — | — |

### Status (2026-11-05)

- Phase 0 ✔. Phase 1a–1d ✔ — all code written; host tests pass (`pytest host/tests`), firmware release + debug builds clean.
- Live check ✔: `wxd preview` renders real ECCC data for nl-24.
- Phase 2: firmware reviewed against protocol / FakeDevice (findings applied, see git log). No hardware yet, so the BLE link, panel sequence and power are **unverified on a real device**.
- Location finding: A1K = **Torbay**; ECCC has no Torbay page → `nl-24` St. John's (CYYT obs); Open-Meteo fallback uses 47.65, −52.73.
- Next: Phase 3 bring-up checklist below.

### Phase 3 bring-up checklist

1. Wire per `hardware.md`; flash the **debug** UF2; open `/dev/ttyACM0`.
2. Splash appears upright? If upside-down, flip `CONFIG_WX_ROTATION` + host `rotate`. Red bar at top red (not inverted)? Else `CONFIG_WX_EPD_RED_INVERT=y`.
3. Note "refresh done in N ms" in log (expect 15–25 s). BUSY timeout → check BUSY wiring/polarity.
4. On host: `wxd scan` sees `WxPaper`; `wxd -v push` completes; log shows COMMIT ok; panel shows weather.
5. Leave `wxd run` going; confirm NO_CHANGE between hourly observation updates.
6. Stop host for > 20 min (debug stale threshold) → OFFLINE badge appears; restart → cleared.
7. Flash release UF2, measure sleep current (target < 10 µA board-level, panel off).

## 5. Firmware design summary (details in `firmware.md`)

State machine in the main thread (no extra threads besides BT host):

1. **BOOT**: init GPIO (panel off), settings, BT enable. Draw "waiting for data" splash on first boot.
2. **POLL**: read battery (SAADC VDDH/5), start connectable advertising (100 ms, name `WxPaper`, 128-bit service UUID). Wait ≤ 30 s for connection + transfer complete (semaphore).
3. **RX**: host reads Status, then either `NO_CHANGE` or `BEGIN` → image chunks → `COMMIT` (CRC32 check). Device disconnects after the final op (or on 20 s inactivity).
4. **REFRESH** (only if a new image was committed, or a stale/low-battery overlay must appear): panel power on → HW reset → init → write BW RAM (0x24) + RED RAM (0x26) → full update (0x22=0xF7, 0x20) → wait BUSY (timeout 40 s) → deep sleep (0x10=0x01) → power off, pins disconnected.
5. **SLEEP**: `k_sleep()` until next poll. Interval 30 min (host can override); after a failed poll, one retry after 5 min.
   - After `STALE_AFTER` (3 h) without host contact: overlay a red "no data" badge and refresh once.
   - Battery < 3.40 V: add low-battery badge; < 3.30 V: stop refreshing, poll every 2 h.

## 6. Host daemon summary (details in `host.md`)

- Python ≥ 3.11, deps: `bleak`, `Pillow`. Stdlib for HTTP/XML/JSON.
- Weather cache refreshed every 10 min independently of the device; device contact uses the latest cached model.
- On device seen (service UUID in adv): connect → read Status (battery, displayed CRC) → render image with current battery → if CRC equal send `NO_CHANGE` else upload → disconnect.
- `wxd preview` renders `preview.png` from live or sample data (no BLE needed).
- systemd user/system service, TOML config (`/etc/wxd.toml` or `~/.config/wxd.toml`).

## 7. Risks / open items

- **ProMicro clone differences**: P0.13 VCC switch and DCDC inductor may not exist on clones → both configurable in the board overlay. Verify on hardware.
- **Panel variant**: YRD0290RWS800F6 assumed equivalent to common SSD1680 2.9" BWR (GxEPD2 `GxEPD2_290_C90c`). Verify RAM X/Y orientation and red polarity on first bring-up.
- **Cold**: tri-color panels are rated ~0–40 °C operating; windowsill in winter may approach the lower limit → refresh slower/paler. Indoor side of the glass is expected to be OK.
- **BlueZ**: continuous scanning on some adapters drops after hours → daemon restarts the scanner periodically and on errors.
- **ECCC data format changes** (Datamart reorganised in 2025) → fallback to Open-Meteo, and the parser is defensive.

## 8. Conventions

- All persistent knowledge goes in `docs/` (this repo). Each decision change → update table in §1.
- Commit after each meaningful step.
- Build: `source scripts/ncs-env.sh && west build -b promicro_nrf52840/nrf52840/uf2 firmware`.
