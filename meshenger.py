#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  instant_meshenger  //  meshtastic terminal                  ║
║  LoRa mesh radio  |  hacker edition  |  off-grid comms       ║
╚══════════════════════════════════════════════════════════════╝

Usage:
  python meshenger.py                       # demo mode
  python meshenger.py --ble                 # Bluetooth LE (auto-discover)
  python meshenger.py --ble "NodeName"      # Bluetooth LE (specific device)
  python meshenger.py --port /dev/ttyUSB0   # USB serial
  python meshenger.py --host 192.168.1.1    # TCP/IP

Install:
  pip install -r requirements.txt
"""

import argparse
import asyncio
import json
import math
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widgets import Input, RichLog, Static

try:
    import meshtastic.serial_interface
    import meshtastic.tcp_interface
    from pubsub import pub
    MESH_AVAILABLE = True
except ImportError:
    MESH_AVAILABLE = False

try:
    import meshtastic.ble_interface
    BLE_AVAILABLE = True
except ImportError:
    BLE_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
HISTORY_FILE = Path.home() / ".mesh_lounge_history.json"
HISTORY_MAX_STORED = 100
HISTORY_LOAD_COUNT = 10

CHANNEL_NAMES = [
    "LongFast", "LongSlow", "VLongSlow", "MedSlow",
    "MedFast", "ShortSlow", "ShortFast", "ShortTurbo",
]

NODE_COLORS = [
    "cyan", "magenta", "yellow", "bright_green",
    "bright_blue", "bright_red", "bright_cyan", "bright_magenta",
    "orange3", "deep_pink1", "green3", "sky_blue1",
    "chartreuse3", "hot_pink", "medium_spring_green", "cornflower_blue",
]

DEMO_NODES = [
    ("CypherNode",      "CYPH"),
    ("Ghost_Radio",     "GHST"),
    ("NullPointer",     "NULL"),
    ("PacketRat",       "PKRT"),
    ("SignalShaman",    "SGNL"),
    ("HexWitch",        "HWTH"),
    ("LoRa_Punk",       "LRPK"),
    ("BitWanderer",     "BITW"),
    ("FreqHopper",      "FREQ"),
    ("NetNomad",        "NOMAD"),
    ("SpectrumDrift",   "SPEC"),
    ("DigitalHobo",     "DHOB"),
    ("static_noise",    "NOIS"),
    ("RF_Anarchist",    "RFAN"),
]

DEMO_MESSAGES = [
    "anyone else seeing interference on chan 3?",
    "just got a packet from 4 hops out, wild",
    "mesh is looking healthy tonight",
    "running the node on solar now, solid uptime",
    "rf conditions are good, snr sitting around +6",
    "added two more nodes to the mesh today",
    "firmware 2.5.x seems solid, no crashes",
    "who's in the downtown sector?",
    "antenna upgrade made a massive difference",
    "got a node on the rooftop, coverage is nuts",
    "anyone monitoring channel 5?",
    "just meshed across the river!",
    "solid 40km range with the big whip antenna",
    "spreading factor 12 = long range baby",
    "got gps lock finally, been waiting for hours",
    "running headless on pi zero, 3 weeks uptime",
    "encrypted? always. trust nobody on the airwaves",
    "new repeater node going up on the hill tomorrow",
    "LoRa never sleeps, neither do we",
    "if the internet goes down, we're still meshing",
    "packet collision, retransmitting on hop 3",
    "900MHz hits different through concrete",
    "building a portable mesh kit for the van",
    "range test: 12km urban, not bad",
    "who else is running on meshtastic in this city?",
]

BOOT_MSGS = [
    "initializing rf stack...",
    "loading channel config...",
    "scanning airwaves...",
    "syncing node database...",
    "mesh topology: online",
]

# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class MeshNode:
    node_id: str
    long_name: str = "Unknown"
    short_name: str = "????"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[int] = None
    battery_level: Optional[int] = None
    voltage: Optional[float] = None
    rssi: Optional[int] = None
    snr: Optional[float] = None
    last_heard: Optional[float] = None
    is_mine: bool = False
    hops_away: int = 0
    air_util_tx: Optional[float] = None
    channel_util: Optional[float] = None
    hw_model: Optional[str] = None
    firmware: Optional[str] = None

    @property
    def is_online(self) -> bool:
        if self.is_mine:
            return True
        if self.last_heard is None:
            return False
        return (time.time() - self.last_heard) < 900  # 15min window

    def battery_text(self) -> Text:
        t = Text()
        if self.battery_level is None:
            t.append("─────", style="dim")
            return t
        lvl = self.battery_level
        blocks = round(lvl / 20)
        bar = "█" * blocks + "░" * (5 - blocks)
        color = "bright_green" if lvl > 60 else "yellow" if lvl > 25 else "red"
        t.append(bar, style=color)
        t.append(f" {lvl}%", style=color)
        return t

    def signal_text(self) -> Text:
        t = Text()
        if self.snr is None:
            t.append("─────", style="dim")
            return t
        snr = self.snr
        if snr >= 5:
            bars, color = "▁▃▅▇", "bright_green"
        elif snr >= 0:
            bars, color = "▁▃▅░", "green"
        elif snr >= -5:
            bars, color = "▁▃░░", "yellow"
        elif snr >= -10:
            bars, color = "▁░░░", "orange3"
        else:
            bars, color = "░░░░", "red"
        t.append(bars, style=color)
        t.append(f" {snr:+.0f}dB", style=color)
        return t

    def hops_text(self) -> Text:
        t = Text()
        color = "bright_green" if self.hops_away == 0 else "yellow" if self.hops_away <= 2 else "orange3"
        hop_sym = "⬤ " * (min(self.hops_away, 4) + 1)
        t.append(f"{self.hops_away} ", style=color)
        t.append(hop_sym, style=f"dim {color}")
        return t


@dataclass
class MeshMessage:
    timestamp: float
    sender_id: str
    sender_name: str
    text: str
    channel: int = 0
    is_dm: bool = False
    is_system: bool = False
    snr: Optional[float] = None
    rssi: Optional[int] = None
    hops: int = 0


# ── Widgets ───────────────────────────────────────────────────────────────────

class HeaderBar(Static):
    """Top banner with logo and live stats."""

    LOGO = (
        " instant_ █▀▄▀█ █▀▀ █▀ █░█ █▀▀ █▄░█ █▀▀ █▀▀ ██▄\n"
        "          █░▀░█ ██▄ ▄█ █▀█ ██▄ █░▀█ █▄█ ██▄ █▀▄"
    )

    connection_status = reactive("◌ OFFLINE")
    node_count = reactive(0)
    channel_name = reactive("LongFast")
    freq_info = reactive("")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._start = time.time()

    def render(self) -> Text:
        t = Text()
        t.append(self.LOGO, style="bold bright_green")
        t.append("\n")
        elapsed = int(time.time() - self._start)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        uptime = f"{h:02d}:{m:02d}:{s:02d}"
        ts = datetime.now().strftime("%H:%M:%S")

        sep = Text("  │  ", style="dim green")

        # Status row
        t.append("  ")
        if "LIVE" in self.connection_status:
            t.append(f" {self.connection_status} ", style="bold bright_green on #001a00")
        elif "DEMO" in self.connection_status:
            t.append(f" {self.connection_status} ", style="bold yellow on #1a1500")
        else:
            t.append(f" {self.connection_status} ", style="bold red on #1a0000")
        t.append_text(sep)
        t.append("CH:", style="dim green")
        t.append(f" {self.channel_name}", style="bold cyan")
        t.append_text(sep)
        t.append("NODES:", style="dim green")
        t.append(f" {self.node_count}", style="bold bright_green")
        t.append_text(sep)
        t.append("UPTIME:", style="dim green")
        t.append(f" {uptime}", style="bold green")
        t.append_text(sep)
        t.append("UTC:", style="dim green")
        t.append(f" {ts}", style="bold green")
        t.append("\n")

        # Frequency + hints row
        t.append("  ")
        t.append("FREQ:", style="dim green")
        t.append(f" {self.freq_info if self.freq_info else '—'}", style="bold cyan")
        t.append_text(sep)
        t.append("/help  /dm  /channel  /nodes  /info  /select  /clear  "
                 "[ctrl+t map]  [ctrl+r refresh]", style="dim")

        return t


class NodeListPanel(Static):
    """Scrollable node list with signal/battery/hops."""

    class NodeSelected(Message):
        def __init__(self, node_id: str) -> None:
            super().__init__()
            self.node_id = node_id

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._mesh_nodes: Dict[str, MeshNode] = {}
        self._node_colors: Dict[str, str] = {}
        self._selected_node_id: Optional[str] = None
        self._node_line_map: List[Tuple[int, int, str]] = []  # (start_line, end_line, node_id)

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        w = self.size.width
        return strip.crop(0, w).extend_cell_length(w)

    def update_nodes(self, nodes: Dict[str, MeshNode], node_colors: Dict[str, str],
                     selected: Optional[str] = None):
        self._mesh_nodes = nodes
        self._node_colors = node_colors
        self._selected_node_id = selected
        self.refresh(layout=True)

    def on_resize(self, event) -> None:
        self.refresh(layout=True)

    def on_click(self, event) -> None:
        y = event.y
        for start, end, node_id in self._node_line_map:
            if start <= y < end:
                self.post_message(NodeListPanel.NodeSelected(node_id))
                break

    def render(self) -> Text:
        t = Text()
        self._node_line_map = []

        online = sum(1 for n in self._mesh_nodes.values() if n.is_online)
        total = len(self._mesh_nodes)

        # content_w is the renderable text width (widget width minus padding 0 1)
        content_w = max(4, self.size.width - 2)
        sep_w = content_w  # separator exactly fills content width — never wraps

        t.append("◈ NODES ", style="bold bright_green")
        t.append(f"[{online} online / {total} total]\n", style="dim green")
        t.append("━" * sep_w + "\n", style="dim green")

        # visual_offset: extra visual rows the header line adds beyond its one logical \n
        # (wrapping only; separator is exactly sep_w so it never wraps)
        header_str = f"◈ NODES [{online} online / {total} total]"
        header_visual_rows = math.ceil(cell_len(header_str) / content_w)
        visual_offset = header_visual_rows - 1

        if not self._mesh_nodes:
            t.append("\n   scanning for nodes...\n", style="dim green")
            t.append("   ▒▒▒░░░░░░░░░░░░░░░░\n", style="dim green")
            return t

        sorted_nodes = sorted(
            self._mesh_nodes.values(),
            key=lambda n: (not n.is_mine, not n.is_online, n.long_name.lower()),
        )

        for node in sorted_nodes:
            color = self._node_colors.get(node.node_id, "white")
            is_selected = node.node_id == self._selected_node_id
            bg = " on #001a00" if is_selected else ""

            start_line = t.plain.count('\n') + visual_offset

            # Status dot + name
            if node.is_mine:
                t.append("◉ ", style=f"bold bright_green{bg}")
                name = node.long_name[:16]
                t.append(f"{name}", style=f"bold {color}{bg}")
                t.append(" ◂you\n", style=f"dim bright_green{bg}")
            elif node.is_online:
                t.append("● ", style=f"bright_green{bg}")
                t.append(f"{node.long_name[:16]}\n", style=f"{color}{bg}")
            else:
                t.append("○ ", style=f"dim{bg}")
                t.append(f"{node.long_name[:16]}\n", style=f"dim{bg}")

            # Node ID
            short_id = node.node_id.lstrip("!")[-8:]
            t.append(f"  !{short_id}", style="dim green")
            t.append(f"  [{node.short_name}]\n", style="dim cyan")

            # Signal
            t.append("  sig ", style="dim")
            t.append(node.signal_text())
            t.append("\n")

            # Battery
            t.append("  bat ", style="dim")
            t.append(node.battery_text())
            t.append("\n")

            # Hops (only for remote nodes)
            if not node.is_mine:
                t.append("  hops ", style="dim")
                t.append(node.hops_text())
                t.append("\n")

                # Last seen
                if node.last_heard:
                    elapsed = time.time() - node.last_heard
                    if elapsed < 60:
                        seen = f"{int(elapsed)}s ago"
                    elif elapsed < 3600:
                        seen = f"{int(elapsed / 60)}m ago"
                    else:
                        seen = f"{int(elapsed / 3600)}h ago"
                    color_seen = "dim green" if node.is_online else "dim red"
                    t.append(f"  seen {seen}\n", style=color_seen)

            # GPS indicator
            if node.latitude is not None:
                t.append("  ⌖ GPS\n", style="dim cyan")

            t.append("╌" * sep_w + "\n", style="dim")

            end_line = t.plain.count('\n') + visual_offset
            self._node_line_map.append((start_line, end_line, node.node_id))

        return t


class MapPanel(Static):
    """ASCII grid map of node positions from GPS data."""

    _MAP_W_DEFAULT = 32
    _MAP_H_DEFAULT = 16

    @property
    def MAP_W(self) -> int:
        return self._MAP_W_DEFAULT

    @property
    def MAP_H(self) -> int:
        return self._MAP_H_DEFAULT

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._mesh_nodes: Dict[str, MeshNode] = {}
        self._node_colors: Dict[str, str] = {}
        self._blink = True
        self._frame = 0

    def on_resize(self, event) -> None:
        self.refresh(layout=True)

    def update_nodes(self, nodes: Dict[str, MeshNode], node_colors: Dict[str, str]):
        self._mesh_nodes = nodes
        self._node_colors = node_colors
        self.refresh(layout=True)

    def tick(self):
        self._blink = not self._blink
        self._frame += 1
        self.refresh()

    def _compute_grid(self) -> Tuple[List[List[str]], List[List[Optional[str]]]]:
        grid = [[" "] * self.MAP_W for _ in range(self.MAP_H)]
        colors = [[None] * self.MAP_W for _ in range(self.MAP_H)]

        # Sparse reference dots at every 8 cols × 4 rows
        for ry in range(0, self.MAP_H, 4):
            for rx in range(0, self.MAP_W, 8):
                grid[ry][rx] = "·"

        nodes_gps = [n for n in self._mesh_nodes.values() if n.latitude is not None]
        if not nodes_gps:
            return grid, colors

        lats = [n.latitude for n in nodes_gps]
        lons = [n.longitude for n in nodes_gps]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        lat_span = max(max_lat - min_lat, 0.005)
        lon_span = max(max_lon - min_lon, 0.005)

        pad_lat = lat_span * 0.18
        pad_lon = lon_span * 0.18
        min_lat -= pad_lat
        max_lat += pad_lat
        min_lon -= pad_lon
        max_lon += pad_lon
        lat_span = max_lat - min_lat
        lon_span = max_lon - min_lon

        for node in nodes_gps:
            px = int((node.longitude - min_lon) / lon_span * (self.MAP_W - 1))
            py = int((1 - (node.latitude - min_lat) / lat_span) * (self.MAP_H - 1))
            px = max(0, min(self.MAP_W - 1, px))
            py = max(0, min(self.MAP_H - 1, py))

            if node.is_mine:
                sym = "◉" if self._blink else "○"
            elif node.is_online:
                sym = "◆"
            else:
                sym = "◇"

            grid[py][px] = sym
            colors[py][px] = self._node_colors.get(node.node_id)

        return grid, colors

    def _border_top(self) -> Tuple[str, str, str]:
        """Return (left_dashes, marker, right_dashes) for top/bottom border."""
        marker = "─N─"
        left = "─" * ((self.MAP_W - len(marker)) // 2)
        right = "─" * (self.MAP_W - len(marker) - len(left))
        return left, marker, right

    def _border_bot(self) -> Tuple[str, str, str]:
        marker = "─S─"
        left = "─" * ((self.MAP_W - len(marker)) // 2)
        right = "─" * (self.MAP_W - len(marker) - len(left))
        return left, marker, right

    def render(self) -> Text:
        t = Text()
        nodes_gps = [n for n in self._mesh_nodes.values() if n.latitude is not None]
        count = len(nodes_gps)

        t.append("◈ GRID MAP", style="bold bright_green")
        if count:
            t.append(f" ── {count} node{'s' if count != 1 else ''}", style="dim green")
        t.append("\n")
        t.append("━" * (self.MAP_W + 2) + "\n", style="dim green")

        tl, tm, tr = self._border_top()
        bl, bm, br = self._border_bot()

        if not nodes_gps:
            t.append("┌" + tl, style="dim green")
            t.append(tm, style="dim cyan")
            t.append(tr + "┐\n", style="dim green")
            mid = self.MAP_H // 2
            for i in range(self.MAP_H):
                t.append("│", style="dim green")
                if i == mid - 1:
                    msg = "awaiting gps fix"
                    pad = self.MAP_W - len(msg)
                    t.append(" " * (pad // 2) + msg + " " * (pad - pad // 2), style="dim green")
                elif i == mid:
                    spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self._frame % 10]
                    msg = f"{spin} scanning"
                    pad = self.MAP_W - len(msg)
                    t.append(" " * (pad // 2) + msg + " " * (pad - pad // 2), style="dim green")
                else:
                    t.append(" " * self.MAP_W)
                t.append("│\n", style="dim green")
            t.append("└" + bl, style="dim green")
            t.append(bm, style="dim cyan")
            t.append(br + "┘\n", style="dim green")
            return t

        grid, colors = self._compute_grid()

        t.append("┌" + tl, style="dim green")
        t.append(tm, style="dim cyan")
        t.append(tr + "┐\n", style="dim green")
        mid_row = self.MAP_H // 2
        for row_i, row in enumerate(grid):
            if row_i == mid_row:
                t.append("W", style="dim cyan")
            else:
                t.append("│", style="dim green")
            for col_i, cell in enumerate(row):
                c = colors[row_i][col_i]
                if c:
                    t.append(cell, style=f"bold {c}")
                elif cell == "·":
                    t.append(cell, style="dim")
                else:
                    t.append(cell)
            if row_i == mid_row:
                t.append("E\n", style="dim cyan")
            else:
                t.append("│\n", style="dim green")
        t.append("└" + bl, style="dim green")
        t.append(bm, style="dim cyan")
        t.append(br + "┘\n", style="dim green")

        # Legend
        t.append("◉ you  ◆ online  ◇ offline\n", style="dim green")

        # My coordinates
        mine = next((n for n in nodes_gps if n.is_mine), None)
        if mine:
            t.append(f"LAT  {mine.latitude:>10.5f}°\n", style="dim cyan")
            t.append(f"LON  {mine.longitude:>10.5f}°\n", style="dim cyan")
            if mine.altitude is not None:
                t.append(f"ALT  {mine.altitude:>8}m\n", style="dim cyan")

        # Range estimate (rough)
        if len(nodes_gps) >= 2:
            lats = [n.latitude for n in nodes_gps]
            lons = [n.longitude for n in nodes_gps]
            lat_d = (max(lats) - min(lats)) * 111
            lon_d = (max(lons) - min(lons)) * 85
            span_km = math.sqrt(lat_d**2 + lon_d**2)
            t.append(f"SPAN ≈ {span_km:.1f}km\n", style="dim green")

        return t


class TelemetryPanel(Static):
    """Detailed telemetry for my node or selected node."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._mesh_nodes: Dict[str, MeshNode] = {}
        self._selected: Optional[str] = None
        self._node_colors: Dict[str, str] = {}

    def update_data(self, nodes: Dict[str, MeshNode], node_colors: Dict[str, str],
                    selected: Optional[str] = None):
        self._mesh_nodes = nodes
        self._node_colors = node_colors
        self._selected = selected
        self.refresh(layout=True)

    def render(self) -> Text:
        t = Text()
        t.append("◈ TELEMETRY\n", style="bold bright_green")
        t.append("━" * 28 + "\n", style="dim green")

        node = (
            self._mesh_nodes.get(self._selected) if self._selected
            else next((n for n in self._mesh_nodes.values() if n.is_mine), None)
        )

        if not node:
            t.append("\n  no data\n", style="dim")
            return t

        color = self._node_colors.get(node.node_id, "white")
        t.append(f"  {node.long_name}\n", style=f"bold {color}")

        hex_id = node.node_id.lstrip("!")[-8:].upper()
        mac = ":".join(hex_id[i:i+2] for i in range(0, 8, 2))
        t.append(f"  !{hex_id.lower()}", style="dim green")
        t.append(f"  [{node.short_name}]\n", style="dim cyan")
        t.append(f"  {mac}\n", style="dim green")

        if node.hw_model:
            t.append(f"  {'hw':<11}", style="dim green")
            t.append(f"{node.hw_model}\n", style="cyan")
        if node.firmware:
            t.append(f"  {'firmware':<11}", style="dim green")
            t.append(f"{node.firmware}\n", style="cyan")
        t.append("\n")

        def row(label: str, value, style: str = "bright_green"):
            t.append(f"  {label:<11}", style="dim green")
            if isinstance(value, Text):
                t.append_text(value)
            else:
                t.append(str(value), style=style)
            t.append("\n")

        if node.battery_level is not None:
            t.append(f"  {'battery':<11}", style="dim green")
            t.append_text(node.battery_text())
            t.append("\n")

        if node.voltage is not None:
            row("voltage", f"{node.voltage:.2f}V")

        if node.rssi is not None:
            rc = "bright_green" if node.rssi > -100 else "yellow" if node.rssi > -120 else "red"
            row("rssi", f"{node.rssi} dBm", rc)

        if node.snr is not None:
            t.append(f"  {'snr':<11}", style="dim green")
            t.append_text(node.signal_text())
            t.append("\n")

        if not node.is_mine:
            t.append(f"  {'hops':<11}", style="dim green")
            t.append_text(node.hops_text())
            t.append("\n")

        if node.air_util_tx is not None:
            util_color = "bright_green" if node.air_util_tx < 20 else "yellow" if node.air_util_tx < 50 else "red"
            row("air tx", f"{node.air_util_tx:.1f}%", util_color)

        if node.channel_util is not None:
            ch_color = "bright_green" if node.channel_util < 15 else "yellow" if node.channel_util < 40 else "red"
            row("ch util", f"{node.channel_util:.1f}%", ch_color)

        if node.latitude is not None:
            row("lat", f"{node.latitude:.5f}°", "cyan")
            row("lon", f"{node.longitude:.5f}°", "cyan")
            if node.altitude is not None:
                row("alt", f"{node.altitude}m", "cyan")

        return t


