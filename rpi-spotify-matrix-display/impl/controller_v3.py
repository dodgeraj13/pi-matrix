#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, inspect, sys, math, time, configparser, argparse, warnings, traceback, random
from pathlib import Path
from PIL import Image

from apps_v2 import spotify_player
from modules import spotify_module


# ── PIL-based screensaver animations ─────────────────────────────────────────

class _PilScreensaver:
    """Cycles through 3 animations: Matrix rain → Fire → Plasma."""

    W = H = 64
    CYCLE_SECONDS = 25  # seconds per animation

    def __init__(self):
        self._t0 = time.time()
        self._frame = 0
        # Matrix rain
        self._rain_cols = [random.randint(0, self.H - 1) for _ in range(self.W)]
        self._rain_speeds = [random.uniform(0.3, 1.0) for _ in range(self.W)]
        self._rain_buf = [[0] * self.H for _ in range(self.W)]
        # Fire
        self._fire = [[0.0] * self.H for _ in range(self.W)]
        # Plasma (precompute nothing — compute per pixel each frame)

    def _phase(self) -> int:
        """Returns 0=rain, 1=fire, 2=plasma"""
        elapsed = time.time() - self._t0
        return int(elapsed / self.CYCLE_SECONDS) % 3

    def next_frame(self) -> Image.Image:
        self._frame += 1
        phase = self._phase()
        if phase == 0:
            return self._rain_frame()
        elif phase == 1:
            return self._fire_frame()
        else:
            return self._plasma_frame()

    # ── Matrix rain ──
    def _rain_frame(self) -> Image.Image:
        img = Image.new("RGB", (self.W, self.H), (0, 0, 0))
        px = img.load()
        # Fade existing buffer
        for x in range(self.W):
            for y in range(self.H):
                v = self._rain_buf[x][y]
                self._rain_buf[x][y] = max(0, v - 18)
        # Advance drops
        for x in range(self.W):
            if random.random() < self._rain_speeds[x] * 0.3:
                head = self._rain_cols[x]
                self._rain_buf[x][head % self.H] = 255
                self._rain_cols[x] = (head + 1) % self.H
        # Draw
        for x in range(self.W):
            for y in range(self.H):
                v = self._rain_buf[x][y]
                if v > 200:
                    px[x, y] = (180, 255, 180)
                elif v > 0:
                    px[x, y] = (0, v, 0)
        return img

    # ── Fire ──
    def _fire_frame(self) -> Image.Image:
        W, H = self.W, self.H
        f = self._fire
        # Seed bottom row
        for x in range(W):
            f[x][H - 1] = random.uniform(0.8, 1.0)
        # Propagate upward
        for y in range(H - 1):
            for x in range(W):
                left = f[(x - 1) % W][y + 1]
                mid  = f[x][y + 1]
                right= f[(x + 1) % W][y + 1]
                f[x][y] = max(0.0, (left + mid + right) / 3.0 - random.uniform(0.0, 0.08))
        # Render
        img = Image.new("RGB", (W, H), (0, 0, 0))
        px = img.load()
        for x in range(W):
            for y in range(H):
                v = f[x][y]
                r = min(255, int(v * 255))
                g = min(255, int(max(0, v - 0.5) * 2 * 200))
                b = min(255, int(max(0, v - 0.8) * 5 * 255))
                px[x, y] = (r, g, b)
        return img

    # ── Plasma ──
    def _plasma_frame(self) -> Image.Image:
        t = time.time() * 1.5
        img = Image.new("RGB", (self.W, self.H), (0, 0, 0))
        px = img.load()
        for x in range(self.W):
            for y in range(self.H):
                cx = x + 0.5 * math.sin(t / 3)
                cy = y + 0.5 * math.cos(t / 2)
                v = (math.sin(x / 8.0 + t) +
                     math.sin(y / 6.0 + t * 1.3) +
                     math.sin((x + y) / 10.0 + t * 0.7) +
                     math.sin(math.sqrt(cx * cx + cy * cy) / 4.0 + t)) / 4.0
                h = (v + 1.0) / 2.0  # 0..1
                r, g, b = _hsv_to_rgb(h, 1.0, 1.0)
                px[x, y] = (int(r * 255), int(g * 255), int(b * 255))
        return img


def _hsv_to_rgb(h: float, s: float, v: float):
    if s == 0:
        return v, v, v
    i = int(h * 6)
    f = h * 6 - i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    i %= 6
    if i == 0: return v, t, p
    if i == 1: return q, v, p
    if i == 2: return p, v, t
    if i == 3: return p, q, v
    if i == 4: return t, p, v
    return v, p, q


# ── Idle-fallback fetcher ─────────────────────────────────────────────────────

