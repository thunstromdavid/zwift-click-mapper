"""
Zwift Click v2 -> Keyboard Mapper
Connects directly to one or more Zwift Click v2 controllers over Bluetooth LE
and turns button presses into keyboard keystrokes. Edit config.json to change
key bindings.

Run:  python zwift_click_mapper.py
Stop: Quit from the overlay (x / right-click menu) or Ctrl+C in the terminal
"""

import asyncio
import json
import platform
import queue
import signal
import sys
import threading
import time
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Missing library. Run:  pip install bleak")
    sys.exit(1)

import tkinter as tk

IS_WINDOWS = platform.system() == "Windows"

# --- key injection backend: pydirectinput on Windows, pynput elsewhere ---
if IS_WINDOWS:
    try:
        import pydirectinput
    except ImportError:
        print("Missing library. Run:  pip install pydirectinput")
        sys.exit(1)
    pydirectinput.PAUSE = 0  # remove built-in delay for low latency

    def key_down_raw(key):
        pydirectinput.keyDown(key)

    def key_up_raw(key):
        pydirectinput.keyUp(key)

    def is_valid_key(key):
        # pydirectinput silently ignores names it doesn't know
        return pydirectinput.KEYBOARD_MAPPING.get(key) is not None
else:
    try:
        from pynput.keyboard import Controller, Key
    except ImportError:
        print("Missing library. Run:  pip install pynput")
        sys.exit(1)
    _kb = Controller()

    def _to_key(name):
        name = name.lower()
        if len(name) == 1:
            return name
        try:
            return Key[name.replace(" ", "_")]
        except KeyError:
            return None

    def is_valid_key(key):
        return _to_key(key) is not None

    def key_down_raw(key):
        k = _to_key(key)
        if k is not None:
            _kb.press(k)

    def key_up_raw(key):
        k = _to_key(key)
        if k is not None:
            _kb.release(k)

# ---------------------------------------------------------------- config ---
CONFIG_PATH = Path(__file__).parent / "config.json"
OVERLAY_STATE_PATH = Path(__file__).parent / "overlay_state.json"

DEFAULTS = {
    "device_name_filter": "Zwift Click",
    "bindings": {
        "plus": "k",
        "minus": "i",
        "up": "u",
        "down": "down",
        "left": "left",
        "right": "right",
    },
    "hold_to_repeat": ["plus", "minus"],
    "repeat_initial_delay_ms": 350,
    "repeat_interval_ms": 150,
    "debounce_ms": 60,
    # A Click v2 is a pair (left + right). Scanning stops once this many are
    # connected and resumes as soon as one drops.
    "controller_count": 2,
    "show_overlay": True,
    "overlay_opacity": 0.92,
}


def load_config():
    """Return (cfg, problems). Never raises: a broken file falls back to defaults."""
    cfg = dict(DEFAULTS)
    problems = []
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(user, dict):
                raise ValueError("top level must be an object")
            for k, v in user.items():
                if k == "bindings" and isinstance(v, dict):
                    cfg["bindings"] = {**DEFAULTS["bindings"], **v}
                else:
                    cfg[k] = v
        except (ValueError, OSError) as e:
            problems.append(f"config.json error: {e} (using defaults)")
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2))
        print(f"Created default config at {CONFIG_PATH}")

    # Key names are case-insensitive for the user; the backends want lowercase.
    # An empty string (or null) unbinds a button.
    clean = {}
    for btn, key in cfg["bindings"].items():
        if not key:
            continue
        key = str(key).strip().lower()
        if not is_valid_key(key):
            problems.append(f"unknown key '{key}' for button '{btn}'")
            continue
        clean[str(btn).lower()] = key
    cfg["bindings"] = clean
    for p in problems:
        print(f"! {p}")
    return cfg, problems


# ------------------------------------------------------------- protocol ---
NOTIFY_CHAR = "00000002-19ca-4651-86e5-fa29dcdd09d1"
WRITE_CHAR = "00000003-19ca-4651-86e5-fa29dcdd09d1"
BATTERY_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"
HANDSHAKE = ["526964654f6e0203", "000800", "000810"]
KEEPALIVE = "000810"
KEEPALIVE_INTERVAL = 5.0
CONNECT_TIMEOUT = 20.0
DISCONNECT_TIMEOUT = 5.0

