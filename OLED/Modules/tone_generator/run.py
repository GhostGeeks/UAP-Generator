#!/usr/bin/env python3
"""
BlackBox Tone Generator (continuous tone, headless JSON stdout protocol)

RULES:
- MUST NOT import luma.oled or access OLED
- MUST NOT manage Bluetooth
- MUST NEVER print non-JSON to stdout (no debug prints, no tracebacks)
- MUST NOT block stdin loop (non-blocking os.read + selectors)
- Communicate ONLY via JSON lines over stdout
- Exit cleanly on back (stop audio first)
- Pi-friendly audio: generate a continuous-tone WAV pattern and loop it with paplay/pw-play in one background process
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
MODULE_VERSION = "tg_v2_continuous"

# Files
PATTERN_WAV = "/tmp/blackbox_tone_pattern.wav"
AUDIO_ERR = "/tmp/blackbox_tone_audio.err"
MODULE_ERR = "/tmp/blackbox_tone_module.err"

# Audio params
RATE = 48000
CHANNELS = 1
SAMPWIDTH = 2  # 16-bit
PATTERN_SECONDS = 8.0  # long enough so paplay overhead is low

# UX rows (cursor)
ROWS = ["tone_type", "freq", "volume", "play"]
TONE_TYPES = ["sine", "square", "saw", "triangle"]

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
    playing: bool = False
    cursor: str = "freq"
    ready: bool = False

    _need_audio_restart: bool = False
    _last_toast_t: float = 0.0


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def _freq_step(freq: int) -> int:
    # “musically sensible” stepping
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
        "playing": bool(st.playing),
        "cursor": st.cursor,
    }


def _toast_throttle(st: TGState, msg: str) -> None:
    now = time.monotonic()
    if now - st._last_toast_t >= TOAST_MIN_INTERVAL_S:
        st._last_toast_t = now
        _toast(msg)


# ----------------------------
# WAV generation (continuous tone pattern)
# ----------------------------
def _waveform_sample(phase: float, kind: str) -> float:
    # phase: 0..1
    if kind == "sine":
        return math.sin(2.0 * math.pi * phase)
    if kind == "square":
        return 1.0 if phase < 0.5 else -1.0
    if kind == "saw":
        return 2.0 * phase - 1.0
    if kind == "triangle":
        return 1.0 - 4.0 * abs(phase - 0.5)
    return math.sin(2.0 * math.pi * phase)


def _write_pattern_wav(tone_type: str, freq_hz: int, volume: int) -> None:
    """
    Generates /tmp/blackbox_tone_pattern.wav:
    - total duration PATTERN_SECONDS
    - continuous tone (no gating)
    """
    freq_hz = _clamp(int(freq_hz), 20, 20000)
    volume = _clamp(int(volume), 0, 100)
    if tone_type not in TONE_TYPES:
        tone_type = "sine"

    total_frames = int(RATE * PATTERN_SECONDS)
    amp = (volume / 100.0) * 0.95  # headroom

    tmp_path = PATTERN_WAV + ".tmp"
    with wave.open(tmp_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPWIDTH)
        wf.setframerate(RATE)

        # Stream frames to avoid large memory usage
        # Maintain phase accumulator for stable waveform
        phase = 0.0
        phase_inc = float(freq_hz) / float(RATE)  # cycles per sample

        block = bytearray()
        block_target_frames = 2048  # small-ish blocks for speed/memory balance

        for _ in range(total_frames):
            s = _waveform_sample(phase % 1.0, tone_type) * amp
            v = int(max(-1.0, min(1.0, s)) * 32767)
            block += int(v).to_bytes(2, byteorder="little", signed=True)

            phase += phase_inc
            if len(block) >= block_target_frames * 2:
                wf.writeframes(block)
                block.clear()

        if block:
            wf.writeframes(block)

    os.replace(tmp_path, PATTERN_WAV)


# ----------------------------
# Audio loop process management
# ----------------------------
def _which_player():
    # Prefer paplay (Pulse/PipeWire-Pulse), fallback pw-play
    p = shutil.which("paplay")
    if p:
        return p
    p = shutil.which("pw-play")
    if p:
        return p
    return None


def _start_audio_loop(player_path: str) -> subprocess.Popen:
    """
    Start one background shell loop that repeatedly plays the pattern WAV.
    Stderr for loop/player goes to AUDIO_ERR.
    """
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
        env=os.environ.copy(),  # keep systemd audio env
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
    elif c == "volume":
        st.volume = _clamp(st.volume - 5, 0, 100)
        st._need_audio_restart = True
        _toast_throttle(st, "Vol: {0}%".format(st.volume))
    elif c == "play":
        # hold on Play means “stop”
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

    player_path = _which_player()

    _hello()

    if not player_path:
        st.ready = False
        _emit(_state_msg(st))
        _fatal("Audio player not available (need paplay or pw-play)")
    else:
        st.ready = True
        _emit(_state_msg(st))

    audio_proc = None  # type: Optional[subprocess.Popen]

    # Initialize pattern wav
    try:
        _write_pattern_wav(st.tone_type, st.freq_hz, st.volume)
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

        # Read stdin commands (non-blocking)
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

        # Audio management (fast, non-blocking)
        try:
            if st._need_audio_restart:
                st._need_audio_restart = False

                _stop_proc(audio_proc)
                audio_proc = None

                if st.playing and st.ready and player_path:
                    try:
                        _write_pattern_wav(st.tone_type, st.freq_hz, st.volume)
                    except Exception as e:
                        _log_err("WAV regen failed: {0!r}".format(e))
                        st.playing = False
                        _emit(_state_msg(st))
                        _fatal("Failed to generate tone pattern")
                    else:
                        audio_proc = _start_audio_loop(player_path)

            # If loop dies unexpectedly while playing, recover
            if st.playing and audio_proc and (audio_proc.poll() is not None):
                _log_err("Audio loop exited unexpectedly; restarting")
                st._need_audio_restart = True

        except Exception as e:
            _log_err("Audio mgmt exception: {0!r}".format(e))
            _fatal("Audio error; stopping playback")
            st.playing = False
            st._need_audio_restart = True

        # Heartbeat at least every 250ms
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
        _log_err("FATAL unhandled: {0!r}".format(e))
        _fatal("Unhandled error in tone generator")
        try:
            _emit({"type": "exit"})
        except Exception:
            pass
        raise SystemExit(1)
