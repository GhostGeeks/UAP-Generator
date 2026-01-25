
#!/usr/bin/env python3
"""
Ghost Geeks Spirit Box Module (TEA5767 FM)

- Controlled via stdin commands from the main OLED UI:
    up, down, select, select_hold, back

- TEA5767 is I2C control, analog audio out (L/R).
  Bluetooth audio for TEA5767 requires an audio capture device (future upgrade).
"""

import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# OLED (same stack you're using)
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas

# I2C bus access
try:
    from smbus2 import SMBus
except Exception:
    try:
        from smbus import SMBus  # type: ignore
    except Exception:
        SMBus = None  # type: ignore


# =========================
# CONFIG
# =========================
MODULE_DIR = Path(__file__).resolve().parent
CFG_PATH = MODULE_DIR / "spirit_box_config.json"

I2C_BUS = 1
TEA5767_ADDR = 0x60

FREQ_MIN = 87.5
FREQ_MAX = 108.0
STEP = 0.1

DWELL_MIN_MS = 50
DWELL_MAX_MS = 350
DWELL_STEP_MS = 50

BT_TIMEOUT_SEC = 30  # pause after 30 seconds without BT (if output wants BT)

OLED_W = 128
OLED_H = 64

# Display tuning: adjust baseline if needed
Y_TOP = 0
Y_BIG = 14
Y_META = 46
Y_FOOT = 56


# =========================
# OLED HELPERS
# =========================
device = ssd1306(i2c(port=I2C_BUS, address=0x3C), width=OLED_W, height=OLED_H)

def draw_main(freq: float, running: bool, dwell_ms: int, style: str, out_mode: str,
              bt_state: str, warning: str):
    big = f"{freq:05.1f}"
    run_txt = "RUN" if running else "STOP"
    meta1 = f"{run_txt}  {dwell_ms}ms  {style}"
    meta2 = f"OUT:{out_mode}  BT:{bt_state}"

    with canvas(device) as d:
        # Title
        d.text((0, Y_TOP), "SPIRIT BOX", fill=255)
        d.line((0, 10, 127, 10), fill=255)

        # Big frequency
        d.text((18, Y_BIG), big, fill=255)  # centered-ish for 128x64

        # Meta
        d.text((0, Y_META), meta1[:21], fill=255)
        d.text((0, Y_META + 9), meta2[:21], fill=255)

        # Warning/footer
        if warning:
            d.text((0, Y_FOOT), warning[:21], fill=255)
        else:
            d.text((0, Y_FOOT), "SEL=run  HOLD=opts  BACK=exit"[:21], fill=255)


def draw_menu(title: str, items: list[str], idx: int, hint: str):
    with canvas(device) as d:
        d.text((0, 0), title[:21], fill=255)
        d.line((0, 10, 127, 10), fill=255)
        for i in range(4):
            j = idx + i
            if j >= len(items):
                break
            y = 12 + i * 12
            prefix = ">" if j == idx else " "
            d.text((0, y), (prefix + " " + items[j])[:21], fill=255)
        d.text((0, 56), hint[:21], fill=255)


# =========================
# TEA5767 CONTROL
# =========================
@dataclass
class Tea5767:
    bus: object

    def set_frequency(self, mhz: float):
        """
        Set frequency.

        Common formula from TEA5767 examples:
          PLL = 4 * ( (F_rf + IF) / F_ref )
        IF = 225 kHz, F_ref = 32.768 kHz (watch crystal).
        """
        freq_hz = int(mhz * 1_000_000)
        if_hz = 225_000
        fref = 32_768

        pll = int(4 * (freq_hz + if_hz) / fref)

        # Bytes:
        # b0: MUTE(0) | SM(0) | PLL[13:8]
        # b1: PLL[7:0]
        # b2: 0xB0 (example: high side LO, stereo on)
        # b3: 0x10 (XTAL=1 -> 32.768 kHz)
        # b4: 0x00
        b0 = (pll >> 8) & 0x3F
        b1 = pll & 0xFF
        data = [b0, b1, 0xB0, 0x10, 0x00]

        # write_i2c_block_data signature differs between smbus/smbus2; handle both
        try:
            self.bus.write_i2c_block_data(TEA5767_ADDR, data[0], data[1:])
        except Exception:
            # fallback: raw write
            self.bus.write_byte_data(TEA5767_ADDR, 0x00, data[0])
            for i, v in enumerate(data[1:], start=1):
                self.bus.write_byte_data(TEA5767_ADDR, i, v)


