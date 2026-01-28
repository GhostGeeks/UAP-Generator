#!/usr/bin/env python3
import sys
import time
import math
import json
import select
import struct
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path

from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas

# ============================================================
# OLED CONFIG
# ============================================================
I2C_PORT = 1
I2C_ADDR = 0x3C
OLED_W, OLED_H = 128, 64

serial = i2c(port=I2C_PORT, address=I2C_ADDR)
device = ssd1306(serial, width=OLED_W, height=OLED_H)

# Layout tuned to keep footer fully visible
HEADER_Y = 0
DIVIDER_Y = 12
LIST_Y0 = 14
ROW_H = 10
FOOTER_LINE_Y = 52
FOOTER_Y = 54

# ============================================================
# INPUT (stdin from parent app.py) — SpiritBox-style
# ============================================================
def read_event():
    """
    Non-blocking read of one token from stdin.
    Expected: up, down, select, select_hold, back
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
    return line.strip().lower()


def drain_stdin(duration_s: float = 0.15) -> None:
    """Drain any pending stdin tokens (helps prevent 'startup back' issues)."""
    end = time.monotonic() + duration_s
    while time.monotonic() < end:
        ev = read_event()
        if ev is None:
            time.sleep(0.01)


# ============================================================
# SIMPLE UI HELPERS
# ============================================================
def draw_header(d, title: str):
    d.text((2, HEADER_Y), title[:21], fill=255)
    d.line((0, DIVIDER_Y, 127, DIVIDER_Y), fill=255)


def draw_footer(d, text: str):
    d.line((0, FOOTER_LINE_Y, 127, FOOTER_LINE_Y), fill=255)
    d.text((2, FOOTER_Y), text[:21], fill=255)


def draw_row(d, y: int, text: str, selected: bool = False):
    marker = ">" if selected else " "
    d.text((0, y), marker, fill=255)
    d.text((10, y), text[:19], fill=255)


def render(draw_fn):
    with canvas(device) as d:
        draw_fn(d)


# ============================================================
# AUDIO STREAM (pw-cat preferred, aplay fallback)
# ============================================================
RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 960  # 20ms @ 48kHz
FMT = "s16le"

def have_pw_cat() -> bool:
    return shutil.which("pw-cat") is not None

def have_aplay() -> bool:
    return shutil.which("aplay") is not None


class AudioStream:
    def __init__(self):
        self.proc = None

    def start(self):
        if self.proc and self.proc.poll() is None:
            return

        if have_pw_cat():
            cmd = ["pw-cat", "--playback", "--rate", str(RATE), "--channels", str(CHANNELS), "--format", FMT]
        elif have_aplay():
            # aplay expects "S16_LE" string
            cmd = ["aplay", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", str(CHANNELS), "-t", "raw", "-"]
        else:
            self.proc = None
            return

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self):
        p = self.proc
        self.proc = None
        if not p:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=0.5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def write(self, pcm_bytes: bytes) -> bool:
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            return False
        try:
            self.proc.stdin.write(pcm_bytes)
            self.proc.stdin.flush()
            return True
        except Exception:
            return False


# ============================================================
# NOISE ENGINE (single-threaded)
# ============================================================
NOISE_TYPES = ["White", "Pink", "Brown"]

@dataclass
class NoiseState:
    noise_idx: int = 0
    playing: bool = False
    volume: int = 80          # 0..100

    pulse_on: bool = False
    pulse_ms: int = 200       # period ms
    duty: int = 50            # percent 5..95


class NoiseSynth:
    """
    Generate continuous noise frames with simple pink/brown approximations.
    Keeps filter states across ticks so the stream is continuous (no loop artifacts).
    """
    def __init__(self):
        self._brown = 0.0
        self._pink_b = [0.0] * 7
        self._rand_seed = 0x12345678

    def _rand01(self) -> float:
        # fast LCG PRNG (deterministic, avoids importing random in tight loop)
        self._rand_seed = (1103515245 * self._rand_seed + 12345) & 0x7FFFFFFF
        return self._rand_seed / 0x7FFFFFFF

    def _white(self) -> float:
        return (self._rand01() * 2.0) - 1.0

    def _pink_from_white(self, x: float) -> float:
        # Paul Kellet-ish approximation; stable and cheap
        b = self._pink_b
        b[0] = 0.99886*b[0] + x*0.0555179
        b[1] = 0.99332*b[1] + x*0.0750759
        b[2] = 0.96900*b[2] + x*0.1538520
        b[3] = 0.86650*b[3] + x*0.3104856
        b[4] = 0.55000*b[4] + x*0.5329522
        b[5] = -0.7616*b[5] - x*0.0168980
        y = b[0] + b[1] + b[2] + b[3] + b[4] + b[5] + b[6] + x*0.5362
        b[6] = x*0.115926
        return y * 0.11

    def _brown_from_white(self, x: float) -> float:
        # leaky integrator to avoid runaway
        self._brown = 0.98*self._brown + 0.02*x
        return self._brown * 3.5

    @staticmethod
    def _clamp1(x: float) -> float:
        if x > 1.0: return 1.0
        if x < -1.0: return -1.0
        return x

    def render_block(self, st: NoiseState, t0: float) -> bytes:
        vol = max(0.0, min(1.0, st.volume / 100.0))

        # pulse gate
        gate = 1.0
        if st.pulse_on:
            period = max(50, min(2000, int(st.pulse_ms))) / 1000.0
            duty = max(5, min(95, int(st.duty))) / 100.0
            ph = (time.monotonic() - t0) % period
            gate = 1.0 if ph < (period * duty) else 0.0

        amp = vol * gate

        # generate interleaved stereo int16
        out = bytearray()
        for _ in range(FRAME_SAMPLES):
            w = self._white()

            if st.noise_idx == 0:
                x = w
            elif st.noise_idx == 1:
                x = self._pink_from_white(w)
            else:
                x = self._brown_from_white(w)

            x = self._clamp1(x * amp)
            s = int(x * 32767)

            # stereo L/R same
            out += struct.pack("<h", s)
            out += struct.pack("<h", s)

        return bytes(out)


# ============================================================
# UI SCREENS
# ============================================================
SCREEN_MENU = "MENU"
SCREEN_PLAYER = "PLAYER"
SCREEN_SETTINGS = "SETTINGS"

def draw_menu(sel_idx: int):
    def _d(d):
        draw_header(d, "NOISE GEN")
        for i, name in enumerate(NOISE_TYPES):
            draw_row(d, LIST_Y0 + i * ROW_H, name, selected=(i == sel_idx))
        draw_footer(d, "SEL enter  BACK exit")
    return _d


def draw_player(st: NoiseState):
    def _d(d):
        draw_header(d, "NOISE")
        draw_row(d, LIST_Y0 + 0 * ROW_H, f"Type: {NOISE_TYPES[st.noise_idx]}", selected=False)
        status = "PLAYING" if st.playing else "STOPPED"
        draw_row(d, LIST_Y0 + 1 * ROW_H, f"State: {status}", selected=False)
        mode = "PULSE" if st.pulse_on else "STEADY"
        draw_row(d, LIST_Y0 + 2 * ROW_H, f"{mode}  Vol {st.volume}%", selected=False)
        draw_footer(d, "SEL toggle  HOLD set")
    return _d


def settings_items(st: NoiseState):
    return [
        f"Pulse: {'On' if st.pulse_on else 'Off'}",
        f"Pulse ms: {st.pulse_ms}",
        f"Duty: {st.duty}%",
        f"Volume: {st.volume}%",
    ]


def draw_settings(st: NoiseState, sel_idx: int):
    items = settings_items(st)

    def _d(d):
        draw_header(d, "SETTINGS")
        # 4 lines fits perfectly with ROW_H=10 + footer lift
        for i, txt in enumerate(items):
            draw_row(d, LIST_Y0 + i * ROW_H, txt, selected=(i == sel_idx))
        draw_footer(d, "UP/DN nav  SEL edit")
    return _d


# ============================================================
# MAIN
# ============================================================
def clamp_int(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def main():
    boot_at = time.monotonic()
    drain_stdin(0.10)  # clear any stale tokens that could have been queued

    st = NoiseState()
    synth = NoiseSynth()
    audio = AudioStream()
    t0 = time.monotonic()

    screen = SCREEN_MENU
    menu_sel = 0
    set_sel = 0

    render(draw_menu(menu_sel))

    try:
        while True:
            ev = read_event()

            # Startup dead-zone to ignore stray launcher noise
            if ev and (time.monotonic() - boot_at) < 0.35:
                if ev in ("back", "select"):
                    ev = None

            # ------------------------------------------------
            # Handle UI events
            # ------------------------------------------------
            if ev:
                if screen == SCREEN_MENU:
                    if ev == "up":
                        menu_sel = (menu_sel - 1) % len(NOISE_TYPES)
                        render(draw_menu(menu_sel))
                    elif ev == "down":
                        menu_sel = (menu_sel + 1) % len(NOISE_TYPES)
                        render(draw_menu(menu_sel))
                    elif ev == "select":
                        st.noise_idx = menu_sel
                        st.playing = False
                        screen = SCREEN_PLAYER
                        render(draw_player(st))
                    elif ev == "back":
                        # exit module
                        st.playing = False
                        break

                elif screen == SCREEN_PLAYER:
                    if ev == "select":
                        st.playing = not st.playing
                        render(draw_player(st))
                    elif ev == "select_hold":
                        screen = SCREEN_SETTINGS
                        set_sel = 0
                        render(draw_settings(st, set_sel))
                    elif ev == "up":
                        st.volume = clamp_int(st.volume + 5, 0, 100)
                        render(draw_player(st))
                    elif ev == "down":
                        st.volume = clamp_int(st.volume - 5, 0, 100)
                        render(draw_player(st))
                    elif ev == "back":
                        # back to menu (NOT exit)
                        st.playing = False
                        screen = SCREEN_MENU
                        render(draw_menu(menu_sel))

                elif screen == SCREEN_SETTINGS:
                    if ev == "up":
                        set_sel = (set_sel - 1) % 4
                        render(draw_settings(st, set_sel))
                    elif ev == "down":
                        set_sel = (set_sel + 1) % 4
                        render(draw_settings(st, set_sel))
                    elif ev == "select":
                        # edit selected item
                        if set_sel == 0:
                            st.pulse_on = not st.pulse_on
                        elif set_sel == 1:
                            st.pulse_ms = clamp_int(st.pulse_ms + 50, 50, 2000)
                        elif set_sel == 2:
                            st.duty = clamp_int(st.duty + 5, 5, 95)
                        elif set_sel == 3:
                            st.volume = clamp_int(st.volume + 5, 0, 100)
                        render(draw_settings(st, set_sel))
                    elif ev == "select_hold":
                        # quick exit settings -> player
                        screen = SCREEN_PLAYER
                        render(draw_player(st))
                    elif ev == "back":
                        screen = SCREEN_PLAYER
                        render(draw_player(st))

            # ------------------------------------------------
            # Audio tick (continuous stream)
            # ------------------------------------------------
            if st.playing:
                audio.start()
                if audio.proc is None:
                    # no audio backend available
                    st.playing = False
                    render(draw_player(st))
                else:
                    pcm = synth.render_block(st, t0)
                    ok = audio.write(pcm)
                    if not ok:
                        st.playing = False
                        audio.stop()
                        render(draw_player(st))
            else:
                # If stopped, keep audio off (best resource behavior)
                if audio.proc is not None:
                    audio.stop()

            time.sleep(0.02)

    finally:
        st.playing = False
        audio.stop()
        # brief "returning" is okay, but don't clear screen (parent redraws)
        render(lambda d: (draw_header(d, "NOISE GEN"), draw_footer(d, "Returning...")))
        time.sleep(0.10)


if __name__ == "__main__":
    main()
