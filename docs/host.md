# Host daemon (wxd)

Linux daemon that fetches weather, renders a 296x128 B/W/R image and pushes it to the e-paper display over BLE when the device wakes up (see `protocol.md`).

## Install

Two options: a **user service** (simplest; runs as you from the repo venv) or a **system service** (dedicated `wxd` user).

### User service (`host/systemd/wxd-user.service`)

```sh
cd /path/to/nrf_weather/host && python3 -m venv .venv && .venv/bin/pip install -e .
mkdir -p ~/.config/wxd ~/.config/systemd/user
cp config.example.toml ~/.config/wxd/config.toml                   # edit location, render, ble (adapter!)
cp systemd/wxd-user.service ~/.config/systemd/user/wxd.service     # adjust ExecStart if the repo isn't ~/nrf_weather
systemctl --user daemon-reload && systemctl --user enable --now wxd
sudo loginctl enable-linger $USER                                  # start at boot, survive logout
journalctl --user -u wxd -f
```

Cache and lock file: `~/.cache/wxd`. Stop it before a manual `wxd push`: `systemctl --user stop wxd`.

### System service (`host/systemd/wxd.service`)

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
| `wxd preview --out x.png [--view tide]` | Fetch + render the weather (or tide) view to PNG, no BLE |
| `wxd scan [--seconds 15]` | List nearby WxPaper devices (shows the address for `ble.device_address`) |
| `wxd serve` | Bridge mode: HTTP server for the ESP32 bridge, no BLE (see "Bridge mode") |
| `wxd push` | Wait for the device, push once, exit (non-zero if the loop is interrupted) |

Global flags: `--config PATH`, `-v`.

Behaviour notes:
- The last good weather model is cached as JSON (`<cache_dir>/model.json`) and reloaded on restart.
- Each session renders both views: weather (slot 0) and tide (slot 1, `[tides]` in the config, CHS predictions refetched every 3 h, cached in `tides.json`). Only slots whose CRC differs from the device's stored one are uploaded, then `DONE`. A view with no data (fetch failing, no cache) is skipped and the device keeps its previous image; with neither, the session is just `DONE`.
- After a successful session the same device is ignored for 60 s. The scanner is restarted every 30 min and after any error (exponential backoff 5 s to 5 min).
- The device drops the link right after `DONE`; a disconnect at that point counts as success (every slot was already committed and acked).
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

## Single instance

`wxd run`, `wxd push` and `wxd testpattern` (not `wxd serve`) take an exclusive lock on `<cache_dir>/wxd.lock`, which holds the PID. If two processes talk to the device at once, they race for the same connection and BlueZ fails both: `org.bluez.Error.InProgress` and then `br-connection-canceled` every ~2 s. This was seen in bring-up. A second instance now exits with `another wxd (pid N) is using the device`. To push or test by hand, stop the service first (`systemctl stop wxd`, or `systemctl --user stop wxd` for the user service), or use `scripts/devcmd.sh poll` to have the running daemon serve the device immediately.

## Bridge mode

When this PC is out of BLE range of the display, an ESP32-C6 bridge near the display does the BLE work and fetches images from the PC over Wi-Fi (contract: `bridge.md`). `wxd serve` is the PC side: same weather/tide fetching and rendering as `wxd run`, plus an HTTP server (`[bridge] listen`/`port`, default `0.0.0.0:8787`). It does no BLE scanning, does not use the adapter and does not take the `wxd.lock`. `[ble] poll_interval_s` is sent to the bridge as the interval for the display.

`wxd serve` replaces `wxd run`. Run only one: if both can reach the display they compete for it. They only coexist if the PC's BLE cannot reach the display, and then `wxd run` is pointless.

Install (user service):

```
mkdir -p ~/.config/wxd ~/.config/systemd/user
cp host/config.example.toml ~/.config/wxd/config.toml      # edit [bridge] / [ble] poll_interval_s
systemctl --user disable --now wxd                          # if the BLE daemon was enabled
cp host/systemd/wxd-serve-user.service ~/.config/systemd/user/wxd-serve.service
systemctl --user daemon-reload && systemctl --user enable --now wxd-serve
sudo loginctl enable-linger $USER                           # start at boot
journalctl --user -u wxd-serve -f
```

Firewall: if ufw is active, `sudo ufw allow 8787/tcp` (ideally restricted to your LAN, e.g. `sudo ufw allow from 192.168.1.0/24 to any port 8787 proto tcp`). There is no authentication; do not expose the port to the internet.

Check it: `curl http://tiny:8787/v1/health` shows when weather and tides were last fetched and the last result the bridge reported. If the port is already taken, `wxd serve` exits with `cannot listen on ...`.

Journal lines: `bridge session <addr>: ... slots=[..] ... render=Nms` for each display contact and `bridge result <addr>: uploaded|no_change|failed ...` after it. The request body is capped at 4 KB and each connection has a 15 s socket timeout.
