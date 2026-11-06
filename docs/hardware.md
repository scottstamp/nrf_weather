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
| POLL button | D8 | P1.04 | momentary switch to **GND**; internal pull-up, active low |
| WEATHER button | D9 | P1.06 | momentary switch to **GND**; internal pull-up, active low |
| TIDE button | D14 | P1.11 | momentary switch to **GND**; internal pull-up, active low |

The SSD1680 is write-only here, so MISO isn't used. Run SPI at 4 MHz. The controller allows up to 20 MHz for writes, but short jumper wires are happier slower.

## Power notes

- **VCC switch**: on the nice!nano v2, **P0.13 high** turns on the VCC pin, which is the regulated 3.3 V external output. The firmware drives P0.13 high only during a refresh. While the panel is off, every panel signal (SCK, MOSI, CS, DC, RST, BUSY) is set to *disconnected*, so the panel can't be back-powered through its ESD diodes.
  - **Clones without the switch**: delete `power-gpios` from the overlay. The panel then sits in SSD1680 deep sleep (about 1 µA) between refreshes. CS and RST stay driven high so they don't float.
- **Battery sense**: the nice!nano runs the nRF52840 in high-voltage mode with the cell on VDDH. The SAADC samples the internal `VDDH/5` input once per poll, so no divider and no pin are needed.
- **32.768 kHz crystal**: the build assumes it is present (nice!nano v2 has one). For clones without it, build with `-DEXTRA_CONF_FILE=rc_lfclk.conf`. If the crystal is missing and XTAL is selected, Bluetooth never starts.
- **DCDC**: off by default. It needs the DCDC inductors on the board, and enabling it on a board without them stops the chip from running. It only saves current while active (radio, SPI), not in sleep, so the gain here is small. It's a Phase 5 item, to check against the board schematic.
- **nice!nano LED / charger**: the blue charge LED only lights while charging. The user LED (P0.15) is used only for a 50 ms blink acknowledging a poll-button press; otherwise it is disconnected.

## Buttons

Three momentary push buttons, each between its pin and **GND**. No external resistors are needed, because the internal pull-ups are enabled. All three work on battery, and no USB is needed.

| Button | nice!nano | nRF pin | Press |
|--------|-----------|---------|-------|
| POLL | D8 | P1.04 | Poll the host now (new weather/tide data, if any, is drawn ~20 s later) |
| WEATHER | D9 | P1.06 | Show the weather view |
| TIDE | D14 | P1.11 | Show the tide view (upcoming high/low tides, see `tides.md`) |

D8 and D9 are the last two pins of the left header, right below the panel wiring (D2–D7). D14 is on the right header. These pins were chosen to stay clear of the NFC pins (D10 = P0.09, D16 = P0.10) and the analog inputs (D19–D21), and all three are on GPIO port 1 so one SENSE mask covers them.

- **While waiting:** presses are detected with the GPIO SENSE mechanism (`sense-edge-mask` on `&gpio1` in the overlay), so waiting costs essentially nothing in sleep.
- **While held:** each pull-up draws about 250 µA, only while its button is pressed.
- **Feedback:** every accepted press blinks the user LED (P0.15) for 50 ms. Debounce is 30 ms.
- **View buttons:** the device keeps the latest weather and tide images in RAM (protocol v2 image slots), so switching redraws the panel straight away (one full refresh, ~20 s) without contacting the host. Pressing the button of the view already shown does nothing but blink. The selected view stays until the other button is pressed. Uploads for the hidden view are stored without a refresh. Before the host has ever sent a tide image, the tide view shows "TIDES / NO DATA YET". After a reset, the device starts on the weather view.
- **Low battery:** below the critical voltage the panel is not refreshed at all, so view presses are ignored (the LED still blinks).
- A press during an ongoing poll or refresh is kept and handled right after it.

The USB console has matching commands: `scripts/devcmd.sh poll | weather | tide`.

## Mounting

The panel is used in landscape (296x128). Which way the FPC connector faces decides the rotation:

- Host: `rotate = 90` or `270` in `config.toml`.
- Firmware: `CONFIG_WX_ROTATION` must have the same value. It is only used for the device's own splash and OFFLINE/LOW BATTERY text.

If the image comes up upside down, change both to the other value.

**Cold windowsills:** tri-colour e-paper is typically rated for 0–40 °C operation. Below about 0 °C, refresh gets slow and red looks washed out. The SSD1680 reads its internal temperature sensor on every refresh (`0x18 = 0x80`) and picks the matching LUT. Keep the device on the room side of the glass.
