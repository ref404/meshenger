# meshenger

A terminal UI for [Meshtastic](https://meshtastic.org) mesh radio networks, built with [Textual](https://textual.textualize.io).

```
 instant_ █▀▄▀█ █▀▀ █▀ █░█ █▀▀ █▄░█ █▀▀ █▀▀ ██▄
          █░▀░█ ██▄ ▄█ █▀█ ██▄ █░▀█ █▄█ ██▄ █▀▄
```

A live, all-in-one cockpit for your mesh: who's online, how strong the links
are, where nodes sit, and what's flying through the air — in a calm,
terminal-themed monochrome UI that blends into whatever color scheme you run.

## Features

**Nodes & messaging**
- Live node list with signal strength, battery, hop count, and last-heard
- Keyboard navigation — `tab` into the list, `j`/`k` (or arrows) to move,
  `enter` to DM the highlighted node
- Message feed with per-node color coding and hanging-indent word wrap
- Channel switching and direct messaging
- Telemetry panel: device model, firmware, MAC, voltage, air/channel util, GPS

**Map**
- ASCII grid map of GPS nodes with range rings and a N/S/E/W compass frame
- Packet **pulse** — a node's marker blooms when it actually transmits
- Mesh **link lines** between nodes (`ctrl+l`)
- GPS-free **hop-topology** view: you in the center, nodes on rings by hop count (`ctrl+g`)
- `/trace` runs a real traceroute and animates the discovered route across the map

**Live visualizations**
- SDR-style **packet waterfall** strip — every packet scrolls by, height = signal, color = node
- Header **gauges**: packets/min counter and a channel-utilization VU bar
- **Packet sniffer** pane (`ctrl+p`) — tcpdump-style live capture
  (`time  src→dst  port  snr  rssi  size  hops`)

**Quality-of-life**
- Neighbor **alerts** — terminal bell on DMs, plus went-dark / low-battery
  notices, scoped to direct neighbors so a busy mesh doesn't flood the feed
- Emoji in node names and messages are stripped to keep the monospace grid
  intact — number-emoji hop "tapbacks" are preserved as plain digits
- Terminal-transparent theme — inherits your terminal's background and palette
- Animated boot splash
- Connection support over Bluetooth LE, USB serial, and TCP/IP

## Requirements

- Python 3.10+
- A Meshtastic device (or run without args for demo mode)

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Demo mode (no device needed — simulated mesh)
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
| `/trace <node>` | Traceroute — animates the path on the map |
| `/channel <0-7>` | Switch channel |
| `/select <node>` | Focus telemetry on a node |
| `/alerts [on\|off]` | Toggle DM / went-dark / low-battery alerts |
| `/clear` | Clear the message feed |

A `<node>` can be a short name (`CYPH`) or a full id (`!deadbeef`).

## Keybindings

| Key | Action |
|---|---|
| `tab` | Enter/exit node-list navigation (then `j`/`k`/arrows, `enter` to DM) |
| `ctrl+t` | Toggle map/telemetry panel |
| `ctrl+g` | Switch map view (GPS grid ↔ hop topology) |
| `ctrl+l` | Toggle mesh link lines on the map |
| `ctrl+p` | Toggle the packet-capture pane |
| `ctrl+r` | Refresh display |

## License

MIT
