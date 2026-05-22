#!/usr/bin/env python3
"""
Stopwatch display for 64x64 RGB LED matrix.

Receives the start time as --start-time (Unix timestamp).
Counts up from that time until killed.
If --start-time is 0, shows 00:00 waiting to start.

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

HEARTBEAT = "/tmp/matrix-heartbeat-12"

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

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-time",      type=float, default=0.0,
                    help="Unix timestamp when stopwatch started (0 = not started)")
    ap.add_argument("--hardware-mapping", default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown",   type=int,   default=2)
    ap.add_argument("--brightness",      type=int,   default=60)
    ap.add_argument("--pixel-mapper",    default=None)
    return ap.parse_args()

def _elapsed_str(elapsed: float) -> str:
    total = int(elapsed)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def _draw_centered(canvas, font, text, color_rgb, y):
    col = graphics.Color(*color_rgb)
    w = graphics.DrawText(canvas, font, -9999, -9999, col, text)
    x = max(0, (64 - w) // 2)
    graphics.DrawText(canvas, font, x, y, col, text)

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

    start_time = args.start_time
    last_hb    = 0.0
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

        if start_time <= 0:
            # Not started — show idle state
            label = "STOPWATCH"
            time_str = "00:00"
            col = (60, 60, 60)
            label_col = (40, 40, 40)
        else:
            elapsed = max(0.0, now - start_time)
            time_str = _elapsed_str(elapsed)
            label = "STOPWATCH"
            label_col = (50, 180, 80)

            # Color shifts: green → cyan → white as time progresses (every 5 min)
            phase = (elapsed % 300) / 300.0
            if phase < 0.5:
                t = phase * 2
                r = int(50 * t)
                g = int(180 + 75 * t)
                b = int(80 + 175 * t)
            else:
                t = (phase - 0.5) * 2
                r = int(50 + 205 * t)
                g = int(255)
                b = int(255)
            col = (r, g, b)

        # Label
        _draw_centered(canvas, fnt_small, label, label_col, 10)

        # Elapsed time
        _draw_centered(canvas, fnt_big, time_str, col, 40)

        canvas = matrix.SwapOnVSync(canvas)
        frame_ctr += 1
        if frame_ctr % 500 == 0:
            gc.collect()

        time.sleep(0.05)  # 20 fps

    canvas.Clear()
    matrix.SwapOnVSync(canvas)
    print("[stopwatch] exited cleanly", flush=True)


if __name__ == "__main__":
    main()
