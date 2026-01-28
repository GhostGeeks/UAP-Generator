#!/usr/bin/env python3
# Ghost Geeks OLED UI - main app
# - Owns GPIO buttons
# - Owns module discovery + launcher
# - Modules render to OLED themselves, but we ALWAYS redraw menu after exit
# - Buttons are forwarded to modules over stdin as text commands

from __future__ import annotations

import os
import sys
import time
import json
import signal
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from gpiozero import Button

# OLED (luma)
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas

# =====================================================
# CONSTANTS / PATHS
# =====================================================

HOME = Path("/home/ghostgeeks01")
OLED_DIR = HOME / "oled"
MODULE_DIR = OLED_DIR / "modules"
LOG_DIR = OLED_DIR / "logs"

# Buttons (BCM pins) - adjust if yours differ
BTN_UP = 17
BTN_DOWN = 27
BTN_SELECT = 22
BTN_BACK = 23

SELECT_HOLD_SECONDS = 0.8

OLED_I2C_BUS = 1
OLED_I2C_ADDR = 0x3C
OLED_W = 128
OLED_H = 64

# Layout
TOP_Y = 0
LINE_H = 10          # font line height
DIV_Y = 12           # divider line y
BODY_Y = 14          # body start y
FOOT_Y = 54          # footer line y (keeps footer fully visible)

# =====================================================
# OLED SETUP
# =====================================================

serial = i2c(port=OLED_I2C_BUS, address=OLED_I2C_ADDR)
device = ssd1306(serial, width=OLED_W, height=OLED_H)

def oled_message(title: str, lines: List[str], footer: str = "") -> None:
    # Simple clean divider line
    with canvas(device) as draw:
        draw.text((0, TOP_Y), title[:21], fill=255)
        draw.line((0, DIV_Y, OLED_W - 1, DIV_Y), fill=255)

        y = BODY_Y
        for s in lines[:4]:
            draw.text((0, y), (s or "")[:21], fill=255)
            y += LINE_H

        if footer:
            # footer divider + footer text
            draw.line((0, FOOT_Y - 2, OLED_W - 1, FOOT_Y - 2), fill=255)
            draw.text((0, FOOT_Y), footer[:21], fill=255)

def splash(min_seconds: float = 5.0) -> None:
    # Minimal splash (clean + readable)
    start = time.time()
    while True:
        elapsed = time.time() - start
        oled_message(
            "GHOST GEEKS",
            ["REAL GHOST GEAR", "", "BOOTING UP THE LAB..."],
            footer=f"{int(max(0, min_seconds - elapsed))}s"
        )
        if elapsed >= min_seconds:
            return
        time.sleep(0.1)

# =====================================================
# UTILITIES
# =====================================================

def hostname() -> str:
    return subprocess.getoutput("hostname").strip()

def get_ip() -> str:
    out = subprocess.getoutput("hostname -I").strip().split()
    return out[0] if out else "0.0.0.0"

