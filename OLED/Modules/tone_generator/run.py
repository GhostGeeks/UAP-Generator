# ============================
# Tone Generator (Headless JSON module) runner
# ============================
import os
import json
import time
import selectors
import subprocess
from luma.core.render import canvas

# Toast behavior
_TONE_TOAST_DURATION = 1.2  # seconds
_TONE_FRAME_DT = 0.05       # 20 FPS cap to avoid flicker / unnecessary I2C traffic

def _tone_text_w_px(s: str) -> int:
    # default 6px per char font assumption (like your existing UI)
    return len(s) * 6

def _tone_draw_header(d, title: str, status: str = ""):
    d.text((2, 0), title[:21], fill=255)
    if status:
        s = status[:6]
        x = max(0, 128 - _tone_text_w_px(s) - 2)
        d.text((x, 0), s, fill=255)
    d.line((0, 12, 127, 12), fill=255)

def _tone_draw_footer(d, text: str):
    d.line((0, 52, 127, 52), fill=255)
    d.text((2, 54), text[:21], fill=255)

def _tone_draw_row(d, y: int, text: str, selected: bool):
    marker = ">" if selected else " "
    d.text((0, y), marker, fill=255)
    d.text((10, y), text[:19], fill=255)

def _tone_format_rows(state: dict):
    tone = state.get("tone_type", "sine")
    freq = int(state.get("freq_hz", 440))
    pulse = int(state.get("pulse_ms", 200))
    vol = int(state.get("volume", 70))
    playing = bool(state.get("playing", False))

    # UX labels (match your spec)
    return [
        ("tone_type", f"Tone Type: {tone.title()}"),
        ("freq",      f"Frequency: {freq}Hz"),
        ("pulse_ms",  f"Sweep Rate: {pulse}ms"),
        ("volume",    f"Volume: {vol}%"),
        ("play",      f"Play: {'STOP' if playing else 'PLAY'}"),
    ]

def _tone_render(device, state: dict, toast_msg: str | None):
    rows = _tone_format_rows(state)
    cursor = state.get("cursor", "freq")
    playing = bool(state.get("playing", False))
    ready = bool(state.get("ready", False))

    # show 3 rows at a time
    keys = [k for (k, _) in rows]
    try:
        ci = keys.index(cursor)
    except ValueError:
        ci = 1  # default to frequency row

    # window of 3 rows, centered when possible
    start = max(0, min(ci - 1, len(rows) - 3))
    window = rows[start:start + 3]

    status = "PLAY" if playing else ("RDY" if ready else "ERR")

    with canvas(device) as d:
        _tone_draw_header(d, "Tone Generator", status=status)

        y0 = 14
        row_h = 12
        for i, (k, label) in enumerate(window):
            _tone_draw_row(d, y0 + i * row_h, label, selected=(k == cursor))

        _tone_draw_footer(d, "SEL change  HOLD rev")

        # Toast overlay (draw last so it sits “on top”)
        if toast_msg:
            # Simple dark box with border to reduce flicker/ghosting
            # (SSD1306 doesn't support alpha; we just overwrite that area)
            x0, y0b, x1, y1 = 0, 38, 127, 51
            d.rectangle((x0, y0b, x1, y1), outline=255, fill=0)
            d.text((2, 40), toast_msg[:21], fill=255)

def _tone_nonblocking_readlines(fd: int, buf: bytearray):
    """
    Read available bytes from fd and split into complete lines.
    Returns (lines:list[str], buf:bytearray)
    """
    lines = []
    try:
        chunk = os.read(fd, 4096)
    except BlockingIOError:
        return lines, buf
    except Exception:
        return lines, buf

    if not chunk:
        return lines, buf

    buf.extend(chunk)
    while True:
        nl = buf.find(b"\n")
        if nl < 0:
            break
        raw = bytes(buf[:nl])
        del buf[:nl + 1]
        s = raw.decode("utf-8", errors="ignore").strip()
        if s:
            lines.append(s)
    return lines, buf

