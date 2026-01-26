#!/usr/bin/env python3
"""
Ghost Geeks - Noise Generator module (continuous stream + correct BACK behavior)

Input (from parent via stdin):
  up, down, select, select_hold, back

Audio:
  Continuous PCM stream to PipeWire via pw-cat (s16le, 48kHz, stereo)
  => no looping WAV chunks, no periodic dropouts.

BACK behavior:
  - BACK from subpages returns to module main menu
  - BACK from module main menu exits module
"""

from __future__ import annotations

import json
import math
import queue
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas

# -----------------------------
# OLED SETUP
# -----------------------------
OLED_ADDR = 0x3C
OLED_W, OLED_H = 128, 64

serial = i2c(port=1, address=OLED_ADDR)
device = ssd1306(serial, width=OLED_W, height=OLED_H)

TOP_H = 12
BOTTOM_H = 10
LINE_H = 10
DIV_Y_TOP = TOP_H
DIV_Y_BOTTOM = OLED_H - BOTTOM_H - 1

def draw_divider(d, y: int):
    d.line((0, y, OLED_W - 1, y), fill=255)

def draw_header(d, title: str):
    d.text((0, 0), title[:16], fill=255)
    draw_divider(d, DIV_Y_TOP)

def draw_footer(d, text: str):
    draw_divider(d, DIV_Y_BOTTOM)
    d.text((0, OLED_H - BOTTOM_H), text[:21], fill=255)

def draw_menu(d, title: str, items: List[str], idx: int, hint: str):
    draw_header(d, title)
    y0 = TOP_H + 1
    visible_lines = (DIV_Y_BOTTOM - y0) // LINE_H
    if visible_lines < 1:
        visible_lines = 1

    start = 0
    if idx >= visible_lines:
        start = idx - visible_lines + 1
    end = min(len(items), start + visible_lines)

    y = y0
    for i in range(start, end):
        prefix = ">" if i == idx else " "
        d.text((0, y), f"{prefix} {items[i]}"[:21], fill=255)
        y += LINE_H

    draw_footer(d, hint)

def draw_big_value(d, title: str, big: str, lines: List[str], hint: str):
    draw_header(d, title)
    d.text((0, TOP_H + 2), big[:18], fill=255)
    y = TOP_H + 2 + 18
    for ln in lines[:3]:
        d.text((0, y), ln[:21], fill=255)
        y += LINE_H
    draw_footer(d, hint)

def oled_render(fn):
    with canvas(device) as d:
        fn(d)

# -----------------------------
# STDIN EVENTS
# -----------------------------
def stdin_event_queue() -> "queue.Queue[str]":
    q: "queue.Queue[str]" = queue.Queue()

    def reader():
        while True:
            line = sys.stdin.readline()
            if not line:
                time.sleep(0.05)
                continue
            line = line.strip()
            if line:
                q.put(line)

    import threading
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    return q

# -----------------------------
# CONFIG / PERSISTENCE
# -----------------------------
CFG_DIR = Path.home() / ".config" / "ghostgeeks"
CFG_PATH = CFG_DIR / "noise_generator.json"

DEFAULTS = {
    "volume": 0.70,      # 0.0 - 1.0
    "noise_type": "white",  # white|pink|brown
    "mode": "steady",    # steady|pulse
    "pulse_on_ms": 250,
    "pulse_off_ms": 250,
}

def load_cfg() -> dict:
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    if not CFG_PATH.exists():
        save_cfg(DEFAULTS.copy())
        return DEFAULTS.copy()
    try:
        data = json.loads(CFG_PATH.read_text())
    except Exception:
        data = {}
    cfg = DEFAULTS.copy()
    for k in cfg:
        if k in data:
            cfg[k] = data[k]

    # normalize
    cfg["volume"] = float(max(0.0, min(1.0, float(cfg["volume"]))))
    if cfg["noise_type"] not in ("white", "pink", "brown"):
        cfg["noise_type"] = "white"
    if cfg["mode"] not in ("steady", "pulse"):
        cfg["mode"] = "steady"
    cfg["pulse_on_ms"] = int(max(50, min(2000, int(cfg["pulse_on_ms"]))))
    cfg["pulse_off_ms"] = int(max(50, min(2000, int(cfg["pulse_off_ms"]))))
    return cfg

def save_cfg(cfg: dict):
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(cfg, indent=2, sort_keys=True))

# -----------------------------
# AUDIO (PipeWire pw-cat)
# -----------------------------
SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 960  # 20ms @ 48kHz

@dataclass
class AudioProc:
    p: subprocess.Popen

def have_pw_cat() -> bool:
    return subprocess.call(["bash", "-lc", "command -v pw-cat >/dev/null 2>&1"]) == 0

