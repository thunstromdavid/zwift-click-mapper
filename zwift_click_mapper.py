"""
Zwift Click v2 -> Keyboard Mapper
Connects directly to a Zwift Click v2 over Bluetooth LE and turns button
presses into keyboard keystrokes. Edit config.json to change key bindings.

Run:  python zwift_click_mapper.py
Stop: Ctrl+C in the terminal window
"""

import asyncio
import json
import sys
import time
import threading
from pathlib import Path

import platform

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Missing library. Run:  pip install bleak")
    sys.exit(1)

# --- overlay UI (tkinter – ships with Python) --------------------------------
import tkinter as tk


class StatusOverlay:
    """
    A small, always-on-top, semi-transparent overlay window that displays the
    live connection state of Zwift Click controllers and recent button activity.
    """

    # colour palette
    BG = "#1a1a2e"
    FG_TITLE = "#e0e0e0"
    FG_STATUS = "#aaaaaa"
    COL_SCANNING = "#f0a500"
    COL_CONNECTING = "#5bc0eb"
    COL_CONNECTED = "#38b000"
    COL_DISCONNECTED = "#e63946"
    COL_BUTTON = "#9d4edd"

    MAX_ACTIVITY = 5  # lines of recent button activity to show

    def __init__(self):
        self._root = tk.Tk()
        self._root.title("Zwift Click Overlay")
        self._root.overrideredirect(True)         # no title bar
        self._root.attributes("-topmost", True)    # always on top
        self._root.attributes("-alpha", 0.88)      # slight transparency
        self._root.configure(bg=self.BG)

        # ---- position: top-right of the primary monitor, with some margin ----
        screen_w = self._root.winfo_screenwidth()
        win_w, win_h = 280, 210
        x = screen_w - win_w - 16
        y = 16
        self._root.geometry(f"{win_w}x{win_h}+{x}+{y}")

        # ---- make the window draggable ----
        self._drag_x = 0
        self._drag_y = 0
        self._root.bind("<ButtonPress-1>", self._start_drag)
        self._root.bind("<B1-Motion>", self._do_drag)

        # ---- widgets ----
        pad = dict(padx=10, anchor="w")

        self._title = tk.Label(
            self._root, text="🎮  Zwift Click Mapper",
            bg=self.BG, fg=self.FG_TITLE,
            font=("Segoe UI", 11, "bold"),
        )
        self._title.pack(pady=(8, 2), **pad)

        # thin separator line
        tk.Frame(self._root, bg="#333355", height=1).pack(fill="x", padx=8, pady=2)

        self._status_label = tk.Label(
            self._root, text="● Initializing...",
            bg=self.BG, fg=self.COL_SCANNING,
            font=("Segoe UI", 10),
        )
        self._status_label.pack(pady=(2, 0), **pad)

        self._device_label = tk.Label(
            self._root, text="",
            bg=self.BG, fg=self.FG_STATUS,
            font=("Segoe UI", 9),
        )
        self._device_label.pack(pady=0, **pad)

        # thin separator
        tk.Frame(self._root, bg="#333355", height=1).pack(fill="x", padx=8, pady=4)

        self._activity_header = tk.Label(
            self._root, text="Recent activity",
            bg=self.BG, fg="#777799",
            font=("Segoe UI", 8),
        )
        self._activity_header.pack(pady=0, **pad)

        self._activity_label = tk.Label(
            self._root, text="—",
            bg=self.BG, fg=self.COL_BUTTON,
            font=("Consolas", 9),
            justify="left",
        )
        self._activity_label.pack(pady=(0, 6), **pad)

        # right-click to close
        self._root.bind("<ButtonPress-3>", lambda e: self._root.destroy())

        # internal state
        self._activity_lines: list[str] = []

    # ---- drag helpers ----
    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event):
        x = self._root.winfo_x() + event.x - self._drag_x
        y = self._root.winfo_y() + event.y - self._drag_y
        self._root.geometry(f"+{x}+{y}")

    # ---- public API (call from any thread via _schedule) ----

    def _schedule(self, fn, *args):
        """Thread-safe: schedule *fn* to run on the Tk main-loop thread."""
        try:
            self._root.after(0, fn, *args)
        except tk.TclError:
            pass  # window already closed

    def set_scanning(self):
        self._schedule(self._set_scanning)

    def _set_scanning(self):
        self._status_label.config(text="● Scanning for Click...", fg=self.COL_SCANNING)
        self._device_label.config(text="Press a button to wake the controller")

    def set_connecting(self, name: str, address: str):
        self._schedule(self._set_connecting, name, address)

    def _set_connecting(self, name, address):
        self._status_label.config(text="● Connecting...", fg=self.COL_CONNECTING)
        self._device_label.config(text=f"{name}  [{address}]")

    def set_connected(self, name: str, address: str, bindings: dict):
        self._schedule(self._set_connected, name, address, bindings)

    def _set_connected(self, name, address, bindings):
        self._status_label.config(text="● Connected", fg=self.COL_CONNECTED)
        self._device_label.config(text=f"{name}  [{address}]")
        self._add_activity_line(f"Connected — {len(bindings)} bindings active")

    def set_disconnected(self, reason: str = ""):
        self._schedule(self._set_disconnected, reason)

    def _set_disconnected(self, reason):
        extra = f" ({reason})" if reason else ""
        self._status_label.config(text=f"● Disconnected{extra}", fg=self.COL_DISCONNECTED)
        self._add_activity_line("Disconnected" + (f": {reason}" if reason else ""))

    def set_not_found(self):
        self._schedule(self._set_not_found)

    def _set_not_found(self):
        self._status_label.config(text="● Not found – retrying…", fg=self.COL_DISCONNECTED)
        self._device_label.config(text="Is the Click awake / blinking blue?")

    def log_button(self, button: str, key: str, direction: str):
        self._schedule(self._log_button, button, key, direction)

    def _log_button(self, button, key, direction):
        ts = time.strftime("%H:%M:%S")
        self._add_activity_line(f"{ts}  {button} → {key} {direction}")

    def _add_activity_line(self, line: str):
        self._activity_lines.append(line)
        if len(self._activity_lines) > self.MAX_ACTIVITY:
            self._activity_lines = self._activity_lines[-self.MAX_ACTIVITY:]
        self._activity_label.config(text="\n".join(self._activity_lines))

    def run(self):
        """Start the Tk main loop (blocks)."""
        self._root.mainloop()

    def close(self):
        try:
            self._root.destroy()
        except tk.TclError:
            pass


