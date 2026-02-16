#!/usr/bin/env python3
"""
BlackBox - Noise Generator (headless module)

Adds "sweep" pulsed static with adjustable pulse rate (150/200/250/300ms).
- JSON-only stdout (NEVER print anything else)
- Non-blocking stdin (selectors + os.read)
- Heartbeat state at least every 250ms
- Playback via background process (paplay preferred, then pw-play, then aplay)
- Capture audio errors to /tmp/blackbox_noise_audio.err (never stdout)
- Exit immediately on back (stop playback first)
"""

import errno
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import wave
import random
from typing import Optional, Dict, Any, List, Tuple

MODULE_NAME = "noise_generator"
MODULE_VERSION = "ng_v1_sweep"

HEARTBEAT_S = 0.25
TICK_S = 0.05

SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2  # int16

# Continuous noise loop duration (longer = fewer loop seams)
WAV_SECONDS = 10.0

# Sweep "tick" size (ms). Pulse rate controls spacing between ticks.
TICK_MS = 70

TMP_PREFIX = "blackbox_noise_"
AUDIO_ERR_LOG = "/tmp/blackbox_noise_audio.err"

PULSE_OPTIONS_MS = [150, 200, 250, 300]

MODES: List[str] = ["white", "pink", "brown", "sweep"]
MODE_LABEL = {"white": "White", "pink": "Pink", "brown": "Brown", "sweep": "Sweep"}


