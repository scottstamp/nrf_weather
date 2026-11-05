# Host daemon (wxd)

Linux daemon that fetches weather, renders a 296x128 B/W/R image and pushes it to the e-paper display over BLE when the device wakes up (see `protocol.md`).

## Install

```sh
sudo useradd --system --home-dir /var/lib/wxd --shell /usr/sbin/nologin --groups bluetooth wxd
sudo mkdir -p /opt/wxd /etc/wxd
sudo python3 -m venv /opt/wxd/venv
sudo /opt/wxd/venv/bin/pip install -e /path/to/nrf_weather/host    # or a non-editable install
sudo cp host/config.example.toml /etc/wxd/config.toml              # edit location, render, ble
sudo cp host/systemd/wxd.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wxd
journalctl -u wxd -f
```

Adjust `ExecStart` in the unit if the venv lives elsewhere. Config search order: `--config`, `$XDG_CONFIG_HOME/wxd/config.toml`, `/etc/wxd/config.toml`; with no file, defaults are used.

## Commands

| Command | Purpose |
|---------|---------|
| `wxd run` | Daemon: weather refresh every `weather.refresh_s`, continuous BLE scan, one session per device wake-up |
| `wxd preview --out x.png` | Fetch + render to PNG, no BLE |
| `wxd scan [--seconds 15]` | List nearby WxPaper devices (shows the address for `ble.device_address`) |
| `wxd push` | Wait for the device, push once, exit (non-zero if the loop is interrupted) |

Global flags: `--config PATH`, `-v`.

Behaviour notes:
- The last good weather model is cached as JSON (`<cache_dir>/model.json`) and reloaded on restart.
- If no weather is available yet, the session answers `NO_CHANGE` (never sends a bogus image).
- After a successful session the same device is ignored for 60 s. The scanner is restarted every 30 min and after any error (exponential backoff 5 s to 5 min).
- The device drops the link right after `COMMIT`/`NO_CHANGE`; a disconnect at that point counts as success. If the commit was actually lost, the next wake-up's `displayed_crc` will not match and the image is re-sent.
- Upload: chunk = min(244, MTU-3) bytes (with the u16 offset header included). A COMMIT/BEGIN error triggers one retry from BEGIN using write-with-response.

## BlueZ notes

- Needs BlueZ with `bluetoothd` running (`systemctl status bluetooth`) and an LE-capable adapter.
- Power the adapter: `bluetoothctl power on`; check `rfkill list` and `hciconfig -a` / `btmgmt info`. wxd never powers it on or pairs: no pairing is needed.
- Normal users can talk to BlueZ over the system D-Bus on most distros. If you get `org.freedesktop.DBus.Error.AccessDenied`, add the user to the `bluetooth` group (the unit already does) or check `/etc/dbus-1/system.d/bluetooth.conf`.
- Set `ble.adapter` if you have several controllers (`hci1`, ...). Pin the display with `ble.device_address` if other WxPaper devices are nearby.
- Do not leave `bluetoothctl scan on` running; it competes with the daemon's scanner.

## Troubleshooting

- `wxd scan` prints "BLE scan failed" and exits 2: adapter missing/down or bluetoothd not running.
- Log `session ... failed`: look at the message. `ATT error 0x81` CRC mismatch, `0x84` sequence error (both auto-retried once), timeouts mean a weak link or the device already went to sleep (advertising lasts only 30 s).
- Display never updates but sessions log `no_change ... no payload`: weather fetch is failing; run `wxd preview --out /tmp/x.png -v`.
- Stale connections: `bluetoothctl disconnect <addr>`, or `sudo systemctl restart bluetooth`.
- Run `wxd -v push` in a terminal to see debug output while waking the device (reset it, or wait for the next poll).