def init_radio() -> Optional[Tea5767]:
    if SMBus is None:
        return None
    try:
        b = SMBus(I2C_BUS)
        # quick ping by attempting a frequency set
        r = Tea5767(b)
        r.set_frequency(99.9)
        return r
    except Exception:
        return None


# =========================
# SETTINGS + BT DETECTION
# =========================
DEFAULT_CFG = {
    "scan_style": "Random",        # Linear, PingPong, Random, Hybrid
    "output_mode": "AUTO",         # AUTO, BT, WIRED
    "pause_on_bt_loss": True,
    "dwell_ms": 150
}

def load_cfg() -> dict:
    if CFG_PATH.exists():
        try:
            return {**DEFAULT_CFG, **json.loads(CFG_PATH.read_text())}
        except Exception:
            return dict(DEFAULT_CFG)
    return dict(DEFAULT_CFG)

def save_cfg(cfg: dict):
    try:
        CFG_PATH.write_text(json.dumps(cfg, indent=2))
        os.sync()
    except Exception:
        pass

def bt_connected() -> bool:
    """
    Connected if we can see a bluez sink.
    This doesn't prove audio is streaming—just that the speaker is connected/available.
    """
    try:
        out = subprocess.check_output(["pactl", "list", "short", "sinks"], text=True)
        return "bluez_output" in out
    except Exception:
        return False


# =========================
# INPUT (stdin commands)
# =========================
def read_cmd_nonblocking() -> Optional[str]:
    """
    Non-blocking-ish stdin read: we poll with a tiny timeout by checking if data exists.
    In practice your runner sends short lines; this is good enough for the module.
    """
    try:
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0.0)
        if r:
            line = sys.stdin.readline()
            return line.strip()
    except Exception:
        return None
    return None


# =========================
# SCAN LOGIC
# =========================
def clamp(x, a, b):
    return a if x < a else b if x > b else x

def next_freq_linear(cur: float) -> float:
    n = cur + STEP
    if n > FREQ_MAX:
        n = FREQ_MIN
    return round(n, 1)

def next_freq_pingpong(cur: float, direction: int) -> tuple[float, int]:
    n = cur + (STEP * direction)
    if n > FREQ_MAX:
        n = FREQ_MAX
        direction = -1
    elif n < FREQ_MIN:
        n = FREQ_MIN
        direction = 1
    return (round(n, 1), direction)

def next_freq_random() -> float:
    # choose random freq on STEP grid
    steps = int(round((FREQ_MAX - FREQ_MIN) / STEP))
    k = random.randint(0, steps)
    return round(FREQ_MIN + k * STEP, 1)

def next_freq_hybrid(cur: float) -> float:
    # 70% random hops, 30% linear
    return next_freq_random() if random.random() < 0.7 else next_freq_linear(cur)


# =========================
# SETTINGS PAGE
# =========================
def settings_page(cfg: dict) -> dict:
    items = [
        f"Scan: {cfg['scan_style']}",
        f"Out : {cfg['output_mode']}",
        f"BTpause: {'ON' if cfg['pause_on_bt_loss'] else 'OFF'}",
        "Save & Exit"
    ]
    idx = 0

    while True:
        items[0] = f"Scan: {cfg['scan_style']}"
        items[1] = f"Out : {cfg['output_mode']}"
        items[2] = f"BTpause: {'ON' if cfg['pause_on_bt_loss'] else 'OFF'}"

        draw_menu("OPTIONS", items, idx, "UP/DN sel  SEL=chg  BACK=done")

        cmd = read_cmd_nonblocking()
        if not cmd:
            time.sleep(0.03)
            continue

        if cmd == "up":
            idx = (idx - 1) % len(items)
        elif cmd == "down":
            idx = (idx + 1) % len(items)
        elif cmd == "select":
            if idx == 0:
                styles = ["Linear", "PingPong", "Random", "Hybrid"]
                curi = styles.index(cfg["scan_style"]) if cfg["scan_style"] in styles else 0
                cfg["scan_style"] = styles[(curi + 1) % len(styles)]
            elif idx == 1:
                outs = ["AUTO", "BT", "WIRED"]
                curi = outs.index(cfg["output_mode"]) if cfg["output_mode"] in outs else 0
                cfg["output_mode"] = outs[(curi + 1) % len(outs)]
            elif idx == 2:
                cfg["pause_on_bt_loss"] = not cfg["pause_on_bt_loss"]
            elif idx == 3:
                save_cfg(cfg)
                return cfg
        elif cmd == "back":
            # no save unless already saved explicitly
            return cfg


