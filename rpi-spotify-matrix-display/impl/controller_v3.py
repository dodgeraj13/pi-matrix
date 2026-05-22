#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, inspect, sys, math, time, configparser, argparse, warnings, traceback, random
from pathlib import Path
from PIL import Image

from apps_v2 import spotify_player
from modules import spotify_module


# ── PIL-based animation helpers ───────────────────────────────────────────────

class _RainAnim:
    NUM_DROPS = 45
    def __init__(self):
        self.drops = [self._new_drop(random.randint(0, 63)) for _ in range(self.NUM_DROPS)]
    def _new_drop(self, y=None):
        return {"x": random.randint(0, 63), "y": float(random.randint(-20, 0) if y is None else y),
                "speed": random.uniform(0.4, 1.8), "length": random.randint(4, 16), "bright": random.randint(140, 255)}
    def reset(self): self.__init__()
    def get_pil_frame(self) -> Image.Image:
        img = Image.new("RGB", (64, 64), (0, 0, 0))
        px = img.load()
        for i in range(len(self.drops)):
            d = self.drops[i]
            d["y"] += d["speed"]
            if d["y"] - d["length"] > 64:
                self.drops[i] = self._new_drop(); continue
            for j in range(d["length"]):
                py = int(d["y"]) - j
                if 0 <= py < 64:
                    fade = 1.0 - j / d["length"]
                    if j == 0: px[d["x"], py] = (int(180*fade), 255, int(180*fade))
                    else: px[d["x"], py] = (0, int(d["bright"]*fade*fade), 0)
        return img


class _FireAnim:
    def __init__(self): self.heat = [[0]*64 for _ in range(64)]
    def reset(self): self.heat = [[0]*64 for _ in range(64)]
    @staticmethod
    def _h2rgb(h):
        if h < 30: return 0, 0, 0
        elif h < 90: t=(h-30)/60; return int(t*200), 0, 0
        elif h < 160: t=(h-90)/70; return 200+int(t*55), int(t*90), 0
        elif h < 220: t=(h-160)/60; return 255, 90+int(t*140), 0
        else: t=min((h-220)/35,1.0); return 255, 230+int(t*25), int(t*120)
    def get_pil_frame(self) -> Image.Image:
        ht = self.heat
        for x in range(64): ht[63][x] = random.randint(180, 255)
        for y in range(62, -1, -1):
            for x in range(64):
                avg = (ht[y+1][(x-1)%64] + ht[y+1][x] + ht[y+1][(x+1)%64]) // 3
                ht[y][x] = max(0, avg - random.randint(3, 10))
        img = Image.new("RGB", (64, 64))
        px = img.load()
        for y in range(64):
            for x in range(64): px[x, y] = self._h2rgb(ht[y][x])
        return img


class _PlasmaAnim:
    def __init__(self):
        self.t = 0.0
        self._sx = [math.sin(x/5.0) for x in range(64)]
        self._sy = [math.sin(y/4.0) for y in range(64)]
        self._sxy= [math.sin((x+y)/7.0) for x in range(64) for y in range(64)]
        cx=cy=32
        self._rad= [math.sin(math.sqrt((x-cx)**2+(y-cy)**2)/6.0) for x in range(64) for y in range(64)]
    def reset(self): self.t = 0.0
    def get_pil_frame(self) -> Image.Image:
        t=self.t; st=math.sin(t); ct=math.cos(t*.8); st2=math.sin(t*1.3)
        img = Image.new("RGB", (64, 64))
        px = img.load()
        for x in range(64):
            sx = self._sx[x]+st
            for y in range(64):
                v=sx+self._sy[y]+ct+self._sxy[x*64+y]+st2+self._rad[x*64+y]
                h=((v*30)+t*40)%360; r,g,b=_hsv_to_rgb(h,1.0,0.85)
                px[x,y]=(r,g,b)
        self.t+=0.045
        return img


# ── PIL Screensaver orchestrator ──────────────────────────────────────────────

