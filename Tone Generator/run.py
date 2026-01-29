#!/usr/bin/env python3
import sys
import time
import math
import struct
import subprocess
import threading
import select
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

HEADER_Y = 0
DIVIDER_Y = 12
LIST_Y0 = 14
ROW_H = 12
FOOTER_LINE_Y = 52
FOOTER_Y = 54


def render(draw_fn):
    with canvas(device) as d:
        draw_fn(d)


def draw_header(d, title: str):
    d.text((2, HEADER_Y), title[:21], fill=255)
    d.line((0, DIVIDER_Y, 127, DIVIDER_Y), fill=255)


def draw_footer(d, text: str):
    d.line((0, FOOTER_LINE_Y, 127, FOOTER_LINE_Y), fill=255)
    d.text((2, FOOTER_Y), text[:21], fill=255)


def draw_row(d, y: int, text: str, selected: bool):
    marker = ">" if selected else " "
    d.text((0, y), marker, fill=255)
    d.text((10, y), text[:19], fill=255)


def read_event():
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


# ============================================================
# AUDIO ENGINE — continuous sine streaming
# ============================================================
RATE = 48000
CH = 2
FRAMES_PER_CHUNK = 960  # 20ms
TWOPI = 2.0 * math.pi


@dataclass
class ToneState:
    mode: str = "STEADY"    # STEADY / PULSE / SWEEP
    playing: bool = False
    volume: int = 70        # 0..100
    hz: float = 432.0

    pulse_on_ms: int = 250
    pulse_off_ms: int = 250

    sweep_start: float = 100.0
    sweep_end: float = 1200.0
    sweep_step: float = 5.0
    sweep_step_ms: int = 100


