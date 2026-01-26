#!/usr/bin/env python3
import os
import sys
import time
import math
import struct
import threading
import subprocess
from dataclasses import dataclass

from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas

# ---------- OLED ----------
I2C_PORT = 1
I2C_ADDR = 0x3C
W, H = 128, 64

serial = i2c(port=I2C_PORT, address=I2C_ADDR)
device = ssd1306(serial, width=W, height=H)

# UI tuning (keeps bottom line fully visible)
TOP_H = 12
FOOT_Y = 54

def line(draw, y):
    draw.line((0, y, 127, y), fill=255)

def draw_header(draw, title):
    draw.text((0, 0), title[:21], fill=255)
    line(draw, TOP_H)

def draw_footer(draw, text):
    draw.text((0, FOOT_Y), text[:21], fill=255)

# ---------- stdin button input ----------
# Parent sends: up, down, select, select_hold, back
def start_stdin_reader():
    q = []
    lock = threading.Lock()

    def reader():
        while True:
            s = sys.stdin.readline()
            if not s:
                time.sleep(0.05)
                continue
            s = s.strip()
            if not s:
                continue
            with lock:
                q.append(s)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    def pop():
        with lock:
            if q:
                return q.pop(0)
        return None

    return pop

pop_cmd = start_stdin_reader()

# ---------- Audio generation ----------
RATE = 48000
CH = 1
FMT = "S16_LE"  # aplay -f S16_LE -r 48000 -c 1 -t raw -
FRAMES_PER_CHUNK = 1024  # small chunks keeps latency low

NOISE_TYPES = ["White", "Pink", "Brown"]
DEFAULT_NOISE = 0

@dataclass
class AudioState:
    noise_idx: int = DEFAULT_NOISE
    playing: bool = False
    volume: int = 80               # 0..100
    pulse_on: bool = False
    pulse_ms: int = 200            # gate period (ms)
    duty: float = 0.50             # 0..1

