#!/usr/bin/env python3
import sys
import time
import json
import select
from pathlib import Path

from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306

# Shared UI helpers (already used in your other modules)
from oled.ui_common import render, draw_row, draw_centered

# =====================
# PATHS / SETTINGS
# =====================
HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "settings.json"

DEFAULT_SETTINGS = {
    "step_mhz": 0.1,
    "sweep_ms": 150,          # 50..350 step 50
    "scan_style": "LOOP",     # LOOP / BOUNCE / RANDOM
    # FM band intentionally removed (fixed/implied)
    # mute/filters intentionally omitted for now (future)
}

# =====================
# OLED INIT
# =====================
serial = i2c(port=1, address=0x3C)
device = ssd1306(serial, width=128, height=64)

# =====================
# UI CONSTANTS
# =====================
HEADER_Y = 0
DIVIDER_Y = 12

LIST_Y0 = 16
ROW_H = 10

FOOTER_LINE_Y = 54
FOOTER_Y = 56

def header(d, title: str):
    d.text((2, HEADER_Y), title, fill=255)
    d.line((0, DIVIDER_Y, 127, DIVIDER_Y), fill=255)

def footer(d, text: str):
    d.line((0, FOOTER_LINE_Y, 127, FOOTER_LINE_Y), fill=255)
    d.text((2, FOOTER_Y), text, fill=255)

# =====================
# SETTINGS I/O
# =====================
def load_settings():
    if SETTINGS_FILE.exists():
        try:
            s = json.loads(SETTINGS_FILE.read_text())
            out = DEFAULT_SETTINGS.copy()
            out.update(s)
            return out
        except Exception:
            pass
    SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, indent=2))
    return DEFAULT_SETTINGS.copy()

def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))

# =====================
# BUTTON INPUT (stdin from parent UI)
# =====================
def read_event():
    """
    Reads one event token from stdin, non-blocking.
    Expected tokens: up, down, select, select_hold, back
    """
    try:
        r, _, _ = select.select([sys.stdin], [], [], 0)
    except Exception:
        return None
    if not r:
        return None
    line = sys.stdin.readline()
    if not line:
        return None
    return line.strip()

# =====================
# TEA5767 RADIO CONTROL (STUB for now)
# =====================
def tune(_freq_mhz: float):
    """
    TODO: Implement TEA5767 tuning over I2C.
    For now, it's a stub so UI + flow works while hardware arrives.
    """
    return

# =====================
# SCREENS
# =====================
def screen_menu(settings, sel):
    items = ["START", "SETTINGS"]  # BACK is hardware-only

    def _draw(d):
        header(d, "SPIRIT BOX")

        # Compact status line (no FM band info shown)
        d.text((2, LIST_Y0), f"Rate {int(settings['sweep_ms'])}ms  {settings['scan_style']}", fill=255)

        menu_y = LIST_Y0 + ROW_H + 2
        draw_row(d, menu_y + 0 * ROW_H, items[0], selected=(sel == 0))
        draw_row(d, menu_y + 1 * ROW_H, items[1], selected=(sel == 1))

        footer(d, "SEL choose   BACK exit")

    return items, _draw

def screen_settings(settings, sel_idx):
    # Each editable item on its own line
    items = ["Sweep Rate", "Step", "Scan Style"]

    def _draw(d):
        header(d, "SETTINGS")

        draw_row(d, LIST_Y0 + 0 * ROW_H,
                 f"Rate: {int(settings['sweep_ms'])}ms",
                 selected=(sel_idx == 0))
        draw_row(d, LIST_Y0 + 1 * ROW_H,
                 f"Step: {settings['step_mhz']:.1f}MHz",
                 selected=(sel_idx == 1))
        draw_row(d, LIST_Y0 + 2 * ROW_H,
                 f"Scan: {settings['scan_style']}",
                 selected=(sel_idx == 2))

        footer(d, "SEL edit   BACK menu")

    return items, _draw

def screen_running(freq, settings):
    def _draw(d):
        header(d, "FM SWEEP")

        # Tight layout, footer always visible
        draw_centered(d, 22, f"{freq:.1f} MHz", invert=False)
        d.text((2, 38), f"Rate: {int(settings['sweep_ms'])}ms", fill=255)
        d.text((2, 48), f"Scan: {settings['scan_style']}", fill=255)

        footer(d, "BACK stop  HOLD settings")

    return _draw

