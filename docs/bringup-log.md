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