# Global overlay instance – created in main, used everywhere
overlay: StatusOverlay | None = None

# --- key injection backend: pydirectinput on Windows, pynput elsewhere ---
IS_WINDOWS = platform.system() == "Windows"

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
            print(f"Unknown key name in config: '{name}'")
            return None

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
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
            for k, v in user.items():
                if k == "bindings":
                    cfg["bindings"] = {**DEFAULTS["bindings"], **v}
                else:
                    cfg[k] = v
        except json.JSONDecodeError as e:
            print(f"config.json has an error ({e}). Using defaults.")
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2))
        print(f"Created default config at {CONFIG_PATH}")
    return cfg


# ------------------------------------------------------------- protocol ---
NOTIFY_CHAR = "00000002-19ca-4651-86e5-fa29dcdd09d1"
WRITE_CHAR = "00000003-19ca-4651-86e5-fa29dcdd09d1"
HANDSHAKE = ["526964654f6e0203", "000800", "000810"]
KEEPALIVE = "000810"

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


def decode_buttons(hx):
    """Return set of pressed button names from a 2308...0F state packet."""
    mask = bytes.fromhex(hx[4:12])
    pressed = set()
    for i, byte in enumerate(mask):
        for bit in range(8):
            if not byte & (1 << bit):
                pressed.add(BIT_NAMES.get((i, bit), f"btn{i}_{bit}"))
    return pressed


