#!/usr/bin/env python3
"""
BlackBox Tone Generator (headless JSON stdout protocol)

RULES:
- MUST NOT import luma.oled or access OLED
- MUST NOT manage Bluetooth
- MUST NEVER print non-JSON to stdout (no debug prints, no tracebacks)
- MUST NOT block stdin loop (non-blocking os.read + selectors)
- Communicate ONLY via JSON lines over stdout
- Exit cleanly on back (stop audio first)
- Pi-friendly audio: generate a pulsed WAV pattern and loop it with paplay/pw-play in one background process
"""

import os
import sys
import json
import time
import math
import wave
import signal
import shutil
import selectors
import subprocess
from dataclasses import dataclass
from typing import Optional

MODULE_NAME = "tone_generator"
MODULE_VERSION = "tg_v1"

# Files
PATTERN_WAV = "/tmp/blackbox_tone_pattern.wav"
AUDIO_ERR = "/tmp/blackbox_tone_audio.err"
MODULE_ERR = "/tmp/blackbox_tone_module.err"

# Audio params
RATE = 48000
CHANNELS = 1
SAMPWIDTH = 2  # 16-bit
PATTERN_SECONDS = 8.0
DUTY = 0.90  # ~90% on within each pulse period

# UX rows (cursor)
ROWS = ["tone_type", "freq", "pulse_ms", "volume", "play"]
TONE_TYPES = ["sine", "square", "saw", "triangle"]
PULSE_MS_OPTIONS = [150, 200, 250, 300]

HEARTBEAT_S = 0.25
TOAST_MIN_INTERVAL_S = 0.10


# ----------------------------
# Safe stderr logging (file only)
# ----------------------------
def _log_err(msg: str) -> None:
    try:
        with open(MODULE_ERR, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ----------------------------
# JSON stdout (STRICT)
# ----------------------------
def _emit(obj: dict) -> None:
    try:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _fatal(message: str) -> None:
    _emit({"type": "fatal", "message": message})


def _toast(message: str) -> None:
    _emit({"type": "toast", "message": message})


def _hello() -> None:
    _emit({"type": "hello", "module": MODULE_NAME, "version": MODULE_VERSION})
    _emit({"type": "page", "name": "main"})


# ----------------------------
# State
# ----------------------------
@dataclass
class TGState:
    tone_type: str = "sine"
    freq_hz: int = 440
    volume: int = 70
    pulse_ms: int = 200
    playing: bool = False
    cursor: str = "freq"
    ready: bool = False

    _need_audio_restart: bool = False
    _last_toast_t: float = 0.0


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def _freq_step(freq: int) -> int:
    if freq < 200:
        return 5
    if freq <= 2000:
        return 10
    return 50


def _next_in_list(lst, cur):
    try:
        i = lst.index(cur)
    except Exception:
        i = 0
    return lst[(i + 1) % len(lst)]


def _prev_in_list(lst, cur):
    try:
        i = lst.index(cur)
    except Exception:
        i = 0
    return lst[(i - 1) % len(lst)]


def _state_msg(st: TGState) -> dict:
    return {
        "type": "state",
        "ready": bool(st.ready),
        "tone_type": st.tone_type,
        "freq_hz": int(st.freq_hz),
        "volume": int(st.volume),
        "pulse_ms": int(st.pulse_ms),
        "playing": bool(st.playing),
        "cursor": st.cursor,
    }


def _toast_throttle(st: TGState, msg: str) -> None:
    now = time.monotonic()
    if now - st._last_toast_t >= TOAST_MIN_INTERVAL_S:
        st._last_toast_t = now
        _toast(msg)


# ----------------------------
# WAV generation (pulsed tone pattern)
# ----------------------------
def _waveform_sample(t: float, freq: float, kind: str) -> float:
    phase = (t * freq) % 1.0  # 0..1
    if kind == "sine":
        return math.sin(2.0 * math.pi * phase)
    if kind == "square":
        return 1.0 if phase < 0.5 else -1.0
    if kind == "saw":
        return 2.0 * phase - 1.0
    if kind == "triangle":
        return 1.0 - 4.0 * abs(phase - 0.5)
    return math.sin(2.0 * math.pi * phase)


def _write_pattern_wav(tone_type: str, freq_hz: int, volume: int, pulse_ms: int) -> None:
    freq_hz = _clamp(int(freq_hz), 20, 20000)
    volume = _clamp(int(volume), 0, 100)
    pulse_ms = _clamp(int(pulse_ms), 50, 2000)
    if tone_type not in TONE_TYPES:
        tone_type = "sine"

    total_frames = int(RATE * PATTERN_SECONDS)
    period_frames = max(1, int(RATE * (pulse_ms / 1000.0)))
    on_frames = max(1, int(period_frames * DUTY))
    off_frames = max(0, period_frames - on_frames)

    amp = (volume / 100.0) * 0.95  # headroom

    tmp_path = PATTERN_WAV + ".tmp"
    with wave.open(tmp_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPWIDTH)
        wf.setframerate(RATE)

        # Precompute one period bytes for speed
        period_bytes = bytearray()

        # Tone segment
        for i in range(on_frames):
            t = i / float(RATE)
            s = _waveform_sample(t, float(freq_hz), tone_type) * amp
            v = int(max(-1.0, min(1.0, s)) * 32767)
            period_bytes += int(v).to_bytes(2, byteorder="little", signed=True)

        # Silence segment
        if off_frames > 0:
            period_bytes += b"\x00\x00" * off_frames

        period_len_frames = on_frames + off_frames
        if period_len_frames <= 0:
            period_bytes = b"\x00\x00" * 1024
            period_len_frames = 1024

        frames_written = 0
        while frames_written + period_len_frames <= total_frames:
            wf.writeframes(period_bytes)
            frames_written += period_len_frames

        remaining = total_frames - frames_written
        if remaining > 0:
            wf.writeframes(period_bytes[: remaining * 2])

    os.replace(tmp_path, PATTERN_WAV)


# ----------------------------
# Audio loop process management
# ----------------------------
def _which_player():
    p = shutil.which("paplay")
    if p:
        return ("paplay", p)
    p = shutil.which("pw-play")
    if p:
        return ("pw-play", p)
    return (None, None)


def _start_audio_loop(player_path: str) -> subprocess.Popen:
    # Loop continuously; stderr goes to AUDIO_ERR; stdout discarded.
    try:
        open(AUDIO_ERR, "a").close()
    except Exception:
        pass

    cmd = 'exec 2>>"{err}"; while true; do "{player}" "{wav}"; done'.format(
        err=AUDIO_ERR, player=player_path, wav=PATTERN_WAV
    )

    return subprocess.Popen(
        ["/bin/sh", "-lc", cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),  # keep systemd env (XDG_RUNTIME_DIR, PULSE_SERVER)
    )


def _stop_proc(p: Optional[subprocess.Popen]) -> None:
    if not p:
        return
    try:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=0.6)
            except Exception:
                pass
        if p.poll() is None:
            p.kill()
    except Exception:
        pass


