# Install on a Raspberry Pi

This guide installs the USB recorder, read-only HTTP server, BACnet/IP gateway,
and database snapshot timer as **your normal user's** systemd services. Use a
Raspberry Pi 5 with 64-bit Raspberry Pi OS, Python 3.11 or newer, an Ethernet
connection, and a **Pico 2 W (RP2350)**. The supplied firmware is version **0.4.5**,
built for `pico2_w`; a first-generation Pico/Pico W (RP2040) is not interchangeable.

Read [WIRING.md](WIRING.md) before connecting the meter. That document covers the
pin mapping, powered/unpowered voltage limits, and required connection order.
The meter's DB9 connector is not an RS-232 port. Do not connect its power output
to the Pico or Pi.

## 1. Download and prepare Python

Run these commands on the Pi as the user who will own the recording. Only the
package/group/boot-persistence commands use `sudo`; do not run the Python
installer or `systemctl --user` with `sudo`.

```sh
sudo apt update
sudo apt install -y git python3 python3-venv
git clone https://github.com/JimBoHa/Westinghouse-IQ-Data-Plus-II-to-BACnet-IP.git
cd Westinghouse-IQ-Data-Plus-II-to-BACnet-IP
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The requirements pin `pyserial==3.5` for USB communication and
`bacpypes3==0.0.108` for BACnet. Services use this `.venv` directly, so you do not
need to activate it in your shell. Keep this checkout in its final location:
generated service files contain absolute paths.

## 2. Flash and identify the Pico

For a new Pico 2 W, leave meter signals disconnected, hold **BOOTSEL**, and plug
its USB cable into the Pi. Release BOOTSEL when the RP2350 boot drive appears.
Copy this file onto that drive using the Pi's file manager:

```text
firmware/pico-live/dist/iqdata_pico_live.uf2
```

The boot drive disappears and the board restarts. The firmware guide includes
artifact verification, [Pico SDK 2.3.1 / picotool source build instructions](FIRMWARE.md),
and update procedures. Use the supplied Pico 2 W build unless you deliberately
build and verify another supported RP2350 target.

Find the persistent USB device name:

```sh
ls -l /dev/serial/by-id/
```

Set the value below to the full name you found; do not use the example literally.
Use `/dev/serial/by-id/...` rather than `/dev/ttyACM0`, which can change at reboot.

```sh
IQDATA_SERIAL='/dev/serial/by-id/REPLACE_WITH_YOUR_PICO_DEVICE'
sudo usermod -aG dialout "$USER"
```

Log out completely and log back in (or reboot) so the group change also reaches
the user service manager. Return to the checkout and set `IQDATA_SERIAL` again
in the new shell. Confirm `id -nG` includes `dialout`, then query the Pico before
starting the poller:

```sh
mkdir -p "$HOME/.local/share/iqdata/state"
.venv/bin/python live/host.py command info \
  --port "$IQDATA_SERIAL" \
  --output "$HOME/.local/share/iqdata/firmware-info-$(date -u +%Y%m%dT%H%M%SZ).json" \
  --state "$HOME/.local/share/iqdata/state"
cat "$HOME/.local/share/iqdata/state/info.json"
```

Check that the saved response reports firmware **0.4.5** and board/build target
**pico2_w**. The target string describes the build; also check the physical board
is a Pico 2 W. A failed query or unexpected version must be resolved before
starting meter collection. Connect the meter using the sequence in
[WIRING.md](WIRING.md), keeping Pico USB power present.

## 3. Choose addresses and review service files

Give the Pi a stable Ethernet IPv4 address (static configuration or a DHCP
reservation). Use `ip -4 address` to inspect its actual address and subnet prefix.
Choose a BACnet device instance that is unique on your building network.
`75151` is an example, not a reserved project allocation.

Set these values for your installation. `192.0.2.10` is a documentation address;
replace it with the Pi's real Ethernet address and prefix. The usual BACnet/IP
UDP port is `47808`.

```sh
IQDATA_BACNET='192.0.2.10/24:47808'
IQDATA_INSTANCE='75151'
IQDATA_NAME='IQData-PlusII-Pi5'
IQDATA_DATA="$HOME/.local/share/iqdata"

python3 scripts/install_pi.py \
  --root "$PWD" --data-dir "$IQDATA_DATA" \
  --serial "$IQDATA_SERIAL" --bacnet-address "$IQDATA_BACNET" \
  --instance "$IQDATA_INSTANCE" --name "$IQDATA_NAME" \
  --output-dir build/systemd