class ClickMapper:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bindings = cfg["bindings"]
        self.repeat_buttons = set(cfg.get("hold_to_repeat", []))
        self.debounce = cfg.get("debounce_ms", 60) / 1000
        self.initial_delay = cfg.get("repeat_initial_delay_ms", 350) / 1000
        self.interval = cfg.get("repeat_interval_ms", 150) / 1000
        self.last_press = {}
        self.pressed = set()

    def key_down(self, button):
        key = self.bindings.get(button)
        if not key:
            return
        now = time.time()
        if now - self.last_press.get(button, 0) < self.debounce:
            return
        self.last_press[button] = now
        key_down_raw(key)
        print(f"  {button:<6} -> {key} DOWN")
        if overlay:
            overlay.log_button(button, key, "DOWN")

    def key_up(self, button):
        key = self.bindings.get(button)
        if not key:
            return
        key_up_raw(key)
        print(f"  {button:<6} -> {key} UP")
        if overlay:
            overlay.log_button(button, key, "UP")

    def handle(self, _sender, data: bytearray):
        hx = data.hex().upper()
        if not hx.startswith("2308") or len(hx) != 14:
            return
        now_pressed = decode_buttons(hx)
        for button in now_pressed - self.pressed:
            if button not in self.bindings:
                print(f"  unbound button: {button}  (add it to config.json)")
            self.key_down(button)
        for button in self.pressed - now_pressed:
            self.key_up(button)
        self.pressed = now_pressed


async def connect_once(cfg, mapper):
    print("Searching for Zwift Click... (press a button to wake it)")
    if overlay:
        overlay.set_scanning()

    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and cfg["device_name_filter"] in d.name,
        timeout=15.0,
    )
    if not device:
        if overlay:
            overlay.set_not_found()
        return False

    print(f"Found: {device.name} [{device.address}] — connecting...")
    if overlay:
        overlay.set_connecting(device.name, device.address)

    client = BleakClient(device.address)
    try:
        await client.connect()
        await client.start_notify(NOTIFY_CHAR, mapper.handle)
        for msg in HANDSHAKE:
            await client.write_gatt_char(
                WRITE_CHAR, bytearray.fromhex(msg), response=False
            )
            await asyncio.sleep(0.1)

        print("\n=== Connected. Bindings ===")
        for btn, key in cfg["bindings"].items():
            tag = " (hold repeats)" if btn in mapper.repeat_buttons else ""
            print(f"  {btn:<6} -> {key}{tag}")
        print("Ctrl+C to quit.\n")

        if overlay:
            overlay.set_connected(device.name, device.address, cfg["bindings"])

        while client.is_connected:
            await client.write_gatt_char(
                WRITE_CHAR, bytearray.fromhex(KEEPALIVE), response=False
            )
            await asyncio.sleep(5)

    except asyncio.CancelledError:
        print("Connection cancelled.")
    except Exception as e:
        print(f"Connection error during session: {e}")
    finally:
        # Clean disconnect so the controller doesn't hang
        try:
            if client.is_connected:
                await client.stop_notify(NOTIFY_CHAR)
                await client.disconnect()
                print("Cleanly disconnected from controller.")
        except Exception as e:
            print(f"Error during disconnect: {e}")

    return True


async def main():
    cfg = load_config()
    mapper = ClickMapper(cfg)
    while True:
        try:
            found = await connect_once(cfg, mapper)
            if not found:
                print("Not found. Retrying in 5s... (is the Click awake/blinking blue?)")
                await asyncio.sleep(5)
            else:
                print("Disconnected. Reconnecting...")
                if overlay:
                    overlay.set_disconnected("link lost")
                await asyncio.sleep(2)
        except Exception as e:
            print(f"Connection error: {e}. Retrying in 5s...")
            if overlay:
                overlay.set_disconnected(str(e))
            await asyncio.sleep(5)


if __name__ == "__main__":
    overlay = StatusOverlay()

    # Run the asyncio BLE loop in a background daemon thread so the
    # tkinter main loop can own the main thread (required on Windows/macOS).
    def _run_async():
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass

    ble_thread = threading.Thread(target=_run_async, daemon=True)
    ble_thread.start()

    try:
        overlay.run()  # blocks – Tk main loop
    except KeyboardInterrupt:
        pass
    finally:
        overlay.close()
        print("\nBye.")