def run_tone_generator_module(device, module_dir: str, consume_button_event):
    """
    Headless JSON module runner for tone_generator.
    - Launches run.py with stdout=PIPE
    - Pumps stdout non-blocking via selectors + os.read
    - Forwards button events to child stdin (newline-delimited)
    - Renders OLED UI from state messages
    - Exits on {"type":"exit"} or process end
    """
    run_py = os.path.join(module_dir, "run.py")
    stderr_path = "/tmp/blackbox_tone_module_child.err"
    errf = open(stderr_path, "a", buffering=1)

    proc = subprocess.Popen(
        [sys.executable, run_py],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=errf,
        bufsize=0,           # binary, unbuffered
        close_fds=True,
        env=os.environ.copy()
    )

    # Make fds non-blocking
    os.set_blocking(proc.stdout.fileno(), False)
    if proc.stdin:
        os.set_blocking(proc.stdin.fileno(), False)

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout.fileno(), selectors.EVENT_READ)

    # UI state
    state = {
        "ready": False,
        "tone_type": "sine",
        "freq_hz": 440,
        "volume": 70,
        "pulse_ms": 200,
        "playing": False,
        "cursor": "freq",
    }
    toast_msg = None
    toast_until = 0.0
    stdout_buf = bytearray()

    last_render = 0.0
    last_state_update = 0.0

    def maybe_render(force=False):
        nonlocal last_render, toast_msg
        now = time.monotonic()
        if not force and (now - last_render) < _TONE_FRAME_DT:
            return
        # expire toast
        if toast_msg and now >= toast_until:
            toast_msg = None
        _tone_render(device, state, toast_msg)
        last_render = now

    # initial render (blank until hello/state arrives)
    maybe_render(force=True)

    try:
        while True:
            now = time.monotonic()

            # 1) Pump stdout (non-blocking)
            events = sel.select(timeout=0.0)
            for key, _mask in events:
                if key.fd != proc.stdout.fileno():
                    continue
                lines, stdout_buf_local = _tone_nonblocking_readlines(key.fd, stdout_buf)
                stdout_buf = stdout_buf_local
                for line in lines:
                    try:
                        msg = json.loads(line)
                    except Exception:
                        # Ignore non-JSON (shouldn't happen); never block UI
                        continue

                    mtype = msg.get("type")
                    if mtype == "hello":
                        # optional: could validate module/version
                        pass
                    elif mtype == "page":
                        # only "main" expected
                        pass
                    elif mtype == "state":
                        # merge state
                        for k in ("ready", "tone_type", "freq_hz", "volume", "pulse_ms", "playing", "cursor"):
                            if k in msg:
                                state[k] = msg[k]
                        last_state_update = now
                        maybe_render(force=True)
                    elif mtype == "toast":
                        nonlocal_toast = msg.get("message", "")
                        toast_msg = str(nonlocal_toast)[:40]
                        toast_until = now + _TONE_TOAST_DURATION
                        maybe_render(force=True)
                    elif mtype == "fatal":
                        toast_msg = str(msg.get("message", "Error"))[:40]
                        toast_until = now + 2.0
                        state["ready"] = False
                        state["playing"] = False
                        maybe_render(force=True)
                    elif mtype == "exit":
                        return
                    else:
                        # unknown message types ignored
                        pass

            # 2) Forward button events to child stdin
            ev = consume_button_event()
            if ev and proc.stdin and proc.poll() is None:
                try:
                    proc.stdin.write((ev.strip().lower() + "\n").encode("utf-8"))
                    # no flush needed for pipes; but harmless if you want:
                    # proc.stdin.flush()
                except Exception:
                    pass

            # 3) If child exited, stop
            if proc.poll() is not None:
                return

            # 4) Render periodically even without new state (toast expiry / keep UI fresh)
            # Also prevents a “stuck” look if heartbeat is delayed.
            maybe_render(force=False)

            # 5) Small sleep to reduce CPU while staying responsive
            time.sleep(0.005)

    finally:
        try:
            sel.close()
        except Exception:
            pass
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.5)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            errf.close()
        except Exception:
            pass

# ============================
# Dispatcher hook (example)
# ============================
# In your module launch switch/dispatcher, do something like:
#
# if module_id == "tone_generator":
#     run_tone_generator_module(device, module_dir, consume)
# else:
#     run_legacy_module(...)
#
# (Keep all other modules legacy as requested.)
