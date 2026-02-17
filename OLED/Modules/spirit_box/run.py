#!/usr/bin/env python3
"""
BlackBox Spirit Box (headless JSON stdout protocol) - sb_v1

Architecture goals:
- NO OLED access
- JSON-only stdout (never print debug)
- Non-blocking stdin (selectors + os.read)
- Persistent loop playback of a generated WAV pattern (Noise Generator pattern)
- Restart audio cleanly when parameters change
- Detect audio process crash + restart 2-3 times; then fatal
- Heartbeat state <=250ms
- Clean exit on back, and on SIGTERM

UI surface (handled by app.py):
Main page:
  Spirit Box
  Sweep Rate: 150/200/250/300 ms
  Direction: FWD/REV
  Mode: Scan/Burst (Burst is future; state supported)
  Play: PLAY/STOP

Controls:
  up/down: move cursor
  select: change value (forward)
  select_hold: change value (reverse)
  back: exit immediately
"""

import os
import sys
import json
import time
import wave
import math
import errno
import signal
import shutil
import selectors
import subprocess
from dataclasses import dataclass
from typing import Optional, List


MODULE_NAME = "spirit_box"
MODULE_VERSION = "sb_v1"

AUDIO_ERR = "/tmp/blackbox_spirit_audio.err"
MODULE_ERR = "/tmp/blackbox_spirit_module.err"

PATTERN_WAV = "/tmp/blackbox_spirit_pattern.wav"

HEARTBEAT_S = 0.25
TICK_S = 0.02  # <=50ms tick requirement

SWEEP_MS_CHOICES = [150, 200, 250, 300]
DIR_CHOICES = ["fwd", "rev"]
MODE_CHOICES = ["scan", "burst"]  # burst is future expansion

CURSOR_CHOICES = ["rate", "direction", "mode", "play"]

# WAV generation
RATE = 22050
CHANNELS = 1
SAMPWIDTH = 2
PATTERN_SECONDS = 9.0  # ~8-10s requested

# Pulse shape inside each "sweep step" (makes it sound like chopping across slices)
PULSE_ON_MS = 38
FADE_MS = 2

# "Frequency slice" simulation for the pattern (not actual FM tuning)
# Keeps CPU low: simple resonator-ish bandpass feel.
SLICE_F_MIN = 250.0
SLICE_F_MAX = 4200.0
SLICE_R = 0.985  # resonance factor; near 1.0 = narrow band


# ---------------- logging (file only) ----------------
def _log_err(msg: str) -> None:
    try:
        with open(MODULE_ERR, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ---------------- strict JSON stdout ----------------
def _emit(obj: dict) -> None:
    try:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _toast(msg: str) -> None:
    _emit({"type": "toast", "message": msg})


def _fatal(msg: str) -> None:
    _emit({"type": "fatal", "message": msg})


# ---------------- stdin reader (non-blocking) ----------------
class StdinReader:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        os.set_blocking(self.fd, False)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.fd, selectors.EVENT_READ)
        self.buf = bytearray()

    def close(self) -> None:
        try:
            self.sel.unregister(self.fd)
        except Exception:
            pass
        try:
            self.sel.close()
        except Exception:
            pass

    def read_commands(self, max_bytes: int = 4096) -> List[str]:
        out: List[str] = []
        if not self.sel.select(timeout=0):
            return out

        drained = 0
        while drained < max_bytes:
            try:
                chunk = os.read(self.fd, min(1024, max_bytes - drained))
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                break

            if not chunk:
                break

            drained += len(chunk)
            self.buf.extend(chunk)

            while b"\n" in self.buf:
                line, _, rest = self.buf.partition(b"\n")
                self.buf = bytearray(rest)
                try:
                    s = line.decode("utf-8", errors="ignore").strip().lower()
                except Exception:
                    continue
                if s:
                    out.append(s)

        return out


# ---------------- audio player + loop (Noise Generator style) ----------------
def _which_player() -> Optional[str]:
    # Prefer paplay; fallback pw-play; last resort aplay
    return shutil.which("paplay") or shutil.which("pw-play") or shutil.which("aplay")


