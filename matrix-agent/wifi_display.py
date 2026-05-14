#!/usr/bin/env python3
"""
WiFi provisioning display — shown on the LED matrix while Matrix-Setup
hotspot is active. Draws a WiFi arc symbol and scrolls connection info.

Run as a subprocess from wifi_setup.py (never directly — needs sudo).
Killed by wifi_setup.py when provisioning completes.
"""

import os
import sys
import time
import signal

# ── Matrix setup ─────────────────────────────────────────────────────────────
try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics
except ImportError:
    print("[wifi_display] rgbmatrix not available — exiting", flush=True)
    sys.exit(0)

_REAL_USER = os.environ.get("SUDO_USER") or os.environ.get("USER") or "pi"
_HOME      = os.path.expanduser(f"~{_REAL_USER}")
FONT_PATH  = os.path.join(_HOME, "rpi-rgb-led-matrix", "fonts", "5x7.bdf")

def make_matrix():
    opts = RGBMatrixOptions()
    opts.rows                      = 64
    opts.cols                      = 64
    opts.hardware_mapping          = "adafruit-hat-pwm"
    opts.gpio_slowdown             = 2
    opts.disable_hardware_pulsing  = False
    opts.drop_privileges           = False
    return RGBMatrix(options=opts)

# ── Drawing helpers ───────────────────────────────────────────────────────────

def _draw_wifi(canvas, cx, cy, color_outer, color_mid, color_inner, color_dot):
    """
    Draw a classic WiFi symbol centred at (cx, cy).
    Three arcs + a dot, using a simple rasterised approach.
    """
    import math

    def draw_arc(canvas, cx, cy, r, thickness, col, angle_start, angle_end):
        steps = max(60, r * 4)
        for i in range(steps + 1):
            a = math.radians(angle_start + (angle_end - angle_start) * i / steps)
            for dr in range(thickness):
                x = int(round(cx + (r + dr) * math.cos(a)))
                y = int(round(cy - (r + dr) * math.sin(a)))
                if 0 <= x < 64 and 0 <= y < 64:
                    canvas.SetPixel(x, y, col[0], col[1], col[2])

    # arcs sweep from 210° to 330° (bottom-centre fan, opening upward)
    draw_arc(canvas, cx, cy, 18, 2, color_outer, 30, 150)
    draw_arc(canvas, cx, cy, 12, 2, color_mid,   40, 140)
    draw_arc(canvas, cx, cy,  6, 2, color_inner,  50, 130)

    # dot
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            if dx*dx + dy*dy <= 4:
                px, py = cx + dx, cy + dy
                if 0 <= px < 64 and 0 <= py < 64:
                    canvas.SetPixel(px, py, color_dot[0], color_dot[1], color_dot[2])


def run():
    matrix = make_matrix()
    canvas = matrix.CreateFrameCanvas()

    # Load font (fall back gracefully)
    font = None
    if os.path.exists(FONT_PATH):
        font = graphics.Font()
        font.LoadFont(FONT_PATH)

    WHITE  = (255, 255, 255)
    BLUE   = ( 80, 160, 255)
    LBLUE  = (140, 200, 255)
    GRAY   = (160, 160, 160)

    # Scroll text
    scroll_text = "  Matrix-Setup  pw: matrix1234  visit 10.42.0.1:8080  "
    scroll_pos  = 64
    frame       = 0
    pulse_chars = ["|", "/", "-", "\\"]

    # Graceful exit on SIGTERM
    _running = [True]
    def _stop(sig, frm):
        _running[0] = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    while _running[0]:
        canvas.Clear()

        # ── WiFi symbol (top half, centred) ──────────────────────────────
        # Animate: pulse the outer arc on every 30 frames
        pulse = (frame // 30) % 2 == 0
        outer_col = BLUE if pulse else LBLUE
        _draw_wifi(canvas, 32, 30, outer_col, LBLUE, WHITE, WHITE)

        # ── Scrolling text (bottom) ───────────────────────────────────────
        if font:
            text_color = graphics.Color(200, 200, 200)
            graphics.DrawText(canvas, font, scroll_pos, 58, text_color, scroll_text)
            scroll_pos -= 1
            text_width = len(scroll_text) * 5   # approx for 5x7 font
            if scroll_pos + text_width < 0:
                scroll_pos = 64
        else:
            # No font — just show a static "WiFi Setup" indicator
            pass

        canvas = matrix.SwapOnVSync(canvas)
        frame += 1
        time.sleep(0.04)   # ~25 fps

    canvas.Clear()
    matrix.SwapOnVSync(canvas)
    print("[wifi_display] exited cleanly", flush=True)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"[wifi_display] error: {e}", flush=True)
        sys.exit(1)