# Each button is one bit in a 4-byte mask (bit cleared = pressed).
# Known bits get friendly names; unknown ones show as btn<byte>_<bit>
# in the terminal — add them to config.json bindings with that name.
BIT_NAMES = {
    (0, 0): "left",
    (0, 1): "up",
    (0, 2): "right",
    (0, 3): "down",
    (0, 4): "a",
    (0, 5): "b",
    (0, 6): "y",
    (1, 0): "z",
    (1, 1): "minus",
    (1, 5): "plus",
}


LEFT_BUTTONS = {"up", "down", "left", "right", "minus"}
RIGHT_BUTTONS = {"a", "b", "y", "z", "plus"}


def decode_buttons(hx):
    """Return set of pressed button names from a 2308...0F state packet."""
    mask = bytes.fromhex(hx[4:12])
    pressed = set()
    for i, byte in enumerate(mask):
        for bit in range(8):
            if not byte & (1 << bit):
                pressed.add(BIT_NAMES.get((i, bit), f"btn{i}_{bit}"))
    return pressed


# ------------------------------------------------------------- UI events ---
class UiBus:
    """Thread-safe one-way channel from the BLE thread to the overlay."""

    def __init__(self):
        self.q = queue.Queue()

    def emit(self, kind, **data):
        self.q.put((kind, data))


# --------------------------------------------------------------- mapping ---
class ClickMapper:
    """
    Turns button state packets into key presses. State is tracked per
    controller so a packet from the left unit never releases a key held on the
    right one, and a controller dropping out releases only its own keys.
    """

    def __init__(self, cfg, ui: UiBus):
        self.ui = ui
        self.last_press = {}
        self.pressed_by_device: dict[str, set[str]] = {}
        self.keys_down: set[str] = set()
        self.repeat_timers: dict[str, asyncio.TimerHandle] = {}
        self.apply_config(cfg)

    def apply_config(self, cfg):
        self.cfg = cfg
        self.bindings = cfg["bindings"]
        self.repeat_buttons = set(cfg.get("hold_to_repeat", []))
        self.debounce = cfg.get("debounce_ms", 60) / 1000
        self.initial_delay = cfg.get("repeat_initial_delay_ms", 350) / 1000
        self.interval = max(0.02, cfg.get("repeat_interval_ms", 150) / 1000)

    def _start_repeat(self, key):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.repeat_timers[key] = loop.call_later(self.initial_delay, self._repeat, key)

    def _repeat(self, key):
        """Re-tap a held key (e.g. keep shifting gears while + is held)."""
        if key not in self.keys_down:
            return
        try:
            key_up_raw(key)
            key_down_raw(key)
        except Exception as e:
            print(f"  key repeat failed for '{key}': {e}")
        loop = asyncio.get_running_loop()
        self.repeat_timers[key] = loop.call_later(self.interval, self._repeat, key)

    def _stop_repeat(self, key):
        handle = self.repeat_timers.pop(key, None)
        if handle:
            handle.cancel()

    def _all_pressed(self):
        out = set()
        for s in self.pressed_by_device.values():
            out |= s
        return out

    def _apply(self, before, after):
        for button in sorted(after - before):
            key = self.bindings.get(button)
            if not key:
                print(f"  unbound button: {button}  (add it to config.json)")
                continue
            now = time.monotonic()
            if now - self.last_press.get(button, 0) < self.debounce:
                continue
            self.last_press[button] = now
            if key in self.keys_down:
                continue
            try:
                key_down_raw(key)
            except Exception as e:
                print(f"  key injection failed for '{key}': {e}")
                continue
            self.keys_down.add(key)
            if button in self.repeat_buttons:
                self._start_repeat(key)
            print(f"  {button:<6} -> {key} DOWN")
            self.ui.emit("button", button=button, key=key, down=True)
        for button in sorted(before - after):
            self._release_button(button)
        self.ui.emit("pressed", buttons=sorted(after))

    def _release_button(self, button):
        key = self.bindings.get(button)
        # Only release keys we actually pressed (skips debounced presses), and
        # keep a key down while another button bound to it is still held.
        if not key or key not in self.keys_down:
            return
        if any(self.bindings.get(b) == key for b in self._all_pressed()):
            return
        self._stop_repeat(key)
        try:
            key_up_raw(key)
        except Exception as e:
            print(f"  key release failed for '{key}': {e}")
        self.keys_down.discard(key)
        print(f"  {button:<6} -> {key} UP")
        self.ui.emit("button", button=button, key=key, down=False)

    def handle(self, device_id, data: bytearray):
        hx = data.hex().upper()
        if not hx.startswith("2308") or len(hx) != 14:
            return
        now = decode_buttons(hx)
        if now & LEFT_BUTTONS and not now & RIGHT_BUTTONS:
            self.ui.emit("side", address=device_id, side="Left")
        elif now & RIGHT_BUTTONS and not now & LEFT_BUTTONS:
            self.ui.emit("side", address=device_id, side="Right")
        before = self._all_pressed()
        self.pressed_by_device[device_id] = now
        self._apply(before, self._all_pressed())

    def device_gone(self, device_id):
        """Release any keys held by a controller that just disconnected."""
        if device_id not in self.pressed_by_device:
            return
        before = self._all_pressed()
        del self.pressed_by_device[device_id]
        self._apply(before, self._all_pressed())

    def release_all(self):
        for key in list(self.repeat_timers):
            self._stop_repeat(key)
        for key in list(self.keys_down):
            try:
                key_up_raw(key)
            except Exception:
                pass
        self.keys_down.clear()
        self.pressed_by_device.clear()
        self.ui.emit("pressed", buttons=[])