class _PilScreensaver:
    """Cycles configured animations with smooth Image.blend() crossfade."""

    _ANIMS = {"rain": _RainAnim, "fire": _FireAnim, "plasma": _PlasmaAnim}

    def __init__(self, animations: str = "rain,fire,plasma",
                 cycle_time: float = 25.0, fade_time: float = 2.0):
        chosen = [a.strip() for a in animations.split(",") if a.strip() in self._ANIMS]
        if not chosen:
            chosen = ["rain", "fire", "plasma"]
        self._active = [(n, self._ANIMS[n]()) for n in chosen]
        self._cycle  = max(3.0, cycle_time)
        self._fade   = max(0.0, min(fade_time, self._cycle * 0.4))
        self._idx    = 0
        self._start  = time.time()
        self._next_reset_done = False

    def next_frame(self) -> Image.Image:
        now     = time.time()
        elapsed = now - self._start

        if elapsed >= self._cycle:
            self._idx   = (self._idx + 1) % len(self._active)
            self._start = now
            elapsed     = 0.0
            self._next_reset_done = False

        fade_thresh = self._cycle - self._fade
        if self._fade > 0 and elapsed >= fade_thresh:
            nxt = (self._idx + 1) % len(self._active)
            if not self._next_reset_done:
                self._active[nxt][1].reset()
                self._next_reset_done = True
            alpha = min(1.0, (elapsed - fade_thresh) / self._fade)
            f1 = self._active[self._idx][1].get_pil_frame()
            f2 = self._active[nxt][1].get_pil_frame()
            return Image.blend(f1.convert("RGB"), f2.convert("RGB"), alpha)
        else:
            return self._active[self._idx][1].get_pil_frame()


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
        import urllib.request, json
        url = f"{backend_base}/idle-fallback"
        req = urllib.request.Request(url, headers={"X-Device-Token": device_token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("idle_fallback", "")
    except Exception:
        return ""


def _fetch_brightness_config(backend_base: str, device_token: str) -> dict:
    """Fetch idle_brightness and dim schedule from backend."""
    try:
        import urllib.request, json
        url = f"{backend_base}/brightness-config"
        req = urllib.request.Request(url, headers={"X-Device-Token": device_token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


def _fetch_screensaver_config(backend_base: str, device_token: str) -> dict:
    """Fetch screensaver animations/timing from backend."""
    try:
        import urllib.request, json
        url = f"{backend_base}/screensaver-config"
        req = urllib.request.Request(url, headers={"X-Device-Token": device_token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


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

    # Read backend + device token for config queries
    backend_base = config.get('Spotify', 'backend_url', fallback='').rstrip('/')
    device_token = config.get('Spotify', 'device_token', fallback='')

    # Effective brightness: the value set at matrix init (from config.ini)
    normal_brightness = options.brightness
    idle_brightness   = 20   # will be updated from /brightness-config

    # Idle fallback + screensaver + brightness config — refresh every 60 s
    idle_fallback = ""
    ss_config: dict = {}
    last_fallback_check = 0.0
    FALLBACK_REFRESH = 60.0

    # Screensaver instance (created lazily / recreated when config changes)
    screensaver: _PilScreensaver | None = None
    last_ss_config_key: str = ""  # track config to detect changes

    # Track is_playing state for brightness transitions
    prev_is_playing: bool | None = None

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

            # Refresh idle-fallback + screensaver + brightness config periodically
            if backend_base and device_token and (current_time - last_fallback_check) > FALLBACK_REFRESH:
                idle_fallback = _fetch_idle_fallback(backend_base, device_token)
                ss_config     = _fetch_screensaver_config(backend_base, device_token)
                bc            = _fetch_brightness_config(backend_base, device_token)
                if bc:
                    idle_brightness = int(bc.get("idle_brightness", idle_brightness))
                last_fallback_check = current_time

            # Adjust matrix brightness on playing↔paused transitions
            if prev_is_playing is not None and prev_is_playing != is_playing:
                try:
                    matrix.brightness = normal_brightness if is_playing else idle_brightness
                except Exception:
                    pass
            prev_is_playing = is_playing

            # Decide what to show
            if is_playing:
                # actively playing: prefer fresh frame, else fall back to cache, else black
                frame_to_show = frame if frame is not None else (last_frame if last_frame is not None else black_screen)
                # Reset screensaver when music resumes so it starts fresh next time
                screensaver = None
                last_ss_config_key = ""
            else:
                # paused / stopped
                if idle_fallback == "screensaver":
                    # Build a config key so we recreate if settings changed
                    cfg_key = (f"{ss_config.get('animations','rain,fire,plasma')}|"
                               f"{ss_config.get('cycle_time',25)}|"
                               f"{ss_config.get('fade_time',2)}")
                    if screensaver is None or cfg_key != last_ss_config_key:
                        screensaver = _PilScreensaver(
                            animations=str(ss_config.get("animations", "rain,fire,plasma")),
                            cycle_time=float(ss_config.get("cycle_time", 25.0)),
                            fade_time =float(ss_config.get("fade_time",  2.0)),
                        )
                        last_ss_config_key = cfg_key
                    frame_to_show = screensaver.next_frame()
                else:
                    # Hold last art until shutdown_delay, then black
                    within_hold_window = (current_time - last_active_time) < shutdown_delay
                    if within_hold_window and last_frame is not None:
                        frame_to_show = last_frame
                    else:
                        frame_to_show = black_screen
                    screensaver = None
                    last_ss_config_key = ""

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
