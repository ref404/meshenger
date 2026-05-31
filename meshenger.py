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
from collections import deque
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

# ── Terminal compat shim ────────────────────────────────────────────────────
# Apple Terminal.app can't parse Textual's in-band window-resize probe
# (CSI ? 2048 $ p) and echoes a stray "p" in the top-left cell at startup.
# Neutralize the probe there; Textual falls back to SIGWINCH for resize, which
# Terminal.app handles fine, so nothing is lost.
if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
    try:
        from textual.drivers.linux_driver import LinuxDriver
        LinuxDriver._query_in_band_window_resize = lambda self: None
    except Exception:
        pass

# ── Constants ─────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
HISTORY_FILE = Path.home() / ".mesh_lounge_history.json"
HISTORY_MAX_STORED = 100
HISTORY_LOAD_COUNT = 10

CHANNEL_NAMES = [
    "LongFast", "LongSlow", "VLongSlow", "MedSlow",
    "MedFast", "ShortSlow", "ShortFast", "ShortTurbo",
]

# Muted, desaturated name colors — distinct hues that sit calmly on the dark theme.
NODE_COLORS = [
    "#9ab87f", "#7fb0a6", "#c2a86a", "#b08fb0",
    "#8aa4c2", "#c28f8f", "#9fb0c8", "#c2a0c2",
    "#bfa37a", "#cf9aa6", "#a6c28f", "#8fbfc2",
    "#b6c28f", "#c89fb4", "#8fc2a8", "#a4b0d0",
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
        color = "#a7c189" if lvl > 60 else "yellow" if lvl > 25 else "red"
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
            bars, color = "▁▃▅▇", "#a7c189"
        elif snr >= 0:
            bars, color = "▁▃▅░", "#8ba672"
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
        color = "#a7c189" if self.hops_away == 0 else "yellow" if self.hops_away <= 2 else "orange3"
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
    packets_per_min = reactive(0)
    channel_util = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._start = time.time()

    @staticmethod
    def _vu_bar(pct: Optional[float], width: int = 10) -> Tuple[str, str]:
        """Return (bar, color) for a 0-100% utilization gauge."""
        if pct is None:
            return "─" * width, "dim #8ba672"
        pct = max(0.0, min(100.0, pct))
        fill = round(pct / 100 * width)
        color = "#a7c189" if pct < 25 else "yellow" if pct < 50 else "red"
        return "▰" * fill + "▱" * (width - fill), color

    def render(self) -> Text:
        t = Text()
        t.append(self.LOGO, style="bold #a7c189")
        t.append("\n")
        elapsed = int(time.time() - self._start)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        uptime = f"{h:02d}:{m:02d}:{s:02d}"
        ts = datetime.now().strftime("%H:%M:%S")

        sep = Text("  │  ", style="dim #8ba672")

        # Status row
        t.append("  ")
        if "LIVE" in self.connection_status:
            t.append(f" {self.connection_status} ", style="bold #a7c189 on #1a2618")
        elif "DEMO" in self.connection_status:
            t.append(f" {self.connection_status} ", style="bold yellow on #262013")
        else:
            t.append(f" {self.connection_status} ", style="bold red on #261515")
        t.append_text(sep)
        t.append("CH:", style="dim #8ba672")
        t.append(f" {self.channel_name}", style="bold #7ba9a0")
        t.append_text(sep)
        t.append("NODES:", style="dim #8ba672")
        t.append(f" {self.node_count}", style="bold #a7c189")
        t.append_text(sep)
        t.append("PKT/M:", style="dim #8ba672")
        t.append(f" {self.packets_per_min}", style="bold #a7c189")
        t.append_text(sep)
        t.append("UPTIME:", style="dim #8ba672")
        t.append(f" {uptime}", style="bold #8ba672")
        t.append_text(sep)
        t.append("UTC:", style="dim #8ba672")
        t.append(f" {ts}", style="bold #8ba672")
        t.append("\n")

        # Frequency + channel-util gauge + hints row
        t.append("  ")
        t.append("FREQ:", style="dim #8ba672")
        t.append(f" {self.freq_info if self.freq_info else '—'}", style="bold #7ba9a0")
        t.append_text(sep)
        t.append("CHUTIL ", style="dim #8ba672")
        bar, bar_color = self._vu_bar(self.channel_util)
        t.append(bar, style=bar_color)
        util_txt = f" {self.channel_util:.0f}%" if self.channel_util is not None else " —"
        t.append(util_txt, style=f"bold {bar_color}")
        t.append_text(sep)
        t.append("/help  [^t map]  [^g view]  [^l links]", style="dim")

        return t


class NodeListPanel(Static):
    """Scrollable node list with signal/battery/hops. Focusable for j/k nav."""

    can_focus = True

    BINDINGS = [
        Binding("down,j", "cursor(1)", "Down", show=False),
        Binding("up,k",   "cursor(-1)", "Up", show=False),
        Binding("enter",  "activate", "DM", show=False),
        Binding("escape", "leave", "Back", show=False),
    ]

    class NodeSelected(Message):
        def __init__(self, node_id: str) -> None:
            super().__init__()
            self.node_id = node_id

    class NodeActivated(Message):
        """User pressed enter on a node — open a DM to it."""
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

    def _sorted(self) -> List[MeshNode]:
        return sorted(
            self._mesh_nodes.values(),
            key=lambda n: (not n.is_mine, not n.is_online, n.long_name.lower()),
        )

    def update_nodes(self, nodes: Dict[str, MeshNode], node_colors: Dict[str, str],
                     selected: Optional[str] = None):
        self._mesh_nodes = nodes
        self._node_colors = node_colors
        self._selected_node_id = selected
        self.refresh(layout=True)

    def on_resize(self, event) -> None:
        self.refresh(layout=True)

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    def on_click(self, event) -> None:
        self.focus()
        y = event.y
        for start, end, node_id in self._node_line_map:
            if start <= y < end:
                self.post_message(NodeListPanel.NodeSelected(node_id))
                break

    def line_of(self, node_id: str) -> Optional[int]:
        for start, _end, nid in self._node_line_map:
            if nid == node_id:
                return start
        return None

    def action_cursor(self, delta: int) -> None:
        order = [n.node_id for n in self._sorted()]
        if not order:
            return
        if self._selected_node_id in order:
            idx = order.index(self._selected_node_id)
            idx = max(0, min(len(order) - 1, idx + delta))
        else:
            idx = 0
        self.post_message(NodeListPanel.NodeSelected(order[idx]))

    def action_activate(self) -> None:
        if self._selected_node_id:
            self.post_message(NodeListPanel.NodeActivated(self._selected_node_id))

    def action_leave(self) -> None:
        try:
            self.app.query_one("#chat-input").focus()
        except Exception:
            pass

    def render(self) -> Text:
        t = Text()
        self._node_line_map = []

        online = sum(1 for n in self._mesh_nodes.values() if n.is_online)
        total = len(self._mesh_nodes)

        # content_w is the renderable text width (widget width minus padding 0 1)
        content_w = max(4, self.size.width - 2)
        sep_w = content_w  # separator exactly fills content width — never wraps

        t.append("◈ NODES ", style="bold #a7c189")
        t.append(f"[{online} online / {total} total]\n", style="dim #8ba672")
        if self.has_focus:
            t.append("  ↑↓/jk move · ⏎ dm · esc\n", style="#7ba9a0")
        else:
            t.append("  tab to navigate ›\n", style="dim #8ba672")
        t.append("━" * sep_w + "\n", style="dim #8ba672")

        # visual_offset: extra visual rows the header line adds beyond its one logical \n
        # (wrapping only; separator is exactly sep_w so it never wraps)
        header_str = f"◈ NODES [{online} online / {total} total]"
        header_visual_rows = math.ceil(cell_len(header_str) / content_w)
        visual_offset = header_visual_rows - 1

        if not self._mesh_nodes:
            t.append("\n   scanning for nodes...\n", style="dim #8ba672")
            t.append("   ▒▒▒░░░░░░░░░░░░░░░░\n", style="dim #8ba672")
            return t

        for node in self._sorted():
            color = self._node_colors.get(node.node_id, "white")
            is_selected = node.node_id == self._selected_node_id
            bg = " on #1a2618" if is_selected else ""

            start_line = t.plain.count('\n') + visual_offset

            # Status dot + name
            if node.is_mine:
                t.append("◉ ", style=f"bold #a7c189{bg}")
                name = node.long_name[:16]
                t.append(f"{name}", style=f"bold {color}{bg}")
                t.append(" ◂you\n", style=f"dim #a7c189{bg}")
            elif node.is_online:
                t.append("● ", style=f"#a7c189{bg}")
                t.append(f"{node.long_name[:16]}\n", style=f"{color}{bg}")
            else:
                t.append("○ ", style=f"dim{bg}")
                t.append(f"{node.long_name[:16]}\n", style=f"dim{bg}")

            # Node ID
            short_id = node.node_id.lstrip("!")[-8:]
            t.append(f"  !{short_id}", style="dim #8ba672")
            t.append(f"  [{node.short_name}]\n", style="dim #7ba9a0")

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
                    color_seen = "dim #8ba672" if node.is_online else "dim red"
                    t.append(f"  seen {seen}\n", style=color_seen)

            # GPS indicator
            if node.latitude is not None:
                t.append("  ⌖ GPS\n", style="dim #7ba9a0")

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

    _PULSE_SECS = 1.6          # how long a node "blooms" after a packet
    _PULSE_GLYPHS = "❖✦✸✦"     # bloom animation frames

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._mesh_nodes: Dict[str, MeshNode] = {}
        self._node_colors: Dict[str, str] = {}
        self._pulse: Dict[str, float] = {}   # node_id -> last packet time
        self._blink = True
        self._frame = 0
        self._show_links = False             # ctrl+l toggles mesh link lines
        self._mode = "gps"                   # "gps" grid, or "hops" topology rings
        self._trace: List[str] = []          # ordered node_ids of an active traceroute
        self._trace_frame = 0                # animation clock for the trace comet

    def on_resize(self, event) -> None:
        self.refresh(layout=True)

    def update_nodes(self, nodes: Dict[str, MeshNode], node_colors: Dict[str, str],
                     pulse: Optional[Dict[str, float]] = None):
        self._mesh_nodes = nodes
        self._node_colors = node_colors
        if pulse is not None:
            self._pulse = pulse
        self.refresh(layout=True)

    def toggle_links(self) -> bool:
        self._show_links = not self._show_links
        self.refresh(layout=True)
        return self._show_links

    def toggle_mode(self) -> str:
        self._mode = "hops" if self._mode == "gps" else "gps"
        self.refresh(layout=True)
        return self._mode

    def set_trace(self, route: List[str]) -> None:
        self._trace = list(route)
        self._trace_frame = 0
        self.refresh(layout=True)

    def tick(self):
        self._blink = not self._blink
        self._frame += 1
        if self._trace:
            self._trace_frame += 1
            if self._trace_frame > 48:   # comet has run its course — clear it
                self._trace = []
        self.refresh()

    # ── geometry helpers ──────────────────────────────────────────────────────

    def _blank_cells(self) -> List[List[Tuple[str, str]]]:
        return [[(" ", "")] * self.MAP_W for _ in range(self.MAP_H)]

    def _node_glyph(self, node: MeshNode, now: float) -> Tuple[str, str]:
        """Return (char, style) for a node, blooming if it transmitted recently."""
        color = self._node_colors.get(node.node_id) or "#8ba672"
        if (now - self._pulse.get(node.node_id, 0.0)) < self._PULSE_SECS:
            glyph = self._PULSE_GLYPHS[self._frame % len(self._PULSE_GLYPHS)]
            return glyph, f"bold {color}"
        if node.is_mine:
            return ("◉" if self._blink else "○"), f"bold {color}"
        if node.is_online:
            return "◆", f"bold {color}"
        return "◇", "dim"

    @staticmethod
    def _line_cells(x0: int, y0: int, x1: int, y1: int) -> List[Tuple[int, int]]:
        """Bresenham line between two grid cells."""
        out = []
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            out.append((x0, y0))
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
        return out

    def _compute_gps(self) -> Tuple[List[List[Tuple[str, str]]], dict]:
        """GPS grid: nodes plotted by lat/lon, with range rings around 'you'."""
        cells = self._blank_cells()
        W, H = self.MAP_W, self.MAP_H
        now = time.time()

        # Sparse reference dots at every 8 cols × 4 rows
        for ry in range(0, H, 4):
            for rx in range(0, W, 8):
                cells[ry][rx] = ("·", "dim")

        nodes_gps = [n for n in self._mesh_nodes.values() if n.latitude is not None]
        if not nodes_gps:
            return cells, {"rings": [], "span_km": None}

        lats = [n.latitude for n in nodes_gps]
        lons = [n.longitude for n in nodes_gps]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        lat_span = max(max_lat - min_lat, 0.005)
        lon_span = max(max_lon - min_lon, 0.005)
        pad_lat, pad_lon = lat_span * 0.18, lon_span * 0.18
        min_lat -= pad_lat; max_lat += pad_lat
        min_lon -= pad_lon; max_lon += pad_lon
        lat_span = max_lat - min_lat
        lon_span = max_lon - min_lon

        klat = 111.0
        klon = 111.0 * math.cos(math.radians((min_lat + max_lat) / 2))

        def to_cell(lat, lon):
            px = int((lon - min_lon) / lon_span * (W - 1))
            py = int((1 - (lat - min_lat) / lat_span) * (H - 1))
            return max(0, min(W - 1, px)), max(0, min(H - 1, py))

        def cell_geo(px, py):
            lon = min_lon + px / (W - 1) * lon_span
            lat = min_lat + (1 - py / (H - 1)) * lat_span
            return lat, lon

        mine = next((n for n in nodes_gps if n.is_mine), None)
        rings: List[float] = []
        if mine is not None:
            mlat, mlon = mine.latitude, mine.longitude

            def km(lat, lon):
                return math.hypot((lat - mlat) * klat, (lon - mlon) * klon)

            far = max((km(n.latitude, n.longitude) for n in nodes_gps), default=0.0)
            if far > 0.02:
                step = far / 3.0
                rings = [step, 2 * step, 3 * step]
                tol = step * 0.16
                for py in range(H):
                    for px in range(W):
                        if cells[py][px][0] not in (" ", "·"):
                            continue
                        d = km(*cell_geo(px, py))
                        if any(abs(d - r) < tol for r in rings):
                            cells[py][px] = ("∘", "dim #2c3a2c")

        # Mesh link lines (you → each node), drawn under the node glyphs
        if self._show_links and mine is not None:
            mx, my = to_cell(mine.latitude, mine.longitude)
            for n in nodes_gps:
                if n.is_mine:
                    continue
                nx, ny = to_cell(n.latitude, n.longitude)
                for lx, ly in self._line_cells(mx, my, nx, ny):
                    if cells[ly][lx][0] in (" ", "·", "∘"):
                        cells[ly][lx] = ("∙", "dim #3a4d36")

        for n in nodes_gps:
            px, py = to_cell(n.latitude, n.longitude)
            cells[py][px] = self._node_glyph(n, now)

        # Traceroute comet: reveal the path hop-by-hop with a bright travelling head
        if self._trace:
            pos = {n.node_id: to_cell(n.latitude, n.longitude) for n in nodes_gps}
            pts = [pos[nid] for nid in self._trace if nid in pos]
            path: List[Tuple[int, int]] = []
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                seg = self._line_cells(ax, ay, bx, by)
                if path and seg and seg[0] == path[-1]:
                    seg = seg[1:]
                path.extend(seg)
            if len(path) >= 2:
                head = min(len(path) - 1, self._trace_frame * 2)
                for i, (cx, cy) in enumerate(path):
                    if i > head:
                        break
                    if i == head:
                        cells[cy][cx] = ("◉", "bold #d7ffd0")
                    elif cells[cy][cx][0] in (" ", "·", "∘", "∙"):
                        cells[cy][cx] = ("•", "#a7c189")

        span_km = None
        if len(nodes_gps) >= 2:
            span_km = math.hypot((max(lats) - min(lats)) * klat,
                                 (max(lons) - min(lons)) * klon)
        return cells, {"rings": rings, "span_km": span_km}

    def _compute_hops(self) -> Tuple[List[List[Tuple[str, str]]], dict]:
        """Topology view: 'you' at center, nodes on concentric rings by hop count."""
        cells = self._blank_cells()
        W, H = self.MAP_W, self.MAP_H
        now = time.time()
        cx, cy = W // 2, H // 2

        nodes = list(self._mesh_nodes.values())
        mine = next((n for n in nodes if n.is_mine), None)
        others = [n for n in nodes if not n.is_mine]
        max_hop = max((max(1, n.hops_away) for n in others), default=1)
        rmax = min(cx - 1, (cy - 1) * 2)

        def ring_radii(h):
            rx = rmax * h / max_hop
            return rx, rx * 0.5

        # Concentric hop rings
        for h in range(1, max_hop + 1):
            rx, ry = ring_radii(h)
            steps = max(28, int(rx * 5))
            for s in range(steps):
                ang = 2 * math.pi * s / steps
                px = int(round(cx + rx * math.cos(ang)))
                py = int(round(cy + ry * math.sin(ang)))
                if 0 <= px < W and 0 <= py < H and cells[py][px][0] == " ":
                    cells[py][px] = ("·", "dim #2c3a2c")

        byhop: Dict[int, List[MeshNode]] = {}
        for n in others:
            byhop.setdefault(max(1, n.hops_away), []).append(n)

        for h, group in byhop.items():
            rx, ry = ring_radii(h)
            for i, n in enumerate(group):
                ang = 2 * math.pi * i / len(group) + h * 0.7
                px = max(0, min(W - 1, int(round(cx + rx * math.cos(ang)))))
                py = max(0, min(H - 1, int(round(cy + ry * math.sin(ang)))))
                if self._show_links:
                    for lx, ly in self._line_cells(cx, cy, px, py):
                        if cells[ly][lx][0] in (" ", "·"):
                            cells[ly][lx] = ("∙", "dim #3a4d36")
                cells[py][px] = self._node_glyph(n, now)

        cells[cy][cx] = self._node_glyph(mine, now) if mine else ("◎", "dim #8ba672")
        return cells, {"max_hop": max_hop}

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

    def _draw_cells(self, t: Text, cells: List[List[Tuple[str, str]]]) -> None:
        tl, tm, tr = self._border_top()
        bl, bm, br = self._border_bot()
        t.append("┌" + tl, style="dim #8ba672")
        t.append(tm, style="dim #7ba9a0")
        t.append(tr + "┐\n", style="dim #8ba672")
        mid_row = self.MAP_H // 2
        for row_i, row in enumerate(cells):
            edge_style = "dim #7ba9a0" if row_i == mid_row else "dim #8ba672"
            t.append("W" if row_i == mid_row else "│", style=edge_style)
            for ch, st in row:
                t.append(ch, style=st)
            t.append("E\n" if row_i == mid_row else "│\n", style=edge_style)
        t.append("└" + bl, style="dim #8ba672")
        t.append(bm, style="dim #7ba9a0")
        t.append(br + "┘\n", style="dim #8ba672")

    def _scanning_box(self, t: Text) -> None:
        tl, tm, tr = self._border_top()
        bl, bm, br = self._border_bot()
        t.append("┌" + tl, style="dim #8ba672")
        t.append(tm, style="dim #7ba9a0")
        t.append(tr + "┐\n", style="dim #8ba672")
        mid = self.MAP_H // 2
        for i in range(self.MAP_H):
            t.append("│", style="dim #8ba672")
            if i == mid - 1:
                msg = "awaiting gps fix"
            elif i == mid:
                msg = f"{'⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[self._frame % 10]} scanning"
            else:
                msg = None
            if msg is None:
                t.append(" " * self.MAP_W)
            else:
                pad = self.MAP_W - len(msg)
                t.append(" " * (pad // 2) + msg + " " * (pad - pad // 2), style="dim #8ba672")
            t.append("│\n", style="dim #8ba672")
        t.append("└" + bl, style="dim #8ba672")
        t.append(bm, style="dim #7ba9a0")
        t.append(br + "┘\n", style="dim #8ba672")

    def _draw_hints(self, t: Text) -> None:
        mode_label = "topology" if self._mode == "hops" else "gps grid"
        links_state = "on" if self._show_links else "off"
        t.append(f"[^g] {mode_label}  [^l] links:{links_state}\n", style="dim #7ba9a0")

    def render(self) -> Text:
        t = Text()

        if self._mode == "hops":
            cells, meta = self._compute_hops()
            mh = meta["max_hop"]
            t.append("◈ MESH TOPOLOGY", style="bold #a7c189")
            t.append(f" ── {mh} hop{'s' if mh != 1 else ''} out\n", style="dim #8ba672")
            t.append("━" * (self.MAP_W + 2) + "\n", style="dim #8ba672")
            self._draw_cells(t, cells)
            t.append("◉ you  ◆ online  ◇ offline  ✦ tx\n", style="dim #8ba672")
            self._draw_hints(t)
            t.append("rings = hops from you\n", style="dim #8ba672")
            return t

        cells, meta = self._compute_gps()
        nodes_gps = [n for n in self._mesh_nodes.values() if n.latitude is not None]
        t.append("◈ GRID MAP", style="bold #a7c189")
        if nodes_gps:
            t.append(f" ── {len(nodes_gps)} node{'s' if len(nodes_gps) != 1 else ''}",
                     style="dim #8ba672")
        t.append("\n")
        t.append("━" * (self.MAP_W + 2) + "\n", style="dim #8ba672")

        if not nodes_gps:
            self._scanning_box(t)
            self._draw_hints(t)
            return t

        self._draw_cells(t, cells)
        t.append("◉ you  ◆ online  ◇ offline  ✦ tx\n", style="dim #8ba672")
        self._draw_hints(t)

        mine = next((n for n in nodes_gps if n.is_mine), None)
        if mine:
            t.append(f"LAT  {mine.latitude:>10.5f}°\n", style="dim #7ba9a0")
            t.append(f"LON  {mine.longitude:>10.5f}°\n", style="dim #7ba9a0")
            if mine.altitude is not None:
                t.append(f"ALT  {mine.altitude:>8}m\n", style="dim #7ba9a0")
        if meta["rings"]:
            r = meta["rings"]
            t.append(f"RING ≈ {r[0]:.1f}/{r[1]:.1f}/{r[2]:.1f}km\n", style="dim #7ba9a0")
        if meta["span_km"]:
            t.append(f"SPAN ≈ {meta['span_km']:.1f}km\n", style="dim #8ba672")
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
        t.append("◈ TELEMETRY\n", style="bold #a7c189")
        t.append("━" * 28 + "\n", style="dim #8ba672")

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
        t.append(f"  !{hex_id.lower()}", style="dim #8ba672")
        t.append(f"  [{node.short_name}]\n", style="dim #7ba9a0")
        t.append(f"  {mac}\n", style="dim #8ba672")

        if node.hw_model:
            t.append(f"  {'hw':<11}", style="dim #8ba672")
            t.append(f"{node.hw_model}\n", style="#7ba9a0")
        if node.firmware:
            t.append(f"  {'firmware':<11}", style="dim #8ba672")
            t.append(f"{node.firmware}\n", style="#7ba9a0")
        t.append("\n")

        def row(label: str, value, style: str = "#a7c189"):
            t.append(f"  {label:<11}", style="dim #8ba672")
            if isinstance(value, Text):
                t.append_text(value)
            else:
                t.append(str(value), style=style)
            t.append("\n")

        if node.battery_level is not None:
            t.append(f"  {'battery':<11}", style="dim #8ba672")
            t.append_text(node.battery_text())
            t.append("\n")

        if node.voltage is not None:
            row("voltage", f"{node.voltage:.2f}V")

        if node.rssi is not None:
            rc = "#a7c189" if node.rssi > -100 else "yellow" if node.rssi > -120 else "red"
            row("rssi", f"{node.rssi} dBm", rc)

        if node.snr is not None:
            t.append(f"  {'snr':<11}", style="dim #8ba672")
            t.append_text(node.signal_text())
            t.append("\n")

        if not node.is_mine:
            t.append(f"  {'hops':<11}", style="dim #8ba672")
            t.append_text(node.hops_text())
            t.append("\n")

        if node.air_util_tx is not None:
            util_color = "#a7c189" if node.air_util_tx < 20 else "yellow" if node.air_util_tx < 50 else "red"
            row("air tx", f"{node.air_util_tx:.1f}%", util_color)

        if node.channel_util is not None:
            ch_color = "#a7c189" if node.channel_util < 15 else "yellow" if node.channel_util < 40 else "red"
            row("ch util", f"{node.channel_util:.1f}%", ch_color)

        if node.latitude is not None:
            row("lat", f"{node.latitude:.5f}°", "#7ba9a0")
            row("lon", f"{node.longitude:.5f}°", "#7ba9a0")
            if node.altitude is not None:
                row("alt", f"{node.altitude}m", "#7ba9a0")

        return t


# ── Waterfall ─────────────────────────────────────────────────────────────────

class WaterfallBar(Static):
    """SDR-style scrolling activity strip — one column per recent packet,
    height = signal strength (SNR), color = sending node."""

    _BLOCKS = " ▁▂▃▄▅▆▇█"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._events: deque = deque(maxlen=512)  # (color, level 1..8)
        self._frame = 0

    def on_mount(self) -> None:
        self.set_interval(0.2, self._tick)

    def _tick(self) -> None:
        self._frame += 1
        self.refresh()

    def push(self, color: Optional[str], snr: Optional[float]) -> None:
        if snr is None:
            level = 4
        else:
            level = max(1, min(8, int((snr + 20) / 34 * 8) + 1))
        self._events.append((color or "#8ba672", level))
        self.refresh()

    def render(self) -> Text:
        w = max(8, self.size.width)
        t = Text(no_wrap=True, overflow="crop")
        t.append("RF ", style="dim #8ba672")
        cols = max(1, w - 5)  # leave room for "RF " + live cursor
        recent = list(self._events)[-cols:]
        pad = cols - len(recent)
        if pad > 0:
            t.append("▁" * pad, style="dim #1c241a")
        for color, level in recent:
            t.append(self._BLOCKS[level], style=color)
        t.append("▍" if self._frame % 2 else "▏", style="#a7c189")
        return t


# ── CSS ───────────────────────────────────────────────────────────────────────

APP_CSS = """
Screen {
    background: ansi_default;
    color: #8ba672;
    layers: base overlay;
}

#init-overlay {
    layer: overlay;
    offset: 0 0;
    width: 100%;
    height: 100%;
    background: ansi_default;
    content-align: center middle;
    color: #a7c189;
}

#header-bar {
    height: 6;
    background: ansi_default;
    border-bottom: solid #243024;
    padding: 1 2;
    color: #a7c189;
}

#waterfall {
    height: 1;
    background: ansi_default;
    color: #8ba672;
    padding: 0 1;
}

#body {
    height: 1fr;
    background: ansi_default;
}

#left-scroll {
    width: 30;
    border-right: solid #243024;
    background: ansi_default;
}

#node-list {
    width: 100%;
    height: auto;
    min-height: 100%;
    padding: 0 1;
    background: ansi_default;
}

#message-feed {
    width: 1fr;
    height: 1fr;
    background: ansi_default;
    scrollbar-color: #2c3a2c;
    scrollbar-background: ansi_default;
    padding: 0 1;
}

#right-panel {
    width: 38;
    height: 1fr;
    border-left: solid #243024;
    background: ansi_default;
}

#map-view {
    width: 100%;
    height: auto;
    background: ansi_default;
    padding: 0;
    border-bottom: solid #243024;
}

#telemetry-view {
    width: 100%;
    height: auto;
    background: ansi_default;
    padding: 0 1;
}

#input-container {
    height: 3;
    background: ansi_default;
    border-top: solid #3a4d36;
    align: left middle;
    padding: 0 1;
}

#prompt-label {
    color: #a7c189;
    width: auto;
    background: ansi_default;
    text-style: bold;
}

#chat-input {
    background: ansi_default;
    color: #a7c189;
    border: none;
    width: 1fr;
}

#chat-input:focus {
    border: none;
    background: ansi_default;
}

#sniffer {
    height: 13;
    display: none;
    background: ansi_default;
    color: #8ba672;
    border: round #243024;
    padding: 0 1;
    scrollbar-color: #2c3a2c;
    scrollbar-background: ansi_default;
}
"""


# ── Emoji handling ────────────────────────────────────────────────────────────
# Meshtastic names/messages routinely contain emoji. Terminal fonts render emoji
# at widths that disagree with Rich's cell math, which shifts the monospace grid.
# We strip decorative emoji but PRESERVE number-emoji "tapbacks" (people react to
# a message with 1..10 keycap emoji to indicate hop count) as plain digits.

# digit/#/* + optional VS16 + combining enclosing keycap  ->  bare char  (3-keycap -> 3)
_KEYCAP_RE = re.compile('([0-9#*])️?⃣')

# Pictograph / emoji ranges to drop (decorative). Excludes U+25xx geometric shapes
# (used by the UI itself) and arrows.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # SMP emoji: faces, animals, symbols
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U00002B00-\U00002B23\U00002B25-\U00002BFF"   # misc symbols & arrows (keep ⬤ U+2B24)
    "\U00002300-\U000023CD\U000023CF-\U000023FF"   # misc technical (keep ⏎ U+23CE)
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000200D"              # zero-width joiner
    "\U000020E3"              # stray combining enclosing keycap
    "]+"
)

def _strip_emoji(text: str) -> str:
    """Drop decorative emoji but keep number-emoji tapbacks as plain digits."""
    if not text:
        return text
    text = text.replace('\U0001F51F', '10')   # keycap-ten -> 10 (before range-strip)
    text = _KEYCAP_RE.sub(r'\1', text)         # 3-keycap -> 3  (preserve hop tapbacks)
    text = _EMOJI_RE.sub('', text)             # drop the rest
    return re.sub(r'\s{2,}', ' ', text).strip()

def _clean_name(name: str, fallback: str) -> str:
    """Emoji-stripped node name; if nothing readable remains, use `fallback`."""
    cleaned = _strip_emoji(name or "")
    return cleaned if cleaned else fallback


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

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

class InitOverlay(Static):
    """Full-screen boot splash: spinner + boot log that types in line-by-line."""

    _frame = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._lines: List[str] = []

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.08, self._tick)

    def _tick(self) -> None:
        if not self.display:  # boot finished and dismissed the overlay — stop ticking
            self._timer.stop()
            return
        self._frame += 1

    def log_line(self, text: str) -> None:
        self._lines.append(text)
        self.refresh()

    def render(self) -> Text:
        spin = _SPINNER[self._frame % len(_SPINNER)]
        dots = "." * ((self._frame // 3) % 4)
        t = Text(justify="center")
        t.append("\n")
        t.append(HeaderBar.LOGO + "\n\n", style="bold #a7c189")
        t.append("mesh network terminal\n\n", style="dim #8ba672")
        t.append(f"{spin} ", style="bold #a7c189")
        t.append(f"initializing meshenger{dots}\n\n", style="bold #a7c189")
        shown = self._lines[-6:]
        for i, ln in enumerate(shown):
            last = i == len(shown) - 1
            t.append(f"{spin if last else '✓'} {ln}\n",
                     style="#a7c189" if last else "dim #8ba672")
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
        Binding("ctrl+l", "toggle_links",   "Map links",      priority=True),
        Binding("ctrl+g", "toggle_mapmode", "Map mode",       priority=True),
        Binding("ctrl+p", "toggle_sniffer", "Packet capture", priority=True),
        Binding("tab",    "focus_nodes",    "Node nav",       priority=True),
    ]

    def __init__(self, port: Optional[str] = None, host: Optional[str] = None,
                 baud: int = 115200, ble: Optional[str] = None, **kwargs):
        # ansi_color=True keeps Textual in native-ANSI mode so `background: ansi_default`
        # emits the terminal's real default background instead of a flattened RGB color.
        super().__init__(ansi_color=True, **kwargs)
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
        self._rf_pulse: Dict[str, float] = {}  # node_id -> last packet time (map bloom + waterfall)
        self._pkt_times: deque = deque(maxlen=4000)  # packet arrival times (for packets/min gauge)
        # ── alerts ──
        self._alerts_on = True
        self._alerts_primed = False
        self._alerts_grace_until = 0.0
        self._online_prev: set = set()
        self._known_nodes: set = set()
        self._lowbatt_warned: set = set()
        # ── traceroute ──
        self._trace_pending: Optional[str] = None  # node_id we asked to trace

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header-bar")
        yield WaterfallBar(id="waterfall")
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
        yield ClippedRichLog(id="sniffer", markup=True, max_lines=400, auto_scroll=True)
        with Horizontal(id="input-container"):
            yield Static("▶ ", id="prompt-label")
            yield Input(
                placeholder="type a message… (/help for commands)",
                id="chat-input",
            )
        yield InitOverlay(id="init-overlay")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        self.query_one("#sniffer").border_title = "⊟ PACKET CAPTURE  ·  ^p close"
        # Run boot in a worker so the splash actually animates (awaiting it here
        # would block the first paint and freeze the spinner on one frame).
        self.run_worker(self._boot_sequence(), exclusive=False)

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
        self._sys("[bold #a7c189]instant_meshenger[/bold #a7c189] v1.0 // meshtastic terminal")
        self._sys("━" * 60)

        if not MESH_AVAILABLE and (self._port or self._host or self._ble is not None):
            self._sys("[yellow]meshtastic not installed — pip install meshtastic pypubsub[/yellow]")

        overlay = self.query_one("#init-overlay", InitOverlay)
        for msg in BOOT_MSGS:
            overlay.log_line(msg)
            self._sys(f"  [dim #8ba672]▸[/dim #8ba672] {msg}")
            await asyncio.sleep(0.22)

        try:
            self._load_message_history()

            if self._demo_mode:
                self._sys("")
                self._sys("[yellow]◈ DEMO MODE[/yellow] — no device connected")
                self._sys("  use [#7ba9a0]--port /dev/ttyUSB0[/#7ba9a0] or [#7ba9a0]--host IP[/#7ba9a0] to connect live")
                self._sys("  radio: preset [#7ba9a0]LongFast[/#7ba9a0]  region [#7ba9a0]US[/#7ba9a0]  [dim](simulated)[/dim]")
                self.query_one("#header-bar", HeaderBar).freq_info = "LongFast/US (demo)"
                self._sys("")
                await asyncio.sleep(0.3)
                await self._init_demo()
            else:
                self._sys("")
                await self._connect_mesh()
        except Exception as e:
            self._sys(f"[red]boot error:[/red] {e}")
        finally:
            # Always start the timers and dismiss the splash, even if connect/demo failed —
            # otherwise the full-screen overlay would cover the UI forever.
            self.set_interval(0.5,  self._tick_header)
            self.set_interval(1.5,  self._tick_panels)
            self.set_interval(0.3,  self._tick_map)
            self.set_interval(3.0,  self._check_alerts)
            self.set_interval(20.0, lambda: self.refresh(layout=True))
            if self._demo_mode:
                self.set_interval(6.0, self._demo_event)
                self.set_interval(0.55, self._demo_rf)
            self.call_after_refresh(self._tick_panels)
            self.call_after_refresh(self._tick_header)
            self.query_one("#init-overlay", InitOverlay).display = False

    # ── System Messages ───────────────────────────────────────────────────────

    def _sys(self, text: str) -> None:
        feed = self.query_one("#message-feed", RichLog)
        ts = datetime.now().strftime("%H:%M:%S")
        line = Text()
        line.append(f"[{ts}] ", style="dim #8ba672")
        line.append("◈ ", style="#a7c189")
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
        t.append(f"[{ts}] ", style="dim #8ba672")

        if msg.is_system:
            t.append("◈ ", style="dim #8ba672")
            t.append(_strip_emoji(msg.text), style="dim #8ba672")
            feed.write(t)
            return

        # Prefer the live node entry's name — it may have been learned after the message was stored
        node = self.nodes.get(msg.sender_id)
        if node:
            sender_name = self._display_name(node)
        elif self._has_name(_strip_emoji(msg.sender_name)):
            sender_name = _strip_emoji(msg.sender_name)
        elif msg.sender_id:
            sender_name = self._unknown_node_name(msg.sender_id)
        else:
            sender_name = "?"

        # Build plain prefix string to measure its visual width for hanging indent
        prefix_plain = f"[{ts}] "
        if msg.is_dm:
            t.append("⬤DM ", style="bold magenta")
            prefix_plain += "⬤DM "
        t.append(f"<{sender_name}>", style=f"bold {color}")
        t.append("  ")
        prefix_plain += f"<{sender_name}>  "
        indent = cell_len(prefix_plain)

        # Word-wrap message so continuation lines align under the message text
        content_w = max(20, (feed.size.width or 100) - 2)  # -2 for widget padding
        wrapped = _wrap_with_indent(_strip_emoji(msg.text), content_w - indent, indent)
        t.append(wrapped, style="white")

        meta = []
        if msg.snr is not None:
            snr_col = "#a7c189" if msg.snr > 0 else "yellow" if msg.snr > -10 else "red"
            meta.append(Text(f"{msg.snr:+.0f}dB", style=f"dim {snr_col}"))
        if msg.rssi is not None:
            meta.append(Text(f"{msg.rssi}dBm", style="dim"))
        if msg.hops > 0:
            meta.append(Text(f"↝{msg.hops}", style="dim #7ba9a0"))

        if meta:
            t.append("  [", style="dim #8ba672")
            for i, m in enumerate(meta):
                if i:
                    t.append(" ", style="dim #8ba672")
                t.append_text(m)
            t.append("]", style="dim #8ba672")

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

        now = time.time()
        header.packets_per_min = sum(1 for t in self._pkt_times if now - t < 60)
        mine = next((n for n in self.nodes.values() if n.is_mine), None)
        header.channel_util = mine.channel_util if mine else None
        header.refresh()

    def _tick_panels(self) -> None:
        nl = self.query_one("#node-list", NodeListPanel)
        nl.update_nodes(self.nodes, self._node_colors, self._selected_node)

        mp = self.query_one("#map-view", MapPanel)
        mp.update_nodes(self.nodes, self._node_colors, self._rf_pulse)

        tp = self.query_one("#telemetry-view", TelemetryPanel)
        tp.update_data(self.nodes, self._node_colors, self._selected_node)

        # Force the right panel scroll container to reflow after height: auto children change
        self.query_one("#right-panel").refresh(layout=True)

    @on(NodeListPanel.NodeSelected)
    def handle_node_selected(self, event: NodeListPanel.NodeSelected) -> None:
        self._selected_node = event.node_id
        tp = self.query_one("#telemetry-view", TelemetryPanel)
        tp.update_data(self.nodes, self._node_colors, self._selected_node)
        nl = self.query_one("#node-list", NodeListPanel)
        nl.update_nodes(self.nodes, self._node_colors, self._selected_node)
        self.call_after_refresh(self._scroll_node_into_view)

    @on(NodeListPanel.NodeActivated)
    def handle_node_activated(self, event: NodeListPanel.NodeActivated) -> None:
        node = self.nodes.get(event.node_id)
        if not node:
            return
        short = node.short_name if self._has_name(node.short_name) else self._unknown_node_name(node.node_id)
        inp = self.query_one("#chat-input", Input)
        inp.value = f"/dm {short} "
        inp.cursor_position = len(inp.value)
        inp.focus()
        self._sys(f"✉ DM to [#7ba9a0]{node.long_name}[/#7ba9a0] — type your message and press enter")

    def _scroll_node_into_view(self) -> None:
        nl = self.query_one("#node-list", NodeListPanel)
        line = nl.line_of(self._selected_node) if self._selected_node else None
        if line is None:
            return
        try:
            self.query_one("#left-scroll").scroll_to(y=max(0, line - 2), animate=False)
        except Exception:
            pass

    def _tick_map(self) -> None:
        self.query_one("#map-view", MapPanel).tick()

    def action_toggle_panels(self) -> None:
        panel = self.query_one("#right-panel")
        panel.display = not panel.display

    def action_focus_nodes(self) -> None:
        """Tab: jump focus into the node list for j/k nav (or back to the input)."""
        nl = self.query_one("#node-list", NodeListPanel)
        if nl.has_focus:
            self.query_one("#chat-input").focus()
            return
        if self._selected_node is None and self.nodes:
            order = sorted(self.nodes.values(),
                           key=lambda n: (not n.is_mine, not n.is_online, n.long_name.lower()))
            self._selected_node = order[0].node_id
        nl.update_nodes(self.nodes, self._node_colors, self._selected_node)
        nl.focus()
        self.call_after_refresh(self._scroll_node_into_view)

    def action_toggle_links(self) -> None:
        on = self.query_one("#map-view", MapPanel).toggle_links()
        self._sys(f"map links [#7ba9a0]{'on' if on else 'off'}[/#7ba9a0]")

    def action_toggle_mapmode(self) -> None:
        mode = self.query_one("#map-view", MapPanel).toggle_mode()
        label = "mesh topology (hops)" if mode == "hops" else "gps grid"
        self._sys(f"map view: [#7ba9a0]{label}[/#7ba9a0]")

    def action_refresh_screen(self) -> None:
        self.refresh(layout=True)
        self._tick_panels()
        self._tick_map()

    def action_toggle_sniffer(self) -> None:
        s = self.query_one("#sniffer")
        s.display = not s.display
        self._sys(f"packet capture [#7ba9a0]{'on' if s.display else 'off'}[/#7ba9a0]")

    _PORT_ABBR = {
        "TEXT_MESSAGE_APP": "TEXT", "POSITION_APP": "POS", "TELEMETRY_APP": "TELE",
        "NODEINFO_APP": "INFO", "ROUTING_APP": "RTE", "TRACEROUTE_APP": "TRACE",
    }
    _PORT_COLOR = {
        "TEXT": "#a7c189", "POS": "#7ba9a0", "TELE": "#b6c28f",
        "INFO": "#8aa4c2", "TRACE": "#c2a86a", "RTE": "#9fb0c8",
    }

    def _capture_packet(self, from_id: str, to_id: str, port: str,
                        snr: Optional[float], rssi: Optional[int], size: int, hops: int) -> None:
        """Append one line to the tcpdump-style packet-capture pane."""
        try:
            log = self.query_one("#sniffer", ClippedRichLog)
        except Exception:
            return
        src = self.nodes.get(from_id)
        src_name = self._display_name(src) if src else self._unknown_node_name(from_id)
        dst = "ALL" if to_id in ("!ffffffff", "!ffffffffff") else (
            self._display_name(self.nodes[to_id]) if to_id in self.nodes
            else self._unknown_node_name(to_id))
        abbr = self._PORT_ABBR.get(port, (port or "?")[:5])
        pc = self._PORT_COLOR.get(abbr, "#8ba672")
        snr_s = f"{snr:+.0f}".rjust(3) if snr is not None else "  ·"
        rssi_s = f"{rssi}".rjust(4) if rssi is not None else "   ·"
        t = Text()
        t.append(datetime.now().strftime("%H:%M:%S "), style="dim #5b6b50")
        t.append(f"{src_name[:10]:<10}", style="#8ba672")
        t.append("→", style="dim #5b6b50")
        t.append(f"{dst[:10]:<10} ", style="dim #8ba672")
        t.append(f"{abbr:<5}", style=f"bold {pc}")
        t.append(f" {snr_s}dB {rssi_s}dBm", style="dim #7ba9a0")
        t.append(f" {size:>3}b", style="dim #8ba672")
        t.append(f" ↝{hops}" if hops else "   ", style="dim #7ba9a0")
        log.write(t)

    def _record_rf(self, node_id: str, snr: Optional[float]) -> None:
        """Register a packet from node_id: blooms the map glyph + feeds the waterfall."""
        self._rf_pulse[node_id] = time.time()
        self._pkt_times.append(time.time())
        try:
            wf = self.query_one("#waterfall", WaterfallBar)
        except Exception:
            return  # widgets not mounted yet (early boot packet)
        wf.push(self._node_colors.get(node_id), snr)

    # ── Alerts ────────────────────────────────────────────────────────────────

    def _alert(self, text: str, *, bell: bool = False) -> None:
        if not self._alerts_on:
            return
        if bell:
            try:
                self.bell()
            except Exception:
                pass
        try:
            feed = self.query_one("#message-feed", RichLog)
        except Exception:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        line = Text()
        line.append(f"[{ts}] ", style="dim #8ba672")
        line.append(" ⚑ ", style="bold #0e120e on #c2a86a")
        line.append(f" {text}", style="bold #c2a86a")
        feed.write(line)

    # Only alert about nodes we can hear directly (≤ this many hops). On a big
    # mesh, distant nodes constantly flap in/out of the heard-window and would
    # flood the feed — that churn isn't actionable, so we ignore it.
    _ALERT_MAX_HOPS = 1

    def _check_alerts(self) -> None:
        """Periodic watch — DIRECT NEIGHBORS only: new neighbor, neighbor dark, low battery."""
        now = time.time()
        now_online = {nid for nid, n in self.nodes.items() if n.is_online and not n.is_mine}

        # First run primes a baseline; a settle window then absorbs the initial
        # node-DB stream (which would otherwise fire a wall of "went dark").
        if not self._alerts_primed:
            self._known_nodes = set(self.nodes)
            self._online_prev = now_online
            self._alerts_primed = True
            self._alerts_grace_until = now + 45.0
            return
        in_grace = now < self._alerts_grace_until

        def is_neighbor(n: MeshNode) -> bool:
            return n is not None and not n.is_mine and n.hops_away <= self._ALERT_MAX_HOPS

        # New neighbor joined (keep baselines current during grace, but stay quiet)
        for nid, n in self.nodes.items():
            if nid in self._known_nodes or n.is_mine or not self._has_name(n.short_name):
                continue
            self._known_nodes.add(nid)
            if not in_grace and is_neighbor(n):
                self._alert(f"✦ new neighbor: {n.long_name}")

        for nid in (self._online_prev - now_online):
            n = self.nodes.get(nid)
            if not in_grace and is_neighbor(n):
                self._alert(f"⚠ neighbor went dark: {n.long_name}")
        self._online_prev = now_online

        for nid, n in self.nodes.items():
            if n.is_mine or n.battery_level is None or n.hops_away > self._ALERT_MAX_HOPS:
                continue
            if n.battery_level < 15 and nid not in self._lowbatt_warned:
                self._lowbatt_warned.add(nid)
                if not in_grace:
                    self._alert(f"🪫 low battery: {n.long_name} at {n.battery_level}%")
            elif n.battery_level > 20:
                self._lowbatt_warned.discard(nid)

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
        self._sys(f"mesh online: [#a7c189]{len(self.nodes)}[/#a7c189] nodes discovered, "
                  f"[#a7c189]{online_count}[/#a7c189] online")
        self._sys(f"channel: [#7ba9a0]{CHANNEL_NAMES[self.current_channel]}[/#7ba9a0]  "
                  f"sf: [#7ba9a0]SF12[/#7ba9a0]  bw: [#7ba9a0]125kHz[/#7ba9a0]  cr: [#7ba9a0]4/5[/#7ba9a0]")
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
            self._record_rf(node.node_id, snr)

        elif roll < 0.65:
            # Telemetry update
            node = random.choice(demo_nodes)
            node.battery_level = max(0, min(100, (node.battery_level or 50) + random.randint(-3, 1)))
            node.snr = round(random.uniform(-15, 12), 1)
            node.rssi = random.randint(-130, -65)
            self._record_rf(node.node_id, node.snr)

        elif roll < 0.72:
            # Position update (small drift)
            node = random.choice([n for n in demo_nodes if n.latitude is not None] or demo_nodes)
            if node.latitude is not None:
                node.latitude += random.uniform(-0.0005, 0.0005)
                node.longitude += random.uniform(-0.0005, 0.0005)
                node.last_heard = time.time()
                self._record_rf(node.node_id, node.snr)

        elif roll < 0.77:
            # Rare: node goes offline/online
            node = random.choice(demo_nodes)
            if node.is_online:
                node.last_heard = time.time() - 1000
            else:
                node.last_heard = time.time()
                self._sys(f"[#a7c189]◆ node online:[/#a7c189] {node.long_name}")

    def _demo_rf(self) -> None:
        """Lightweight simulated airwaves chatter — feeds the waterfall + map pulse
        without posting chat. Online nodes transmit more often."""
        online = [n for n in self.nodes.values() if not n.is_mine and n.is_online]
        if not online:
            return
        weights = [3 if n.hops_away == 0 else 2 if n.hops_away <= 2 else 1 for n in online]
        node = random.choices(online, weights=weights, k=1)[0]
        snr = node.snr if node.snr is not None else round(random.uniform(-12, 10), 1)
        self._record_rf(node.node_id, snr)
        port = random.choice(["TELEMETRY_APP", "POSITION_APP", "NODEINFO_APP", "TEXT_MESSAGE_APP"])
        self._capture_packet(node.node_id, "!ffffffff", port, snr, node.rssi,
                             random.randint(8, 64), node.hops_away)

    # ── Meshtastic Connection ─────────────────────────────────────────────────

    async def _connect_mesh(self) -> None:
        if not MESH_AVAILABLE:
            self._sys("[red]meshtastic library not available[/red]")
            self._sys("  run: [#7ba9a0]pip install meshtastic pypubsub[/#7ba9a0]")
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
                self._sys(f"connecting via BLE ([#7ba9a0]{label}[/#7ba9a0])...")
                self._sys("  [dim]make sure Bluetooth is on and node is nearby[/dim]")
                # BLEInterface blocks during scan+connect — run in thread so the UI stays alive
                self.interface = await loop.run_in_executor(
                    None, lambda: meshtastic.ble_interface.BLEInterface(target)
                )
            elif self._host:
                self._sys(f"connecting to [#7ba9a0]{self._host}[/#7ba9a0] via TCP...")
                self.interface = await loop.run_in_executor(
                    None, lambda: meshtastic.tcp_interface.TCPInterface(self._host)
                )
            else:
                self._sys(f"connecting to [#7ba9a0]{self._port}[/#7ba9a0] ({self._baud} baud)...")
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
            self.set_interval(0.55, self._demo_rf)
            await self._init_demo()

    def _on_connected(self, interface, topic=pub.AUTO_TOPIC) -> None:
        def update():
            self._sys("[#a7c189]◆ connected to device[/#a7c189]")
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
                f"preset [#7ba9a0]{preset_name}[/#7ba9a0]",
                f"region [#7ba9a0]{region_name}[/#7ba9a0]",
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
                        parts.append(f"[#7ba9a0]{freq_mhz:.3f} MHz[/#7ba9a0]")

            if freq_offset:
                parts.append(f"offset [#7ba9a0]{freq_offset:+}Hz[/#7ba9a0]")
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
            node = self._lookup_node_from_interface(from_id) or MeshNode(node_id=from_id)
            self.nodes[from_id] = node
            self._assign_color(from_id)

    @staticmethod
    def _has_name(name: Optional[str]) -> bool:
        """True if a node name is set and not the unknown-node placeholder."""
        return bool(name) and name != "????"

    @staticmethod
    def _unknown_node_name(node_id: str) -> str:
        """Canonical display name for a node we have no name for: last 4 hex of its id."""
        return node_id.lstrip("!")[-4:].upper()

    def _lookup_node_from_interface(self, node_id: str) -> Optional[MeshNode]:
        """Build a MeshNode from the meshtastic lib's node cache, if it knows this node."""
        iface = getattr(self, "interface", None)
        if iface is None:
            return None
        data = (getattr(iface, "nodes", None) or {}).get(node_id)
        if not data:
            try:
                num = int(node_id.lstrip("!"), 16)
            except ValueError:
                return None
            data = (getattr(iface, "nodesByNum", None) or {}).get(num)
        if not data:
            return None
        try:
            return self._parse_node(node_id, data)
        except Exception:
            return None

    def _refresh_name_if_missing(self, node: MeshNode) -> None:
        """If we still hold the default name, try a late lookup from the device's node DB."""
        if self._has_name(node.short_name):
            return
        fresh = self._lookup_node_from_interface(node.node_id)
        if fresh is None:
            return
        if self._has_name(fresh.short_name):
            node.short_name = fresh.short_name
        if fresh.long_name and fresh.long_name != "Unknown":
            node.long_name = fresh.long_name

    def _display_name(self, node: MeshNode) -> str:
        """Best available short name for chat display; falls back to hex id when unknown."""
        if self._has_name(node.short_name):
            return node.short_name
        return self._unknown_node_name(node.node_id)

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

            self._sys(f"device nodeDB: [#7ba9a0]{len(nodes_raw)}[/#7ba9a0] entries")

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

            self._sys(f"loaded [#a7c189]{len(self.nodes)}[/#a7c189] nodes from device")
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
        hex4 = node_id.lstrip("!")[-4:].upper()
        return MeshNode(
            node_id=node_id,
            long_name=_clean_name(user.get("longName", "Unknown"), f"node-{hex4.lower()}"),
            short_name=_clean_name(user.get("shortName", "????"), "????"),
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
                elif portnum == "TRACEROUTE_APP":
                    self._rx_traceroute(packet, decoded)
                # Every packet (any port) registers RF activity for the map + waterfall
                from_id = f"!{packet.get('from', 0):08x}"
                self._record_rf(from_id, packet.get("rxSnr"))
                self._capture_packet(
                    from_id, f"!{packet.get('to', 0xFFFFFFFF):08x}", portnum,
                    packet.get("rxSnr"), packet.get("rxRssi"),
                    len(decoded.get("payload", b"") or b""),
                    max(0, packet.get("hopStart", 0) - packet.get("hopLimit", 0)),
                )
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
        self._refresh_name_if_missing(node)
        node.last_heard = time.time()
        node.snr = snr
        node.rssi = rssi

        hops = packet.get("hopStart", 0) - packet.get("hopLimit", 0)
        # Use rxTime (when the device received it) rather than wall-clock now
        rx_time = packet.get("rxTime") or time.time()
        is_dm = (to_id != "!ffffffff")
        self._add_message(MeshMessage(
            timestamp=rx_time,
            sender_id=from_id,
            sender_name=self._display_name(node),
            text=text,
            channel=channel,
            is_dm=is_dm,
            snr=snr,
            rssi=rssi,
            hops=max(0, hops),
        ))
        if is_dm:
            self._alert(f"✉ direct message from {self._display_name(node)}", bell=True)

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
        hex4 = from_id.lstrip("!")[-4:].upper()
        before = (n.short_name, n.long_name)
        n.long_name = _clean_name(user.get("longName", n.long_name), f"node-{hex4.lower()}")
        n.short_name = _clean_name(user.get("shortName", n.short_name), "????")
        n.last_heard = time.time()
        if (n.short_name, n.long_name) != before:  # don't spam on periodic re-broadcasts
            self._sys(f"node info: [{n.short_name}] [#7ba9a0]{n.long_name}[/#7ba9a0]")

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
        elif cmd == "/trace" and len(parts) >= 2:
            self._cmd_trace(parts[1])
        elif cmd == "/alerts":
            self._cmd_alerts(parts[1] if len(parts) >= 2 else "")
        else:
            self._sys(f"[red]unknown command:[/red] {cmd}  (try [#7ba9a0]/help[/#7ba9a0])")

    def _cmd_help(self) -> None:
        lines = [
            "━" * 55,
            "  [bold #a7c189]instant_meshenger COMMANDS[/bold #a7c189]",
            "━" * 55,
            "  [#7ba9a0]/help[/#7ba9a0]                 this help screen",
            "  [#7ba9a0]/nodes[/#7ba9a0]                list all known nodes",
            "  [#7ba9a0]/info[/#7ba9a0]                 my node info & telemetry",
            "  [#7ba9a0]/map[/#7ba9a0]                  show GPS coordinates of all nodes",
            "  [#7ba9a0]/dm <node> <msg>[/#7ba9a0]      direct message (name or !id)",
            "  [#7ba9a0]/channel <0-7>[/#7ba9a0]        switch channel",
            "  [#7ba9a0]/select <node>[/#7ba9a0]        focus telemetry on a node",
            "  [#7ba9a0]/trace <node>[/#7ba9a0]         traceroute — animates the path on the map",
            "  [#7ba9a0]/alerts [on|off][/#7ba9a0]      toggle DM / offline / low-battery alerts",
            "  [#7ba9a0]/clear[/#7ba9a0]                clear message feed",
            "━" * 55,
            "  [dim]keys:[/dim] [#7ba9a0]tab[/#7ba9a0] node nav (j/k, ⏎ dm)   "
            "[#7ba9a0]^g[/#7ba9a0] map view   [#7ba9a0]^l[/#7ba9a0] links   [#7ba9a0]^t[/#7ba9a0] map",
            "  node can be short name [#7ba9a0]CYPH[/#7ba9a0] or full id [#7ba9a0]!deadbeef[/#7ba9a0]",
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
        self._sys(f"  id:      [#7ba9a0]{mine.node_id}[/#7ba9a0]")
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
        self._sys(f"  channel: [#7ba9a0]{CHANNEL_NAMES[self.current_channel]}[/#7ba9a0] ({self.current_channel})")
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
                self._sys(f"switched to channel [#7ba9a0]{ch}[/#7ba9a0] ({CHANNEL_NAMES[ch]})")
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
        self._sys(f"telemetry focused on [#7ba9a0]{node.long_name}[/#7ba9a0]")

    def _cmd_alerts(self, arg: str) -> None:
        arg = arg.strip().lower()
        if arg in ("on", "off"):
            self._alerts_on = (arg == "on")
        else:
            self._alerts_on = not self._alerts_on
        self._sys(f"alerts [#7ba9a0]{'on' if self._alerts_on else 'off'}[/#7ba9a0]")

    # ── Traceroute ────────────────────────────────────────────────────────────

    def _cmd_trace(self, target: str) -> None:
        node = self._find_node(target)
        if not node:
            self._sys(f"[red]node not found:[/red] {target}")
            return
        if node.is_mine:
            self._sys("[yellow]can't trace your own node[/yellow]")
            return
        self._sys(f"◌ tracing route to [#7ba9a0]{node.long_name}[/#7ba9a0]...")
        if self._demo_mode or not self.interface:
            self._demo_trace(node)
            return
        self._trace_pending = node.node_id
        dest = int(node.node_id.lstrip("!"), 16)
        ch = self.current_channel
        name = self._display_name(node)

        def _send():
            # sendTraceRoute blocks (waitForTraceRoute); keep it off the UI thread.
            try:
                self.interface.sendTraceRoute(dest, hopLimit=7, channelIndex=ch)
                # If a reply arrived, _rx_traceroute already cleared _trace_pending.
                self.call_from_thread(self._trace_no_reply, node.node_id, name)
            except Exception as e:
                self.call_from_thread(self._sys, f"[red]traceroute failed:[/red] {e}")
                self.call_from_thread(setattr, self, "_trace_pending", None)

        self.run_worker(_send, thread=True)

    def _trace_no_reply(self, node_id: str, name: str) -> None:
        if self._trace_pending == node_id:   # still pending → no response came back
            self._sys(f"[yellow]no traceroute reply from {name}[/yellow] — node may be "
                      f"unreachable, or rate-limited (mesh allows ~1 trace / 30s)")
            self._trace_pending = None

    def _demo_trace(self, target: MeshNode) -> None:
        """Synthesize a plausible route through online relays for demo mode."""
        mine = next((n for n in self.nodes.values() if n.is_mine), None)
        relays = [n for n in self.nodes.values()
                  if not n.is_mine and n.is_online and n.node_id != target.node_id]
        random.shuffle(relays)
        hops = max(0, min(target.hops_away, len(relays)))
        route = ([mine.node_id] if mine else [])
        route += [r.node_id for r in relays[:hops]]
        route.append(target.node_id)
        self._show_trace_route(route)

    def _show_trace_route(self, route: List[str]) -> None:
        # drop consecutive duplicates
        clean: List[str] = []
        for nid in route:
            if not clean or clean[-1] != nid:
                clean.append(nid)
        if len(clean) < 2:
            self._sys("[yellow]traceroute: no route data[/yellow]")
            return
        self.query_one("#map-view", MapPanel).set_trace(clean)
        names = []
        for nid in clean:
            n = self.nodes.get(nid)
            names.append(self._display_name(n) if n else self._unknown_node_name(nid))
        self._sys("route: [#a7c189]" + " → ".join(names) + f"[/#a7c189]  "
                  f"([#7ba9a0]{len(clean) - 1}[/#7ba9a0] hops)")

    def _rx_traceroute(self, packet: dict, decoded: dict) -> None:
        tr = decoded.get("traceroute") or {}
        route_nums = tr.get("route", []) or []
        dest_id = f"!{packet.get('from', 0):08x}"
        mine = next((n for n in self.nodes.values() if n.is_mine), None)
        route = [mine.node_id] if mine else []
        route += [f"!{int(num):08x}" for num in route_nums]
        route.append(dest_id)
        for nid in route:
            self._ensure_node(nid)
        self._show_trace_route(route)
        self._trace_pending = None

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
        self._record_rf(sender_id, mine.snr if mine else None)  # pulse my node on TX

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
