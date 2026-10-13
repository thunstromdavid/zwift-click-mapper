# Zwift Click v2 → Keyboard Mapper (Windows + macOS)

Use your **Zwift Click v2** as a keyboard on Windows — bind any Click button to any key. Works with Zwift, MyWhoosh, indieVelo, emulators, or any app that takes keyboard input. Yes, you can play Pokémon on your trainer.

No companion app needed — connects directly to the Click over Bluetooth LE.

## Features

- All 10 buttons bindable (D-pad, A/B/Y/Z, plus/minus) via a simple `config.json`
- Real press-and-hold behaviour (key goes down when you press, up when you release) — works in games and emulators that poll key state
- Multiple simultaneous buttons (diagonals, combos)
- Hardware-level scan code injection (`pydirectinput`) — works where soft keypresses don't
- Connects **both** Click v2 units (left + right) at once, each tracked separately
- Always-on-top overlay: per-controller status + battery, a live button pad that lights up on press, recent activity
- Auto-reconnect if a Click drops out or goes to sleep, with full Bluetooth cleanup so it can be found again
- Held keys are released automatically if a controller disconnects or the app closes
- Unknown buttons are auto-detected and printed so you can bind them
- Hold-to-repeat (e.g. hold + to keep shifting), live reload of `config.json` (no restart needed), typo'd key names flagged in the overlay
- `start.bat` asks for admin rights and installs dependencies on first run

## Requirements

- Windows 10/11 or macOS, with Bluetooth LE
- Python 3.10+ (tick **"Add Python to PATH"** during install!)
- A Zwift Click v2

## Setup

1. Install Python from https://www.python.org/downloads/
2. Download this repo (green **Code** button → Download ZIP) and extract it anywhere.
3. Windows: nothing else, `start.bat` installs what it needs. Manual install: `pip install -r requirements.txt`

## Usage

1. Make sure the Click is **not** connected to Zwift / Companion / your phone — it can only talk to one thing at a time.
2. Press a Click button to wake it (blue blinking LED).
3. Double-click `start.bat` and accept the admin prompt (admin is needed for key injection into most apps).
4. Wait for the overlay to show "2/2 connected". Done.

### The overlay

- **Drag** it anywhere; its position is remembered.
- **—** toggles compact mode (status only); **✕** quits cleanly.
- **Right-click** for the menu: compact mode, **Reconnect controllers** (forces a fresh connection if something looks stuck), Quit.

Options in `config.json`:

- `"controller_count": 2`: how many Click units to connect (set 1 if you only use one)
- `"show_overlay": true`: set `false` for terminal-only
- `"overlay_opacity": 0.92`

## Configuring bindings

Edit `config.json` in Notepad:

```json
"bindings": {
  "up": "up",
  "down": "down",
  "left": "left",
  "right": "right",
  "a": "x",
  "b": "z",
  "y": "v",
  "z": "c",
  "plus": "q",
  "minus": "w"
}
```

Left side = Click button, right side = keyboard key to send. Key names: letters, numbers, `up/down/left/right`, `space`, `enter`, `esc`, `f1`–`f12`, etc. Use `""` to unbind a button. Just save the file: changes apply immediately, and unknown key names show up as a warning in the overlay.

Hold-to-repeat: buttons listed in `"hold_to_repeat"` (default `["plus", "minus"]`) re-tap their key while held, after `repeat_initial_delay_ms` and then every `repeat_interval_ms`. Set it to `[]` to turn that off.

## Troubleshooting

- **"python not found"** — reinstall Python and tick "Add Python to PATH"
- **"already running"** — another copy is open (maybe an elevated one in another window); only one can talk to the Clicks at a time
- **Device not found** — wake the Click (press a button); make sure nothing else is connected to it
- **Click won't reconnect / seems stuck** — use **Reconnect controllers** from the overlay menu. If the Click shows up under Windows *Settings → Bluetooth & devices*, **remove it there**: this tool talks to it directly, and a Windows pairing can hold on to the connection so the Click stops advertising. As a last resort, toggle Bluetooth off/on.
- **Always quit via the overlay ✕ or Ctrl+C** rather than killing Python, so the controllers get a clean disconnect
- **Terminal shows presses but the app ignores them** — run as administrator; click the app window so it has focus; check the app's own key bindings match your config
- **`unbound button: btnX_Y`** — that button isn't in your config yet; add it using that exact name
- **LED flashing orange** — flat CR2032 battery

## macOS

Same script works on Mac:

1. Install Python 3 (from python.org or `brew install python`)
2. In Terminal: `pip3 install -r requirements.txt`
3. Run: `python3 zwift_click_mapper.py`
4. **Grant permissions** when macOS asks (or pre-emptively in System Settings → Privacy & Security):
   - **Bluetooth** for Terminal/Python
   - **Accessibility** for Terminal/Python (required for key injection — without this, keys silently do nothing)

Key names in config.json are the same. No admin/sudo needed.

## Credits & disclaimer

Built on the BLE protocol reverse-engineering work in [andriuz29/Zwift-Click-V2-Universal-PC-Controller](https://github.com/andriuz29/Zwift-Click-V2-Universal-PC-Controller). Thanks!

**This is an unofficial community tool**, not affiliated with or endorsed by Zwift. The protocol is reverse-engineered — a Zwift firmware update could break it at any time. Use at your own risk.

## License

MIT — do whatever you want with it, no warranty.