# ----------------------------
# Non-blocking stdin reader
# ----------------------------
class StdinReader:
    def __init__(self):
        self.sel = selectors.DefaultSelector()
        self.fd = sys.stdin.fileno()
        try:
            os.set_blocking(self.fd, False)
        except Exception:
            pass
        self.sel.register(self.fd, selectors.EVENT_READ)
        self.buf = b""

    def poll_lines(self, timeout: float = 0.0):
        out = []
        try:
            events = self.sel.select(timeout)
        except Exception:
            return out

        for key, _ in events:
            if key.fd != self.fd:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            except Exception:
                continue
            if not chunk:
                continue
            self.buf += chunk

        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            s = line.decode("utf-8", errors="ignore").strip().lower()
            if s:
                out.append(s)
        return out


# ----------------------------
# Button handling
# ----------------------------
def _move_cursor(st: TGState, direction: int) -> None:
    try:
        i = ROWS.index(st.cursor)
    except Exception:
        i = 0
    i = (i + direction) % len(ROWS)
    st.cursor = ROWS[i]


def _apply_forward(st: TGState) -> None:
    c = st.cursor
    if c == "tone_type":
        st.tone_type = _next_in_list(TONE_TYPES, st.tone_type)
        st._need_audio_restart = True
        _toast_throttle(st, "Tone: {0}".format(st.tone_type))
    elif c == "freq":
        step = _freq_step(st.freq_hz)
        st.freq_hz = _clamp(st.freq_hz + step, 20, 20000)
        st._need_audio_restart = True
        _toast_throttle(st, "Freq: {0}Hz".format(st.freq_hz))
    elif c == "pulse_ms":
        st.pulse_ms = _next_in_list(PULSE_MS_OPTIONS, st.pulse_ms)
        st._need_audio_restart = True
        _toast_throttle(st, "Sweep: {0}ms".format(st.pulse_ms))
    elif c == "volume":
        st.volume = _clamp(st.volume + 5, 0, 100)
        st._need_audio_restart = True
        _toast_throttle(st, "Vol: {0}%".format(st.volume))
    elif c == "play":
        st.playing = not st.playing
        st._need_audio_restart = True
        _toast_throttle(st, "PLAY" if st.playing else "STOP")


