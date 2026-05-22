#!/usr/bin/env python3
"""
Screensaver display for 64x64 RGB LED matrix.

Supports 3 animations: rain, fire, plasma.
Configurable via --animations, --cycle-time, --fade-time.
Smooth pixel-blend crossfade between transitions.

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
W, H = 64, 64

# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hardware-mapping", default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown",   type=int,   default=2)
    ap.add_argument("--brightness",      type=int,   default=60)
    ap.add_argument("--pixel-mapper",    default=None)
    ap.add_argument("--animations",      default="rain,fire,plasma",
                    help="Comma-separated list of animations: rain,fire,plasma")
    ap.add_argument("--cycle-time",      type=float, default=25.0,
                    help="Seconds to show each animation before transitioning")
    ap.add_argument("--fade-time",       type=float, default=2.0,
                    help="Seconds for crossfade between animations (0 to disable)")
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

# ── Frame helpers ─────────────────────────────────────────────────────────────

def _blend(f1, f2, alpha):
    """Pixel-blend two flat (r,g,b) buffers. alpha=0→f1, alpha=1→f2."""
    a1 = 1.0 - alpha
    return [
        (int(r1 * a1 + r2 * alpha),
         int(g1 * a1 + g2 * alpha),
         int(b1 * a1 + b2 * alpha))
        for (r1, g1, b1), (r2, g2, b2) in zip(f1, f2)
    ]

def _apply(canvas, frame):
    """Write flat buffer to canvas; skip pure black for speed."""
    canvas.Clear()
    for i, (r, g, b) in enumerate(frame):
        if r or g or b:
            canvas.SetPixel(i % W, i // W, r, g, b)

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

    def get_frame(self):
        buf = [(0, 0, 0)] * (W * H)
        for i in range(len(self.drops)):
            d = self.drops[i]
            d["y"] += d["speed"]
            if d["y"] - d["length"] > H:
                self.drops[i] = self._new_drop()
                continue
            head   = int(d["y"])
            length = d["length"]
            bright = d["bright"]
            for j in range(length):
                py = head - j
                if 0 <= py < H:
                    fade = 1.0 - j / length
                    if j == 0:
                        r, g, b = int(180 * fade), 255, int(180 * fade)
                    else:
                        r, g, b = 0, int(bright * fade * fade), 0
                    buf[d["x"] + py * W] = (r, g, b)
        return buf

# ── Animation 1 — Fire ────────────────────────────────────────────────────────

class Fire:
    def __init__(self):
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
            t = min((h - 220) / 35.0, 1.0)
            return 255, 230 + int(t * 25), int(t * 120)

    def get_frame(self):
        heat = self.heat
        for x in range(W):
            heat[H - 1][x] = random.randint(180, 255)
        for y in range(H - 2, -1, -1):
            for x in range(W):
                xl = (x - 1) % W
                xr = (x + 1) % W
                avg = (heat[y + 1][xl] + heat[y + 1][x] + heat[y + 1][xr]) // 3
                heat[y][x] = max(0, avg - random.randint(3, 10))
        buf = [(0, 0, 0)] * (W * H)
        for y in range(H):
            for x in range(W):
                buf[x + y * W] = self._heat_to_rgb(heat[y][x])
        return buf

# ── Animation 2 — Plasma ─────────────────────────────────────────────────────

class Plasma:
    def __init__(self):
        self.t = 0.0
        self._sin_x  = [math.sin(x / 5.0) for x in range(W)]
        self._sin_y  = [math.sin(y / 4.0) for y in range(H)]
        self._sin_xy = [math.sin((x + y) / 7.0) for x in range(W) for y in range(H)]
        cx, cy = W // 2, H // 2
        self._rad = [
            math.sin(math.sqrt((x - cx) ** 2 + (y - cy) ** 2) / 6.0)
            for x in range(W) for y in range(H)
        ]

    def reset(self):
        self.t = 0.0

    def get_frame(self):
        t   = self.t
        st  = math.sin(t)
        ct  = math.cos(t * 0.8)
        st2 = math.sin(t * 1.3)
        buf = [(0, 0, 0)] * (W * H)
        for x in range(W):
            sx = self._sin_x[x] + st
            for y in range(H):
                v = (sx + self._sin_y[y] + ct
                     + self._sin_xy[x * H + y] + st2 + self._rad[x * H + y])
                hue = ((v * 30) + t * 40) % 360
                buf[x + y * W] = _hsv_to_rgb(hue, 1.0, 0.85)
        self.t += 0.045
        return buf

# ── Main loop ─────────────────────────────────────────────────────────────────

_ANIM_CLASSES = {"rain": MatrixRain, "fire": Fire, "plasma": Plasma}

def main():
    args = parse_args()

    # Parse animations list
    requested = [a.strip().lower() for a in args.animations.split(",")]
    active_anims = []
    for name in requested:
        if name in _ANIM_CLASSES:
            active_anims.append((name, _ANIM_CLASSES[name]()))
    if not active_anims:
        active_anims = [("rain", MatrixRain()), ("fire", Fire()), ("plasma", Plasma())]

    cycle_time = max(3.0, args.cycle_time)
    fade_time  = max(0.0, min(args.fade_time, cycle_time * 0.4))

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

    anim_idx        = 0
    anim_start      = time.time()
    next_reset_done = False
    last_hb         = 0.0
    frame_ctr       = 0
    running         = [True]

    def _stop(sig, frm):
        running[0] = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    print(f"[screensaver] starting: anims={[n for n,_ in active_anims]} "
          f"cycle={cycle_time}s fade={fade_time}s", flush=True)

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

        elapsed = now - anim_start

        # Full cycle complete — advance to next animation
        if elapsed >= cycle_time:
            anim_idx        = (anim_idx + 1) % len(active_anims)
            anim_start      = now
            elapsed         = 0.0
            next_reset_done = False
            print(f"[screensaver] → {active_anims[anim_idx][0]}", flush=True)

        fade_threshold = cycle_time - fade_time
        if fade_time > 0 and elapsed >= fade_threshold:
            next_idx = (anim_idx + 1) % len(active_anims)
            # Reset next animation once at the start of fade
            if not next_reset_done:
                active_anims[next_idx][1].reset()
                next_reset_done = True
            alpha = min(1.0, (elapsed - fade_threshold) / fade_time)
            f1    = active_anims[anim_idx][1].get_frame()
            f2    = active_anims[next_idx][1].get_frame()
            frame = _blend(f1, f2, alpha)
        else:
            frame = active_anims[anim_idx][1].get_frame()

        _apply(canvas, frame)
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