# =========================
# MAIN
# =========================
def main():
    cfg = load_cfg()

    radio = init_radio()
    if radio is None:
        # Helpful error
        with canvas(device) as d:
            d.text((0, 0), "SPIRIT BOX", fill=255)
            d.text((0, 14), "No TEA5767 I2C", fill=255)
            d.text((0, 26), "Check wiring", fill=255)
            d.text((0, 38), "I2C enabled?", fill=255)
            d.text((0, 56), "BACK=exit", fill=255)
        # wait for back
        while True:
            cmd = read_cmd_nonblocking()
            if cmd == "back":
                return
            time.sleep(0.05)

    # initial state
    freq = 99.9
    dwell_ms = int(cfg.get("dwell_ms", 150))
    dwell_ms = int(clamp(dwell_ms, DWELL_MIN_MS, DWELL_MAX_MS))

    running = False
    direction = 1  # for pingpong
    bt_lost_since = None  # timestamp
    paused_for_bt = False

    # tune initial
    try:
        radio.set_frequency(freq)
    except Exception:
        pass

    while True:
        style = cfg["scan_style"]
        out_mode = cfg["output_mode"]

        # BT state logic (availability)
        bt_ok = bt_connected()
        bt_state = "OK" if bt_ok else "OFF"
        warning = ""

        wants_bt = (out_mode in ("AUTO", "BT"))
        if wants_bt:
            if not bt_ok:
                if bt_lost_since is None:
                    bt_lost_since = time.time()
                elapsed = int(time.time() - bt_lost_since)
                warning = f"BT LOST {elapsed:02d}s"
                if cfg["pause_on_bt_loss"] and elapsed >= BT_TIMEOUT_SEC:
                    running = False
                    paused_for_bt = True
                    warning = "BT LOST: PAUSED"
            else:
                bt_lost_since = None
                if paused_for_bt:
                    # auto resume sweep when BT returns
                    running = True
                    paused_for_bt = False
        else:
            bt_lost_since = None
            paused_for_bt = False

        draw_main(freq, running, dwell_ms, style, out_mode, bt_state, warning)

        # handle commands
        cmd = read_cmd_nonblocking()
        if cmd:
            if cmd == "back":
                # exit module
                with canvas(device) as d:
                    d.text((0, 0), "SPIRIT BOX", fill=255)
                    d.text((0, 20), "Returning...", fill=255)
                time.sleep(0.4)
                cfg["dwell_ms"] = dwell_ms
                save_cfg(cfg)
                return

            elif cmd == "select":
                running = not running
                paused_for_bt = False  # user override

            elif cmd == "up":
                dwell_ms = int(clamp(dwell_ms - DWELL_STEP_MS, DWELL_MIN_MS, DWELL_MAX_MS))
                cfg["dwell_ms"] = dwell_ms
                save_cfg(cfg)

            elif cmd == "down":
                dwell_ms = int(clamp(dwell_ms + DWELL_STEP_MS, DWELL_MIN_MS, DWELL_MAX_MS))
                cfg["dwell_ms"] = dwell_ms
                save_cfg(cfg)

            elif cmd == "select_hold":
                cfg["dwell_ms"] = dwell_ms
                cfg = settings_page(cfg)
                # apply persisted dwell if changed
                dwell_ms = int(clamp(int(cfg.get("dwell_ms", dwell_ms)), DWELL_MIN_MS, DWELL_MAX_MS))

        # scan tick
        if running:
            if style == "Linear":
                freq = next_freq_linear(freq)
            elif style == "PingPong":
                freq, direction = next_freq_pingpong(freq, direction)
            elif style == "Random":
                freq = next_freq_random()
            else:  # Hybrid
                freq = next_freq_hybrid(freq)

            try:
                radio.set_frequency(freq)
            except Exception:
                # if tuning fails, stop to avoid runaway loop
                running = False

            time.sleep(dwell_ms / 1000.0)
        else:
            time.sleep(0.03)


if __name__ == "__main__":
    main()