# ── CSS ───────────────────────────────────────────────────────────────────────

APP_CSS = """
Screen {
    background: #060c06;
    color: #00cc33;
    layers: base overlay;
}

#init-overlay {
    layer: overlay;
    offset: 0 0;
    width: 100%;
    height: 100%;
    background: #060c06;
    content-align: center middle;
    color: #00ff41;
}

#header-bar {
    height: 6;
    background: #020802;
    border-bottom: solid #0d2b0d;
    padding: 1 2;
    color: #00ff41;
}

#body {
    height: 1fr;
    background: #060c06;
}

#left-scroll {
    width: 30;
    border-right: solid #0d2b0d;
    background: #030a03;
}

#node-list {
    width: 100%;
    height: auto;
    min-height: 100%;
    padding: 0 1;
    background: #030a03;
}

#message-feed {
    width: 1fr;
    height: 1fr;
    background: #060c06;
    scrollbar-color: #1a3d1a #060c06;
    padding: 0 1;
}

#right-panel {
    width: 38;
    height: 1fr;
    border-left: solid #0d2b0d;
    background: #030a03;
}

#map-view {
    width: 100%;
    height: auto;
    background: #030a03;
    padding: 0;
    border-bottom: solid #0d2b0d;
}

#telemetry-view {
    width: 100%;
    height: auto;
    background: #030a03;
    padding: 0 1;
}

#input-container {
    height: 3;
    background: #020802;
    border-top: solid #00ff41;
    align: left middle;
    padding: 0 1;
}

#prompt-label {
    color: #00ff41;
    width: auto;
    background: #020802;
    text-style: bold;
}

#chat-input {
    background: #020802;
    color: #00ff41;
    border: none;
    width: 1fr;
}

#chat-input:focus {
    border: none;
    background: #020802;
}
"""


