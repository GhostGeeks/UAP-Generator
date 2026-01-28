#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
import time
import json
import select
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional

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
ROW_H = 10
FOOTER_LINE_Y = 52
FOOTER_Y = 54

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

def draw_centered(d, y: int, text: str):
    x = max(0, (OLED_W - (len(text) * 6)) // 2)
    d.text((x, y), text[:21], fill=255)

def render(draw_fn):
    with canvas(device) as d:
        draw_fn(d)

# ============================================================
# INPUT (stdin) — SpiritBox-style
# ============================================================
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

def drain_stdin(duration_s: float = 0.15) -> None:
    end = time.monotonic() + duration_s
    while time.monotonic() < end:
        ev = read_event()
        if ev is None:
            time.sleep(0.01)

# ============================================================
# CONFIG / PERSISTENCE
# ============================================================
CFG_DIR = Path.home() / ".config" / "ghostgeeks"
CFG_PATH = CFG_DIR / "tone_generator.json"

DEFAULTS = {
    "volume": 0.65,          # 0.0 - 1.0
    "steady_hz": 432.0,
    "pulse_hz": 528.0,
    "pulse_on_ms": 250,
    "pulse_off_ms": 250,
    "sweep_start_hz": 100.0,
    "sweep_end_hz": 1200.0,
    "sweep_step_hz": 5.0,
    "sweep_step_ms": 100,
    "favorites": [432.0, 528.0],
}

def load_cfg() -> dict:
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    if not CFG_PATH.exists():
        CFG_PATH.write_text(json.dumps(DEFAULTS, indent=2, sort_keys=True))
        return DEFAULTS.copy()
    try:
        data = json.loads(CFG_PATH.read_text())
    except Exception:
        data = {}
    merged = DEFAULTS.copy()
    for k in merged.keys():
        if k in data:
            merged[k] = data[k]
    # normalize
    merged["volume"] = float(max(0.0, min(1.0, float(merged["volume"]))))
    merged["favorites"] = list(dict.fromkeys([float(x) for x in merged.get("favorites", [])]))[:30]
    return merged

def save_cfg(cfg: dict) -> None:
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(cfg, indent=2, sort_keys=True))

# ============================================================
# AUDIO (pw-cat preferred; aplay fallback)
# ============================================================
SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 960   # 20ms
TWOPI = 2.0 * math.pi

def have_pw_cat() -> bool:
    return shutil.which("pw-cat") is not None

def have_aplay() -> bool:
    return shutil.which("aplay") is not None

@dataclass
class AudioProc:
    p: subprocess.Popen
    phase: float = 0.0