# ------------------------------------------------------------------- BLE ---
class ClickManager:
    """
    Owns all BLE work. Scans in short windows, opens one session per
    controller found, and always tears sessions down fully (even after the
    link has already dropped) so Windows doesn't keep a half-open GATT
    connection that stops the controller from advertising again.
    """

    def __init__(self, cfg, mapper: ClickMapper, ui: UiBus):
        self.cfg = cfg
        self.mapper = mapper
        self.ui = ui
        self.name_filter = cfg["device_name_filter"]
        self.want = max(1, int(cfg.get("controller_count", 2)))
        self.sessions: dict[str, asyncio.Task] = {}
        self.clients: dict[str, BleakClient] = {}
        self.stop_event: asyncio.Event | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stopping = False
        # Windows stacks get unreliable when scanning and connecting overlap,
        # so the two take turns.
        self.radio_lock: asyncio.Lock | None = None

    # ---- called from other threads ----
    def request_stop(self):
        self.stopping = True
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def request_reconnect_all(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self._reconnect_all)

    def _reconnect_all(self):
        print("Reconnecting all controllers...")
        for task in self.sessions.values():
            task.cancel()

    # ---- main ----
    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.radio_lock = asyncio.Lock()
        if self.stopping:
            return
        scan_task = asyncio.create_task(self._scan_loop())
        watch_task = asyncio.create_task(self._watch_config())
        try:
            await self.stop_event.wait()
        finally:
            print("Shutting down — disconnecting controllers...")
            scan_task.cancel()
            watch_task.cancel()
            for task in list(self.sessions.values()):
                task.cancel()
            pending = [scan_task, watch_task, *self.sessions.values()]
            await asyncio.gather(*pending, return_exceptions=True)
            self.mapper.release_all()
            print("All controllers disconnected.")

    async def _watch_config(self):
        """Reload config.json when it is saved, so bindings change without a restart."""
        def mtime():
            try:
                return CONFIG_PATH.stat().st_mtime
            except OSError:
                return None

        last = mtime()
        while True:
            await asyncio.sleep(1.0)
            current = mtime()
            if current == last:
                continue
            last = current
            await asyncio.sleep(0.2)  # let the editor finish writing
            cfg, problems = load_config()
            self.mapper.release_all()
            self.mapper.apply_config(cfg)
            self.cfg = cfg
            self.want = max(1, int(cfg.get("controller_count", 2)))
            print("config.json reloaded.")
            print_bindings(cfg, waiting=False)
            self.ui.emit("config", cfg=cfg, problems=problems)

    async def _scan_loop(self):
        while True:
            if len(self.sessions) >= self.want:
                self.ui.emit("scan", active=False)
                await asyncio.sleep(1.0)
                continue

            self.ui.emit("scan", active=True)
            found = {}

            def on_detect(device, adv):
                name = device.name or adv.local_name or ""
                if self.name_filter in name and device.address not in self.sessions:
                    found[device.address] = (device, name)

            try:
                async with self.radio_lock:
                    async with BleakScanner(on_detect):
                        await asyncio.sleep(3.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"Scan error: {e}")
                self.ui.emit("log", text=f"Scan error: {e}")
                await asyncio.sleep(3.0)
                continue

            for address, (device, name) in found.items():
                if address in self.sessions or len(self.sessions) >= self.want:
                    continue
                task = asyncio.create_task(self._session(device, name))
                self.sessions[address] = task
                task.add_done_callback(
                    lambda t, a=address: self.sessions.pop(a, None)
                    if self.sessions.get(a) is t else None
                )

            # short gap so a freshly started session can grab the radio
            await asyncio.sleep(0.3)

    async def _session(self, device, name):
        address = device.address
        dropped = asyncio.Event()
        client = BleakClient(
            device,
            disconnected_callback=lambda _c: dropped.set(),
            timeout=CONNECT_TIMEOUT,
        )
        reason = "link lost"
        print(f"Found: {name} [{address}] — connecting...")
        self.ui.emit("device", address=address, name=name, state="connecting")
        try:
            async with self.radio_lock:
                await client.connect()
                await client.start_notify(
                    NOTIFY_CHAR, lambda _s, d: self.mapper.handle(address, d)
                )
                for msg in HANDSHAKE:
                    await client.write_gatt_char(
                        WRITE_CHAR, bytearray.fromhex(msg), response=False
                    )
                    await asyncio.sleep(0.1)
            self.clients[address] = client

            print(f"=== Connected: {name} [{address}] ===")
            self.ui.emit("device", address=address, name=name, state="connected")
            await self._read_battery(client, address)

            while not dropped.is_set():
                try:
                    await asyncio.wait_for(dropped.wait(), KEEPALIVE_INTERVAL)
                except asyncio.TimeoutError:
                    await client.write_gatt_char(
                        WRITE_CHAR, bytearray.fromhex(KEEPALIVE), response=False
                    )
        except asyncio.CancelledError:
            reason = "closed"
            raise
        except Exception as e:
            reason = str(e) or type(e).__name__
            print(f"{name} [{address}]: {reason}")
        finally:
            self.clients.pop(address, None)
            self.mapper.device_gone(address)
            await self._teardown(client, address)
            print(f"Disconnected: {name} [{address}] ({reason})")
            self.ui.emit("device", address=address, name=name,
                         state="disconnected", reason=reason)

    async def _teardown(self, client, address):
        # Always call disconnect(), even if the link is already gone: on
        # Windows that is what releases the GATT session/service handles.
        # Skipping it leaves the OS holding the controller, which then stops
        # advertising and looks "dead" until Bluetooth is toggled.
        try:
            if client.is_connected:
                await asyncio.wait_for(client.stop_notify(NOTIFY_CHAR), 2.0)
        except BaseException:
            pass
        try:
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
        except BaseException as e:
            if not isinstance(e, asyncio.CancelledError):
                print(f"[{address}] disconnect cleanup: {e}")

    async def _read_battery(self, client, address):
        try:
            data = await asyncio.wait_for(client.read_gatt_char(BATTERY_CHAR), 3.0)
            self.ui.emit("battery", address=address, level=int(data[0]))
            await client.start_notify(
                BATTERY_CHAR,
                lambda _s, d: self.ui.emit("battery", address=address, level=int(d[0])),
            )
        except Exception:
            pass  # battery service is optional