def start_audio_stream() -> Optional[AudioProc]:
    if not have_pw_cat():
        return None
    cmd = [
        "pw-cat",
        "--playback",
        "--rate", str(SAMPLE_RATE),
        "--channels", str(CHANNELS),
        "--format", "s16le",
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return AudioProc(p=p)

def stop_audio_stream(ap: Optional[AudioProc]):
    if not ap:
        return
    try:
        if ap.p.stdin:
            ap.p.stdin.close()
    except Exception:
        pass
    try:
        ap.p.terminate()
    except Exception:
        pass
    try:
        ap.p.wait(timeout=0.5)
    except Exception:
        try:
            ap.p.kill()
        except Exception:
            pass

def s16(x: float) -> int:
    if x > 1.0: x = 1.0
    if x < -1.0: x = -1.0
    return int(x * 32767.0)

def write_frames(ap: AudioProc, samples: List[int]) -> bool:
    if ap.p.poll() is not None:
        return False
    try:
        b = bytearray()
        for v in samples:
            b += int(v).to_bytes(2, byteorder="little", signed=True)
        ap.p.stdin.write(b)  # type: ignore
        ap.p.stdin.flush()   # type: ignore
        return True
    except Exception:
        return False

# -----------------------------
# NOISE GENERATORS
# -----------------------------
# White: random uniform
# Pink: simple Voss-McCartney (good enough for this device UI)
# Brown: integrated white with clamp

class PinkNoise:
    def __init__(self, rows: int = 16):
        self.rows = rows
        self.values = [random.uniform(-1.0, 1.0) for _ in range(rows)]
        self.running_sum = sum(self.values)
        self.counter = 0

    def sample(self) -> float:
        # flip a random subset of rows each sample based on trailing zeros of counter
        self.counter += 1
        c = self.counter
        if c == 0:
            c = 1
        # number of trailing zeros -> which rows change
        n = 0
        while (c & 1) == 0:
            n += 1
            c >>= 1
        if n >= self.rows:
            n = self.rows - 1
        # update rows 0..n
        for i in range(n + 1):
            old = self.values[i]
            new = random.uniform(-1.0, 1.0)
            self.values[i] = new
            self.running_sum += (new - old)
        # normalize
        return self.running_sum / self.rows

class BrownNoise:
    def __init__(self):
        self.state = 0.0

    def sample(self) -> float:
        # integrate small white steps
        self.state += random.uniform(-1.0, 1.0) * 0.02
        # clamp
        if self.state > 1.0: self.state = 1.0
        if self.state < -1.0: self.state = -1.0
        return self.state

pink = PinkNoise(rows=16)
brown = BrownNoise()

def gen_noise_block(kind: str, vol: float) -> List[int]:
    out: List[int] = []
    for _ in range(FRAME_SAMPLES):
        if kind == "white":
            v = random.uniform(-1.0, 1.0)
        elif kind == "pink":
            v = pink.sample()
        else:
            v = brown.sample()
        v *= vol
        iv = s16(v)
        out.append(iv)
        out.append(iv)
    return out

def gen_silence_block() -> List[int]:
    return [0] * (FRAME_SAMPLES * CHANNELS)

# -----------------------------
# UI STATE
# -----------------------------
STATE_MAIN = "main"
STATE_PLAY = "play"
STATE_SETTINGS = "settings"

MAIN_ITEMS = [
    "Noise Type",
    "Start / Stop",
    "Mode: Steady/Pulse",
    "Settings",
    "Exit",
]

SETTINGS_ITEMS = [
    "Volume",
    "Pulse on/off",
    "Back",
]

NOISE_TYPES = ["white", "pink", "brown"]
NOISE_LABEL = {"white": "White", "pink": "Pink", "brown": "Brown"}

def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

# -----------------------------
# MAIN
# -----------------------------
def main():
    cfg = load_cfg()
    q = stdin_event_queue()

    ap = start_audio_stream()
    if ap is None:
        oled_render(lambda d: draw_big_value(d, "NOISE GEN", "NO AUDIO", ["Missing pw-cat", "Install pipewire", "or pw-cat"], "BACK = exit"))
        time.sleep(1.2)
        return

    state = STATE_MAIN
    main_idx = 0
    settings_idx = 0

    playing = False
    pulse_is_on = True
    pulse_ms = 0

    last_draw = 0.0

    def stop_playback():
        nonlocal playing, pulse_is_on, pulse_ms
        playing = False
        pulse_is_on = True
        pulse_ms = 0

    def start_playback():
        nonlocal playing, pulse_is_on, pulse_ms
        playing = True
        pulse_is_on = True
        pulse_ms = 0

    def draw():
        nonlocal last_draw
        now = time.monotonic()
        if now - last_draw < 0.05:
            return
        last_draw = now

        def _draw(d):
            nonlocal state, main_idx, settings_idx, playing
            if state == STATE_MAIN:
                # render current selections inline
                mode_label = "Pulse" if cfg["mode"] == "pulse" else "Steady"
                start_label = "STOP" if playing else "START"
                items = [
                    f"Type: {NOISE_LABEL[cfg['noise_type']]}",
                    f"{start_label}",
                    f"Mode: {mode_label}",
                    "Settings",
                    "Exit",
                ]
                draw_menu(d, "NOISE GEN", items, main_idx, "SEL=enter  BACK=exit")

            elif state == STATE_SETTINGS:
                items = [
                    f"Volume: {int(cfg['volume']*100)}%",
                    f"Pulse: {cfg['pulse_on_ms']}/{cfg['pulse_off_ms']}ms",
                    "Back",
                ]
                draw_menu(d, "SETTINGS", items, settings_idx, "UP/DN edit  BACK")

            elif state == STATE_PLAY:
                mode_label = "Pulse" if cfg["mode"] == "pulse" else "Steady"
                big = NOISE_LABEL[cfg["noise_type"]]
                lines = [
                    f"Mode: {mode_label}",
                    f"Vol: {int(cfg['volume']*100)}%",
                    "HOLD=toggle mode",
                ]
                draw_big_value(d, "PLAYING", big, lines, "SEL=stop  BACK=menu")

        oled_render(_draw)

    try:
        while True:
            # ---- Input event ----
            ev = None
            try:
                ev = q.get_nowait()
            except queue.Empty:
                ev = None

            if ev:
                # BACK behavior: only exits module from MAIN screen
                if ev == "back":
                    if state == STATE_MAIN:
                        stop_playback()
                        break
                    else:
                        # return to main menu; do not exit module
                        state = STATE_MAIN
                        stop_playback()
                        main_idx = 0

                elif state == STATE_MAIN:
                    if ev == "up":
                        main_idx = (main_idx - 1) % len(MAIN_ITEMS)
                    elif ev == "down":
                        main_idx = (main_idx + 1) % len(MAIN_ITEMS)
                    elif ev == "select":
                        if main_idx == 0:
                            # noise type cycles
                            cur = NOISE_TYPES.index(cfg["noise_type"])
                            cfg["noise_type"] = NOISE_TYPES[(cur + 1) % len(NOISE_TYPES)]
                            save_cfg(cfg)
                        elif main_idx == 1:
                            # start/stop
                            if playing:
                                stop_playback()
                            else:
                                start_playback()
                                state = STATE_PLAY
                        elif main_idx == 2:
                            # toggle mode
                            cfg["mode"] = "pulse" if cfg["mode"] == "steady" else "steady"
                            save_cfg(cfg)
                        elif main_idx == 3:
                            state = STATE_SETTINGS
                            settings_idx = 0
                        elif main_idx == 4:
                            stop_playback()
                            break

                elif state == STATE_SETTINGS:
                    if ev == "up":
                        if settings_idx == 0:
                            cfg["volume"] = float(clamp(cfg["volume"] + 0.05, 0.0, 1.0))
                        elif settings_idx == 1:
                            cfg["pulse_on_ms"] = int(clamp(cfg["pulse_on_ms"] + 50, 50, 2000))
                        save_cfg(cfg)
                    elif ev == "down":
                        if settings_idx == 0:
                            cfg["volume"] = float(clamp(cfg["volume"] - 0.05, 0.0, 1.0))
                        elif settings_idx == 1:
                            cfg["pulse_on_ms"] = int(clamp(cfg["pulse_on_ms"] - 50, 50, 2000))
                        save_cfg(cfg)
                    elif ev == "select":
                        settings_idx = (settings_idx + 1) % len(SETTINGS_ITEMS)

                elif state == STATE_PLAY:
                    if ev == "select":
                        stop_playback()
                        state = STATE_MAIN
                        main_idx = 0
                    elif ev == "select_hold":
                        # quick toggle mode while playing
                        cfg["mode"] = "pulse" if cfg["mode"] == "steady" else "steady"
                        save_cfg(cfg)
                    elif ev == "up":
                        # quick volume up
                        cfg["volume"] = float(clamp(cfg["volume"] + 0.05, 0.0, 1.0))
                        save_cfg(cfg)
                    elif ev == "down":
                        # quick volume down
                        cfg["volume"] = float(clamp(cfg["volume"] - 0.05, 0.0, 1.0))
                        save_cfg(cfg)

            # ---- Audio tick (20ms) ----
            if playing:
                vol = float(cfg["volume"])
                if cfg["mode"] == "steady":
                    block = gen_noise_block(cfg["noise_type"], vol)
                    if not write_frames(ap, block):
                        break
                else:
                    # pulse/chop
                    on_ms = int(cfg["pulse_on_ms"])
                    off_ms = int(cfg["pulse_off_ms"])
                    pulse_ms += 20

                    if pulse_is_on:
                        block = gen_noise_block(cfg["noise_type"], vol)
                        if pulse_ms >= on_ms:
                            pulse_is_on = False
                            pulse_ms = 0
                    else:
                        block = gen_silence_block()
                        if pulse_ms >= off_ms:
                            pulse_is_on = True
                            pulse_ms = 0

                    if not write_frames(ap, block):
                        break
            else:
                # keep pipe alive quietly
                if not write_frames(ap, gen_silence_block()):
                    break
                time.sleep(0.02)

            draw()

    finally:
        stop_audio_stream(ap)
        # clear display so parent redraw is obvious
        oled_render(lambda d: d.rectangle((0, 0, OLED_W - 1, OLED_H - 1), outline=0, fill=0))

if __name__ == "__main__":
    main()