# =====================
# EDITORS
# =====================
def edit_sweep_rate(settings):
    original = int(settings["sweep_ms"])
    val = original

    MIN_MS, MAX_MS, STEP_MS = 50, 350, 50

    blink = False
    last_blink = time.time()

    while True:
        now = time.time()
        if now - last_blink > 0.35:
            blink = not blink
            last_blink = now

        def _draw(d):
            header(d, "SWEEP RATE")
            draw_centered(d, 26, f"{val} ms", invert=blink)
            d.text((2, 40), "UP/DN adjust", fill=255)
            footer(d, "SEL save  BACK cancel")

        render(device, _draw)

        ev = read_event()
        if ev == "up":
            val = min(MAX_MS, val + STEP_MS)
        elif ev == "down":
            val = max(MIN_MS, val - STEP_MS)
        elif ev == "select":
            settings["sweep_ms"] = val
            return settings, True
        elif ev == "back":
            settings["sweep_ms"] = original
            return settings, False

        time.sleep(0.03)

def edit_scan_style(settings):
    styles = ["LOOP", "BOUNCE", "RANDOM"]
    cur = settings.get("scan_style", "LOOP")
    idx = styles.index(cur) if cur in styles else 0

    while True:
        def _draw(d):
            header(d, "SCAN STYLE")
            for i, s in enumerate(styles):
                draw_row(d, LIST_Y0 + i * ROW_H, s, selected=(i == idx))
            footer(d, "SEL save  BACK cancel")

        render(device, _draw)

        ev = read_event()
        if ev == "up":
            idx = (idx - 1) % len(styles)
        elif ev == "down":
            idx = (idx + 1) % len(styles)
        elif ev == "select":
            settings["scan_style"] = styles[idx]
            return settings, True
        elif ev == "back":
            return settings, False

        time.sleep(0.03)

# =====================
# FLOWS
# =====================
def settings_flow(settings, return_to="MENU"):
    """
    return_to:
      - "MENU": BACK returns to menu
      - "SWEEP": BACK returns to sweep (when opened from hold)
    """
    sel = 0

    while True:
        items, draw_fn = screen_settings(settings, sel)
        if sel < 0 or sel >= len(items):
            sel = 0

        render(device, draw_fn)

        ev = read_event()
        if ev == "up":
            sel = (sel - 1) % len(items)
        elif ev == "down":
            sel = (sel + 1) % len(items)
        elif ev == "back":
            return settings, return_to
        elif ev == "select":
            if sel == 0:
                settings, changed = edit_sweep_rate(settings)
                if changed:
                    save_settings(settings)
            elif sel == 1:
                # Step fixed for now (0.1). Hook for future editor lives here.
                pass
            elif sel == 2:
                settings, changed = edit_scan_style(settings)
                if changed:
                    save_settings(settings)

        time.sleep(0.05)

def run_sweep(settings):
    # FM range implied; pick a reasonable working range until your TEA5767 code lands.
    # If you want a different fixed range, set these constants:
    FMIN = 76.0
    FMAX = 108.0

    freq = FMIN
    step = float(settings["step_mhz"])
    direction = 1

    while True:
        tune(freq)
        render(device, screen_running(freq, settings))

        ev = read_event()
        if ev == "back":
            # Stop sweep and return to Spirit Box menu (do NOT exit module)
            return settings, "MENU"
        if ev == "select_hold":
            # Open settings, then return to sweep after closing
            settings, _ = settings_flow(settings, return_to="SWEEP")
            save_settings(settings)

        style = settings.get("scan_style", "LOOP")

        if style == "LOOP":
            freq += step
            if freq > FMAX:
                freq = FMIN

        elif style == "BOUNCE":
            freq += step * direction
            if freq >= FMAX:
                freq = FMAX
                direction = -1
            elif freq <= FMIN:
                freq = FMIN
                direction = 1

        else:  # RANDOM (deterministic hop w/o importing random)
            span = max(0.1, (FMAX - FMIN))
            freq = FMIN + ((freq * 13.7 + 1.3) % span)

        delay = max(0.05, int(settings["sweep_ms"]) / 1000.0)
        time.sleep(delay)

# =====================
# MAIN STATE MACHINE
# =====================
def main():
    settings = load_settings()
    state = "MENU"
    sel = 0

    while True:
        if state == "MENU":
            menu, draw_fn = screen_menu(settings, sel)
            render(device, draw_fn)

            ev = read_event()
            if ev == "up":
                sel = (sel - 1) % len(menu)
            elif ev == "down":
                sel = (sel + 1) % len(menu)
            elif ev == "back":
                # BACK from menu exits module (returns to module selector)
                return
            elif ev == "select":
                if menu[sel] == "START":
                    state = "SWEEP"
                else:
                    state = "SETTINGS"

            time.sleep(0.05)

        elif state == "SETTINGS":
            settings, next_state = settings_flow(settings, return_to="MENU")
            save_settings(settings)
            state = next_state

        elif state == "SWEEP":
            settings, next_state = run_sweep(settings)
            save_settings(settings)
            state = next_state

        else:
            state = "MENU"

if __name__ == "__main__":
    main()