def _start_audio_loop(player_path: str, wav_path: str) -> subprocess.Popen:
    try:
        open(AUDIO_ERR, "a").close()
    except Exception:
        pass

    base = os.path.basename(player_path)

    if base == "paplay":
        play_cmd = f'"{player_path}" "{wav_path}" 1>/dev/null'
    elif base == "pw-play":
        play_cmd = f'"{player_path}" "{wav_path}" 1>/dev/null'
    else:
        # aplay is noisy; ensure stdout muted and stderr is captured by shell redirection
        play_cmd = f'"{player_path}" -q "{wav_path}" 1>/dev/null'

    # One persistent loop inside one PGID so stop is immediate via killpg
    cmd = f'exec 2>>"{AUDIO_ERR}"; while true; do {play_cmd}; done'
    return subprocess.Popen(
        ["/bin/sh", "-lc", cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )


def _stop_proc(p: Optional[subprocess.Popen]) -> None:
    if not p:
        return
    try:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass

            t0 = time.time()
            while time.time() - t0 < 0.20:
                if p.poll() is not None:
                    break
                time.sleep(0.01)

        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    except Exception:
        pass


def _tail_err(path: str, max_bytes: int = 1800) -> str:
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read()
        return data.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


# ---------------- pattern generator ----------------
def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _write_wav_from_pcm16(path: str, pcm16: bytes, rate: int) -> None:
    tmp_path = path + ".tmp"
    with wave.open(tmp_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPWIDTH)
        wf.setframerate(rate)
        wf.writeframes(pcm16)
    os.replace(tmp_path, path)


def _gen_spirit_pattern_wav(
    path: str,
    sweep_ms: int,
    direction: str,
    mode: str,
) -> None:
    """
    Generate a ~9s WAV containing pulsed “slice” segments.
    This is intentionally simple + deterministic to keep CPU low on Pi Zero 2W.
    """

    sweep_ms = int(sweep_ms)
    sweep_ms = int(_clamp(sweep_ms, 80, 500))
    direction = "rev" if str(direction).lower().startswith("r") else "fwd"
    mode = str(mode).lower().strip()
    if mode not in MODE_CHOICES:
        mode = "scan"

    total_frames = int(RATE * PATTERN_SECONDS)
    step_frames = max(1, int(RATE * (sweep_ms / 1000.0)))
    on_frames = max(1, int(RATE * (PULSE_ON_MS / 1000.0)))
    on_frames = min(on_frames, step_frames)
    off_frames = step_frames - on_frames

    # Fade ramps to reduce clicks
    fade_frames = max(1, int(RATE * (FADE_MS / 1000.0)))
    fade_frames = min(fade_frames, max(1, on_frames // 2))

    # Number of steps in pattern
    n_steps = max(1, total_frames // step_frames)

    # Determine slice frequencies across range
    # Use a slight non-linear mapping for more variation
    freqs: List[float] = []
    for i in range(n_steps):
        t = i / max(1, (n_steps - 1))
        # skew towards mid highs a bit
        u = (0.15 * t) + (0.85 * (t ** 0.6))
        f = SLICE_F_MIN + u * (SLICE_F_MAX - SLICE_F_MIN)
        freqs.append(f)

    if direction == "rev":
        freqs = list(reversed(freqs))

    # Mode: "burst" can inject occasional longer “on” pulses in the future.
    # For now it just slightly changes the on/off ratio to feel different.
    if mode == "burst":
        on_frames = min(step_frames, max(1, int(on_frames * 1.35)))
        off_frames = step_frames - on_frames
        fade_frames = min(fade_frames, max(1, on_frames // 2))

    # Simple resonator filter per step:
    # y[n] = 2*r*cos(w)*y[n-1] - r^2*y[n-2] + (1-r)*x[n]
    # x[n] is pseudo-random noise (LCG) to be deterministic.
    r = SLICE_R

    # Deterministic LCG seed derived from parameters
    seed = (sweep_ms * 1315423911) ^ (0x9E3779B9 if direction == "rev" else 0x1234567) ^ (0xABCDEF if mode == "burst" else 0x13579B)
    seed &= 0xFFFFFFFF

    def rand_u32() -> int:
        nonlocal seed
        # LCG constants
        seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
        return seed

    pcm = bytearray()
    y1 = 0.0
    y2 = 0.0

    frames_written = 0
    for i, fc in enumerate(freqs):
        if frames_written >= total_frames:
            break

        w = 2.0 * math.pi * float(fc) / float(RATE)
        a1 = 2.0 * r * math.cos(w)
        a2 = -(r * r)
        b0 = (1.0 - r)

        # ON pulse
        for n in range(on_frames):
            if frames_written >= total_frames:
                break

            # noise in [-1,1]
            x = (rand_u32() / 2147483648.0) - 1.0

            y = (a1 * y1) + (a2 * y2) + (b0 * x)
            y2, y1 = y1, y

            # fade in/out
            g = 1.0
            if n < fade_frames:
                g = n / float(fade_frames)
            elif n > (on_frames - fade_frames):
                g = max(0.0, (on_frames - n) / float(fade_frames))

            # clamp, scale
            v = _clamp(y * g * 0.95, -1.0, 1.0)
            iv = int(v * 32767.0)
            pcm += int(iv).to_bytes(2, byteorder="little", signed=True)
            frames_written += 1

        # OFF gap (silence)
        for _ in range(off_frames):
            if frames_written >= total_frames:
                break
            pcm += (0).to_bytes(2, byteorder="little", signed=True)
            frames_written += 1

    # If short, pad to full length
    while frames_written < total_frames:
        pcm += (0).to_bytes(2, byteorder="little", signed=True)
        frames_written += 1

    _write_wav_from_pcm16(path, bytes(pcm), RATE)


# ---------------- state ----------------
@dataclass
class UIState:
    page: str = "main"
    ready: bool = False

    sweep_ms: int = 200
    direction: str = "fwd"
    mode: str = "scan"
    playing: bool = False

    cursor: str = "rate"

    # internal
    _last_toast_t: float = 0.0
    _restart_tries: int = 0
    _fatal_active: bool = False


def _emit_page(st: UIState) -> None:
    _emit({"type": "page", "name": st.page})


def _emit_state(st: UIState) -> None:
    _emit({
        "type": "state",
        "ready": bool(st.ready),
        "sweep_ms": int(st.sweep_ms),
        "direction": str(st.direction),
        "mode": str(st.mode),
        "playing": bool(st.playing),
        "cursor": str(st.cursor),
    })


def _toast_throttle(st: UIState, msg: str, min_interval_s: float = 0.10) -> None:
    now = time.monotonic()
    if now - st._last_toast_t >= min_interval_s:
        st._last_toast_t = now
        _toast(msg)


def _cycle_choice(cur: str, choices: List[str], delta: int) -> str:
    cur = str(cur).lower().strip()
    try:
        idx = choices.index(cur)
    except Exception:
        idx = 0
    idx = (idx + delta) % len(choices)
    return choices[idx]


def _cycle_int_choice(cur: int, choices: List[int], delta: int) -> int:
    try:
        idx = choices.index(int(cur))
    except Exception:
        idx = 0
    idx = (idx + delta) % len(choices)
    return int(choices[idx])


def main() -> int:
    exiting = {"flag": False}

    def _sig_handler(_signo, _frame):
        exiting["flag"] = True

    try:
        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGINT, _sig_handler)
    except Exception:
        pass

    reader = StdinReader()
    st = UIState()

    _emit({"type": "hello", "module": MODULE_NAME, "version": MODULE_VERSION})
    _emit_page(st)

    player_path = _which_player()
    if not player_path:
        st.ready = False
        _emit_state(st)
        st._fatal_active = True
        _fatal("Audio player not available (need paplay/pw-play/aplay)")
    else:
        st.ready = True
        _emit_state(st)

    audio_proc: Optional[subprocess.Popen] = None

    def stop_audio() -> None:
        nonlocal audio_proc
        _stop_proc(audio_proc)
        audio_proc = None

    def build_pattern_or_fatal() -> bool:
        """
        Build the WAV synchronously (allowed), but never from inside a tight UI loop
        other than during a param-change event.
        """
        try:
            _gen_spirit_pattern_wav(PATTERN_WAV, st.sweep_ms, st.direction, st.mode)
            return True
        except Exception as e:
            _log_err(f"pattern_build_failed: {e!r}")
            st.ready = False
            st.playing = False
            st._fatal_active = True
            _emit_state(st)
            _fatal("Failed to generate pattern.wav")
            return False

    def start_audio() -> None:
        nonlocal audio_proc
        if not player_path:
            st.ready = False
            st.playing = False
            st._fatal_active = True
            _emit_state(st)
            _fatal("Audio device not available")
            return

        if not build_pattern_or_fatal():
            return

        stop_audio()

        try:
            audio_proc = _start_audio_loop(player_path, PATTERN_WAV)
        except Exception as e:
            _log_err(f"audio_start_failed: {e!r}")
            st.ready = False
            st.playing = False
            st._fatal_active = True
            _emit_state(st)
            _fatal("Failed to start audio loop")
            audio_proc = None
            return

        st._restart_tries = 0

    def restart_audio_on_param_change() -> None:
        # Only restart if playing; otherwise just rebuild on next play
        if st.playing:
            start_audio()

    def cleanup_files() -> None:
        try:
            if os.path.exists(PATTERN_WAV):
                os.remove(PATTERN_WAV)
        except Exception:
            pass

    last_hb = 0.0
    last_tick = time.monotonic()

    try:
        while not exiting["flag"]:
            now = time.monotonic()

            # ---------- process stdin (never blocking) ----------
            cmds = reader.read_commands()
            for cmd in cmds:
                if cmd == "back":
                    exiting["flag"] = True
                    break

                # ignore inputs if fatal is active? allow navigation still so user can back out
                if cmd == "up":
                    try:
                        idx = CURSOR_CHOICES.index(st.cursor)
                    except Exception:
                        idx = 0
                    st.cursor = CURSOR_CHOICES[(idx - 1) % len(CURSOR_CHOICES)]
                    _emit_state(st)

                elif cmd == "down":
                    try:
                        idx = CURSOR_CHOICES.index(st.cursor)
                    except Exception:
                        idx = 0
                    st.cursor = CURSOR_CHOICES[(idx + 1) % len(CURSOR_CHOICES)]
                    _emit_state(st)

                elif cmd in ("select", "select_hold"):
                    delta = +1 if cmd == "select" else -1

                    if st.cursor == "rate":
                        st.sweep_ms = _cycle_int_choice(st.sweep_ms, SWEEP_MS_CHOICES, delta)
                        _toast_throttle(st, f"Sweep: {st.sweep_ms}ms")
                        _emit_state(st)
                        restart_audio_on_param_change()

                    elif st.cursor == "direction":
                        st.direction = _cycle_choice(st.direction, DIR_CHOICES, delta)
                        _toast_throttle(st, f"Direction: {'REV' if st.direction=='rev' else 'FWD'}")
                        _emit_state(st)
                        restart_audio_on_param_change()

                    elif st.cursor == "mode":
                        st.mode = _cycle_choice(st.mode, MODE_CHOICES, delta)
                        _toast_throttle(st, f"Mode: {st.mode.upper()}")
                        _emit_state(st)
                        restart_audio_on_param_change()

                    elif st.cursor == "play":
                        if st.playing:
                            st.playing = False
                            _emit_state(st)
                            stop_audio()
                            _toast_throttle(st, "STOP")
                        else:
                            st.playing = True
                            _emit_state(st)
                            start_audio()
                            # if audio failed, st.playing will still be true; normalize it
                            if audio_proc is None:
                                st.playing = False
                                _emit_state(st)
                            else:
                                _toast_throttle(st, "PLAY")

            # ---------- monitor audio proc (crash detection + restart) ----------
            if st.playing and audio_proc is not None:
                rc = audio_proc.poll()
                if rc is not None:
                    err_tail = _tail_err(AUDIO_ERR)
                    _log_err(f"audio_proc_died rc={rc} tail={err_tail[-400:]!r}")

                    st._restart_tries += 1
                    if st._restart_tries <= 3:
                        _toast_throttle(st, f"Audio restart {st._restart_tries}/3")
                        # attempt restart
                        start_audio()
                        if audio_proc is None:
                            # failed to restart
                            pass
                    else:
                        st.ready = False
                        st.playing = False
                        st._fatal_active = True
                        _emit_state(st)
                        stop_audio()
                        _fatal("Audio crashed repeatedly")
                        # remain alive so BACK works

            # ---------- heartbeat state (<=250ms) ----------
            if (now - last_hb) >= HEARTBEAT_S:
                _emit_state(st)
                last_hb = now

            # ---------- tick pacing (<=50ms sleeps) ----------
            elapsed = now - last_tick
            last_tick = now
            if elapsed < TICK_S:
                time.sleep(TICK_S - elapsed)

    finally:
        try:
            reader.close()
        except Exception:
            pass
        try:
            stop_audio()
        except Exception:
            pass
        cleanup_files()
        _emit({"type": "exit"})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