cat build/systemd/iqdata-*.service build/systemd/iqdata-snapshot.timer
systemd-analyze --user verify build/systemd/iqdata-*.service build/systemd/iqdata-snapshot.timer
```

This renders files only. It does not install units, start services, flash the
Pico, or change existing data. To send periodic directed I-Am announcements to
a Metasys supervisor, add `--metasys 'YOUR_SUPERVISOR_IPV4:47808'` to both this
command and the installation command below. Omit it for normal broadcast
discovery without a configured supervisor.

The generated settings are:

| Unit | Behavior |
|---|---|
| `iqdata-poll.service` | `all_standard` measurements, 500 ms transaction limit, at least 1.05 seconds between request starts, recovery/backoff, and interleaved read-only diagnostics |
| `iqdata-http.service` | Saved telemetry and snapshot download on all IPv4 interfaces, TCP 8080 |
| `iqdata-bacnet.service` | Read-only BACnet/IP points at the address and instance you supplied |
| `iqdata-snapshot.service` | Creates a consistent SQLite download with SQLite's backup API |
| `iqdata-snapshot.timer` | First snapshot after boot delay; refreshes every five minutes |

Diagnostic requests take their own request slots, so some measurement gaps are
about 2.1 seconds. Diagnostics are current state; they are not added as history
rows in the measurement database. Use the HTTP and BACnet services on your
trusted building LAN; HTTP has no login or TLS. Permit TCP 8080 and the selected
BACnet UDP port through any configured firewall as appropriate for that LAN.

## 4. Install and start

After reviewing the generated files, install the same configuration:

```sh
python3 scripts/install_pi.py \
  --root "$PWD" --data-dir "$IQDATA_DATA" \
  --serial "$IQDATA_SERIAL" --bacnet-address "$IQDATA_BACNET" \
  --instance "$IQDATA_INSTANCE" --name "$IQDATA_NAME" \
  --install

systemctl --user daemon-reload
systemctl --user enable --now iqdata-poll.service iqdata-http.service iqdata-bacnet.service iqdata-snapshot.timer
sudo loginctl enable-linger "$USER"
```

`--install` writes the five unit files to `~/.config/systemd/user` (or
`$XDG_CONFIG_HOME/systemd/user` when set) and creates the external state directory.
Starting services is a separate command above. Linger lets this user's enabled
services run at boot and after logout. Existing unit files are protected:
re-running either render or install requires `--replace`, which first saves
copies in a dated `iqdata-backup-*` directory beside the unit files.

Confirm the poller has created `$IQDATA_DATA/iqdata.sqlite`, then add the metadata
and convenient `samples` view and make the first download:

```sh
systemctl --user status iqdata-poll.service iqdata-http.service iqdata-bacnet.service --no-pager
ls -lh "$IQDATA_DATA/iqdata.sqlite"
.venv/bin/python live/configure_recording.py "$IQDATA_DATA/iqdata.sqlite"
systemctl --user start iqdata-snapshot.service
systemctl --user list-timers iqdata-snapshot.timer
```

Run `configure_recording.py` once for each new recording database. It adds
metadata/views without rewriting captured measurement rows.

## 5. Verify and operate

From another computer on the same LAN, open these URLs using the Pi's address:

- `http://PI_ADDRESS:8080/status` — saved device information and telemetry availability.
- `http://PI_ADDRESS:8080/telemetry` — readings, timestamps, validity, and stale status.
- `http://PI_ADDRESS:8080/database` — the most recent consistent SQLite snapshot.

Check live timestamps, valid readings, and agreement with the physical meter.
A running process alone does not prove correct meter data. Discover the selected
device instance in your BACnet client and inspect point status and values.
The database download may lag the live recorder by up to the snapshot interval.

Useful commands:

```sh
journalctl --user -u iqdata-poll.service -u iqdata-bacnet.service -n 100 --no-pager
systemctl --user status iqdata-snapshot.service --no-pager
systemctl --user stop iqdata-poll.service
systemctl --user start iqdata-poll.service
```

Only one process should own the USB device. Stop `iqdata-poll.service` before
running manual `host.py command ...` commands or updating firmware. Follow the
wiring power sequence before unplugging USB. For an address/path change, render
the revised configuration, review it, install with `--replace`, run
`systemctl --user daemon-reload`, then restart the affected services.

Persistent files live under `$IQDATA_DATA`, outside the checkout: `state/`
(including raw journals and saved COV subscriptions), `iqdata.sqlite` (live WAL
database), and `iqdata-latest.sqlite` (download snapshot). Back up the consistent
snapshot while collection continues; copying only the live `.sqlite` file can
miss transactions still in its WAL. Raw journals and history keep growing, so
monitor storage with `df -h` and `du -sh "$IQDATA_DATA"`. Source updates should
preserve this directory.

Offline checks, which do not open USB or contact a meter:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python live/poll_meter.py --self-test
```
