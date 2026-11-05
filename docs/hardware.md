# Hardware

## Parts

| Part | Notes |
|------|-------|
| nice!nano v2 (or ProMicro nRF52840 clone) | nRF52840, Adafruit UF2 bootloader with SoftDevice S140 v6 layout; app linked at `0x26000`. LiPo charger onboard. Zephyr board: `promicro_nrf52840/nrf52840/uf2`. |
| 2.9" B/W/R e-paper on an SPI breakout | Panel YRD0290RWS800F6, 128x296, SSD1680(Z8). The breakout supplies the booster/charge-pump circuit. Typical header: VCC GND DIN CLK CS DC RST BUSY. |
| 1-cell LiPo, e.g. 1000–2000 mAh | Goes to the nice!nano `B+`/`B-` pads. |

## Wiring

Pins are chosen so they all sit on the left header (D2–D7) next to each other. Defined in `firmware/boards/promicro_nrf52840_nrf52840_uf2.overlay`:

| Breakout | nice!nano | nRF pin | Function |
|----------|-----------|---------|----------|
| VCC  | **VCC** | (switched 3.3 V) | Panel power, cut between refreshes |
| GND  | GND | — | |
| DIN / SDA | D2 | P0.17 | SPIM2 MOSI |
| CLK / SCL | D3 | P0.20 | SPIM2 SCK |
| CS   | D4 | P0.22 | chip select (GPIO, active low) |
| DC   | D5 | P0.24 | data/command |
| RST  | D6 | P1.00 | reset (active low) |
| BUSY | D7 | P0.11 | busy (active high) |
| Poll button | D8 | P1.04 | momentary switch to **GND**; internal pull-up, active low |

The SSD1680 is write-only here, so MISO isn't used. Run SPI at 4 MHz. The controller allows up to 20 MHz for writes, but short jumper wires are happier slower.

## Power notes

- **VCC switch**: on the nice!nano v2, **P0.13 high** turns on the VCC pin, which is the regulated 3.3 V external output. The firmware drives P0.13 high only during a refresh. While the panel is off, every panel signal (SCK, MOSI, CS, DC, RST, BUSY) is set to *disconnected*, so the panel can't be back-powered through its ESD diodes.
  - **Clones without the switch**: delete `power-gpios` from the overlay. The panel then sits in SSD1680 deep sleep (about 1 µA) between refreshes. CS and RST stay driven high so they don't float.
- **Battery sense**: the nice!nano runs the nRF52840 in high-voltage mode with the cell on VDDH. The SAADC samples the internal `VDDH/5` input once per poll, so no divider and no pin are needed.
- **32.768 kHz crystal**: the build assumes it is present (nice!nano v2 has one). For clones without it, build with `-DEXTRA_CONF_FILE=rc_lfclk.conf`. If the crystal is missing and XTAL is selected, Bluetooth never starts.
- **DCDC**: off by default. It needs the DCDC inductors on the board, and enabling it on a board without them stops the chip from running. It only saves current while active (radio, SPI), not in sleep, so the gain here is small. It's a Phase 5 item, to check against the board schematic.
- **nice!nano LED / charger**: the blue charge LED only lights while charging. The user LED (P0.15) is used only for a 50 ms blink acknowledging a poll-button press; otherwise it is disconnected.

## Poll button

A momentary push button between **D8 (P1.04)** and **GND** makes the device poll the host immediately. This works on battery, and no USB is needed. No external resistor is needed, because the internal pull-up is enabled.

- **While waiting:** the press is detected with the GPIO SENSE mechanism (`sense-edge-mask` in the overlay), so waiting for it costs essentially nothing in sleep.
- **While held:** the pull-up draws about 250 µA, only while the button is pressed.
- **On a press:** the user LED (P0.15) blinks for 50 ms. If there's new data, the panel refreshes about 20 s later. If there's no new data, the blink is the only feedback.

A press during an ongoing poll or refresh is kept, and a new poll starts right after it.

## Mounting

The panel is used in landscape (296x128). Which way the FPC connector faces decides the rotation:

- Host: `rotate = 90` or `270` in `config.toml`.
- Firmware: `CONFIG_WX_ROTATION` must have the same value. It is only used for the device's own splash and OFFLINE/LOW BATTERY text.

If the image comes up upside down, change both to the other value.

**Cold windowsills:** tri-colour e-paper is typically rated for 0–40 °C operation. Below about 0 °C, refresh gets slow and red looks washed out. The SSD1680 reads its internal temperature sensor on every refresh (`0x18 = 0x80`) and picks the matching LUT. Keep the device on the room side of the glass.