def _emit(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _safe_err(e: BaseException) -> str:
    s = (str(e) or e.__class__.__name__).strip()
    return s[:200]


def _which(cmd: str) -> Optional[str]:
    for p in os.environ.get("PATH", "").split(":"):
        fp = os.path.join(p, cmd)
        if os.path.isfile(fp) and os.access(fp, os.X_OK):
            return fp
    return None


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def _int16(x: float) -> int:
    if x < -1.0:
        x = -1.0
    elif x > 1.0:
        x = 1.0
    return int(x * 32767.0)


def _gen_white(n: int, rng: random.Random) -> List[float]:
    return [rng.uniform(-1.0, 1.0) for _ in range(n)]


def _gen_pink(n: int, rng: random.Random) -> List[float]:
    # Voss-McCartney; lightweight
    rows_n = 16
    rows = [rng.uniform(-1.0, 1.0) for _ in range(rows_n)]
    s = sum(rows)
    out: List[float] = []
    counter = 0
    for _ in range(n):
        counter += 1
        c = counter
        row = 0
        while (c & 1) == 0 and row < rows_n:
            c >>= 1
            row += 1
        if row < rows_n:
            s -= rows[row]
            rows[row] = rng.uniform(-1.0, 1.0)
            s += rows[row]
        white = rng.uniform(-1.0, 1.0)
        out.append((s + white) / (rows_n + 1))
    return out


def _gen_brown(n: int, rng: random.Random) -> List[float]:
    out: List[float] = []
    x = 0.0
    for _ in range(n):
        x += rng.uniform(-1.0, 1.0) * 0.02
        x *= 0.999
        out.append(x)
    mx = max(1e-9, max(abs(v) for v in out))
    scale = 1.0 / mx
    return [v * scale for v in out]


def _apply_fade(mono: List[float], fade_ms: int) -> List[float]:
    if not mono:
        return mono
    fade_n = int(SAMPLE_RATE * (fade_ms / 1000.0))
    fade_n = max(1, min(fade_n, len(mono) // 4))
    N = len(mono)
    out = mono[:]  # copy
    for i in range(N):
        g = 1.0
        if i < fade_n:
            g = i / float(fade_n)
        elif i >= (N - fade_n):
            g = (N - 1 - i) / float(fade_n)
            if g < 0.0:
                g = 0.0
        out[i] = out[i] * g
    return out


def _write_wav(path: str, mono: List[float], volume_pct: int) -> None:
    amp = (volume_pct / 100.0) * 0.8  # headroom
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)

        frames = bytearray()
        for s in mono:
            v = _int16(s * amp)
            b = int(v).to_bytes(2, "little", signed=True)
            frames += b
            frames += b
        wf.writeframes(frames)


def _build_continuous_noise(mode: str, volume: int, seconds: float) -> List[float]:
    n = int(SAMPLE_RATE * seconds)
    rng = random.Random(0xB10C0 + volume + (MODES.index(mode) * 1337))

    if mode == "white":
        mono = _gen_white(n, rng)
    elif mode == "pink":
        mono = _gen_pink(n, rng)
    else:
        mono = _gen_brown(n, rng)

    # Small fades smooth loop edges
    mono = _apply_fade(mono, fade_ms=35)
    return mono


def _build_sweep_ticks(volume: int, tick_ms: int, variants: int = 3) -> List[List[float]]:
    """
    Build a few short "static tick" variants. These are intentionally short and repeated rapidly.
    """
    seconds = max(0.02, tick_ms / 1000.0)
    n = int(SAMPLE_RATE * seconds)

    ticks: List[List[float]] = []
    for k in range(variants):
        rng = random.Random(0x515745 + volume * 17 + k * 999)
        # Use mostly white-like noise but a tad softer and with fade
        mono = _gen_white(n, rng)
        # slight shaping: soften extremes a touch
        mono = [max(-1.0, min(1.0, x * 0.9)) for x in mono]
        mono = _apply_fade(mono, fade_ms=8)
        ticks.append(mono)
    return ticks


class AudioLoop:
    """
    Runs a single background /bin/sh loop process.

    Two modes:
      - continuous: loop a single WAV as fast as possible (EOF restart)
      - sweep: play short tick WAVs and sleep to achieve pulse_ms cadence

    Stderr from the player(s) is appended to AUDIO_ERR_LOG (never stdout).
    """

    def __init__(self) -> None:
        # Prefer Pulse in your systemd env
        for c in ("paplay", "pw-play", "aplay"):
            p = _which(c)
            if p:
                self.player = c
                self.player_path = p
                break
        else:
            self.player = None
            self.player_path = None

        self.proc: Optional[subprocess.Popen] = None
        self.last_exit_code: Optional[int] = None

    def available(self) -> bool:
        return self.player_path is not None

    def backend_name(self) -> str:
        return self.player or "none"

    def _err_tail(self) -> str:
        try:
            if os.path.exists(AUDIO_ERR_LOG):
                with open(AUDIO_ERR_LOG, "rb") as f:
                    data = f.read()[-1024:]
                txt = data.decode("utf-8", errors="ignore").strip()
                return txt[-220:]
        except Exception:
            pass
        return ""

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except Exception:
                pass
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.5:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.02)
            if self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass

        if self.proc and self.proc.poll() is not None:
            self.last_exit_code = self.proc.returncode
        self.proc = None

    def died(self) -> bool:
        return self.proc is not None and (self.proc.poll() is not None)

    def died_reason(self) -> str:
        rc = self.proc.returncode if self.proc else self.last_exit_code
        tail = self._err_tail()
        if rc is None:
            return "unknown"
        return f"rc={rc} {tail}".strip() if tail else f"rc={rc}"

    def _player_cmd(self, wav_path: str) -> str:
        if self.player == "pw-play":
            return f'"{self.player_path}" "{wav_path}"'
        if self.player == "paplay":
            return f'"{self.player_path}" "{wav_path}"'
        # aplay
        return f'"{self.player_path}" -q "{wav_path}"'

    def start_continuous(self, wav_path: str) -> None:
        self.stop()
        self.last_exit_code = None
        if not self.available():
            raise RuntimeError("No audio player found (paplay/pw-play/aplay)")

        try:
            with open(AUDIO_ERR_LOG, "w") as f:
                f.write("")
        except Exception:
            pass

        play_cmd = self._player_cmd(wav_path)
        loop_cmd = f'while true; do {play_cmd} 1>/dev/null 2>>"{AUDIO_ERR_LOG}"; done'

        self.proc = subprocess.Popen(
            ["/bin/sh", "-c", loop_cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
            close_fds=True,
        )

        time.sleep(0.05)
        if self.proc.poll() is not None:
            self.last_exit_code = self.proc.returncode
            raise RuntimeError(f"Audio loop failed ({self.died_reason()})")

    def start_sweep(self, tick_paths: List[str], pulse_ms: int, tick_ms: int) -> None:
        """
        Plays tick files in a tight loop and sleeps to approximate pulse cadence.
        Uses one /bin/sh process; no external random/shuf dependency.
        """
        self.stop()
        self.last_exit_code = None
        if not self.available():
            raise RuntimeError("No audio player found (paplay/pw-play/aplay)")

        if not tick_paths:
            raise RuntimeError("No tick paths provided")

        try:
            with open(AUDIO_ERR_LOG, "w") as f:
                f.write("")
        except Exception:
            pass

        # Sleep between ticks to achieve cadence
        gap_ms = max(0, int(pulse_ms) - int(tick_ms))
        gap_s = gap_ms / 1000.0

        # Round-robin tick selection in shell: i=(i+1)%N
        # Note: bash arithmetic is available via /bin/sh on Debian (dash supports $(( )))
        # Keep it simple with a case ladder.
        n = len(tick_paths)
        # Build a case statement for i
        case_lines = []
        for idx, tp in enumerate(tick_paths):
            play_cmd = self._player_cmd(tp)
            case_lines.append(f'{idx}) {play_cmd} 1>/dev/null 2>>"{AUDIO_ERR_LOG}" ;;')
        case_block = " ".join(case_lines)

        # dash-compatible loop:
        # i=0; while true; do case $i in ... esac; i=$(( (i+1) % n )); sleep gap; done
        loop_cmd = (
            f'i=0; while true; do '
            f'case "$i" in {case_block} esac; '
            f'i=$(( (i+1) % {n} )); '
            + (f'sleep {gap_s:.3f}; ' if gap_s > 0 else "")
            + 'done'
        )

        self.proc = subprocess.Popen(
            ["/bin/sh", "-c", loop_cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
            close_fds=True,
        )

        time.sleep(0.05)
        if self.proc.poll() is not None:
            self.last_exit_code = self.proc.returncode
            raise RuntimeError(f"Sweep loop failed ({self.died_reason()})")


class NoiseModule:
    def __init__(self) -> None:
        self.sel = selectors.DefaultSelector()
        self.stdin_fd = sys.stdin.fileno()
        os.set_blocking(self.stdin_fd, False)
        self.sel.register(self.stdin_fd, selectors.EVENT_READ)

        self.audio = AudioLoop()
        self._stop = False
        self._fatal: Optional[str] = None

        self.page = "main"
        self.mode_idx = 0
        self.mode = MODES[self.mode_idx]
        self.playing = False
        self.volume = 70
        self.loop = True  # informational only for now; always looping behavior

        self.pulse_idx = 1  # default 200ms
        self.pulse_ms = PULSE_OPTIONS_MS[self.pulse_idx]

        # focus cycles: mode -> volume -> pulse -> mode
        self.focus = "mode"
        self._stdin_buf = b""

        # temp paths
        self._tmpdir = tempfile.gettempdir()
        self._wav_path = os.path.join(self._tmpdir, f"{TMP_PREFIX}{os.getpid()}_loop.wav")
        self._tick_paths = [
            os.path.join(self._tmpdir, f"{TMP_PREFIX}{os.getpid()}_tick0.wav"),
            os.path.join(self._tmpdir, f"{TMP_PREFIX}{os.getpid()}_tick1.wav"),
            os.path.join(self._tmpdir, f"{TMP_PREFIX}{os.getpid()}_tick2.wav"),
        ]

        self._last_state_emit = 0.0

    def hello(self) -> None:
        _emit({"type": "hello", "module": MODULE_NAME, "version": MODULE_VERSION})
        _emit({"type": "page", "name": self.page})
        self.emit_state(force=True)

    def toast(self, msg: str) -> None:
        _emit({"type": "toast", "message": str(msg)[:160]})

    def fatal(self, msg: str) -> None:
        self._fatal = str(msg)[:200]
        _emit({"type": "fatal", "message": self._fatal})

    def emit_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_state_emit) < HEARTBEAT_S:
            return
        self._last_state_emit = now
        _emit({
            "type": "state",
            "ready": bool(self._fatal is None),
            "mode": self.mode,
            "playing": bool(self.playing),
            "volume": int(self.volume),
            "duration_s": 0,
            "loop": bool(self.loop),
            "focus": self.focus,
            "pulse_ms": int(self.pulse_ms),
            "backend": self.audio.backend_name(),
        })

    def _write_loop_wav(self) -> None:
        mono = _build_continuous_noise(self.mode, self.volume, WAV_SECONDS)
        _write_wav(self._wav_path, mono, self.volume)

    def _write_tick_wavs(self) -> None:
        ticks = _build_sweep_ticks(self.volume, TICK_MS, variants=len(self._tick_paths))
        for tp, mono in zip(self._tick_paths, ticks):
            _write_wav(tp, mono, self.volume)

    def _start_audio(self) -> None:
        if self.playing:
            return
        if not self.audio.available():
            raise RuntimeError("Audio backend not available (need paplay/pw-play/aplay)")

        if self.mode == "sweep":
            self._write_tick_wavs()
            self.audio.start_sweep(self._tick_paths, self.pulse_ms, TICK_MS)
        else:
            self._write_loop_wav()
            self.audio.start_continuous(self._wav_path)

        self.playing = True

    def _stop_audio(self) -> None:
        self.audio.stop()
        self.playing = False

    def _restart_if_playing(self) -> None:
        if not self.playing:
            return
        self._stop_audio()
        self._start_audio()

    def _set_mode(self, idx: int) -> None:
        self.mode_idx = idx % len(MODES)
        self.mode = MODES[self.mode_idx]
        self.toast(f"Mode: {MODE_LABEL.get(self.mode, self.mode.title())}")
        self._restart_if_playing()

    def _change_mode(self, delta: int) -> None:
        self._set_mode(self.mode_idx + delta)

    def _change_volume(self, delta: int) -> None:
        self.volume = _clamp(self.volume + delta, 0, 100)
        self.toast(f"Volume: {self.volume}")
        self._restart_if_playing()

    def _set_pulse_idx(self, idx: int) -> None:
        self.pulse_idx = idx % len(PULSE_OPTIONS_MS)
        self.pulse_ms = PULSE_OPTIONS_MS[self.pulse_idx]
        self.toast(f"Pulse: {self.pulse_ms}ms")
        if self.mode == "sweep":
            self._restart_if_playing()

    def _change_pulse(self, delta: int) -> None:
        self._set_pulse_idx(self.pulse_idx + delta)

    def _cycle_focus(self) -> None:
        if self.focus == "mode":
            self.focus = "volume"
        elif self.focus == "volume":
            self.focus = "pulse"
        else:
            self.focus = "mode"
        self.toast(f"Adjust: {self.focus}")
        self.emit_state(force=True)

    def handle(self, cmd: str) -> None:
        if cmd == "back":
            try:
                self._stop_audio()
            except Exception:
                pass
            self._stop = True
            _emit({"type": "exit"})
            return

        # after fatal, ignore everything except back
        if self._fatal is not None:
            return

        if cmd == "select":
            try:
                if self.playing:
                    self._stop_audio()
                    self.toast("STOP")
                else:
                    self._start_audio()
                    self.toast("PLAY")
            except Exception as e:
                self.fatal(_safe_err(e))
            finally:
                self.emit_state(force=True)
            return

        if cmd == "select_hold":
            self._cycle_focus()
            return

        if cmd == "up":
            try:
                if self.focus == "mode":
                    self._change_mode(+1)
                elif self.focus == "volume":
                    self._change_volume(+5)
                else:
                    self._change_pulse(+1)
            except Exception as e:
                self.fatal(_safe_err(e))
            finally:
                self.emit_state(force=True)
            return

        if cmd == "down":
            try:
                if self.focus == "mode":
                    self._change_mode(-1)
                elif self.focus == "volume":
                    self._change_volume(-5)
                else:
                    self._change_pulse(-1)
            except Exception as e:
                self.fatal(_safe_err(e))
            finally:
                self.emit_state(force=True)
            return

        # ignore unknown cmd

    def _read_stdin(self) -> None:
        try:
            while True:
                try:
                    chunk = os.read(self.stdin_fd, 4096)
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        break
                    raise
                if not chunk:
                    self._stop = True
                    return
                self._stdin_buf += chunk
                while b"\n" in self._stdin_buf:
                    line, self._stdin_buf = self._stdin_buf.split(b"\n", 1)
                    cmd = line.decode("utf-8", errors="ignore").strip()
                    if cmd:
                        self.handle(cmd)
                        if self._stop:
                            return
        except Exception as e:
            self.fatal(f"stdin error: {_safe_err(e)}")

    def run(self) -> int:
        self.hello()

        while not self._stop:
            try:
                events = self.sel.select(timeout=TICK_S)
                for key, mask in events:
                    if (mask & selectors.EVENT_READ) and key.fileobj == self.stdin_fd:
                        self._read_stdin()

                # detect unexpected audio death
                if self.playing and self.audio.died():
                    reason = self.audio.died_reason()
                    self.playing = False

                    recovered = False
                    for _ in range(3):
                        try:
                            time.sleep(0.15)
                            self._start_audio()
                            recovered = True
                            self.toast("Audio recovered")
                            break
                        except Exception:
                            recovered = False

                    if not recovered:
                        self.fatal(f"Audio playback stopped ({reason})")
                    self.emit_state(force=True)

                self.emit_state(force=False)

            except Exception as e:
                self.fatal(_safe_err(e))
                self.emit_state(force=True)

        return 0


def main() -> int:
    mod = NoiseModule()

    def _sig(_signum, _frame):
        try:
            mod._stop_audio()
        except Exception:
            pass
        mod._stop = True
        try:
            _emit({"type": "exit"})
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        return mod.run()
    finally:
        try:
            mod._stop_audio()
        except Exception:
            pass
        # cleanup temp files
        for p in [mod._wav_path] + mod._tick_paths:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException as e:
        try:
            _emit({"type": "fatal", "message": _safe_err(e)})
            _emit({"type": "exit"})
        except Exception:
            pass
        sys.exit(1)
