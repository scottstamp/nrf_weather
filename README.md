# nrf_weather — WxPaper

Battery-powered 2.9" black/white/red e-paper weather display (nice!nano nRF52840 + SSD1680) for Torbay / St. John's, NL, fed over BLE by a Linux host daemon using Environment Canada data.

| Path | What |
|------|------|
| `docs/PLAN.md` | Plan, decisions, phases, power budget — start here |
| `docs/protocol.md` | BLE protocol v1 (device ⇄ host contract) |
| `docs/hardware.md` | Wiring, power switching, mounting |
| `docs/firmware.md` | Firmware build/flash, design, Kconfig |
| `docs/host.md` | Host daemon install, systemd, BlueZ notes |
| `docs/weather-sources.md` | ECCC / Open-Meteo endpoints and mapping |
| `docs/layout.md` | Display layout and readability rules |
| `firmware/` | Zephyr app (NCS v3.4.2) |
| `host/` | Python daemon `wxd` (bleak + Pillow) |

Quick start:

```sh
# firmware
source scripts/ncs-env.sh && cd firmware && west build -b promicro_nrf52840/nrf52840/uf2 -d build .
# copy build/firmware/zephyr/zephyr.uf2 to the NICENANO drive (double-tap reset)

# host
python3 -m venv host/.venv && host/.venv/bin/pip install -e 'host[dev]'
host/.venv/bin/wxd preview --out preview.png   # no hardware needed
host/.venv/bin/wxd run                          # daemon
```