def _apply_reverse(st: TGState) -> None:
    c = st.cursor
    if c == "tone_type":
        st.tone_type = _prev_in_list(TONE_TYPES, st.tone_type)
        st._need_audio_restart = True
        _toast_throttle(st, "Tone: {0}".format(st.tone_type))
    elif c == "freq":
        step = _freq_step(st.freq_hz)
        st.freq_hz = _clamp(st.freq_hz - step, 20, 20000)
        st._need_audio_restart = True
        _toast_throttle(st, "Freq: {0}Hz".format(st.freq_hz))
    elif c == "pulse_ms":
        st.pulse_ms = _prev_in_list(PULSE_MS_OPTIONS, st.pulse_ms)
        st._need_audio_restart = True
        _toast_throttle(st, "Sweep: {0}ms".format(st.pulse_ms))
    elif c == "volume":
        st.volume = _clamp(st.volume - 5, 0, 100)
        st._need_audio_restart = True
        _toast_throttle(st, "Vol: {0}%".format(st.volume))
    elif c == "play":
        if st.playing:
            st.playing = False
            st._need_audio_restart = True
            _toast_throttle(st, "STOP")


# ----------------------------
# Main
# ----------------------------
def main() -> int:
    exiting = {"flag": False}

    def _sig_handler(_signo, _frame):
        exiting["flag"] = True

    try:
        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGINT, _sig_handler)
    except Exception:
        pass

    st = TGState()
    reader = StdinReader()

    player_kind, player_path = _which_player()

    _hello()

    if not player_kind or not player_path:
        st.ready = False
        _emit(_state_msg(st))
        _fatal("Audio player not available (need paplay or pw-play)")
    else:
        st.ready = True
        _emit(_state_msg(st))

    audio_proc = None  # type: Optional[subprocess.Popen]

    # initialize pattern wav
    try:
        _write_pattern_wav(st.tone_type, st.freq_hz, st.volume, st.pulse_ms)
    except Exception as e:
        _log_err("WAV init failed: {0!r}".format(e))
        st.ready = False
        st.playing = False
        _emit(_state_msg(st))
        _fatal("Failed to initialize tone pattern")

    last_hb = 0.0

    while True:
        now = time.monotonic()

        if exiting["flag"]:
            st.playing = False
            st._need_audio_restart = True

        # read stdin commands (non-blocking)
        for cmd in reader.poll_lines(timeout=0.0):
            if cmd == "up":
                _move_cursor(st, -1)
                _emit(_state_msg(st))
            elif cmd == "down":
                _move_cursor(st, +1)
                _emit(_state_msg(st))
            elif cmd == "select":
                _apply_forward(st)
                _emit(_state_msg(st))
            elif cmd == "select_hold":
                _apply_reverse(st)
                _emit(_state_msg(st))
            elif cmd == "back":
                st.playing = False
                st._need_audio_restart = True
                exiting["flag"] = True

        # audio mgmt (fast, non-blocking)
        try:
            if st._need_audio_restart:
                st._need_audio_restart = False

                _stop_proc(audio_proc)
                audio_proc = None

                if st.playing and st.ready and player_path:
                    try:
                        _write_pattern_wav(st.tone_type, st.freq_hz, st.volume, st.pulse_ms)
                    except Exception as e:
                        _log_err("WAV regen failed: {0!r}".format(e))
                        st.playing = False
                        _emit(_state_msg(st))
                        _fatal("Failed to generate tone pattern")
                    else:
                        audio_proc = _start_audio_loop(player_path)

            # if it died unexpectedly, recover
            if st.playing and audio_proc and (audio_proc.poll() is not None):
                _log_err("Audio loop exited unexpectedly; restarting")
                st._need_audio_restart = True

        except Exception as e:
            _log_err("Audio mgmt exception: {0!r}".format(e))
            _fatal("Audio error; stopping playback")
            st.playing = False
            st._need_audio_restart = True

        # heartbeat (at least every 250ms)
        if now - last_hb >= HEARTBEAT_S:
            last_hb = now
            _emit(_state_msg(st))

        if exiting["flag"]:
            _stop_proc(audio_proc)
            _emit({"type": "exit"})
            return 0

        time.sleep(0.01)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        # Never leak tracebacks to stdout
        _log_err("FATAL unhandled: {0!r}".format(e))
        _fatal("Unhandled error in tone generator")
        try:
            _emit({"type": "exit"})
        except Exception:
            pass
        raise SystemExit(1)