# ── Keycap emoji width compensation ──────────────────────────────────────────

_KEYCAP_RE = re.compile('\uFE0F\u20E3')

def _fix_keycap_width(text: str) -> str:
    """Insert a space after each keycap emoji (1️⃣  2️⃣  etc.).

    Keycap sequences (digit + U+FE0F + U+20E3) are measured as 2 cells by
    Rich but rendered as 1 cell by many terminals.  Adding a space after each
    makes the terminal rendering match Rich's cell count.
    """
    return _KEYCAP_RE.sub('\uFE0F\u20E3 ', text)


def _wrap_with_indent(text: str, line_w: int, indent: int) -> str:
    """Word-wrap `text` to `line_w` columns; continuation lines are indented by `indent` spaces."""
    if not text or line_w <= 0:
        return text
    pad = " " * indent
    words = text.split(" ")
    lines: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for word in words:
        wl = cell_len(word)
        if cur and cur_len + 1 + wl > line_w:
            lines.append(" ".join(cur))
            cur = [word]
            cur_len = wl
        else:
            if cur:
                cur_len += 1
            cur.append(word)
            cur_len += wl
    if cur:
        lines.append(" ".join(cur))
    return ("\n" + pad).join(lines)


# ── Init overlay ─────────────────────────────────────────────────────────────