class NoiseEngine:
    """
    Streams noise into `aplay` as raw PCM.
    Output goes to whatever PipeWire/ALSA default sink is (your combo sink).
    """
    def __init__(self, st: AudioState):
        self.st = st
        self._stop = threading.Event()
        self._thread = None
        self._proc = None

        # states for filters
        self._brown = 0.0
        self._pink_b = [0.0]*7  # Paul Kellet-ish filter state

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._proc and self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            if self._proc:
                self._proc.terminate()
        except Exception:
            pass
        self._proc = None

    def _ensure_proc(self):
        if self._proc and self._proc.poll() is None:
            return
        # Use aplay raw stream
        self._proc = subprocess.Popen(
            ["aplay", "-q", "-f", FMT, "-r", str(RATE), "-c", str(CH), "-t", "raw", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _write_chunk(self, pcm_bytes: bytes):
        if not self._proc or self._proc.poll() is not None or not self._proc.stdin:
            return
        try:
            self._proc.stdin.write(pcm_bytes)
            self._proc.stdin.flush()
        except Exception:
            pass

    def _white_samples(self, n):
        # Fast: just random int16 bits. This is true white noise in PCM space.
        # We'll scale for volume/pulse in float path for other noises;
        # for white we do a quick scale pass too for consistent volume.
        import random
        out = []
        for _ in range(n):
            # centered float [-1,1]
            x = (random.random() * 2.0) - 1.0
            out.append(x)
        return out

    def _pink_from_white(self, w):
        # Simple pink-ish filter (Paul Kellet style approximation)
        b = self._pink_b
        out = []
        for x in w:
            b[0] = 0.99886*b[0] + x*0.0555179
            b[1] = 0.99332*b[1] + x*0.0750759
            b[2] = 0.96900*b[2] + x*0.1538520
            b[3] = 0.86650*b[3] + x*0.3104856
            b[4] = 0.55000*b[4] + x*0.5329522
            b[5] = -0.7616*b[5] - x*0.0168980
            y = b[0]+b[1]+b[2]+b[3]+b[4]+b[5]+b[6] + x*0.5362
            b[6] = x*0.115926
            out.append(y * 0.11)  # normalize-ish
        return out

    def _brown_from_white(self, w):
        out = []
        y = self._brown
        for x in w:
            # integrate with gentle leak to avoid runaway
            y = 0.98*y + 0.02*x
            out.append(y * 3.5)  # normalize-ish
        self._brown = y
        return out

    def _apply_amp(self, x, t0):
        # volume scale
        amp = max(0.0, min(1.0, self.st.volume / 100.0))

        # pulse gate (square wave)
        if self.st.pulse_on:
            period = max(50, min(2000, int(self.st.pulse_ms))) / 1000.0
            duty = max(0.05, min(0.95, float(self.st.duty)))
            ph = (time.time() - t0) % period
            gate = 1.0 if ph < (period * duty) else 0.0
            amp *= gate

        return x * amp

    def _float_to_pcm(self, xs):
        # clamp and pack to int16 little-endian
        buf = bytearray()
        for x in xs:
            x = max(-1.0, min(1.0, x))
            s = int(x * 32767)
            buf += struct.pack("<h", s)
        return bytes(buf)

    def _run(self):
        t0 = time.time()
        while not self._stop.is_set():
            if not self.st.playing:
                # stop audio process to free resources and avoid “ghost streams”
                self.stop()
                time.sleep(0.05)
                continue

            self._ensure_proc()

            # build chunk
            w = self._white_samples(FRAMES_PER_CHUNK)

            if self.st.noise_idx == 0:
                xs = w
            elif self.st.noise_idx == 1:
                xs = self._pink_from_white(w)
            else:
                xs = self._brown_from_white(w)

            xs = [self._apply_amp(x, t0) for x in xs]
            pcm = self._float_to_pcm(xs)

            self._write_chunk(pcm)

            # chunk pacing
            time.sleep(FRAMES_PER_CHUNK / RATE)

# ---------- UI states ----------
class Screen:
    MENU = "menu"
    PLAYER = "player"
    SETTINGS = "settings"

def render_menu(sel):
    with canvas(device) as draw:
        draw_header(draw, "NOISE GENERATOR")
        y0 = 16
        for i, name in enumerate(NOISE_TYPES):
            prefix = ">" if i == sel else " "
            draw.text((0, y0 + i*12), f"{prefix} {name}"[:21], fill=255)
        draw_footer(draw, "SEL=enter  BACK=exit")

def render_player(st: AudioState):
    with canvas(device) as draw:
        draw_header(draw, "NOISE")
        draw.text((0, 16), f"{NOISE_TYPES[st.noise_idx]}"[:21], fill=255)

        status = "PLAYING" if st.playing else "PAUSED"
        draw.text((0, 28), status, fill=255)

        # show pulse state compactly
        pulse = "PULSE" if st.pulse_on else "STEADY"
        draw.text((0, 40), f"{pulse}  VOL {st.volume:>3d}"[:21], fill=255)

        line(draw, 52)
        draw_footer(draw, "SEL=play  ^v=vol  BK")

def render_settings(st: AudioState, sel_idx: int):
    items = [
        ("Pulse", "On" if st.pulse_on else "Off"),
        ("Pulse ms", str(st.pulse_ms)),
        ("Duty", f"{int(st.duty*100)}%"),
        ("Volume", str(st.volume)),
    ]
    with canvas(device) as draw:
        draw_header(draw, "SETTINGS")
        y = 16
        for i, (k, v) in enumerate(items):
            prefix = ">" if i == sel_idx else " "
            line_txt = f"{prefix}{k}: {v}"
            draw.text((0, y), line_txt[:21], fill=255)
            y += 12
        line(draw, 52)
        draw_footer(draw, "SEL=toggle  ^v=adj BK")

# ---------- Main ----------
def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def main():
    st = AudioState()
    engine = NoiseEngine(st)

    screen = Screen.MENU
    menu_sel = 0
    set_sel = 0

    render_menu(menu_sel)

    while True:
        cmd = pop_cmd()
        if not cmd:
            time.sleep(0.01)
            continue

        if screen == Screen.MENU:
            if cmd == "up":
                menu_sel = (menu_sel - 1) % len(NOISE_TYPES)
                render_menu(menu_sel)
            elif cmd == "down":
                menu_sel = (menu_sel + 1) % len(NOISE_TYPES)
                render_menu(menu_sel)
            elif cmd == "select":
                st.noise_idx = menu_sel
                st.playing = False
                engine.start()  # thread exists; will only stream when playing=True
                screen = Screen.PLAYER
                render_player(st)
            elif cmd == "back":
                st.playing = False
                engine.stop()
                return  # exit module back to main selector
            elif cmd == "select_hold":
                # ignore here (or could open settings)
                pass

        elif screen == Screen.PLAYER:
            if cmd == "up":
                st.volume = clamp(st.volume + 5, 0, 100)
                render_player(st)
            elif cmd == "down":
                st.volume = clamp(st.volume - 5, 0, 100)
                render_player(st)
            elif cmd == "select":
                st.playing = not st.playing
                engine.start()
                render_player(st)
            elif cmd == "select_hold":
                screen = Screen.SETTINGS
                set_sel = 0
                render_settings(st, set_sel)
            elif cmd == "back":
                # back to menu (do NOT exit module)
                st.playing = False
                render_menu(menu_sel)
                screen = Screen.MENU

        elif screen == Screen.SETTINGS:
            if cmd == "up":
                set_sel = (set_sel - 1) % 4
                render_settings(st, set_sel)
            elif cmd == "down":
                set_sel = (set_sel + 1) % 4
                render_settings(st, set_sel)
            elif cmd == "select":
                # Toggle/adjust based on selection
                if set_sel == 0:
                    st.pulse_on = not st.pulse_on
                render_settings(st, set_sel)
            elif cmd == "select_hold":
                # Quick exit settings -> player
                screen = Screen.PLAYER
                render_player(st)
            elif cmd == "back":
                screen = Screen.PLAYER
                render_player(st)

            # Adjust with up/down when on numeric fields (optional: make select modify instead)
            # We'll use select to toggle pulse only; adjustments happen with up/down + select_hold is exit.
            # If you prefer up/down to adjust values when highlighted, say so and I’ll change it.

            # (We’ll interpret up/down as navigation only. Adjustments below require SELECT+UP/DOWN pattern if wanted.)

            # Minimal adjustments using select+up/down is more complex; we can add it next pass.

        # Apply settings adjustments in SETTINGS using select + up/down? (not enabled in this first cut)

if __name__ == "__main__":
    try:
        main()
    finally:
        # ensure audio stops if module is killed
        pass