# --------------------------------------------------------------- overlay ---
class StatusOverlay:
    """
    Small always-on-top overlay: per-controller connection status and
    battery, a live button pad that lights up on press, and recent activity.
    Drag to move, right-click for the menu. Position is remembered.
    """

    BG = "#14141f"
    PANEL = "#1e1e2e"
    BORDER = "#2c2c44"
    FG = "#e6e6f0"
    FG_DIM = "#8a8aa8"
    FG_FAINT = "#5a5a78"
    COL_SCANNING = "#f0a500"
    COL_CONNECTING = "#5bc0eb"
    COL_CONNECTED = "#38d26a"
    COL_DISCONNECTED = "#e63946"
    COL_PRESS = "#9d4edd"
    BTN_IDLE = "#2a2a3e"

    WIDTH = 300
    MAX_ACTIVITY = 4

    # (button name, label, column, row) on a 7x3 grid, left and right clusters
    PAD = [
        ("up", "▲", 1, 0), ("left", "◀", 0, 1), ("right", "▶", 2, 1),
        ("down", "▼", 1, 2), ("minus", "−", 0, 0),
        ("y", "Y", 5, 0), ("z", "Z", 4, 1), ("b", "B", 6, 1),
        ("a", "A", 5, 2), ("plus", "+", 6, 0),
    ]

    def __init__(self, ui: UiBus, cfg, on_quit, on_reconnect):
        self.ui = ui
        self.cfg = cfg
        self.on_quit = on_quit
        self.on_reconnect = on_reconnect
        self.bindings = cfg["bindings"]
        self.want = max(1, int(cfg.get("controller_count", 2)))
        self.devices: dict[str, dict] = {}
        self.scanning = False
        self.compact = False
        self.activity: list[tuple[str, str]] = []
        self.quit_requested = False
        self._blink = False

        r = self.root = tk.Tk()
        r.title("Zwift Click Mapper")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", float(cfg.get("overlay_opacity", 0.92)))
        r.configure(bg=self.BORDER)

        self.frame = tk.Frame(r, bg=self.BG)
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)

        self._build_header()
        self.body = tk.Frame(self.frame, bg=self.BG)
        self.body.pack(fill="both", expand=True)
        self.dev_frame = tk.Frame(self.body, bg=self.BG)
        self.dev_frame.pack(fill="x", padx=10, pady=(2, 6))
        self._build_pad()
        self._build_activity()

        self.menu = tk.Menu(r, tearoff=0, bg=self.PANEL, fg=self.FG,
                            activebackground=self.COL_PRESS, bd=0)
        self.menu.add_command(label="Compact mode", command=self.toggle_compact)
        self.menu.add_command(label="Reconnect controllers", command=self.on_reconnect)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.request_quit)

        for w in (r, self.frame, self.header, self.title_lbl, self.summary_lbl):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._do_drag)
            w.bind("<ButtonRelease-1>", lambda e: self._save_state())
            w.bind("<ButtonPress-3>", self._show_menu)

        self._restore_state()
        self._render_devices()
        self._tick()
        self._blink_tick()

    # ---- layout ----
    def _build_header(self):
        h = self.header = tk.Frame(self.frame, bg=self.PANEL)
        h.pack(fill="x")
        self.title_lbl = tk.Label(h, text="ZWIFT CLICK", bg=self.PANEL, fg=self.FG,
                                  font=("Segoe UI", 10, "bold"))
        self.title_lbl.pack(side="left", padx=(10, 6), pady=6)
        self.summary_lbl = tk.Label(h, text="", bg=self.PANEL, fg=self.FG_DIM,
                                    font=("Segoe UI", 9))
        self.summary_lbl.pack(side="left", pady=6)
        for text, cmd in (("✕", self.request_quit), ("—", self.toggle_compact)):
            b = tk.Label(h, text=text, bg=self.PANEL, fg=self.FG_DIM,
                         font=("Segoe UI", 10), cursor="hand2", padx=6)
            b.pack(side="right", padx=(0, 4))
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.config(fg=self.FG))
            b.bind("<Leave>", lambda e, w=b: w.config(fg=self.FG_DIM))

    def _build_pad(self):
        self.pad_frame = tk.Frame(self.body, bg=self.BG)
        self.pad_frame.pack(fill="x", padx=10, pady=(0, 6))
        cell, gap = 34, 4
        w = 7 * cell + 6 * gap
        self.pad = tk.Canvas(self.pad_frame, width=w, height=3 * cell + 2 * gap + 2,
                             bg=self.BG, highlightthickness=0)
        self.pad.pack()
        self.pad_items = {}
        self.pad_keys = {}
        for name, label, col, row in self.PAD:
            x0, y0 = col * (cell + gap), row * (cell + gap) + 1
            rect = self._round_rect(x0, y0, x0 + cell, y0 + cell, 8,
                                    fill=self.BTN_IDLE, outline="")
            self.pad.create_text(x0 + cell / 2, y0 + cell / 2 - 5, text=label,
                                 fill=self.FG, font=("Segoe UI", 10, "bold"))
            self.pad_keys[name] = self.pad.create_text(
                x0 + cell / 2, y0 + cell - 7, text="",
                fill=self.FG_DIM, font=("Consolas", 7))
            self.pad_items[name] = rect
        self._label_pad()

    def _label_pad(self):
        for name, item in self.pad_keys.items():
            key = self.bindings.get(name, "")
            self.pad.itemconfig(item, text=key[:5] or "·")

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.pad.create_polygon(pts, smooth=True, **kw)

    def _build_activity(self):
        self.act_frame = tk.Frame(self.body, bg=self.BG)
        self.act_frame.pack(fill="x", padx=10, pady=(0, 8))
        tk.Frame(self.act_frame, bg=self.BORDER, height=1).pack(fill="x", pady=(0, 4))
        self.act_lbl = tk.Label(self.act_frame, text="", bg=self.BG, fg=self.FG_DIM,
                                font=("Consolas", 8), justify="left", anchor="w",
                                wraplength=self.WIDTH - 24)
        self.act_lbl.pack(fill="x")
        self.hint_lbl = tk.Label(self.act_frame, text="drag to move · right-click for menu",
                                 bg=self.BG, fg=self.FG_FAINT, font=("Segoe UI", 7))
        self.hint_lbl.pack(anchor="w", pady=(2, 0))

    # ---- rendering ----
    def _state_style(self, state):
        return {
            "connected": (self.COL_CONNECTED, "Connected"),
            "connecting": (self.COL_CONNECTING, "Connecting…"),
            "disconnected": (self.COL_DISCONNECTED, "Reconnecting…"),
        }.get(state, (self.FG_DIM, state))

    def _render_devices(self):
        for w in self.dev_frame.winfo_children():
            w.destroy()
        self.dots = {}

        rows = list(self.devices.items())
        for i, (addr, d) in enumerate(rows):
            col, text = self._state_style(d["state"])
            row = tk.Frame(self.dev_frame, bg=self.PANEL)
            row.pack(fill="x", pady=2)
            dot = tk.Label(row, text="●", bg=self.PANEL, fg=col, font=("Segoe UI", 10))
            dot.pack(side="left", padx=(8, 4), pady=3)
            self.dots[addr] = (dot, d["state"], col)
            tk.Label(row, text=d.get("label", f"Controller {i + 1}"), bg=self.PANEL,
                     fg=self.FG, font=("Segoe UI", 9, "bold")).pack(side="left")
            if not self.compact:
                tk.Label(row, text=addr[-5:], bg=self.PANEL, fg=self.FG_FAINT,
                         font=("Consolas", 7)).pack(side="left", padx=(6, 0))
            bat = d.get("battery")
            if bat is not None:
                bcol = self.COL_DISCONNECTED if bat <= 15 else (
                    self.COL_SCANNING if bat <= 35 else self.FG_DIM)
                tk.Label(row, text=f"{bat}%", bg=self.PANEL, fg=bcol,
                         font=("Segoe UI", 8)).pack(side="right", padx=(4, 8))
            tk.Label(row, text=text, bg=self.PANEL, fg=col,
                     font=("Segoe UI", 8)).pack(side="right", padx=(4, 4 if bat is not None else 8))

        connected = sum(1 for d in self.devices.values() if d["state"] == "connected")
        if connected < self.want and (self.scanning or not rows):
            row = tk.Frame(self.dev_frame, bg=self.BG)
            row.pack(fill="x", pady=2)
            dot = tk.Label(row, text="●", bg=self.BG, fg=self.COL_SCANNING,
                           font=("Segoe UI", 10))
            dot.pack(side="left", padx=(8, 4))
            self.dots["_scan"] = (dot, "scanning", self.COL_SCANNING)
            tk.Label(row, text="Scanning — press a Click button to wake it",
                     bg=self.BG, fg=self.FG_DIM, font=("Segoe UI", 8)).pack(side="left")

        col = self.COL_CONNECTED if connected >= self.want else (
            self.COL_SCANNING if connected else self.COL_DISCONNECTED)
        self.summary_lbl.config(text=f"{connected}/{self.want} connected", fg=col)

    def _render_activity(self):
        self.act_lbl.config(text="\n".join(f"{t}  {m}" for t, m in self.activity) or "—")

    def _set_pressed(self, buttons):
        buttons = set(buttons)
        for name, item in self.pad_items.items():
            self.pad.itemconfig(item, fill=self.COL_PRESS if name in buttons else self.BTN_IDLE)

    def _log(self, text):
        self.activity.append((time.strftime("%H:%M:%S"), text))
        self.activity = self.activity[-self.MAX_ACTIVITY:]
        self._render_activity()

    def toggle_compact(self, save=True):
        self.compact = not self.compact
        if self.compact:
            self.pad_frame.pack_forget()
            self.act_frame.pack_forget()
        else:
            self.pad_frame.pack(fill="x", padx=10, pady=(0, 6))
            self.act_frame.pack(fill="x", padx=10, pady=(0, 8))
        self._render_devices()
        self._resize()
        if save:
            self._save_state()

    def _resize(self):
        self.root.update_idletasks()
        h = self.frame.winfo_reqheight() + 2
        self.root.geometry(f"{self.WIDTH}x{h}")

    # ---- event pump ----
    def _tick(self):
        if self.quit_requested:
            self.root.quit()
            return
        dirty = False
        try:
            while True:
                kind, d = self.ui.q.get_nowait()
                dirty |= self._handle(kind, d)
        except queue.Empty:
            pass
        if dirty:
            self._render_devices()
            self._resize()
        self.root.after(40, self._tick)

    def _handle(self, kind, d):
        if kind == "device":
            addr = d["address"]
            dev = self.devices.setdefault(addr, {"label": f"Controller {len(self.devices) + 1}"})
            prev = dev.get("state")
            dev["state"] = d["state"]
            if d["state"] == "connected":
                self._log(f"{dev['label']} connected")
            elif d["state"] == "disconnected" and prev != "disconnected":
                why = d.get("reason", "")
                self._log(f"{dev['label']} lost" + (f" ({why[:30]})" if why else ""))
            return True
        if kind == "battery":
            if d["address"] in self.devices:
                self.devices[d["address"]]["battery"] = d["level"]
                return True
        elif kind == "side":
            dev = self.devices.get(d["address"])
            if dev and dev["label"] != d["side"]:
                dev["label"] = d["side"]
                return True
        elif kind == "scan":
            if self.scanning != d["active"]:
                self.scanning = d["active"]
                return True
        elif kind == "pressed":
            self._set_pressed(d["buttons"])
        elif kind == "button" and d["down"]:
            self._log(f"{d['button']} → {d['key']}")
        elif kind == "log":
            self._log(d["text"])
        elif kind == "config":
            self.cfg = d["cfg"]
            self.bindings = self.cfg["bindings"]
            self.want = max(1, int(self.cfg.get("controller_count", 2)))
            self._label_pad()
            self._log("config.json reloaded")
            for p in d["problems"]:
                self._log(p)
            return True
        return False

    def _blink_tick(self):
        self._blink = not self._blink
        for dot, state, col in getattr(self, "dots", {}).values():
            if state in ("scanning", "connecting", "disconnected"):
                try:
                    dot.config(fg=col if self._blink else self.FG_FAINT)
                except tk.TclError:
                    pass
        self.root.after(500, self._blink_tick)

    # ---- window behaviour ----
    def _start_drag(self, event):
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _do_drag(self, event):
        dx, dy = self._drag
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _show_menu(self, event):
        self.menu.entryconfig(0, label="Full mode" if self.compact else "Compact mode")
        self.menu.tk_popup(event.x_root, event.y_root)

    def _restore_state(self):
        x, y = self.root.winfo_screenwidth() - self.WIDTH - 16, 16
        compact = False
        try:
            s = json.loads(OVERLAY_STATE_PATH.read_text())
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            if 0 <= s["x"] < sw - 40 and 0 <= s["y"] < sh - 40:
                x, y = s["x"], s["y"]
            compact = bool(s.get("compact"))
        except Exception:
            pass
        self.root.geometry(f"+{x}+{y}")
        if compact:
            self.toggle_compact(save=False)
        self._resize()

    def _save_state(self):
        try:
            OVERLAY_STATE_PATH.write_text(json.dumps(
                {"x": self.root.winfo_x(), "y": self.root.winfo_y(), "compact": self.compact}))
        except Exception:
            pass

    def request_quit(self):
        self._save_state()
        self.quit_requested = True
        self.on_quit()

    def show_closing(self):
        self.summary_lbl.config(text="closing…", fg=self.FG_DIM)
        self.root.update()

    def run(self):
        self.root.mainloop()

    def close(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ------------------------------------------------------------------ main ---
def print_bindings(cfg, waiting=True):
    repeat = set(cfg.get("hold_to_repeat", []))
    print("=== Bindings ===")
    for btn, key in cfg["bindings"].items():
        tag = " (hold repeats)" if btn in repeat else ""
        print(f"  {btn:<6} -> {key}{tag}")
    if waiting:
        print("Waiting for controllers... press a button on each Click to wake it.\n")


def install_console_close_handler(on_close):
    """
    On Windows, closing the console window kills the process without running
    any cleanup. Hook it so controllers get disconnected and keys released.
    """
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(event):
        if event in (2, 5, 6):  # CLOSE, LOGOFF, SHUTDOWN
            on_close()
            return True
        return False

    cb = HANDLER(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True)
    return cb  # caller must keep a reference alive


def acquire_single_instance():
    """
    Two copies running at once fight over the same controllers (each grabs
    one, then they knock each other off). Hold a localhost port as a lock.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47613))
        s.listen(1)
    except OSError:
        s.close()
        return None
    return s


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    lock = acquire_single_instance()  # noqa: F841 - held for the process lifetime
    if lock is None:
        print("Zwift Click Mapper is already running (check for another window).")
        return
    cfg, problems = load_config()
    ui = UiBus()
    for p in problems:
        ui.emit("log", text=p)
    mapper = ClickMapper(cfg, ui)
    manager = ClickManager(cfg, mapper, ui)
    print_bindings(cfg)

    if not cfg.get("show_overlay", True):
        try:
            asyncio.run(manager.run())
        except KeyboardInterrupt:
            pass
        print("Bye.")
        return

    ble_thread = threading.Thread(target=lambda: asyncio.run(manager.run()),
                                  name="ble", daemon=True)

    def shutdown_ble():
        manager.request_stop()
        ble_thread.join(timeout=DISCONNECT_TIMEOUT + 3)

    overlay = StatusOverlay(ui, cfg, on_quit=manager.request_stop,
                            on_reconnect=manager.request_reconnect_all)

    # Ctrl+C: Tk swallows KeyboardInterrupt inside callbacks, so route it
    # through a flag the overlay's event pump checks.
    signal.signal(signal.SIGINT, lambda *_: setattr(overlay, "quit_requested", True))
    _console_cb = install_console_close_handler(shutdown_ble)

    ble_thread.start()
    try:
        overlay.run()
    finally:
        overlay.show_closing()
        shutdown_ble()
        overlay.close()
        print("Bye.")


if __name__ == "__main__":
    main()
