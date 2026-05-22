#!/usr/bin/env python3
"""
Countdown timer display for 64x64 RGB LED matrix.

Receives the timer end time as --end-time (Unix timestamp).
Counts down to zero, then flashes "DONE!" until killed.

Run as a subprocess via agent.py — never directly (needs sudo).
"""

import os, sys, time, math, argparse, signal, gc

_HOME = os.environ.get("HOME", "/home/pi_two")

def _add_path(p):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

_add_path(os.path.join(_HOME, "rpi-rgb-led-matrix", "bindings", "python"))
_add_path(os.path.join(_HOME, "rpi-spotify-matrix-display", "rpi-rgb-led-matrix", "bindings", "python"))

from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

FONT_ROOTS = [
    os.path.join(_HOME, "mlb-led-scoreboard", "assets", "fonts", "patched"),
    os.path.join(_HOME, "mlb-led-scoreboard", "rpi-rgb-led-matrix", "fonts"),
    os.path.join(_HOME, "rpi-spotify-matrix-display", "rpi-rgb-led-matrix", "fonts"),
    os.path.join(_HOME, "rpi-rgb-led-matrix", "fonts"),
]

HEARTBEAT = "/tmp/matrix-heartbeat-10"

# ── Font helpers ──────────────────────────────────────────────────────────────

def _find_font(names):
    for root in FONT_ROOTS:
        for name in names:
            p = os.path.join(root, name)
            if os.path.exists(p):
                return p
    return None

def _load_font(names):
    f = graphics.Font()
    p = _find_font(names)
    if p:
        f.LoadFont(p)
    return f

# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end-time",        type=float, default=0.0,
                    help="Unix timestamp when timer expires")
    ap.add_argument("--duration",        type=float, default=0.0,
                    help="Total timer duration in seconds (for progress bar)")
    ap.add_argument("--hardware-mapping", default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown",   type=int,   default=2)
    ap.add_argument("--brightness",      type=int,   default=60)
    ap.add_argument("--pixel-mapper",    default=None)
    return ap.parse_args()

# ── Color helpers ─────────────────────────────────────────────────────────────

def _lerp_color(c1, c2, t):
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )

def _time_color(remaining):
    """White → yellow → orange → red as time runs out."""
    if remaining > 60:
        return (255, 255, 255)       # white
    elif remaining > 30:
        t = (60 - remaining) / 30.0
        return _lerp_color((255, 255, 255), (255, 200, 0), t)  # white → yellow
    elif remaining > 10:
        t = (30 - remaining) / 20.0
        return _lerp_color((255, 200, 0), (255, 80, 0), t)     # yellow → orange
    else:
        # Pulse red/bright-red during last 10 seconds
        pulse = 0.5 + 0.5 * math.sin(time.time() * math.pi * 2)
        v = int(180 + 75 * pulse)
        return (255, v // 4, 0)

# ── Drawing ───────────────────────────────────────────────────────────────────

def _draw_centered_text(canvas, font, text, color_rgb, y):
    """Draw text centred horizontally at row y. Returns width."""
    col = graphics.Color(*color_rgb)
    w = graphics.DrawText(canvas, font, -9999, -9999, col, text)
    x = max(0, (64 - w) // 2)
    graphics.DrawText(canvas, font, x, y, col, text)
    return w

def _draw_progress_bar(canvas, remaining, duration, color_rgb):
    """Draw a thin progress bar at the bottom (rows 60-62)."""
    if duration <= 0:
        return
    frac = max(0.0, min(1.0, remaining / duration))
    bar_w = int(frac * 60)  # max 60 px wide, 2 px margin each side
    bg_col = graphics.Color(40, 40, 40)
    bar_col = graphics.Color(*color_rgb)
    for y in range(60, 63):
        # Background track
        graphics.DrawLine(canvas, 2, y, 61, y, bg_col)
        # Filled portion
        if bar_w > 0:
            graphics.DrawLine(canvas, 2, y, 2 + bar_w - 1, y, bar_col)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    opts = RGBMatrixOptions()
    opts.rows = 64
    opts.cols = 64
    opts.hardware_mapping = args.hardware_mapping
    opts.brightness = max(1, min(100, args.brightness))
    opts.gpio_slowdown = args.gpio_slowdown
    opts.drop_privileges = False
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper

    matrix = RGBMatrix(options=opts)
    canvas = matrix.CreateFrameCanvas()

    fnt_big   = _load_font(["10x20.bdf", "9x18.bdf", "8x13.bdf", "7x13.bdf"])
    fnt_small = _load_font(["5x8.bdf", "4x6.bdf", "6x10.bdf"])

    end_time = args.end_time
    duration = args.duration

    last_hb    = 0.0
    done_frame = 0
    frame_ctr  = 0
    running    = [True]

    def _stop(sig, frm):
        running[0] = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    while running[0]:
        now = time.time()

        # Heartbeat
        if now - last_hb > 30.0:
            try:
                with open(HEARTBEAT, "w") as f:
                    f.write(str(now))
            except Exception:
                pass
            last_hb = now

        canvas.Clear()

        # ── Determine state ───────────────────────────────────────────────────
        if end_time <= 0:
            # No timer configured yet
            label      = "TIMER"
            time_str   = "--:--"
            col        = (80, 80, 80)
            label_col  = (50, 50, 50)
            remaining  = 0.0
            show_bar   = False
        else:
            remaining = max(0.0, end_time - now)
            is_done   = (remaining == 0.0)

            if is_done:
                # Flash "DONE!" alternating red / white
                done_frame += 1
                flash_on = (done_frame // 12) % 2 == 0
                col        = (255, 0, 0) if flash_on else (255, 255, 255)
                label_col  = col
                label      = "DONE!"
                time_str   = "00:00"
                show_bar   = False
            else:
                done_frame = 0
                col        = _time_color(remaining)
                label_col  = (70, 70, 70)
                label      = "TIMER"
                show_bar   = (duration > 0)

                total = int(remaining)
                h = total // 3600
                m = (total % 3600) // 60
                s = total % 60
                if h > 0:
                    time_str = f"{h}:{m:02d}:{s:02d}"
                else:
                    time_str = f"{m:02d}:{s:02d}"

        # ── Draw label ────────────────────────────────────────────────────────
        _draw_centered_text(canvas, fnt_small, label, label_col, 10)

        # ── Draw countdown ────────────────────────────────────────────────────
        _draw_centered_text(canvas, fnt_big, time_str, col,
                            38 if show_bar else 40)

        # ── Progress bar ──────────────────────────────────────────────────────
        if show_bar:
            _draw_progress_bar(canvas, remaining, duration, col)

        canvas = matrix.SwapOnVSync(canvas)
        frame_ctr += 1
        if frame_ctr % 500 == 0:
            gc.collect()

        time.sleep(0.05)  # 20 fps

    canvas.Clear()
    matrix.SwapOnVSync(canvas)
    print("[countdown] exited cleanly", flush=True)


if __name__ == "__main__":
    main()