def start_audio_stream() -> Optional[AudioProc]:
    if have_pw_cat():
        cmd = ["pw-cat", "--playback", "--rate", str(SAMPLE_RATE), "--channels", str(CHANNELS), "--format", "s16le"]
    elif have_aplay():
        cmd = ["aplay", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", str(CHANNELS), "-t", "raw", "-"]
    else:
        return None

    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return AudioProc(p=p, phase=0.0)

def stop_audio_stream(ap: Optional[AudioProc]) -> None:
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

def gen_sine_block(ap: AudioProc, hz: float, vol: float) -> List[int]:
    inc = TWOPI * hz / SAMPLE_RATE
    out: List[int] = []
    ph = ap.phase
    for _ in range(FRAME_SAMPLES):
        v = math.sin(ph) * vol
        iv = s16(v)
        out.append(iv)
        out.append(iv)
        ph += inc
        if ph > TWOPI:
            ph -= TWOPI
    ap.phase = ph
    return out

def gen_silence_block() -> List[int]:
    return [0] * (FRAME_SAMPLES * CHANNELS)

# ============================================================
# PRESETS
# ============================================================
PRESETS: List[Tuple[str, float]] = [
    ("A=432", 432.0),
    ("528", 528.0),
    ("396", 396.0),
    ("417", 417.0),
    ("639", 639.0),
    ("741", 741.0),
    ("852", 852.0),
    ("963", 963.0),
    ("174", 174.0),
    ("285", 285.0),
]

def fmt_hz(hz: float) -> str:
    if hz >= 1000:
        return f"{hz/1000.0:.3f}kHz"
    return f"{hz:.1f}Hz"

def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def tritone_ratio() -> float:
    return math.sqrt(2.0)

# ============================================================
# UI / STATE MACHINE
# ============================================================
STATE_MAIN = "MAIN"
STATE_STEADY = "STEADY"
STATE_PULSE = "PULSE"
STATE_SWEEP = "SWEEP"
STATE_PRESETS = "PRESETS"
STATE_FAVS = "FAVS"
STATE_SETTINGS = "SETTINGS"
STATE_SEQUENCE = "SEQUENCE"

MAIN_ITEMS = ["Steady", "Pulse", "Sweep", "Presets", "Favorites", "Settings"]

# settings lines are “edit in place” (UP/DN changes; SELECT cycles field)
SET_FIELDS = ["Volume", "Pulse on", "Pulse off", "Sweep end", "Sweep step", "Sweep step ms"]

class Mode:
    STOPPED = 0
    STEADY = 1
    PULSE = 2
    SWEEP = 3
    SEQ = 4

def draw_menu(title: str, items: List[str], idx: int, footer: str):
    def _d(d):
        draw_header(d, title)
        # scrolling window
        visible = 4
        start = max(0, min(idx - 1, max(0, len(items) - visible)))
        for row in range(visible):
            i = start + row
            if i >= len(items):
                break
            draw_row(d, LIST_Y0 + row * ROW_H, items[i], selected=(i == idx))
        draw_footer(d, footer)
    return _d

def draw_player(title: str, hz: float, line2: str, line3: str, footer: str):
    def _d(d):
        draw_header(d, title)
        draw_centered(d, 26, fmt_hz(hz))
        d.text((2, 38), line2[:21], fill=255)
        d.text((2, 48), line3[:21], fill=255)
        draw_footer(d, footer)
    return _d

# ============================================================
# MAIN
# ============================================================
def main():
    boot_at = time.monotonic()
    drain_stdin(0.10)

    cfg = load_cfg()
    ap = start_audio_stream()
    if ap is None:
        render(lambda d: (draw_header(d, "TONE GEN"), draw_centered(d, 28, "NO AUDIO"), draw_footer(d, "BACK exit")))
        # wait for a back
        while True:
            ev = read_event()
            if ev == "back":
                return
            time.sleep(0.05)

    state = STATE_MAIN
    idx = 0
    presets_idx = 0
    fav_idx = 0
    set_idx = 0

    mode = Mode.STOPPED

    # pulse internals
    pulse_phase_ms = 0
    pulse_is_on = True

    # sweep internals
    sweep_hz = float(cfg["sweep_start_hz"])
    sweep_dir = 1
    sweep_acc_ms = 0

    # sequence internals
    seq_step = 0
    seq_hz = 110.0
    seq_next_change = time.monotonic()
    seq_kind = "tritone_rise"

    def stop_playback():
        nonlocal mode, pulse_phase_ms, pulse_is_on
        mode = Mode.STOPPED
        pulse_phase_ms = 0
        pulse_is_on = True

    def toggle_favorite(hz: float):
        hz = float(round(hz, 2))
        favs = list(cfg.get("favorites", []))
        if hz in favs:
            favs.remove(hz)
        else:
            favs.append(hz)
        cfg["favorites"] = favs[:30]
        save_cfg(cfg)

    # initial draw
    render(draw_menu("TONE GEN", MAIN_ITEMS, idx, "SEL enter  BACK exit"))

    try:
        while True:
            ev = read_event()

            # startup dead-zone: ignore stray "back/select"
            if ev and (time.monotonic() - boot_at) < 0.35:
                if ev in ("back", "select"):
                    ev = None

            # ----------------------------
            # Global BACK behavior
            # ----------------------------
            if ev == "back":
                if state == STATE_MAIN:
                    stop_playback()
                    break
                else:
                    stop_playback()
                    state = STATE_MAIN
                    idx = 0
                    render(draw_menu("TONE GEN", MAIN_ITEMS, idx, "SEL enter  BACK exit"))
                    ev = None

            # select-hold quick favorite save in play states
            if ev == "select_hold":
                if state == STATE_STEADY:
                    toggle_favorite(float(cfg["steady_hz"]))
                elif state == STATE_PULSE:
                    toggle_favorite(float(cfg["pulse_hz"]))
                elif state == STATE_SWEEP:
                    toggle_favorite(float(round(sweep_hz, 2)))
                elif state == STATE_SEQUENCE:
                    toggle_favorite(float(round(seq_hz, 2)))

            # ----------------------------
            # State machine input
            # ----------------------------
            if ev:
                if state == STATE_MAIN:
                    if ev == "up":
                        idx = (idx - 1) % len(MAIN_ITEMS)
                        render(draw_menu("TONE GEN", MAIN_ITEMS, idx, "SEL enter  BACK exit"))
                    elif ev == "down":
                        idx = (idx + 1) % len(MAIN_ITEMS)
                        render(draw_menu("TONE GEN", MAIN_ITEMS, idx, "SEL enter  BACK exit"))
                    elif ev == "select":
                        choice = MAIN_ITEMS[idx]
                        if choice == "Steady":
                            state = STATE_STEADY
                            mode = Mode.STEADY
                            render(draw_player("STEADY", float(cfg["steady_hz"]), f"Vol {int(cfg['volume']*100)}%", "HOLD fav", "SEL stop  BACK menu"))
                        elif choice == "Pulse":
                            state = STATE_PULSE
                            mode = Mode.PULSE
                            pulse_phase_ms = 0
                            pulse_is_on = True
                            render(draw_player("PULSE", float(cfg["pulse_hz"]), f"{cfg['pulse_on_ms']}/{cfg['pulse_off_ms']}ms", "HOLD fav", "SEL stop  BACK menu"))
                        elif choice == "Sweep":
                            state = STATE_SWEEP
                            mode = Mode.SWEEP
                            sweep_hz = float(cfg["sweep_start_hz"])
                            sweep_dir = 1
                            sweep_acc_ms = 0
                            render(draw_player("SWEEP", sweep_hz, f"{fmt_hz(cfg['sweep_start_hz'])}->{fmt_hz(cfg['sweep_end_hz'])}", f"Step {cfg['sweep_step_hz']}Hz", "SEL stop  BACK menu"))
                        elif choice == "Presets":
                            state = STATE_PRESETS
                            presets_idx = 0
                            items = [f"{n} {fmt_hz(h)}" for (n, h) in PRESETS] + ["Tritone seq", "Back"]
                            render(draw_menu("PRESETS", items, presets_idx, "SEL play  HOLD fav"))
                        elif choice == "Favorites":
                            state = STATE_FAVS
                            fav_idx = 0
                            favs = cfg.get("favorites", [])
                            items = [fmt_hz(float(x)) for x in favs] + ["Back"]
                            render(draw_menu("FAVORITES", items, fav_idx, "SEL play  HOLD del"))
                        elif choice == "Settings":
                            state = STATE_SETTINGS
                            set_idx = 0
                            render(draw_menu("SETTINGS", SET_FIELDS, set_idx, "UP/DN edit SEL next"))

                elif state == STATE_PRESETS:
                    items = [f"{n} {fmt_hz(h)}" for (n, h) in PRESETS] + ["Tritone seq", "Back"]
                    if ev == "up":
                        presets_idx = (presets_idx - 1) % len(items)
                        render(draw_menu("PRESETS", items, presets_idx, "SEL play  HOLD fav"))
                    elif ev == "down":
                        presets_idx = (presets_idx + 1) % len(items)
                        render(draw_menu("PRESETS", items, presets_idx, "SEL play  HOLD fav"))
                    elif ev == "select":
                        if presets_idx < len(PRESETS):
                            _, hz = PRESETS[presets_idx]
                            cfg["steady_hz"] = float(hz)
                            save_cfg(cfg)
                            state = STATE_STEADY
                            mode = Mode.STEADY
                            render(draw_player("STEADY", float(cfg["steady_hz"]), f"Vol {int(cfg['volume']*100)}%", "HOLD fav", "SEL stop  BACK menu"))
                        elif presets_idx == len(PRESETS):
                            # Tritone sequence
                            state = STATE_SEQUENCE
                            mode = Mode.SEQ
                            seq_kind = "tritone_rise"
                            seq_step = 0
                            seq_hz = 110.0
                            seq_next_change = time.monotonic()
                            render(draw_player("SEQUENCE", seq_hz, "Tritone rise", "HOLD fav", "SEL stop  BACK menu"))
                        else:
                            state = STATE_MAIN
                            idx = 0
                            render(draw_menu("TONE GEN", MAIN_ITEMS, idx, "SEL enter  BACK exit"))

                elif state == STATE_FAVS:
                    favs = [float(x) for x in cfg.get("favorites", [])]
                    items = [fmt_hz(x) for x in favs] + ["Back"]
                    if ev == "up":
                        fav_idx = (fav_idx - 1) % len(items)
                        render(draw_menu("FAVORITES", items, fav_idx, "SEL play  HOLD del"))
                    elif ev == "down":
                        fav_idx = (fav_idx + 1) % len(items)
                        render(draw_menu("FAVORITES", items, fav_idx, "SEL play  HOLD del"))
                    elif ev == "select":
                        if fav_idx < len(favs):
                            cfg["steady_hz"] = float(favs[fav_idx])
                            save_cfg(cfg)
                            state = STATE_STEADY
                            mode = Mode.STEADY
                            render(draw_player("STEADY", float(cfg["steady_hz"]), f"Vol {int(cfg['volume']*100)}%", "HOLD fav", "SEL stop  BACK menu"))
                        else:
                            state = STATE_MAIN
                            idx = 0
                            render(draw_menu("TONE GEN", MAIN_ITEMS, idx, "SEL enter  BACK exit"))
                    elif ev == "select_hold":
                        if fav_idx < len(favs):
                            hz = float(favs[fav_idx])
                            favs.remove(hz)
                            cfg["favorites"] = favs
                            save_cfg(cfg)
                            fav_idx = int(clamp(fav_idx, 0, max(0, len(favs))))
                            items = [fmt_hz(x) for x in favs] + ["Back"]
                            render(draw_menu("FAVORITES", items, fav_idx, "SEL play  HOLD del"))

                elif state == STATE_SETTINGS:
                    # UP/DN edits current field; SELECT moves to next field
                    if ev == "select":
                        set_idx = (set_idx + 1) % len(SET_FIELDS)
                        render(draw_menu("SETTINGS", SET_FIELDS, set_idx, "UP/DN edit SEL next"))
                    elif ev == "up":
                        if set_idx == 0:
                            cfg["volume"] = float(clamp(cfg["volume"] + 0.05, 0.0, 1.0))
                        elif set_idx == 1:
                            cfg["pulse_on_ms"] = int(clamp(cfg["pulse_on_ms"] + 50, 50, 5000))
                        elif set_idx == 2:
                            cfg["pulse_off_ms"] = int(clamp(cfg["pulse_off_ms"] + 50, 50, 5000))
                        elif set_idx == 3:
                            cfg["sweep_end_hz"] = float(clamp(cfg["sweep_end_hz"] + 10.0, 10.0, 20000.0))
                        elif set_idx == 4:
                            cfg["sweep_step_hz"] = float(clamp(cfg["sweep_step_hz"] + 1.0, 0.1, 2000.0))
                        elif set_idx == 5:
                            cfg["sweep_step_ms"] = int(clamp(cfg["sweep_step_ms"] + 50, 50, 350))
                        save_cfg(cfg)
                    elif ev == "down":
                        if set_idx == 0:
                            cfg["volume"] = float(clamp(cfg["volume"] - 0.05, 0.0, 1.0))
                        elif set_idx == 1:
                            cfg["pulse_on_ms"] = int(clamp(cfg["pulse_on_ms"] - 50, 50, 5000))
                        elif set_idx == 2:
                            cfg["pulse_off_ms"] = int(clamp(cfg["pulse_off_ms"] - 50, 50, 5000))
                        elif set_idx == 3:
                            cfg["sweep_end_hz"] = float(clamp(cfg["sweep_end_hz"] - 10.0, 10.0, 20000.0))
                        elif set_idx == 4:
                            cfg["sweep_step_hz"] = float(clamp(cfg["sweep_step_hz"] - 1.0, 0.1, 2000.0))
                        elif set_idx == 5:
                            cfg["sweep_step_ms"] = int(clamp(cfg["sweep_step_ms"] - 50, 50, 350))
                        save_cfg(cfg)
                    # redraw compact status for current
