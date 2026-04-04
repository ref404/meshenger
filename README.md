# meshenger

A terminal UI for [Meshtastic](https://meshtastic.org) mesh radio networks, built with [Textual](https://textual.textualize.io).

```
 instant_ █▀▄▀█ █▀▀ █▀ █░█ █▀▀ █▄░█ █▀▀ █▀▀ ██▄
          █░▀░█ ██▄ ▄█ █▀█ ██▄ █░▀█ █▄█ ██▄ █▀▄
```

## Features

- Live node list with signal strength, battery, and hop count
- ASCII grid map of nodes with GPS coordinates
- Telemetry panel with device info, firmware version, and MAC address
- Message feed with per-node color coding and hanging-indent word wrap
- Bluetooth LE, USB serial, and TCP/IP connection support
- Channel switching and direct messaging
- Collapsible map panel (`ctrl+t`)
- Screen refresh (`ctrl+r`)

## Requirements

- Python 3.10+
- A Meshtastic device (or run without args for demo mode)

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Demo mode (no device needed)
python meshenger.py

# Bluetooth LE — auto-discover
python meshenger.py --ble

# Bluetooth LE — specific device
python meshenger.py --ble "Meshtastic_ABCD"

# USB serial
python meshenger.py --port /dev/ttyUSB0

# TCP (via Meshtastic app network API)
python meshenger.py --host 192.168.1.100
```

## Commands

| Command | Description |
|---|---|
| `/help` | Show all commands |
| `/nodes` | List all known nodes |
| `/info` | Your node info and telemetry |
| `/map` | GPS coordinates of all nodes |
| `/dm <node> <msg>` | Direct message a node |
| `/channel <0-7>` | Switch channel |
| `/select <node>` | Focus telemetry on a node |
| `/clear` | Clear the message feed |

## Keybindings

| Key | Action |
|---|---|
| `ctrl+t` | Toggle map/telemetry panel |
| `ctrl+r` | Refresh display |

## License

MIT
