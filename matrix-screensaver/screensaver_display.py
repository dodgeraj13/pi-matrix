#!/usr/bin/env python3
"""
Screensaver display for 64x64 RGB LED matrix.

Cycles through three animations every 25 seconds:
  0 — Matrix rain   (green falling drops)
  1 — Fire          (classic heat simulation)
  2 — Plasma        (sine-wave colour waves)

Run as a subprocess via agent.py — never directly (needs sudo).
"""

import os, sys, time, math, argparse, signal, gc, random

_HOME = os.environ.get("HOME", "/home/pi_two")

def _add_path(p):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

_add_path(os.path.join(_HOME, "rpi-rgb-led-matrix", "bindings", "python"))
_add_path(os.path.join(_HOME, "rpi-spotify-matrix-display", "rpi-rgb-led-matrix", "bindings", "python"))

from rgbmatrix import RGBMatrix, RGBMatrixOptions

HEARTBEAT = "/tmp/matrix-heartbeat-11"
W, H      = 64, 64
ANIM_SECS = 25   # seconds per animation before cycling

# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hardware-mapping", default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown",   type=int, default=2)
    ap.add_argument("--brightness",      type=int, default=60)
    ap.add_argument("--pixel-mapper",    default=None)
    return ap.parse_args()

# ── Colour helpers ────────────────────────────────────────────────────────────

def _hsv_to_rgb(h, s, v):
    """h 0–360, s/v 0–1 → (r, g, b) 0–255"""
    h = h % 360
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if   h < 60:  r, g, b = c, x, 0
    elif h < 120: r, g, b = x, c, 0
    elif h < 180: r, g, b = 0, c, x
    elif h < 240: r, g, b = 0, x, c
    elif h < 300: r, g, b = x, 0, c
    else:         r, g, b = c, 0, x
    return int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)

# ── Animation 0 — Matrix Rain ─────────────────────────────────────────────────

class MatrixRain:
    NUM_DROPS = 45

    def __init__(self):
        self.drops = []
        for _ in range(self.NUM_DROPS):
            self.drops.append(self._new_drop(random.randint(0, H + 20)))

    def _new_drop(self, y=None):
        return {
            "x":      random.randint(0, W - 1),
            "y":      float(random.randint(-20, 0) if y is None else y),
            "speed":  random.uniform(0.4, 1.8),
            "length": random.randint(4, 16),
            "bright": random.randint(140, 255),
        }

    def reset(self):
        self.__init__()

    def draw(self, canvas):
        for d in self.drops:
            d["y"] += d["speed"]
            if d["y"] - d["length"] > H:
                self.drops[self.drops.index(d)] = self._new_drop()
                continue
            head = int(d["y"])
            length = d["length"]
            bright = d["bright"]
            for i in range(length):
                py = head - i
                if 0 <= py < H:
                    fade = 1.0 - i / length
                    if i == 0:
                        # Bright white-green head
                        r = int(180 * fade)
                        g = 255
                        b = int(180 * fade)
                    else:
                        r = 0
                        g = int(bright * fade * fade)
                        b = 0
                    canvas.SetPixel(d["x"], py, r, g, b)

# ── Animation 1 — Fire ────────────────────────────────────────────────────────

class Fire:
    def __init__(self):
        # heat[y][x], y=0 is top, y=H-1 is bottom (hottest)
        self.heat = [[0] * W for _ in range(H)]

    def reset(self):
        self.heat = [[0] * W for _ in range(H)]

    @staticmethod
    def _heat_to_rgb(h):
        if h < 30:
            return 0, 0, 0
        elif h < 90:
            t = (h - 30) / 60.0
            return int(t * 200), 0, 0
        elif h < 160:
            t = (h - 90) / 70.0
            return 200 + int(t * 55), int(t * 90), 0
        elif h < 220:
            t = (h - 160) / 60.0
            return 255, 90 + int(t * 140), 0
        else:
            t = (h - 220) / 35.0
            t = min(t, 1.0)
            return 255, 230 + int(t * 25), int(t * 120)

    def draw(self, canvas):
        heat = self.heat

        # Seed bottom row with random high heat
        for x in range(W):
            heat[H - 1][x] = random.randint(180, 255)

        # Propagate upward with diffusion + decay
        for y in range(H - 2, -1, -1):
            for x in range(W):
                xl = (x - 1) % W
                xr = (x + 1) % W
                total = (heat[y + 1][xl] + heat[y + 1][x] + heat[y + 1][xr])
                avg   = total // 3
                heat[y][x] = max(0, avg - random.randint(3, 10))

        # Render
        for y in range(H):
            for x in range(W):
                r, g, b = self._heat_to_rgb(heat[y][x])
                canvas.SetPixel(x, y, r, g, b)

# ── Animation 2 — Plasma ─────────────────────────────────────────────────────

class Plasma:
    def __init__(self):
        self.t = 0.0
        # Precompute sin tables to reduce math calls at runtime
        self._sin_x  = [math.sin(x / 5.0) for x in range(W)]
        self._sin_y  = [math.sin(y / 4.0) for y in range(H)]
        self._sin_xy = [math.sin((x + y) / 7.0) for x in range(W) for y in range(H)]
        # Radial component table
        cx, cy = W // 2, H // 2
        self._rad = [
            math.sin(math.sqrt((x - cx) ** 2 + (y - cy) ** 2) / 6.0)
            for x in range(W) for y in range(H)
        ]

    def reset(self):
        self.t = 0.0

    def draw(self, canvas):
        t  = self.t
        st = math.sin(t)
        ct = math.cos(t * 0.8)
        st2= math.sin(t * 1.3)

        for x in range(W):
            sx  = self._sin_x[x] + st
            for y in range(H):
                v = sx + self._sin_y[y] + ct + self._sin_xy[x * H + y] + st2 + self._rad[x * H + y]
                hue = ((v * 30) + t * 40) % 360
                r, g, b = _hsv_to_rgb(hue, 1.0, 0.85)
                canvas.SetPixel(x, y, r, g, b)

        self.t += 0.045

# ── Main loop ─────────────────────────────────────────────────────────────────

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

    animations = [MatrixRain(), Fire(), Plasma()]
    anim_idx   = 0
    anim_start = time.time()

    last_hb   = 0.0
    frame_ctr = 0
    running   = [True]

    def _stop(sig, frm):
        running[0] = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    print(f"[screensaver] starting animation {anim_idx}", flush=True)

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

        # Cycle animation
        if now - anim_start >= ANIM_SECS:
            anim_idx   = (anim_idx + 1) % len(animations)
            anim_start = now
            animations[anim_idx].reset()
            print(f"[screensaver] switching to animation {anim_idx}", flush=True)

        # Draw
        canvas.Clear()
        animations[anim_idx].draw(canvas)
        canvas = matrix.SwapOnVSync(canvas)

        frame_ctr += 1
        if frame_ctr % 300 == 0:
            gc.collect()

        time.sleep(0.04)  # ~25 fps

    canvas.Clear()
    matrix.SwapOnVSync(canvas)
    print("[screensaver] exited cleanly", flush=True)


if __name__ == "__main__":
    main()