class InitOverlay(Static):
    """Full-screen splash shown while the app boots, with a cycling dot animation."""

    _dot_step = reactive(0)

    def on_mount(self) -> None:
        self.set_interval(0.4, self._tick)

    def _tick(self) -> None:
        self._dot_step = (self._dot_step + 1) % 3

    def render(self) -> Text:
        dots = "." * (self._dot_step + 1)
        t = Text()
        t.append("Initializing", style="bold bright_green")
        t.append(dots, style="bold bright_green")
        return t


# ── Clipped RichLog ───────────────────────────────────────────────────────────

class ClippedRichLog(RichLog):
    """RichLog that hard-clips every rendered strip to the widget boundary."""

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        w = self.size.width
        return strip.crop(0, w).extend_cell_length(w)


# ── Main App ──────────────────────────────────────────────────────────────────

class MeshLoungeApp(App):
    """instant_meshenger — meshtastic terminal"""

    CSS = APP_CSS
    TITLE = "instant_meshenger"
    SUB_TITLE = "mesh network terminal"
    BINDINGS = [
        Binding("ctrl+t", "toggle_panels", "Toggle map",     priority=True),
        Binding("ctrl+r", "refresh_screen", "Refresh screen", priority=True),
    ]

    def __init__(self, port: Optional[str] = None, host: Optional[str] = None,
                 baud: int = 115200, ble: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self._port = port
        self._host = host
        self._baud = baud
        self._ble = ble  # device name/address, or "" for auto-discover
        self._demo_mode = (port is None and host is None and ble is None) or not MESH_AVAILABLE
        self.nodes: Dict[str, MeshNode] = {}
        self.current_channel: int = 0
        self.interface = None
        self._node_colors: Dict[str, str] = {}
        self._color_idx: int = 0
        self._selected_node: Optional[str] = None

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header-bar")
        with Horizontal(id="body"):
            with ScrollableContainer(id="left-scroll"):
                yield NodeListPanel(id="node-list")
            yield ClippedRichLog(
                id="message-feed",
                highlight=True,
                markup=True,
                auto_scroll=True,
                wrap=True,
            )
            with ScrollableContainer(id="right-panel"):
                yield MapPanel(id="map-view")
                yield TelemetryPanel(id="telemetry-view")
        with Horizontal(id="input-container"):
            yield Static("▶ ", id="prompt-label")
            yield Input(
                placeholder="type a message… (/help for commands)",
                id="chat-input",
            )
        yield InitOverlay(id="init-overlay")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        await self._boot_sequence()

    def on_unmount(self) -> None:
        """Unsubscribe pubsub listeners. Do NOT call interface.close() here —
        BLEInterface.close() blocks on a threading.wait() which raises
        KeyboardInterrupt when Ctrl+C fires inside the asyncio shutdown path.
        os._exit(0) in main() drops the BLE connection by killing the process."""
        if MESH_AVAILABLE:
            try:
                pub.unsubscribe(self._on_receive,   "meshtastic.receive")
                pub.unsubscribe(self._on_connected, "meshtastic.connection.established")
                pub.unsubscribe(self._on_lost,      "meshtastic.connection.lost")
            except Exception:
                pass

    def on_app_focus(self) -> None:
        """Redraw when the terminal window regains focus after stepping away."""
        self.refresh(layout=True)
        self._tick_panels()
        self._tick_map()

    async def _boot_sequence(self) -> None:
        self._sys("━" * 60)
        self._sys("[bold bright_green]instant_meshenger[/bold bright_green] v1.0 // meshtastic terminal")
        self._sys("━" * 60)

        if not MESH_AVAILABLE and (self._port or self._host or self._ble is not None):
            self._sys("[yellow]meshtastic not installed — pip install meshtastic pypubsub[/yellow]")

        for msg in BOOT_MSGS:
            self._sys(f"  [dim green]▸[/dim green] {msg}")
            await asyncio.sleep(0.12)

        self._load_message_history()

        if self._demo_mode:
            self._sys("")
            self._sys("[yellow]◈ DEMO MODE[/yellow] — no device connected")
            self._sys("  use [cyan]--port /dev/ttyUSB0[/cyan] or [cyan]--host IP[/cyan] to connect live")
            self._sys("  radio: preset [cyan]LongFast[/cyan]  region [cyan]US[/cyan]  [dim](simulated)[/dim]")
            self.query_one("#header-bar", HeaderBar).freq_info = "LongFast/US (demo)"
            self._sys("")
            await asyncio.sleep(0.3)
            await self._init_demo()
        else:
            self._sys("")
            await self._connect_mesh()

        self.set_interval(0.5,  self._tick_header)
        self.set_interval(1.5,  self._tick_panels)
        self.set_interval(0.7,  self._tick_map)
        self.set_interval(20.0, lambda: self.refresh(layout=True))
        if self._demo_mode:
            self.set_interval(6.0, self._demo_event)
        self.call_after_refresh(self._tick_panels)
        self.call_after_refresh(self._tick_header)

        # Boot complete — dismiss the init overlay
        self.query_one("#init-overlay", InitOverlay).display = False

    # ── System Messages ───────────────────────────────────────────────────────

    def _sys(self, text: str) -> None:
        feed = self.query_one("#message-feed", RichLog)
        ts = datetime.now().strftime("%H:%M:%S")
        line = Text()
        line.append(f"[{ts}] ", style="dim green")
        line.append("◈ ", style="bright_green")
        line.append_text(Text.from_markup(text))
        feed.write(line)

    def _save_message_to_history(self, msg: MeshMessage) -> None:
        # Run in a thread so file I/O doesn't block the UI event loop
        def _write():
            try:
                records: list = []
                if HISTORY_FILE.exists():
                    try:
                        records = json.loads(HISTORY_FILE.read_text())
                    except Exception:
                        records = []
                records.append({
                    "timestamp": msg.timestamp,
                    "sender_id": msg.sender_id,
                    "sender_name": msg.sender_name,
                    "text": msg.text,
                    "channel": msg.channel,
                    "is_dm": msg.is_dm,
                    "is_system": msg.is_system,
                    "snr": msg.snr,
                    "rssi": msg.rssi,
                    "hops": msg.hops,
                })
                if len(records) > HISTORY_MAX_STORED:
                    records = records[-HISTORY_MAX_STORED:]
                HISTORY_FILE.write_text(json.dumps(records))
            except Exception:
                pass
        self.run_worker(_write, thread=True)

    def _load_message_history(self) -> None:
        if not HISTORY_FILE.exists():
            return
        try:
            records = json.loads(HISTORY_FILE.read_text())
        except Exception:
            return
        recent = records[-HISTORY_LOAD_COUNT:]
        if not recent:
            return
        self._sys(f"[dim]── {len(recent)} previous messages ──[/dim]")
        for r in recent:
            msg = MeshMessage(
                timestamp=r.get("timestamp", time.time()),
                sender_id=r.get("sender_id", ""),
                sender_name=r.get("sender_name", "?"),
                text=r.get("text", ""),
                channel=r.get("channel", 0),
                is_dm=r.get("is_dm", False),
                is_system=r.get("is_system", False),
                snr=r.get("snr"),
                rssi=r.get("rssi"),
                hops=r.get("hops", 0),
            )
            self._add_message(msg, save=False)
        self._sys("[dim]── end of history ──[/dim]")

    def _add_message(self, msg: MeshMessage, save: bool = True) -> None:
        if save and not msg.is_system:
            self._save_message_to_history(msg)
        feed = self.query_one("#message-feed", ClippedRichLog)
        ts = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
        color = self._node_colors.get(msg.sender_id, "white")

        t = Text()
        t.append(f"[{ts}] ", style="dim green")

        if msg.is_system:
            t.append("◈ ", style="dim green")
            t.append(_fix_keycap_width(msg.text), style="dim green")
            feed.write(t)
            return

        # Build plain prefix string to measure its visual width for hanging indent
        prefix_plain = f"[{ts}] "
        if msg.is_dm:
            t.append("⬤DM ", style="bold magenta")
            prefix_plain += "⬤DM "
        t.append(f"<{msg.sender_name}>", style=f"bold {color}")
        t.append("  ")
        prefix_plain += f"<{msg.sender_name}>  "
        indent = cell_len(prefix_plain)

        # Word-wrap message so continuation lines align under the message text
        content_w = max(20, (feed.size.width or 100) - 2)  # -2 for widget padding
        wrapped = _wrap_with_indent(_fix_keycap_width(msg.text), content_w - indent, indent)
        t.append(wrapped, style="white")

        meta = []
        if msg.snr is not None:
            snr_col = "bright_green" if msg.snr > 0 else "yellow" if msg.snr > -10 else "red"
            meta.append(Text(f"{msg.snr:+.0f}dB", style=f"dim {snr_col}"))
        if msg.rssi is not None:
            meta.append(Text(f"{msg.rssi}dBm", style="dim"))
        if msg.hops > 0:
            meta.append(Text(f"↝{msg.hops}", style="dim cyan"))

        if meta:
            t.append("  [", style="dim green")
            for i, m in enumerate(meta):
                if i:
                    t.append(" ", style="dim green")
                t.append_text(m)
            t.append("]", style="dim green")

        feed.write(t)

    # ── Timer Ticks ───────────────────────────────────────────────────────────

    def _tick_header(self) -> None:
        header = self.query_one("#header-bar", HeaderBar)
        online = sum(1 for n in self.nodes.values() if n.is_online)
        header.node_count = online
        header.channel_name = CHANNEL_NAMES[self.current_channel]
        if self._demo_mode:
            header.connection_status = "◈ DEMO"
        else:
            header.connection_status = "● LIVE" if self.interface else "◌ OFFLINE"
        header.refresh()

    def _tick_panels(self) -> None:
        nl = self.query_one("#node-list", NodeListPanel)
        nl.update_nodes(self.nodes, self._node_colors, self._selected_node)

        mp = self.query_one("#map-view", MapPanel)
        mp.update_nodes(self.nodes, self._node_colors)

        tp = self.query_one("#telemetry-view", TelemetryPanel)
        tp.update_data(self.nodes, self._node_colors, self._selected_node)

        # Force the right panel scroll container to reflow after height: auto children change
        self.query_one("#right-panel").refresh(layout=True)

    @on(NodeListPanel.NodeSelected)
    def handle_node_selected(self, event: NodeListPanel.NodeSelected) -> None:
        self._selected_node = event.node_id
        node = self.nodes.get(event.node_id)
        if node:
            self._sys(f"telemetry focused on [cyan]{node.long_name}[/cyan]")
        tp = self.query_one("#telemetry-view", TelemetryPanel)
        tp.update_data(self.nodes, self._node_colors, self._selected_node)
        nl = self.query_one("#node-list", NodeListPanel)
        nl.update_nodes(self.nodes, self._node_colors, self._selected_node)

    def _tick_map(self) -> None:
        self.query_one("#map-view", MapPanel).tick()

    def action_toggle_panels(self) -> None:
        panel = self.query_one("#right-panel")
        panel.display = not panel.display

    def action_refresh_screen(self) -> None:
        self.refresh(layout=True)
        self._tick_panels()
        self._tick_map()

    # ── Demo Mode ─────────────────────────────────────────────────────────────

    async def _init_demo(self) -> None:
        center_lat = 37.7749
        center_lon = -122.4194

        mine = MeshNode(
            node_id="!deadbeef",
            long_name="LocalNode",
            short_name="LCAL",
            latitude=center_lat,
            longitude=center_lon,
            altitude=24,
            battery_level=82,
            voltage=3.91,
            rssi=-78,
            snr=8.5,
            last_heard=time.time(),
            is_mine=True,
            hops_away=0,
            air_util_tx=11.2,
            channel_util=7.4,
        )
        self.nodes[mine.node_id] = mine
        self._assign_color(mine.node_id)

        num = random.randint(5, len(DEMO_NODES))
        for i in range(num):
            nid = f"!{random.randint(0x10000000, 0xFFFFFFFF):08x}"
            long_name, short_name = DEMO_NODES[i % len(DEMO_NODES)]
            hops = random.randint(0, 3)
            has_gps = random.random() > 0.25

            node = MeshNode(
                node_id=nid,
                long_name=long_name,
                short_name=short_name,
                latitude=center_lat + random.uniform(-0.06, 0.06) if has_gps else None,
                longitude=center_lon + random.uniform(-0.08, 0.08) if has_gps else None,
                altitude=random.randint(5, 200) if has_gps else None,
                battery_level=random.randint(8, 100),
                voltage=round(random.uniform(3.4, 4.2), 2),
                rssi=random.randint(-130, -65),
                snr=round(random.uniform(-15, 12), 1),
                last_heard=time.time() - random.randint(0, 700),
                is_mine=False,
                hops_away=hops,
                air_util_tx=round(random.uniform(0, 28), 1),
                channel_util=round(random.uniform(0, 18), 1),
            )
            self.nodes[nid] = node
            self._assign_color(nid)
            await asyncio.sleep(0.04)

        online_count = sum(1 for n in self.nodes.values() if n.is_online)
        self._sys(f"mesh online: [bright_green]{len(self.nodes)}[/bright_green] nodes discovered, "
                  f"[bright_green]{online_count}[/bright_green] online")
        self._sys(f"channel: [cyan]{CHANNEL_NAMES[self.current_channel]}[/cyan]  "
                  f"sf: [cyan]SF12[/cyan]  bw: [cyan]125kHz[/cyan]  cr: [cyan]4/5[/cyan]")
        self._sys("━" * 60)

        demo_nodes = [n for n in self.nodes.values() if not n.is_mine]
        for _ in range(4):
            if demo_nodes:
                n = random.choice(demo_nodes)
                self._add_message(MeshMessage(
                    timestamp=time.time() - random.randint(0, 600),
                    sender_id=n.node_id,
                    sender_name=n.short_name,
                    text=random.choice(DEMO_MESSAGES),
                    channel=0,
                    snr=n.snr,
                    rssi=n.rssi,
                    hops=n.hops_away,
                ), save=False)

        self._tick_panels()

    def _demo_event(self) -> None:
        """Simulate incoming packets."""
        if not self.nodes:
            return
        demo_nodes = [n for n in self.nodes.values() if not n.is_mine]
        if not demo_nodes:
            return

        roll = random.random()

        if roll < 0.45:
            # Incoming message
            node = random.choice(demo_nodes)
            snr = round(random.uniform(-15, 10), 1)
            rssi = random.randint(-130, -65)
            self._add_message(MeshMessage(
                timestamp=time.time(),
                sender_id=node.node_id,
                sender_name=node.short_name,
                text=random.choice(DEMO_MESSAGES),
                channel=self.current_channel,
                snr=snr,
                rssi=rssi,
                hops=node.hops_away,
            ), save=False)
            node.last_heard = time.time()
            node.snr = snr
            node.rssi = rssi

        elif roll < 0.65:
            # Telemetry update
            node = random.choice(demo_nodes)
            node.battery_level = max(0, min(100, (node.battery_level or 50) + random.randint(-3, 1)))
            node.snr = round(random.uniform(-15, 12), 1)
            node.rssi = random.randint(-130, -65)

        elif roll < 0.72:
            # Position update (small drift)
            node = random.choice([n for n in demo_nodes if n.latitude is not None] or demo_nodes)
            if node.latitude is not None:
                node.latitude += random.uniform(-0.0005, 0.0005)
                node.longitude += random.uniform(-0.0005, 0.0005)
                node.last_heard = time.time()

        elif roll < 0.77:
            # Rare: node goes offline/online
            node = random.choice(demo_nodes)
            if node.is_online:
                node.last_heard = time.time() - 1000
            else:
                node.last_heard = time.time()
                self._sys(f"[bright_green]◆ node online:[/bright_green] {node.long_name}")

    # ── Meshtastic Connection ─────────────────────────────────────────────────

    async def _connect_mesh(self) -> None:
        if not MESH_AVAILABLE:
            self._sys("[red]meshtastic library not available[/red]")
            self._sys("  run: [cyan]pip install meshtastic pypubsub[/cyan]")
            self._demo_mode = True
            await self._init_demo()
            return

        try:
            pub.subscribe(self._on_receive, "meshtastic.receive")
            pub.subscribe(self._on_connected, "meshtastic.connection.established")
            pub.subscribe(self._on_lost, "meshtastic.connection.lost")

            loop = asyncio.get_running_loop()

            if self._ble is not None:
                if not BLE_AVAILABLE:
                    self._sys("[red]BLE not available — run: pip install bleak[/red]")
                    raise RuntimeError("bleak not installed")
                target = self._ble if self._ble else None  # empty string = auto-discover
                label = self._ble if self._ble else "auto-discover"
                self._sys(f"connecting via BLE ([cyan]{label}[/cyan])...")
                self._sys("  [dim]make sure Bluetooth is on and node is nearby[/dim]")
                # BLEInterface blocks during scan+connect — run in thread so the UI stays alive
                self.interface = await loop.run_in_executor(
                    None, lambda: meshtastic.ble_interface.BLEInterface(target)
                )
            elif self._host:
                self._sys(f"connecting to [cyan]{self._host}[/cyan] via TCP...")
                self.interface = await loop.run_in_executor(
                    None, lambda: meshtastic.tcp_interface.TCPInterface(self._host)
                )
            else:
                self._sys(f"connecting to [cyan]{self._port}[/cyan] ({self._baud} baud)...")
                self.interface = await loop.run_in_executor(
                    None, lambda: meshtastic.serial_interface.SerialInterface(
                        self._port, debugOut=None
                    )
                )
        except Exception as e:
            self._sys(f"[red]connection failed:[/red] {e}")
            self._sys("[yellow]falling back to demo mode[/yellow]")
            self._demo_mode = True
            self.set_interval(6.0, self._demo_event)
            await self._init_demo()

    def _on_connected(self, interface, topic=pub.AUTO_TOPIC) -> None:
        def update():
            self._sys("[bright_green]◆ connected to device[/bright_green]")
            self._log_radio_config(interface)
            self._load_node_db(interface)
        self.call_from_thread(update)

    def _log_radio_config(self, interface) -> None:
        _PRESETS = {
            0: "LongFast", 1: "LongSlow", 2: "VLongSlow", 3: "MedSlow",
            4: "MedFast",  5: "ShortSlow", 6: "ShortFast", 7: "ShortTurbo",
        }
        _REGIONS = {
            0: "Unset", 1: "US", 2: "EU_433", 3: "EU_868", 4: "CN",
            5: "JP", 6: "ANZ", 7: "KR", 8: "TW", 9: "RU", 10: "IN",
            11: "NZ_865", 12: "TH", 13: "LORA_24",
        }
        try:
            lora = getattr(getattr(interface, "localConfig", None), "lora", None)
            if not lora:
                return
            preset_name = _PRESETS.get(getattr(lora, "modem_preset", 0), "Custom")
            region_val  = getattr(lora, "region", 0)
            region_name = _REGIONS.get(region_val, f"region:{region_val}")
            freq_offset = getattr(lora, "frequency_offset", 0)

            # Build log message
            parts = [
                f"preset [cyan]{preset_name}[/cyan]",
                f"region [cyan]{region_name}[/cyan]",
            ]
            freq_mhz = None

            # Primary channel exact frequency if available
            channels = getattr(interface, "channels", None)
            if channels:
                primary = next((c for c in channels.values()
                                if getattr(c, "role", None) == 1
                                or getattr(getattr(c, "role", None), "value", None) == 1), None)
                if primary:
                    fhz = getattr(primary, "frequency", None) or \
                          getattr(getattr(primary, "settings", None), "channel_num", None)
                    # Field is in Hz when > 1 MHz; values <= 100 are likely unset/zero
                    if fhz and fhz > 1_000_000:
                        freq_mhz = fhz / 1e6
                        parts.append(f"[cyan]{freq_mhz:.3f} MHz[/cyan]")

            if freq_offset:
                parts.append(f"offset [cyan]{freq_offset:+}Hz[/cyan]")
            self._sys("  radio: " + "  ".join(parts))

            # Push to header bar
            header = self.query_one("#header-bar", HeaderBar)
            label = f"{preset_name}/{region_name}"
            if freq_mhz:
                label += f" {freq_mhz:.3f}MHz"
            header.freq_info = label
        except Exception:
            pass  # radio config is informational only

    @staticmethod
    def _get_my_node_num(interface) -> Optional[int]:
        """Return this device's node number from whichever API version is present."""
        my_info = getattr(interface, "myInfo", None)
        if my_info:
            num = getattr(my_info, "my_node_num", None) or getattr(my_info, "myNodeNum", None)
            if num:
                return num
        local_node = getattr(interface, "localNode", None)
        if local_node:
            return getattr(local_node, "nodeNum", None)
        return None

    def _ensure_node(self, from_id: str) -> None:
        """Create a placeholder node entry if we haven't seen this ID before."""
        if from_id not in self.nodes:
            self.nodes[from_id] = MeshNode(node_id=from_id)
            self._assign_color(from_id)

    def _on_lost(self, interface, topic=pub.AUTO_TOPIC) -> None:
        def update():
            self._sys("[red]◌ connection lost[/red]")
        self.call_from_thread(update)

    def _load_node_db(self, interface) -> None:
        try:
            # Try interface.nodes first (keyed by "!hexid"); fall back to nodesByNum
            nodes_raw = interface.nodes or {}
            nodes_by_num = getattr(interface, "nodesByNum", {}) or {}

            # If nodesByNum has more entries, build nodes_raw from it
            if len(nodes_by_num) > len(nodes_raw):
                nodes_raw = {
                    f"!{num:08x}": data
                    for num, data in nodes_by_num.items()
                }

            self._sys(f"device nodeDB: [cyan]{len(nodes_raw)}[/cyan] entries")

            my_num = self._get_my_node_num(interface)

            # Read device metadata (firmware, hardware model) for my node
            meta = getattr(interface, "metadata", None)
            hw_model_str = None
            firmware_str = None
            if meta:
                fw = getattr(meta, "firmware_version", None)
                if fw:
                    firmware_str = str(fw)
                hwm = getattr(meta, "hw_model", None)
                if hwm is not None:
                    hw_model_str = getattr(hwm, "name", str(hwm)).replace("_", " ").title()

            for nid, data in nodes_raw.items():
                node = self._parse_node(nid, data)
                if my_num and nid == f"!{my_num:08x}":
                    node.is_mine = True
                    node.hw_model = hw_model_str
                    node.firmware = firmware_str
                self.nodes[nid] = node
                self._assign_color(nid)

            self._sys(f"loaded [bright_green]{len(self.nodes)}[/bright_green] nodes from device")
            self._tick_panels()

            # Schedule a delayed reload — some devices stream nodeinfo after initial connect
            self._interface_ref = interface
            self.set_timer(8.0, self._reload_node_db)
        except Exception as e:
            self._sys(f"[red]error loading node db:[/red] {e}")

    def _reload_node_db(self) -> None:
        """Second-pass node load — picks up nodes the device streams after initial connect."""
        interface = getattr(self, "_interface_ref", None)
        if interface is None:
            return
        try:
            nodes_raw = interface.nodes or {}
            nodes_by_num = getattr(interface, "nodesByNum", {}) or {}
            if len(nodes_by_num) > len(nodes_raw):
                nodes_raw = {f"!{num:08x}": data for num, data in nodes_by_num.items()}

            added = 0
            my_num = self._get_my_node_num(interface)

            for nid, data in nodes_raw.items():
                if nid not in self.nodes:
                    node = self._parse_node(nid, data)
                    if my_num and nid == f"!{my_num:08x}":
                        node.is_mine = True
                    self.nodes[nid] = node
                    self._assign_color(nid)
                    added += 1

            if added:
                self._sys(f"[dim]late nodeDB sync: +{added} nodes ({len(self.nodes)} total)[/dim]")
                self._tick_panels()
        except Exception as e:
            self._sys(f"[red]reload node db error:[/red] {e}")

    def _parse_node(self, node_id: str, data: dict) -> MeshNode:
        user = data.get("user", {})
        pos = data.get("position", {})
        metrics = data.get("deviceMetrics", {})
        # protobuf defaults unset int fields to 0; treat 0 as None so is_online works correctly
        last_heard = data.get("lastHeard") or data.get("last_heard") or None
        # Use explicit None checks — never use `or None` for coords because 0.0 is a valid value
        lat = pos["latitude"] if "latitude" in pos and pos["latitude"] is not None else None
        lon = pos["longitude"] if "longitude" in pos and pos["longitude"] is not None else None
        return MeshNode(
            node_id=node_id,
            long_name=user.get("longName", "Unknown"),
            short_name=user.get("shortName", "????"),
            latitude=lat,
            longitude=lon,
            altitude=pos.get("altitude") if pos.get("altitude") is not None else None,
            battery_level=metrics.get("batteryLevel") or None,
            voltage=metrics.get("voltage") or None,
            rssi=data.get("rssi") or None,
            snr=data.get("snr") if data.get("snr") is not None else None,
            last_heard=last_heard,
            hops_away=data.get("hopsAway", 0),
            air_util_tx=metrics.get("airUtilTx") or None,
            channel_util=metrics.get("channelUtilization") or None,
        )

    def _on_receive(self, packet: dict, interface) -> None:
        def process():
            decoded = packet.get("decoded", {})
            portnum = decoded.get("portnum", "")
            try:
                if portnum == "TEXT_MESSAGE_APP":
                    self._rx_text(packet, decoded)
                elif portnum == "TELEMETRY_APP":
                    self._rx_telemetry(packet, decoded)
                elif portnum == "POSITION_APP":
                    self._rx_position(packet, decoded)
                elif portnum == "NODEINFO_APP":
                    self._rx_nodeinfo(packet, decoded)
            except Exception as e:
                self._sys(f"[red]rx error:[/red] {e}")
        self.call_from_thread(process)

    def _rx_text(self, packet: dict, decoded: dict) -> None:
        from_id = f"!{packet.get('from', 0):08x}"
        to_id = f"!{packet.get('to', 0xFFFFFFFF):08x}"
        text = decoded.get("text", "")
        channel = packet.get("channel", 0)
        snr = packet.get("rxSnr")
        rssi = packet.get("rxRssi")

        self._ensure_node(from_id)
        node = self.nodes[from_id]
        node.last_heard = time.time()
        node.snr = snr
        node.rssi = rssi

        hops = packet.get("hopStart", 0) - packet.get("hopLimit", 0)
        # Use rxTime (when the device received it) rather than wall-clock now
        rx_time = packet.get("rxTime") or time.time()
        self._add_message(MeshMessage(
            timestamp=rx_time,
            sender_id=from_id,
            sender_name=node.short_name,
            text=text,
            channel=channel,
            is_dm=(to_id != "!ffffffff"),
            snr=snr,
            rssi=rssi,
            hops=max(0, hops),
        ))

    def _rx_telemetry(self, packet: dict, decoded: dict) -> None:
        from_id = f"!{packet.get('from', 0):08x}"
        metrics = decoded.get("telemetry", {}).get("deviceMetrics", {})
        self._ensure_node(from_id)
        n = self.nodes[from_id]
        if "batteryLevel" in metrics:
            n.battery_level = metrics["batteryLevel"]
        if "voltage" in metrics:
            n.voltage = metrics["voltage"]
        if "airUtilTx" in metrics:
            n.air_util_tx = metrics["airUtilTx"]
        if "channelUtilization" in metrics:
            n.channel_util = metrics["channelUtilization"]
        n.last_heard = time.time()

    def _rx_position(self, packet: dict, decoded: dict) -> None:
        from_id = f"!{packet.get('from', 0):08x}"
        pos = decoded.get("position", {})
        self._ensure_node(from_id)
        n = self.nodes[from_id]
        if "latitude" in pos:
            n.latitude = pos["latitude"]
        if "longitude" in pos:
            n.longitude = pos["longitude"]
        if "altitude" in pos:
            n.altitude = pos["altitude"]
        n.last_heard = time.time()

    def _rx_nodeinfo(self, packet: dict, decoded: dict) -> None:
        from_id = f"!{packet.get('from', 0):08x}"
        user = decoded.get("user", {})
        self._ensure_node(from_id)
        n = self.nodes[from_id]
        n.long_name = user.get("longName", n.long_name)
        n.short_name = user.get("shortName", n.short_name)
        n.last_heard = time.time()
        self._sys(f"node info: [{n.short_name}] [cyan]{n.long_name}[/cyan]")

    # ── Input Handling ────────────────────────────────────────────────────────

    @on(Input.Submitted, "#chat-input")
    def handle_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._handle_command(text)
        else:
            self._send_message(text)

    def _handle_command(self, raw: str) -> None:
        parts = raw.split(maxsplit=3)
        cmd = parts[0].lower()

        if cmd == "/help":
            self._cmd_help()
        elif cmd == "/nodes":
            self._cmd_nodes()
        elif cmd == "/info":
            self._cmd_info()
        elif cmd == "/map":
            self._cmd_map()
        elif cmd == "/clear":
            self.query_one("#message-feed", RichLog).clear()
            self._sys("feed cleared")
        elif cmd == "/channel" and len(parts) >= 2:
            self._cmd_channel(parts[1])
        elif cmd == "/dm" and len(parts) >= 3:
            self._cmd_dm(parts[1], " ".join(parts[2:]))
        elif cmd == "/select" and len(parts) >= 2:
            self._cmd_select(parts[1])
        else:
            self._sys(f"[red]unknown command:[/red] {cmd}  (try [cyan]/help[/cyan])")

    def _cmd_help(self) -> None:
        lines = [
            "━" * 55,
            "  [bold bright_green]instant_meshenger COMMANDS[/bold bright_green]",
            "━" * 55,
            "  [cyan]/help[/cyan]                 this help screen",
            "  [cyan]/nodes[/cyan]                list all known nodes",
            "  [cyan]/info[/cyan]                 my node info & telemetry",
            "  [cyan]/map[/cyan]                  show GPS coordinates of all nodes",
            "  [cyan]/dm <node> <msg>[/cyan]      direct message (name or !id)",
            "  [cyan]/channel <0-7>[/cyan]        switch channel",
            "  [cyan]/select <node>[/cyan]        focus telemetry on a node",
            "  [cyan]/clear[/cyan]                clear message feed",
            "━" * 55,
            "  node can be short name [cyan]CYPH[/cyan] or full id [cyan]!deadbeef[/cyan]",
        ]
        for line in lines:
            self._sys(line)

    def _cmd_nodes(self) -> None:
        self._sys("━" * 55)
        self._sys("  [bold]NODES[/bold]")
        for node in sorted(self.nodes.values(), key=lambda n: (not n.is_mine, not n.is_online)):
            status = "●" if node.is_online else "○"
            gps = "⌖" if node.latitude is not None else " "
            bat = f"{node.battery_level}%" if node.battery_level is not None else "─%"
            snr_str = f"{node.snr:+.0f}dB" if node.snr is not None else "─dB"
            me = " [ME]" if node.is_mine else ""
            self._sys(
                f"  {status} {gps} [{node.short_name}] {node.long_name}{me}"
                f"  bat:{bat}  snr:{snr_str}  hops:{node.hops_away}"
            )
        self._sys("━" * 55)

    def _cmd_info(self) -> None:
        mine = next((n for n in self.nodes.values() if n.is_mine), None)
        if not mine:
            self._sys("[red]my node not found[/red]")
            return
        self._sys("━" * 55)
        self._sys(f"  [bold]MY NODE[/bold]  {mine.long_name}  [{mine.short_name}]")
        self._sys(f"  id:      [cyan]{mine.node_id}[/cyan]")
        if mine.latitude is not None:
            self._sys(f"  pos:     {mine.latitude:.5f}°, {mine.longitude:.5f}°")
        if mine.altitude is not None:
            self._sys(f"  alt:     {mine.altitude}m")
        if mine.battery_level is not None:
            volt_str = f"  ({mine.voltage:.2f}V)" if mine.voltage is not None else ""
            self._sys(f"  battery: {mine.battery_level}%{volt_str}")
        if mine.air_util_tx is not None:
            self._sys(f"  air tx:  {mine.air_util_tx:.1f}%")
        if mine.channel_util is not None:
            self._sys(f"  ch util: {mine.channel_util:.1f}%")
        self._sys(f"  channel: [cyan]{CHANNEL_NAMES[self.current_channel]}[/cyan] ({self.current_channel})")
        self._sys("━" * 55)

    def _cmd_map(self) -> None:
        nodes_gps = [n for n in self.nodes.values() if n.latitude is not None]
        self._sys(f"━━ MAP ─── {len(nodes_gps)} nodes with GPS ──────────────────────")
        for n in nodes_gps:
            me = " ◂ you" if n.is_mine else ""
            self._sys(f"  [{n.short_name}] {n.long_name:16}  "
                      f"{n.latitude:.5f}°, {n.longitude:.5f}°{me}")
        if not nodes_gps:
            self._sys("  no gps data received yet")
        self._sys("━" * 55)

    def _cmd_channel(self, ch_str: str) -> None:
        try:
            ch = int(ch_str)
            if 0 <= ch <= 7:
                self.current_channel = ch
                self._sys(f"switched to channel [cyan]{ch}[/cyan] ({CHANNEL_NAMES[ch]})")
            else:
                self._sys("[red]channel must be 0–7[/red]")
        except ValueError:
            self._sys("[red]invalid channel number[/red]")

    def _cmd_dm(self, target: str, message: str) -> None:
        node = self._find_node(target)
        if not node:
            self._sys(f"[red]node not found:[/red] {target}")
            return

        mine = next((n for n in self.nodes.values() if n.is_mine), None)
        sender_name = f"{mine.short_name}→{node.short_name}" if mine else f"ME→{node.short_name}"
        sender_id = mine.node_id if mine else "!local"

        if not self._demo_mode and self.interface:
            try:
                self.interface.sendText(message, destinationId=node.node_id,
                                        channelIndex=self.current_channel)
            except Exception as e:
                self._sys(f"[red]send failed:[/red] {e}")
                return

        self._add_message(MeshMessage(
            timestamp=time.time(),
            sender_id=sender_id,
            sender_name=sender_name,
            text=message,
            is_dm=True,
            channel=self.current_channel,
        ))

    def _cmd_select(self, target: str) -> None:
        node = self._find_node(target)
        if not node:
            self._sys(f"[red]node not found:[/red] {target}")
            return
        self._selected_node = node.node_id
        self._sys(f"telemetry focused on [cyan]{node.long_name}[/cyan]")

    # ── Messaging ─────────────────────────────────────────────────────────────

    def _send_message(self, text: str) -> None:
        mine = next((n for n in self.nodes.values() if n.is_mine), None)
        sender_id = mine.node_id if mine else "!local"
        sender_name = mine.short_name if mine else "ME"

        if not self._demo_mode and self.interface:
            try:
                self.interface.sendText(text, channelIndex=self.current_channel)
            except Exception as e:
                self._sys(f"[red]send failed:[/red] {e}")
                return

        self._add_message(MeshMessage(
            timestamp=time.time(),
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            channel=self.current_channel,
        ))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_node(self, target: str) -> Optional[MeshNode]:
        target_up = target.upper().lstrip("!")
        for node in self.nodes.values():
            if node.short_name.upper() == target_up:
                return node
            if node.node_id.lstrip("!").upper().endswith(target_up):
                return node
        return None

    def _assign_color(self, node_id: str) -> str:
        if node_id not in self._node_colors:
            self._node_colors[node_id] = NODE_COLORS[self._color_idx % len(NODE_COLORS)]
            self._color_idx += 1
        return self._node_colors[node_id]


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="instant_meshenger — meshtastic terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--port", "-p", metavar="PORT",
                        help="serial port  e.g. /dev/ttyUSB0  COM3")
    parser.add_argument("--host", "-H", metavar="HOST",
                        help="TCP host  e.g. 192.168.1.100")
    parser.add_argument("--baud", "-b", metavar="BAUD", type=int, default=115200,
                        help="baud rate (default: 115200)")
    parser.add_argument("--ble", metavar="NAME_OR_ADDR", nargs="?", const="",
                        help="connect via Bluetooth LE  (omit value to auto-discover)")
    args = parser.parse_args()

    app = MeshLoungeApp(port=args.port, host=args.host, baud=args.baud, ble=args.ble)
    try:
        app.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        # stty sane: restores readline / arrow-key history after Textual raw mode.
        # os._exit: hard-kills BLE/serial interface threads without waiting;
        # calling interface.close() from the async unmount path deadlocks on BLE.
        try:
            subprocess.run(["stty", "sane"], check=False, capture_output=True)
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    main()
