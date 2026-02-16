#!/usr/bin/env python3
"""
BlackBox - Noise Generator (headless module) - ALWAYS PULSED (spirit-box style)

- Noise is always played as repeated short "ticks" at a user-selected pulse rate.
- Noise types change the tick texture (white/pink/brown; future types easy to add).
- Pulse rate options: 150/200/250/300 ms (spirit box sweep style)
- JSON-only stdout (NEVER print anything else)
- Non-blocking stdin (selectors + os.read)
- Heartbeat state at least every 250ms
- Playback via one background /bin/sh loop (paplay preferred, then pw-play, then aplay)
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
from typing import Optional, Dict, Any, List

MODULE_NAME = "noise_generator"
MODULE_VERSION = "ng_v2_pulsed"

HEARTBEAT_S = 0.25
TICK_S = 0.05

SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2  # int16

# Length of each "static tick" in milliseconds.
# Pulse rate controls the spacing between ticks.
TICK_MS = 70

TMP_PREFIX = "blackbox_noise_"
AUDIO_ERR_LOG = "/tmp/blackbox_noise_audio.err"

PULSE_OPTIONS_MS = [150, 200, 250, 300]

# Noise types (future values can be added here)
MODES: List[str] = ["white", "pink", "brown"]
MODE_LABEL = {"white": "White", "pink": "Pink", "brown": "Brown"}


def _emit(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _safe_err(e: BaseException) -> str:
    s = (str(e) or e.__class__.__name__).strip()
    return s[:220]


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
    # Voss-McCartney; lightweight and good enough for short ticks
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
    # Integrated noise; normalize for tick window
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
    fade_n = max(1, min(fade_n, len(mono) // 3))
    N = len(mono)
    out = mono[:]
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
    amp = (volume_pct / 100.0) * 0.85  # headroom
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


def _build_tick_variants(mode: str, volume: int, tick_ms: int, variants: int = 3) -> List[List[float]]:
    seconds = max(0.02, tick_ms / 1000.0)
    n = int(SAMPLE_RATE * seconds)
    ticks: List[List[float]] = []

    for k in range(max(1, variants)):
        rng = random.Random(0x515745 + volume * 17 + k * 999 + (MODES.index(mode) * 1337))

        if mode == "white":
            mono = _gen_white(n, rng)
        elif mode == "pink":
            mono = _gen_pink(n, rng)
        else:
            mono = _gen_brown(n, rng)

        # Slight shaping (makes it feel more "radio/static" and less harsh)
        mono = [max(-1.0, min(1.0, x * 0.92)) for x in mono]

        # Fade removes clicks at tick edges
        mono = _apply_fade(mono, fade_ms=8)

        ticks.append(mono)

    return ticks


class AudioLoop:
    """
    One background /bin/sh loop:
      play a tick wav
      sleep gap
      play next tick wav (round-robin)
      ...

    Player stderr is appended to AUDIO_ERR_LOG (never stdout).
    """

    def __init__(self) -> None:
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
        return f'"{self.player_path}" -q "{wav_path}"'

    def start_pulsed(self, tick_paths: List[str], pulse_ms: int, tick_ms: int) -> None:
        self.stop()
        self.last_exit_code = None

        if not self.available():
            raise RuntimeError("No audio player found (paplay/pw-play/aplay)")
        if not tick_paths:
            raise RuntimeError("No tick paths provided")

        # clear error log
        try:
            with open(AUDIO_ERR_LOG, "w") as f:
                f.write("")
        except Exception:
            pass

        gap_ms = max(0, int(pulse_ms) - int(tick_ms))
        gap_s = gap_ms / 1000.0

        # Build a dash-compatible round-robin case statement
        n = len(tick_paths)
        case_lines = []
        for idx, tp in enumerate(tick_paths):
            play_cmd = self._player_cmd(tp)
            case_lines.append(f'{idx}) {play_cmd} 1>/dev/null 2>>"{AUDIO_ERR_LOG}" ;;')
        case_block = " ".join(case_lines)

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
            raise RuntimeError(f"Pulsed loop failed ({self.died_reason()})")


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

        self.pulse_idx = 1  # default 200ms
        self.pulse_ms = PULSE_OPTIONS_MS[self.pulse_idx]

        # Focus order matches your UI rows: Noise Type -> Sweep Rate -> Volume
        self.focus = "mode"  # "mode" | "pulse" | "volume"
        self._stdin_buf = b""

        self._tmpdir = tempfile.gettempdir()
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
        self._fatal = str(msg)[:220]
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
            "pulse_ms": int(self.pulse_ms),
            "focus": self.focus,
            # backend left for logs/debug but you can ignore in UI
            "backend": self.audio.backend_name(),
        })

    def _write_tick_wavs(self) -> None:
        ticks = _build_tick_variants(self.mode, self.volume, TICK_MS, variants=len(self._tick_paths))
        for tp, mono in zip(self._tick_paths, ticks):
            _write_wav(tp, mono, self.volume)

    def _start_audio(self) -> None:
        if self.playing:
            return
        if not self.audio.available():
            raise RuntimeError("Audio backend not available (need paplay/pw-play/aplay)")

        self._write_tick_wavs()
        self.audio.start_pulsed(self._tick_paths, self.pulse_ms, TICK_MS)
        self.playing = True

    def _stop_audio(self) -> None:
        self.audio.stop()
        self.playing = False

    def _restart_if_playing(self) -> None:
        if not self.playing:
            return
        self._stop_audio()
        self._start_audio()

    def _cycle_focus(self) -> None:
        if self.focus == "mode":
            self.focus = "pulse"
        elif self.focus == "pulse":
            self.focus = "volume"
        else:
            self.focus = "mode"
        self.emit_state(force=True)

    def _change_mode(self, delta: int) -> None:
        self.mode_idx = (self.mode_idx + delta) % len(MODES)
        self.mode = MODES[self.mode_idx]
        self.toast(f"Noise Type: {MODE_LABEL.get(self.mode, self.mode.title())}")
        self._restart_if_playing()

    def _change_pulse(self, delta: int) -> None:
        self.pulse_idx = (self.pulse_idx + delta) % len(PULSE_OPTIONS_MS)
        self.pulse_ms = PULSE_OPTIONS_MS[self.pulse_idx]
        self.toast(f"Sweep Rate: {self.pulse_ms}ms")
        self._restart_if_playing()

    def _change_volume(self, delta: int) -> None:
        self.volume = _clamp(self.volume + delta, 0, 100)
        self.toast(f"Volume: {self.volume}%")
        self._restart_if_playing()

    def handle(self, cmd: str) -> None:
        if cmd == "back":
            try:
                self._stop_audio()
            except Exception:
                pass
            self._stop = True
            _emit({"type": "exit"})
            return

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
                elif self.focus == "pulse":
                    self._change_pulse(+1)
                else:
                    self._change_volume(+5)
            except Exception as e:
                self.fatal(_safe_err(e))
            finally:
                self.emit_state(force=True)
            return

        if cmd == "down":
            try:
                if self.focus == "mode":
                    self._change_mode(-1)
                elif self.focus == "pulse":
                    self._change_pulse(-1)
                else:
                    self._change_volume(-5)
            except Exception as e:
                self.fatal(_safe_err(e))
            finally:
                self.emit_state(force=True)
            return

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
        for p in mod._tick_paths:
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
