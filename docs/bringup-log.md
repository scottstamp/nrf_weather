# Hardware bring-up log

## 2026-11-05 — first hardware session

**Setup**

- **Board:** ProMicro nRF52840 clone (USB `239a:00b3`, "Adafruit ProMicro NRF52840").
  - UF2 bootloader `0.9.2-OTAFIX1.2-BP1.1`, Board-ID `nRF52840-promicro`.
  - `INFO_UF2.TXT` says **"SoftDevice: not found"**.
  - The original flash was backed up from `CURRENT.UF2` before the first flash. It held an app at 0x1000–0x52C00.
- **Host adapter:** Dynex/Broadcom BCM20702A0 USB dongle (BT 4.0) on **hci1**. hci0 is the VM's virtual adapter, which is down.
- **Panel:** 2.9" B/W/R SSD1680 breakout, wired per `hardware.md`.

**Findings**

| Item | Result |
|------|--------|
| App load address | The bootloader has no SoftDevice, so it starts the app at **0x1000**, right after the MBR. The `uf2` board variant links at 0x26000 and would not boot. Fix: build with `-DEXTRA_DTC_OVERLAY_FILE=boards/uf2_no_softdevice.overlay` (see `firmware.md`). |
| 32.768 kHz crystal | Present. The SoftDevice Controller starts with `K32SRC_XTAL`. |
| Panel init and refresh | Works. A full B/W/R refresh takes **18.53 s**, consistent across 3 boots. BUSY polarity is OK. |
| Orientation | `rotate = 90` / `CONFIG_WX_ROTATION=90` is upright. |
| Red polarity | Normal (`CONFIG_WX_EPD_RED_INVERT=n`). Verified with `wxd testpattern`: the red triangle, and the RED/BLACK/WHITE blocks, all show correctly. |
| Battery (USB-powered) | VDDH reads 4140–4150 mV. |
| BLE session | Works end to end: Status read → BEGIN → 9472 B → COMMIT ok → device disconnects (reason 0x16). The host connects about 0.2–0.3 s after advertising starts. |
| Transfer speed | The first sessions took **6.8 s**, with about 5 s of data transfer. Cause: bleak on BlueZ reported MTU 23 (default) because `_acquire_mtu()` was never called, so each write carried only 20 bytes (about 527 writes). Fixed in `ble.bleak_connector`; re-measuring. |
| Debug USB log | `udc: Failed to allocate net_buf 4095, ep 0x80` appears once at enumeration. It is harmless (debug build only; release has no USB). |

**Notes**

- `SET_INTERVAL` floor lowered to 60 s for debugging, on both firmware and host. Debug build: poll and retry every 60 s.
- Gotcha: a host process started with an old config pushes its own `poll_interval_s` through `SET_INTERVAL`. The device keeps that value until the next reset or the next `SET_INTERVAL`.

## Session 1, continued: tooling and link tuning

**Dev console:** `poll`, `reset`, `boot` and the 1200-baud touch were all verified on hardware.

- `poll` → NO_CHANGE in 1.56 s.
- `reset` → reboot without the splash; the host reconnects as soon as its debounce ends.
- `boot` → `PROMICRO` drive in about 3–5 s, used by `scripts/flash.sh`.

**Host MTU fix:** after the fix, the host negotiates MTU 247 and sends 40 writes of 244 B.

**Link timing with the BCM20702 (BT 4.0) adapter:**

- **PHY:** stays at 1M. The adapter has no 2M PHY and no data-length extension, so LL payloads are 27 B, about 390 packets per image.
- **Connection interval:**
  - BlueZ opens at 48.75 ms, or at its cached value (15 ms) for a known device.
  - The device's 7.5–15 ms request is deferred 5 s by Zephyr (`BT_CONN_PARAM_UPDATE_TIMEOUT`, as the spec requires).
  - With the deferral set to 0, BlueZ immediately re-imposed 48.75 ms with a 420 ms timeout, so the default was kept.
- **Result:** the data drains in about 4–5 s at roughly 4–5 packets per connection event. A session takes 6.1–6.4 s for an upload and 0.7–1.2 s for NO_CHANGE.
- **Power impact:** radio-on time scales with packet count, not with wall time, so the slow interval costs little battery. A bigger win would be compressing the payload (fewer packets). This is a possible protocol v2 item.

**Host debounce:** lowered from 60 s to 20 s. The 60 s value blocked polls in the 60 s debug cadence.

**Determinism:** the same weather input gives the same CRC (`bbd89302`) across daemon restarts and reboots.

## USB power finding (2026-11-05)

The boot-time CDC ACM helper calls `usbd_enable()` unconditionally. In `drivers/usb/udc/udc_nrf.c`, `udc_nrf_enable()` does `onoff_request(hfxo_mgr)` and enables USBD whether or not VBUS is present; VBUS only gates the D+ pull-up (`USB_EVT_READY`). So every debug build flashed during bring-up ran the HFXO continuously. That costs roughly 250 µA or more, about 100 times the sleep target, if used on battery.

Fix: set `CDC_ACM_SERIAL_ENABLE_AT_BOOT=n`, and have the app enable and disable USB on VBUS messages. This is now in the production build.

Verified on hardware:

- With the cable present at boot, the device enumerates (the boot-race path works).
- `help` answers.
- The 1200-baud touch reaches the bootloader in 4 s.

- **Unplug and replug on battery** (10:22): the cable was unplugged at 10:22:00 and replugged at 10:22:07. Afterwards, `status` reported `uptime 40 s` (no reboot) and `USB enabled via VBUS_READY event at uptime 35 s`, so the VBUS event path works. The `USB cable detected` log line itself is never seen, because it is written before the host opens the port. The same applies to the boot banner on a battery boot. Use `status` instead.

Still to verify:

- **Sleep current on battery:** measure with a PPK2.