def _fetch_idle_fallback(backend_base: str, device_token: str) -> str:
    """Fetch idle_fallback setting from backend. Returns empty string on failure."""
    try:
        import urllib.request
        url = f"{backend_base}/idle-fallback"
        req = urllib.request.Request(url, headers={"X-Device-Token": device_token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json
            data = json.loads(resp.read())
            return data.get("idle_fallback", "")
    except Exception:
        return ""


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    canvas_width = 64
    canvas_height = 64

    # args
    parser = argparse.ArgumentParser(
        prog='RpiSpotifyMatrixDisplay',
        description='Displays album art of currently playing song on an LED matrix'
    )
    parser.add_argument('-f', '--fullscreen', action='store_true', help='Always display album art in fullscreen')
    parser.add_argument('-e', '--emulated', action='store_true', help='Run in a matrix emulator')
    args = parser.parse_args()

    is_emulated = args.emulated
    is_full_screen_always = args.fullscreen

    # locate this script directory and repo root
    currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    repo_root = Path(currentdir).parent

    # add absolute path to rgbmatrix python bindings
    rgb_bindings = repo_root / "rpi-rgb-led-matrix" / "bindings" / "python"
    if rgb_bindings.exists():
        sys.path.append(str(rgb_bindings))

    # config (use absolute path so services/agents work)
    config = configparser.ConfigParser()
    config_path = repo_root / "config.ini"
    parsed_configs = config.read(str(config_path))
    if len(parsed_configs) == 0:
        print(f"no config file found at {config_path}")
        sys.exit(1)

    # connect to Spotify and create display image
    modules = {'spotify': spotify_module.SpotifyModule(config)}
    app_list = [spotify_player.SpotifyScreen(config, modules, is_full_screen_always)]

    # switch matrix library import if emulated
    if is_emulated:
        from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions
    else:
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

    # setup matrix
    options = RGBMatrixOptions()
    options.hardware_mapping = config.get('Matrix', 'hardware_mapping', fallback='regular')
    options.rows = canvas_width
    options.cols = canvas_height
    options.brightness = 100 if is_emulated else config.getint('Matrix', 'brightness', fallback=100)
    options.gpio_slowdown = config.getint('Matrix', 'gpio_slowdown', fallback=1)
    options.limit_refresh_rate_hz = config.getint('Matrix', 'limit_refresh_rate_hz', fallback=0)
    # honor rotation / pixel mapper (e.g., "Rotate:90")
    options.pixel_mapper_config = config.get('Matrix', 'pixel_mapper_config', fallback='')
    options.drop_privileges = False

    matrix = RGBMatrix(options=options)

    shutdown_delay = config.getint('Matrix', 'shutdown_delay', fallback=600)  # seconds
    black_screen = Image.new("RGB", (canvas_width, canvas_height), (0, 0, 0))
    last_active_time = math.floor(time.time())
    last_frame = None  # cache of last successfully generated frame

    # Read backend + device token for idle-fallback queries
    backend_base = config.get('Spotify', 'backend_url', fallback='').rstrip('/')
    device_token = config.get('Spotify', 'device_token', fallback='')

    # Idle fallback — refresh every 60 s
    idle_fallback = ""
    last_fallback_check = 0.0
    FALLBACK_REFRESH = 60.0

    # Screensaver instance (created lazily when first needed)
    screensaver: _PilScreensaver | None = None

    # main loop
    while True:
        try:
            frame, is_playing = app_list[0].generate()
            current_time = math.floor(time.time())

            if frame is not None:
                # got a fresh frame — cache it
                last_frame = frame
                if is_playing:
                    last_active_time = current_time

            # Refresh idle-fallback setting periodically
            if backend_base and device_token and (current_time - last_fallback_check) > FALLBACK_REFRESH:
                idle_fallback = _fetch_idle_fallback(backend_base, device_token)
                last_fallback_check = current_time

            # Decide what to show
            if is_playing:
                # actively playing: prefer fresh frame, else fall back to cache, else black
                frame_to_show = frame if frame is not None else (last_frame if last_frame is not None else black_screen)
                # Reset screensaver when music resumes
                screensaver = None
            else:
                # paused / stopped
                if idle_fallback == "screensaver":
                    # Screensaver animation
                    if screensaver is None:
                        screensaver = _PilScreensaver()
                    frame_to_show = screensaver.next_frame()
                else:
                    # Hold last art until shutdown_delay, then black
                    within_hold_window = (current_time - last_active_time) < shutdown_delay
                    if within_hold_window and last_frame is not None:
                        frame_to_show = last_frame
                    else:
                        frame_to_show = black_screen
                    screensaver = None

            matrix.SetImage(frame_to_show)
            time.sleep(0.08)

        except Exception:
            # Log the traceback so it shows up in journal/system logs,
            # but keep running so a transient issue doesn't kill the process.
            traceback.print_exc()
            time.sleep(1)


if __name__ == '__main__':
    try:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        main()
    except KeyboardInterrupt:
        print('Interrupted with Ctrl-C')
        sys.exit(0)