def uptime_short() -> str:
    # e.g. "2h13m"
    try:
        secs = float(subprocess.getoutput("cut -d. -f1 /proc/uptime").strip())
    except Exception:
        return "?"
    m = int(secs // 60)
    h = m // 60
    m = m % 60
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"

def sd_write_check() -> Optional[str]:
    """
    Basic sanity check that writes are hitting the SD/rootfs.
    Returns an error string if something looks off, else None.
    """
    try:
        test_dir = OLED_DIR / ".sd_write_test"
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "write_test.txt"
        test_file.write_text(f"ok {time.time()}\n")
        data = test_file.read_text().strip()
        if not data.startswith("ok "):
            return "SD write test failed"
        return None
    except Exception as e:
        return f"SD write error: {e}"

# =====================================================
# MODULE DISCOVERY
# =====================================================

@dataclass
class Module:
    id: str
    name: str
    subtitle: str
    entry_path: str
    order: int = 999

def discover_modules(root: Path) -> List[Module]:
    mods: List[Module] = []
    if not root.exists():
        return mods

    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue

        meta_path = d / "module.json"
        entry_path = d / "run.py"
        if not entry_path.exists():
            continue

        meta: Dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                meta = {}

        mods.append(
            Module(
                id=str(meta.get("id", d.name)),
                name=str(meta.get("name", d.name)),
                subtitle=str(meta.get("subtitle", "")),
                entry_path=str(entry_path),
                order=int(meta.get("order", 999)),
            )
        )

    mods.sort(key=lambda m: (m.order, m.name.lower()))
    return mods

# =====================================================
# BUTTONS (GPIOZERO)
# =====================================================

def init_buttons() -> Tuple:
    events = {"up": False, "down": False, "select": False, "select_hold": False, "back": False}

    btn_up = Button(BTN_UP, pull_up=True, bounce_time=0.06)
    btn_down = Button(BTN_DOWN, pull_up=True, bounce_time=0.06)
    btn_select = Button(BTN_SELECT, pull_up=True, bounce_time=0.06, hold_time=SELECT_HOLD_SECONDS)
    btn_back = Button(BTN_BACK, pull_up=True, bounce_time=0.06)

    btn_up.when_pressed = lambda: events.__setitem__("up", True)
    btn_down.when_pressed = lambda: events.__setitem__("down", True)
    btn_select.when_pressed = lambda: events.__setitem__("select", True)
    btn_select.when_held = lambda: events.__setitem__("select_hold", True)
    btn_back.when_pressed = lambda: events.__setitem__("back", True)

    def consume(k: str) -> bool:
        if events.get(k):
            events[k] = False
            return True
        return False

    def clear_events() -> None:
        for k in events:
            events[k] = False

    # Return button objects so gpiozero keeps them alive
    return consume, clear_events, (btn_up, btn_down, btn_select, btn_back)

# =====================================================
# MENU RENDER
# =====================================================

def draw_menu(mods: List[Module], idx: int) -> None:
    # 4 visible lines in body
    visible = 4
    start = max(0, min(idx - 1, max(0, len(mods) - visible)))
    window = mods[start:start + visible]

    lines = []
    for i, m in enumerate(window):
        pointer = ">" if (start + i) == idx else " "
        label = f"{pointer} {m.name}"
        lines.append(label[:21])

    footer = "SEL=start  BACK=settings"
    oled_message("MODULES", lines, footer)

# =====================================================
# MODULE RUNNER (FIXES: env, stdin, logs, redraw)
# =====================================================

def run_module(mod: Module, consume, clear_events) -> None:
    """
    Runs a module as a child process and forwards button events via stdin.

    Module receives lines:
      up, down, select, select_hold, back

    IMPORTANT:
    - We ALWAYS redraw the main menu after exit (prevents blank OLED).
    - We provide PYTHONPATH so module can import shared helpers.
    - We log stdout/stderr to /home/ghostgeeks01/oled/logs/<module>_<timestamp>.log
    """
    LOG_DIR.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{mod.id}_{ts}.log"

    oled_message("LAUNCH", [mod.name, mod.subtitle], "BACK=abort")
    time.sleep(0.15)

    env = os.environ.copy()
    # So modules can import shared files if they live in /home/ghostgeeks01/oled
    env["PYTHONPATH"] = f"{OLED_DIR}:{env.get('PYTHONPATH','')}".strip(":")
    # Helps audio routing if needed (PipeWire user session)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    # GPIO backend already used by parent, modules should NOT touch GPIO
    env.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
    env["GG_LAUNCHED_BY_UI"] = "1"

    cmd = ["/home/ghostgeeks01/oledenv/bin/python", mod.entry_path]

    with open(log_path, "w") as logf:
        logf.write(f"[launcher] cmd={cmd}\n")
        logf.flush()

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=logf,
                stderr=logf,
                text=True,
                bufsize=1,
                env=env,
                cwd=str(Path(mod.entry_path).parent),
            )
        except Exception as e:
            oled_message("LAUNCH FAIL", [mod.name, str(e)[:21]], "BACK=menu")
            time.sleep(1.2)
            return

        def send(line: str) -> None:
            try:
                if proc.poll() is None and proc.stdin:
                    proc.stdin.write(line + "\n")
                    proc.stdin.flush()
            except Exception:
                pass

        # Forward events until module exits
        while proc.poll() is None:
            if consume("up"):
                send("up")
            if consume("down"):
                send("down")
            if consume("select"):
                send("select")
            if consume("select_hold"):
                send("select_hold")

            if consume("back"):
                # Ask module to go back/exit gracefully
                send("back")
                # Give it a moment to comply
                for _ in range(30):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                if proc.poll() is None:
                    proc.terminate()
                break

            time.sleep(0.02)

        # Cleanup
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass

        try:
            rc = proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            rc = -9

        logf.write(f"[launcher] exit_code={rc}\n")
        logf.flush()

    clear_events()
    # NOTE: do NOT clear the OLED here; returning caller will redraw menu immediately.

# =====================================================
# SETTINGS PAGE
# =====================================================

def settings_screen(consume, clear_events) -> None:
    clear_events()
    while True:
        oled_message(
            "SETTINGS",
            [hostname(), f"IP {get_ip()}", f"UP {uptime_short()}"],
            "BACK=modules"
        )
        if consume("back"):
            clear_events()
            return
        time.sleep(0.08)

# =====================================================
# MAIN LOOP
# =====================================================

def main() -> None:
    # Sanity SD check (optional)
    if err := sd_write_check():
        oled_message("SD ERROR", [err[:21], "Check rootfs"], "")
        while True:
            time.sleep(1)

    consume, clear_events, _btn_refs = init_buttons()

    splash(min_seconds=5.0)

    mods = discover_modules(MODULE_DIR)
    if not mods:
        oled_message("NO MODULES", ["No modules found", str(MODULE_DIR)], "BACK=retry")
        while True:
            if consume("back"):
                mods = discover_modules(MODULE_DIR)
                if mods:
                    break
            time.sleep(0.2)

    idx = 0
    draw_menu(mods, idx)

    while True:
        # Basic nav
        if consume("up"):
            idx = (idx - 1) % len(mods)
            draw_menu(mods, idx)

        if consume("down"):
            idx = (idx + 1) % len(mods)
            draw_menu(mods, idx)

        if consume("back"):
            settings_screen(consume, clear_events)
            # ALWAYS redraw after returning
            draw_menu(mods, idx)

        if consume("select"):
            run_module(mods[idx], consume, clear_events)

            # Re-discover in case you added/removed modules while running
            mods = discover_modules(MODULE_DIR) or mods
            idx = max(0, min(idx, len(mods) - 1))

            # CRITICAL: redraw immediately so you never get a blank OLED
            draw_menu(mods, idx)

        time.sleep(0.02)

if __name__ == "__main__":
    main()
