#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, time, math, argparse, gc, json
from pathlib import Path

def _add_path(p: str):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

# Repo root is one level above matrix-clock/ — clock_utils.py lives there
_add_path(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

# Import shared clock constants
try:
    from clock_utils import (
        CLOCK_CENTER, CLOCK_RADIUS, NUM_TICKS,
        RING_COLOR, TICK_COLOR, NUMBER_COLOR,
        HOUR_HAND_COLOR, MINUTE_HAND_COLOR, CENTER_COLOR,
        HOUR_HAND_LENGTH, MINUTE_HAND_LENGTH,
        HOUR_HAND_WIDTH, MINUTE_HAND_WIDTH, CENTER_DOT_RADIUS,
        polar_point, get_hand_angles
    )
    USE_CLOCK_UTILS = True
except ImportError:
    USE_CLOCK_UTILS = False
    # Fallback to local definitions
    def polar_point(cx, cy, r, deg):
        a = math.radians(deg)
        return (cx + int(round(r * math.cos(a))), cy + int(round(r * math.sin(a))))

from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

_HOME = os.environ.get("HOME", "/home/pi_two")
FONT_ROOTS = [
    f"{_HOME}/mlb-led-scoreboard/assets/fonts/patched",
    f"{_HOME}/mlb-led-scoreboard/assets/fonts",
    f"{_HOME}/mlb-led-scoreboard/rpi-rgb-led-matrix/fonts",
    f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/fonts",
    f"{_HOME}/rpi-rgb-led-matrix/fonts",
]

# Try to import requests for backend fetching, but don't require it
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

BACKEND_URL = os.environ.get("BACKEND_URL", "https://matrix-backend-lv4k.onrender.com")

def find_font_path(candidates):
    for root in FONT_ROOTS:
        for name in candidates:
            p = os.path.join(root, name)
            if os.path.exists(p):
                return p
    return None

def load_font(candidates):
    f = graphics.Font()
    p = find_font_path(candidates)
    if p:
        f.LoadFont(p)
        return f
    pf = find_font_path(["6x10.bdf"]) or find_font_path(["5x8.bdf"])
    if pf:
        f.LoadFont(pf)
    return f

def parse_args():
    ap = argparse.ArgumentParser(prog="MatrixClock")
    ap.add_argument("--brightness", type=int, default=None)
    ap.add_argument("--pixel-mapper", type=str, default=None)
    ap.add_argument("--hardware-mapping", type=str, default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown", type=int, default=2)
    ap.add_argument("--clock-type", type=str, default=None, choices=["digital", "analog"])
    return ap.parse_args()

def clear_canvas(canvas):
    black = graphics.Color(0, 0, 0)
    for y in range(64):
        graphics.DrawLine(canvas, 0, y, 63, y, black)

def draw_analog_clock(canvas, fnt):
    """Draw analog clock face with hour/minute hands using shared constants"""
    cx, cy = CLOCK_CENTER if USE_CLOCK_UTILS else (32, 32)
    radius = CLOCK_RADIUS if USE_CLOCK_UTILS else 30

    # Draw outer ring
    ring = graphics.Color(*RING_COLOR) if USE_CLOCK_UTILS else graphics.Color(220, 230, 255)
    graphics.DrawCircle(canvas, cx, cy, radius, ring)

    # Draw tick marks (12 hourly marks)
    tick_color = graphics.Color(*TICK_COLOR) if USE_CLOCK_UTILS else graphics.Color(102, 102, 102)
    for i in range(NUM_TICKS if USE_CLOCK_UTILS else 12):
        ang = i * 30
        r1 = radius - 5
        r2 = radius
        x1, y1 = polar_point(cx, cy, r1, ang - 90)
        x2, y2 = polar_point(cx, cy, r2, ang - 90)
        graphics.DrawLine(canvas, x1, y1, x2, y2, tick_color)

    # Hour numbers
    num_color = graphics.Color(*NUMBER_COLOR) if USE_CLOCK_UTILS else graphics.Color(204, 204, 204)
    fnt_small = load_font(["5x8.bdf", "4x6.bdf", "6x10.bdf"])

    # Draw 12, 3, 6, 9
    def tw(txt):
        return graphics.DrawText(canvas, fnt_small, -9999, -9999, num_color, txt)

    w12 = tw("12")
    graphics.DrawText(canvas, fnt_small, 32 - w12 // 2, 12, num_color, "12")

    w3 = tw("3")
    graphics.DrawText(canvas, fnt_small, 64 - 6 - w3, 35, num_color, "3")

    w6 = tw("6")
    graphics.DrawText(canvas, fnt_small, 32 - w6 // 2, 59, num_color, "6")

    graphics.DrawText(canvas, fnt_small, 6, 35, num_color, "9")

    # Get current time
    t = time.localtime()
    if USE_CLOCK_UTILS:
        hour_angle, min_angle = get_hand_angles(t.tm_hour, t.tm_min)
    else:
        hour_angle = ((t.tm_hour % 12) + t.tm_min / 60.0) * 30 - 90
        min_angle = (t.tm_min % 60) * 6 - 90

    # Colors
    hour_col = graphics.Color(*HOUR_HAND_COLOR) if USE_CLOCK_UTILS else graphics.Color(255, 210, 120)
    min_col = graphics.Color(*MINUTE_HAND_COLOR) if USE_CLOCK_UTILS else graphics.Color(120, 200, 255)
    center_col = graphics.Color(*CENTER_COLOR) if USE_CLOCK_UTILS else graphics.Color(255, 255, 255)

    # Hand lengths
    hour_len = HOUR_HAND_LENGTH if USE_CLOCK_UTILS else 18
    min_len = MINUTE_HAND_LENGTH if USE_CLOCK_UTILS else 26

    # Draw minute hand
    mx, my = polar_point(cx, cy, min_len, min_angle)
    graphics.DrawLine(canvas, cx, cy, mx, my, min_col)

    # Draw hour hand (thicker)
    hx, hy = polar_point(cx, cy, hour_len, hour_angle)
    graphics.DrawLine(canvas, cx, cy, hx, hy, hour_col)
    graphics.DrawLine(canvas, cx + 1, cy, hx + 1, hy, hour_col)

    # Center dot
    graphics.DrawCircle(canvas, cx, cy, CENTER_DOT_RADIUS if USE_CLOCK_UTILS else 2, center_col)

def draw_digital_clock(canvas, fnt_big):
    """Draw digital clock using graphics library"""
    white = graphics.Color(255, 255, 255)
    gray = graphics.Color(150, 150, 150)

    t = time.localtime()

    # Format time: HH:MM
    hour = t.tm_hour
    minute = t.tm_min

    # Convert to 12-hour format
    if hour == 0:
        hour = 12
    elif hour > 12:
        hour = hour - 12

    time_str = f"{hour}:{minute:02d}"
    date_str = f"{t.tm_mon}/{t.tm_mday}"

    # Draw time (centered)
    w = graphics.DrawText(canvas, fnt_big, -9999, -9999, white, time_str)
    x = (64 - w) // 2
    y = 28  # Centered vertically
    graphics.DrawText(canvas, fnt_big, x, y, white, time_str)

    # Draw date below
    w2 = graphics.DrawText(canvas, fnt_big, -9999, -9999, gray, date_str)
    x2 = (64 - w2) // 2
    graphics.DrawText(canvas, fnt_big, x2, y + 16, gray, date_str)

def fetch_clock_settings():
    """Fetch clock type setting from backend"""
    if not HAS_REQUESTS:
        return "analog"  # Default to analog if no requests

    try:
        resp = requests.get(f"{BACKEND_URL}/clock-settings", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("clock_type", "analog")
    except Exception:
        pass
    return "analog"

def main():
    args = parse_args()

    opts = RGBMatrixOptions()
    opts.rows = 64
    opts.cols = 64
    opts.hardware_mapping = args.hardware_mapping
    if args.brightness is not None:
        opts.brightness = max(0, min(100, int(args.brightness)))
    opts.gpio_slowdown = int(args.gpio_slowdown)
    opts.limit_refresh_rate_hz = 0
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper
    opts.drop_privileges = False

    matrix = RGBMatrix(options=opts)
    off = matrix.CreateFrameCanvas()

    # Load bigger fonts
    fnt = load_font(["10x20.bdf", "9x18.bdf", "8x13.bdf", "7x13.bdf", "6x12.bdf"])
    fnt_big = load_font(["10x20.bdf", "9x18.bdf", "8x13.bdf", "7x13.bdf", "6x12.bdf"])

    last_hb = 0.0
    last_settings_fetch = 0.0

    # Use command line arg if provided, otherwise fetch from backend
    clock_type = args.clock_type if args.clock_type else "analog"

    frame_ctr = 0

    try:
        while True:
            now = time.time()

            # Heartbeat
            if now - last_hb > 30.0:
                try:
                    with open("/tmp/matrix-heartbeat-3", "w") as f:
                        f.write(str(now))
                except Exception:
                    pass
                last_hb = now

            # Fetch settings from backend every 30 seconds (if no command line arg)
            if args.clock_type is None and now - last_settings_fetch > 30.0:
                clock_type = fetch_clock_settings()
                last_settings_fetch = now

            try:
                clear_canvas(off)
                if clock_type == "digital":
                    draw_digital_clock(off, fnt_big)
                else:
                    draw_analog_clock(off, fnt)
                off = matrix.SwapOnVSync(off)
            except Exception as e:
                sys.stderr.write(f"[clock] draw error: {e}\n")

            frame_ctr += 1
            if frame_ctr % 600 == 0:
                gc.collect()

            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()