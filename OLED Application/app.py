#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple

from gpiozero import Button

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

BTN_UP = 17
BTN_DOWN = 27
BTN_SELECT = 22
BTN_BACK = 23
SELECT_HOLD_SECONDS = 0.8

OLED_I2C_BUS = 1
OLED_I2C_ADDR = 0x3C
OLED_W = 128
OLED_H = 64

# Layout tuned so bottom line is visible
TOP_Y = 0
DIV_Y = 12
BODY_Y = 14
LINE_H = 10
FOOT_Y = 54

# Globals (recreated by reset_oled)
serial = None
device = None


# =====================================================
# OLED HELPERS
# =====================================================

def reset_oled() -> None:
    """
    Hard-reset the OLED driver object.
    This fixes the common case where a child module leaves the display OFF (black),
    or otherwise changes display state.
    """
    global serial, device
    try:
        serial = i2c(port=OLED_I2C_BUS, address=OLED_I2C_ADDR)
        device = ssd1306(serial, width=OLED_W, height=OLED_H)
        # Clear screen
        device.clear()
        device.show()
    except Exception:
        # If OLED init fails, we don't want the whole UI to crash-loop
        device = None


def oled_message(title: str, lines: List[str], footer: str = "") -> None:
    if device is None:
        return

    with canvas(device) as draw:
        draw.text((0, TOP_Y), title[:21], fill=255)
        draw.line((0, DIV_Y, OLED_W - 1, DIV_Y), fill=255)

        y = BODY_Y
        for s in lines[:4]:
            draw.text((0, y), (s or "")[:21], fill=255)
            y += LINE_H

        if footer:
            draw.line((0, FOOT_Y - 2, OLED_W - 1, FOOT_Y - 2), fill=255)
            draw.text((0, FOOT_Y), footer[:21], fill=255)


def splash(min_seconds: float = 2.0) -> None:
    start = time.time()
    while True:
        elapsed = time.time() - start
        oled_message("GHOST GEEKS", ["BOOTING UI...", "", "READY SOON"], "")
        if elapsed >= min_seconds:
            return
        time.sleep(0.05)


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

        entry_path = d / "run.py"
        if not entry_path.exists():
            continue

        meta_path = d / "module.json"
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
# BUTTONS
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

    return consume, clear_events, (btn_up, btn_down, btn_select, btn_back)


# =====================================================
# MENU RENDER
# =====================================================

def draw_menu(mods: List[Module], idx: int) -> None:
    visible = 4
    start = max(0, min(idx - 1, max(0, len(mods) - visible)))
    window = mods[start:start + visible]

    lines = []
    for i, m in enumerate(window):
        pointer = ">" if (start + i) == idx else " "
        lines.append(f"{pointer} {m.name}"[:21])

    oled_message("MODULES", lines, "SEL=start  BACK=settings")


# =====================================================
# MODULE RUNNER
# =====================================================

def run_module(mod: Module, consume, clear_events) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{mod.id}_{ts}.log"

    oled_message("LAUNCH", [mod.name, mod.subtitle], "BACK=abort")
    time.sleep(0.1)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{OLED_DIR}:{env.get('PYTHONPATH','')}".strip(":")
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
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

        # If it exits instantly, show it clearly (prevents “it just kicked me out” confusion)
        time.sleep(0.20)
        if proc.poll() is not None:
            rc = proc.returncode
            logf.write(f"[launcher] exit_code={rc}\n")
            logf.flush()
            oled_message("MODULE EXIT", [mod.name, f"exit={rc}", "See logs/"], "BACK=menu")
            time.sleep(1.2)
            clear_events()
            return

        def send(line: str) -> None:
            try:
                if proc.poll() is None and proc.stdin:
                    proc.stdin.write(line + "\n")
                    proc.stdin.flush()
            except Exception:
                pass

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
                send("back")
                for _ in range(30):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                if proc.poll() is None:
                    proc.terminate()
                break

            time.sleep(0.02)

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
    # IMPORTANT: do NOT clear OLED here; main loop will redraw menu


# =====================================================
# SETTINGS
# =====================================================

def settings_screen(consume, clear_events) -> None:
    clear_events()
    while True:
        oled_message("SETTINGS", ["(placeholder)", "Add later", ""], "BACK=modules")
        if consume("back"):
            clear_events()
            return
        time.sleep(0.08)


# =====================================================
# MAIN
# =====================================================

def main() -> None:
    reset_oled()
    splash(2.0)

    consume, clear_events, _btn_refs = init_buttons()
    mods = discover_modules(MODULE_DIR)

    if not mods:
        oled_message("NO MODULES", ["No run.py found", str(MODULE_DIR)], "BACK=retry")
        while True:
            if consume("back"):
                mods = discover_modules(MODULE_DIR)
                if mods:
                    break
            time.sleep(0.2)

    idx = 0
    draw_menu(mods, idx)

    while True:
        if consume("up"):
            idx = (idx - 1) % len(mods)
            draw_menu(mods, idx)

        if consume("down"):
            idx = (idx + 1) % len(mods)
            draw_menu(mods, idx)

        if consume("back"):
            settings_screen(consume, clear_events)
            # redraw always
            reset_oled()
            draw_menu(mods, idx)

        if consume("select"):
            run_module(mods[idx], consume, clear_events)

            # CRITICAL FIX for blank screen after module exit:
            reset_oled()

            # Re-discover in case modules changed
            mods = discover_modules(MODULE_DIR) or mods
            idx = max(0, min(idx, len(mods) - 1))

            # redraw immediately
            draw_menu(mods, idx)

        time.sleep(0.02)


if __name__ == "__main__":
    main()