class ToneEngine:
    def __init__(self, st: ToneState):
        self.st = st
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self._phase = 0.0

        self._pulse_acc = 0
        self._pulse_on = True
        self._sweep_acc = 0
        self._sweep_hz = st.sweep_start
        self._sweep_dir = 1

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        self._close_proc()

    def _pick_player(self):
        # Prefer PipeWire
        if subprocess.call(["bash", "-lc", "command -v pw-cat >/dev/null 2>&1"]) == 0:
            return ["pw-cat", "--playback", "--rate", str(RATE), "--channels", str(CH), "--format", "s16le"]
        return ["aplay", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", str(CH), "-t", "raw", "-"]

    def _ensure_proc(self):
        if self._proc and self._proc.poll() is None:
            return
        cmd = self._pick_player()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _close_proc(self):
        try:
            if self._proc and self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass
        self._proc = None

    def _write(self, buf: bytes):
        try:
            if self._proc and self._proc.stdin:
                self._proc.stdin.write(buf)
                self._proc.stdin.flush()
        except Exception:
            pass

    def _gen_sine(self, hz: float, amp: float) -> bytes:
        inc = TWOPI * hz / RATE
        ph = self._phase

        out = bytearray()
        for _ in range(FRAMES_PER_CHUNK):
            v = math.sin(ph) * amp
            s = int(max(-1.0, min(1.0, v)) * 32767)
            # stereo interleaved
            out += struct.pack("<h", s)
            out += struct.pack("<h", s)

            ph += inc
            if ph > TWOPI:
                ph -= TWOPI

        self._phase = ph
        return bytes(out)

    def _gen_silence(self) -> bytes:
        return b"\x00\x00" * (FRAMES_PER_CHUNK * CH)

    def _run(self):
        tick_ms = 20
        while not self._stop.is_set():
            if not self.st.playing:
                self._close_proc()
                time.sleep(0.03)
                continue

            self._ensure_proc()

            amp = max(0.0, min(1.0, self.st.volume / 100.0))

            # Determine hz by mode
            hz = self.st.hz

            if self.st.mode == "PULSE":
                self._pulse_acc += tick_ms
                if self._pulse_on:
                    buf = self._gen_sine(hz, amp)
                    if self._pulse_acc >= self.st.pulse_on_ms:
                        self._pulse_acc = 0
                        self._pulse_on = False
                else:
                    buf = self._gen_silence()
                    if self._pulse_acc >= self.st.pulse_off_ms:
                        self._pulse_acc = 0
                        self._pulse_on = True

            elif self.st.mode == "SWEEP":
                # generate at current sweep hz
                buf = self._gen_sine(self._sweep_hz, amp)

                self._sweep_acc += tick_ms
                if self._sweep_acc >= self.st.sweep_step_ms:
                    self._sweep_acc = 0
                    self._sweep_hz += self.st.sweep_step * self._sweep_dir
                    if self._sweep_hz >= self.st.sweep_end:
                        self._sweep_hz = self.st.sweep_end
                        self._sweep_dir = -1
                    elif self._sweep_hz <= self.st.sweep_start:
                        self._sweep_hz = self.st.sweep_start
                        self._sweep_dir = 1

            else:  # STEADY
                buf = self._gen_sine(hz, amp)

            self._write(buf)
            time.sleep(FRAMES_PER_CHUNK / RATE)


# ============================================================
# UI SCREENS
# ============================================================
MAIN_ITEMS = ["Steady", "Pulse", "Sweep"]


def menu_screen(sel):
    def _draw(d):
        draw_header(d, "TONE GEN")
        for i, it in enumerate(MAIN_ITEMS):
            draw_row(d, LIST_Y0 + i * ROW_H, it, selected=(i == sel))
        draw_footer(d, "SEL enter  BACK exit")
    return _draw


def play_screen(st: ToneState):
    def _draw(d):
        draw_header(d, "TONE")
        d.text((2, LIST_Y0), f"Mode: {st.mode}"[:21], fill=255)
        d.text((2, LIST_Y0 + 12), f"{st.hz:.1f} Hz"[:21], fill=255)
        d.text((2, LIST_Y0 + 24), f"VOL {st.volume}%"[:21], fill=255)
        status = "PLAYING" if st.playing else "STOPPED"
        d.text((2, LIST_Y0 + 36), status[:21], fill=255)
        draw_footer(d, "SEL toggle  HOLD cfg  BK")
    return _draw


def settings_screen(st: ToneState, sel_idx: int):
    items = [
        ("Hz", f"{st.hz:.1f}"),
        ("Vol", f"{st.volume}%"),
        ("Pulse on", f"{st.pulse_on_ms}ms"),
        ("Pulse off", f"{st.pulse_off_ms}ms"),
        ("Sweep step", f"{st.sweep_step:.1f}"),
        ("Sweep ms", f"{st.sweep_step_ms}ms"),
    ]

    def _draw(d):
        draw_header(d, "SETTINGS")
        # show first 4 cleanly; scroll if needed
        start = 0
        visible = 3
        if sel_idx >= visible:
            start = sel_idx - (visible - 1)
        for r in range(visible):
            i = start + r
            if i >= len(items):
                break
            k, v = items[i]
            draw_row(d, LIST_Y0 + r * ROW_H, f"{k}: {v}", selected=(i == sel_idx))
        draw_footer(d, "^v adj  SEL mode  BK")
    return _draw, len(items)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ============================================================
# MAIN (SpiritBox model)
# ============================================================
def main():
    st = ToneState()
    engine = ToneEngine(st)
    engine.start()  # thread idle unless playing

    STATE_MENU = "MENU"
    STATE_PLAY = "PLAY"
    STATE_CFG = "CFG"

    state = STATE_MENU
    menu_sel = 0
    cfg_sel = 0

    while True:
        if state == STATE_MENU:
            render(menu_screen(menu_sel))
        elif state == STATE_PLAY:
            render(play_screen(st))
        elif state == STATE_CFG:
            draw_fn, n = settings_screen(st, cfg_sel)
            render(draw_fn)

        ev = read_event()
        if ev is None:
            time.sleep(0.02)
            continue

        # -------- MENU --------
        if state == STATE_MENU:
            if ev == "up":
                menu_sel = (menu_sel - 1) % len(MAIN_ITEMS)
            elif ev == "down":
                menu_sel = (menu_sel + 1) % len(MAIN_ITEMS)
            elif ev == "select":
                st.mode = MAIN_ITEMS[menu_sel].upper()
                st.playing = False
                state = STATE_PLAY
            elif ev == "back":
                break  # EXIT MODULE only from top menu

        # -------- PLAY --------
        elif state == STATE_PLAY:
            if ev == "select":
                st.playing = not st.playing
            elif ev == "select_hold":
                cfg_sel = 0
                state = STATE_CFG
            elif ev == "back":
                st.playing = False
                state = STATE_MENU
            elif ev == "up":
                st.volume = clamp(st.volume + 5, 0, 100)
            elif ev == "down":
                st.volume = clamp(st.volume - 5, 0, 100)

        # -------- CFG --------
        elif state == STATE_CFG:
            draw_fn, n = settings_screen(st, cfg_sel)

            if ev == "back":
                state = STATE_PLAY
            elif ev == "select":
                # cycle mode quickly
                if st.mode == "STEADY":
                    st.mode = "PULSE"
                elif st.mode == "PULSE":
                    st.mode = "SWEEP"
                else:
                    st.mode = "STEADY"
            elif ev == "up":
                if cfg_sel == 0: st.hz = clamp(st.hz + 1.0, 1.0, 20000.0)
                if cfg_sel == 1: st.volume = clamp(st.volume + 5, 0, 100)
                if cfg_sel == 2: st.pulse_on_ms = clamp(st.pulse_on_ms + 50, 50, 2000)
                if cfg_sel == 3: st.pulse_off_ms = clamp(st.pulse_off_ms + 50, 50, 2000)
                if cfg_sel == 4: st.sweep_step = clamp(st.sweep_step + 1.0, 0.1, 2000.0)
                if cfg_sel == 5: st.sweep_step_ms = clamp(st.sweep_step_ms + 50, 50, 2000)
            elif ev == "down":
                if cfg_sel == 0: st.hz = clamp(st.hz - 1.0, 1.0, 20000.0)
                if cfg_sel == 1: st.volume = clamp(st.volume - 5, 0, 100)
                if cfg_sel == 2: st.pulse_on_ms = clamp(st.pulse_on_ms - 50, 50, 2000)
                if cfg_sel == 3: st.pulse_off_ms = clamp(st.pulse_off_ms - 50, 50, 2000)
                if cfg_sel == 4: st.sweep_step = clamp(st.sweep_step - 1.0, 0.1, 2000.0)
                if cfg_sel == 5: st.sweep_step_ms = clamp(st.sweep_step_ms - 50, 50, 2000)

            # navigate fields with select_hold (nice on OLED)
            if ev == "select_hold":
                cfg_sel = (cfg_sel + 1) % n

        time.sleep(0.02)

    st.playing = False
    engine.shutdown()
    render(lambda d: d.rectangle((0, 0, OLED_W - 1, OLED_H - 1), outline=0, fill=0))
    time.sleep(0.15)


if __name__ == "__main__":
    main()
