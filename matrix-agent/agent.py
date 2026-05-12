# -*- coding: utf-8 -*-
#!/usr/bin/env python3
import asyncio, json, os, signal, subprocess, sys, time
from pathlib import Path

import requests
import websockets
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

API_TOKEN    = os.getenv("API_TOKEN", "")
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "")  # Per-device UUID token
BACKEND_BASE = (os.getenv("BACKEND_BASE") or os.getenv("SERVER_URL") or "").rstrip("/")
_ws_base     = os.getenv("WS_URL") or (BACKEND_BASE.replace("https://","wss://").replace("http://","ws://") + "/ws")
WS_URL       = f"{_ws_base}?device={DEVICE_TOKEN}" if DEVICE_TOKEN and "?device=" not in _ws_base else _ws_base

MLB_DIR      = os.getenv("MLB_DIR", "/home/pi_two/mlb-led-scoreboard")
MUSIC_DIR    = os.getenv("MUSIC_DIR", "/home/pi_two/rpi-spotify-matrix-display")
MUSIC_IMPL   = os.path.join(MUSIC_DIR, "impl")
CLOCK_DIR    = os.getenv("CLOCK_DIR", "/home/pi_two/matrix-clock")
WEATHER_DIR  = os.getenv("WEATHER_DIR", "/home/pi_two/matrix-weather")
PICTURE_DIR  = os.getenv("PICTURE_DIR", "/home/pi_two/matrix-picture")
DRAWING_DIR  = os.getenv("DRAWING_DIR", "/home/pi_two/matrix-drawing")
TEXT_DIR     = os.getenv("TEXT_DIR", "/home/pi_two/matrix-text")

HEADERS = {}
if DEVICE_TOKEN:
    HEADERS["X-Device-Token"] = DEVICE_TOKEN
elif API_TOKEN:
    HEADERS["Authorization"] = f"Bearer {API_TOKEN}"

def heartbeat_path(mode:int) -> str:
    return f"/tmp/matrix-heartbeat-{mode}"

def _now() -> float:
    return time.time()

class Runner:
    def __init__(self):
        self.mode = 0
        self.brightness = 60
        self.rotation = 0  # 0,90,180,270
        self.mlb_proc: subprocess.Popen | None = None
        self.music_proc: subprocess.Popen | None = None
        self.clock_proc: subprocess.Popen | None = None
        self.weather_proc: subprocess.Popen | None = None
        self.picture_proc: subprocess.Popen | None = None
        self.drawing_proc: subprocess.Popen | None = None
        self.text_proc: subprocess.Popen | None = None

    @staticmethod
    def _is_running(p):
        try:
            return p and p.poll() is None
        except Exception:
            return False

    @staticmethod
    def _stop(name, p):
        if not Runner._is_running(p):
            return None
        try:
            print(f"[agent] stopping {name} pid={p.pid}", flush=True)
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception as e:
            print(f"[agent] stop {name} error: {e}", flush=True)
        return None

    def _pixel_mapper(self):
        if self.rotation in (90, 180, 270):
            return [f"--pixel-mapper", f"Rotate:{self.rotation}"]  # capital R
        return []

    def _child_env(self):
        # unbuffered logs; stable HOME/CACHE
        env_home = "/home/pi_two"
        env_cache = "/home/pi_two/.cache"
        env = os.environ.copy()
        env["HOME"] = env_home
        env["XDG_CACHE_HOME"] = env_cache
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _write_music_ini(self):
        import configparser
        cfg = configparser.ConfigParser()
        ini_path = os.path.join(MUSIC_DIR, "config.ini")
        if os.path.exists(ini_path):
            try:
                with open(ini_path, "r") as f:
                    cfg.read_file(f)
            except Exception as e:
                print(f"[agent] warning: could not read existing config.ini: {e}", flush=True)
        if not cfg.has_section("Matrix"):
            cfg.add_section("Matrix")
        cfg.set("Matrix", "hardware_mapping", "adafruit-hat-pwm")
        cfg.set("Matrix", "brightness", str(self.brightness))
        cfg.set("Matrix", "gpio_slowdown", "2")
        cfg.set("Matrix", "limit_refresh_rate_hz", "0")
        cfg.set("Matrix", "shutdown_delay", "999999999")
        if self.rotation in (90, 180, 270):
            cfg.set("Matrix", "pixel_mapper_config", f"Rotate:{self.rotation}")
        else:
            if cfg.has_option("Matrix", "pixel_mapper_config"):
                cfg.remove_option("Matrix", "pixel_mapper_config")
        with open(ini_path, "w") as f:
            cfg.write(f)

    def _start_mlb(self):
        if self._is_running(self.mlb_proc): return
        try:
            print("[agent] starting MLB ...", flush=True)
            os.chdir(MLB_DIR)
            cmd = [
                "sudo","-n","./main.py",
                "--led-rows=64","--led-cols=64",
                "--led-gpio-mapping=adafruit-hat-pwm",
                f"--led-brightness={self.brightness}",
                "--led-slowdown-gpio=2",
            ]
            # MLB tool rotates internally via --led-pixel-mapper if supported:
            if self.rotation in (90,180,270):
                cmd.append(f"--led-pixel-mapper=Rotate:{self.rotation}")
            self.mlb_proc = subprocess.Popen(cmd, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] MLB start error: {e}", flush=True)
            self.mlb_proc = None

    def _start_music(self):
        if self._is_running(self.music_proc): return
        try:
            print("[agent] starting Music ...", flush=True)
            self._write_music_ini()
            os.chdir(MUSIC_IMPL)
            py = os.path.join(MUSIC_DIR, ".venv", "bin", "python3")
            if not os.path.exists(py):
                py = sys.executable
            cmd = [
                "sudo","-n","env", f"HOME=/home/pi_two", f"XDG_CACHE_HOME=/home/pi_two/.cache",
                py, "controller_v3.py"
            ]
            self.music_proc = subprocess.Popen(cmd, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] Music start error: {e}", flush=True)
            self.music_proc = None

    def _start_clock(self):
        if self._is_running(self.clock_proc): return
        try:
            print("[agent] starting Clock ...", flush=True)
            os.chdir(CLOCK_DIR)
            cmd = [
                "sudo","-n","/usr/bin/env",
                "HOME=/home/pi_two","XDG_CACHE_HOME=/home/pi_two/.cache","PYTHONUNBUFFERED=1",
                os.path.join(BASE, ".venv", "bin", "python"),
                os.path.join(CLOCK_DIR, "clock_display.py"),
                "--hardware-mapping","adafruit-hat-pwm",
                "--gpio-slowdown","2",
                "--brightness", str(self.brightness),
                *self._pixel_mapper(),
            ]
            self.clock_proc = subprocess.Popen(cmd, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] Clock start error: {e}", flush=True)
            self.clock_proc = None

    def _start_weather(self):
        if self._is_running(self.weather_proc): return
        try:
            print("[agent] starting Weather ...", flush=True)
            os.chdir(WEATHER_DIR)
            cmd = [
                "sudo","-n","/usr/bin/env",
                "HOME=/home/pi_two","XDG_CACHE_HOME=/home/pi_two/.cache","PYTHONUNBUFFERED=1",
                os.path.join(BASE, ".venv", "bin", "python"),
                os.path.join(WEATHER_DIR, "weather_display.py"),
                "--hardware-mapping","adafruit-hat-pwm",
                "--gpio-slowdown","2",
                "--brightness", str(self.brightness),
                *self._pixel_mapper(),
            ]
            self.weather_proc = subprocess.Popen(cmd, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] Weather start error: {e}", flush=True)
            self.weather_proc = None

    def _start_picture(self):
        if self._is_running(self.picture_proc): return
        try:
            print("[agent] starting Picture ...", flush=True)
            os.chdir(PICTURE_DIR if os.path.exists(PICTURE_DIR) else BASE)
            cmd = [
                "sudo","-n","/usr/bin/env",
                "HOME=/home/pi_two","XDG_CACHE_HOME=/home/pi_two/.cache","PYTHONUNBUFFERED=1",
                os.path.join(BASE, ".venv", "bin", "python"),
                os.path.join(PICTURE_DIR, "picture.py"),
                "--api-base", BACKEND_BASE,
                "--hardware-mapping","adafruit-hat-pwm",
                "--gpio-slowdown","2",
                "--brightness", str(self.brightness),
                *self._pixel_mapper(),
                *(["--device-token", DEVICE_TOKEN] if DEVICE_TOKEN else []),
            ]
            self.picture_proc = subprocess.Popen(cmd, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] Picture start error: {e}", flush=True)
            self.picture_proc = None

    def _start_drawing(self):
        if self._is_running(self.drawing_proc): return
        try:
            print("[agent] starting Drawing ...", flush=True)
            os.chdir(DRAWING_DIR if os.path.exists(DRAWING_DIR) else BASE)
            cmd = [
                "sudo","-n","/usr/bin/env",
                "HOME=/home/pi_two","XDG_CACHE_HOME=/home/pi_two/.cache","PYTHONUNBUFFERED=1",
                os.path.join(BASE, ".venv", "bin", "python"),
                os.path.join(DRAWING_DIR, "drawing_display.py"),
                "--api-base", BACKEND_BASE,
                "--hardware-mapping","adafruit-hat-pwm",
                "--gpio-slowdown","2",
                "--brightness", str(self.brightness),
                *self._pixel_mapper(),
                *(["--device-token", DEVICE_TOKEN] if DEVICE_TOKEN else []),
            ]
            self.drawing_proc = subprocess.Popen(cmd, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] Drawing start error: {e}", flush=True)
            self.drawing_proc = None

    def _start_text(self):
        if self._is_running(self.text_proc): return
        try:
            print("[agent] starting Text ...", flush=True)
            os.chdir(TEXT_DIR if os.path.exists(TEXT_DIR) else BASE)
            cmd = [
                "sudo","-n","/usr/bin/env",
                "HOME=/home/pi_two","XDG_CACHE_HOME=/home/pi_two/.cache","PYTHONUNBUFFERED=1",
                os.path.join(BASE, ".venv", "bin", "python"),
                os.path.join(TEXT_DIR, "text_display.py"),
                "--api-base", BACKEND_BASE,
                "--hardware-mapping","adafruit-hat-pwm",
                "--gpio-slowdown","2",
                "--brightness", str(self.brightness),
                *self._pixel_mapper(),
                *(["--device-token", DEVICE_TOKEN] if DEVICE_TOKEN else []),
            ]
            self.text_proc = subprocess.Popen(cmd, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] Text start error: {e}", flush=True)
            self.text_proc = None

    def _kill_all(self):
        self.mlb_proc     = self._stop("mlb", self.mlb_proc)
        self.music_proc   = self._stop("music", self.music_proc)
        self.clock_proc   = self._stop("clock", self.clock_proc)
        self.weather_proc = self._stop("weather", self.weather_proc)
        self.picture_proc = self._stop("picture", self.picture_proc)
        self.drawing_proc = self._stop("drawing", self.drawing_proc)
        self.text_proc    = self._stop("text", self.text_proc)

    def apply_mode(self, m: int):
        if m == self.mode:
            return
        print(f"[agent] mode {self.mode} -> {m}", flush=True)
        self._kill_all()
        if m == 1:   self._start_mlb()
        elif m == 2: self._start_music()  # Live Music
        elif m == 3: self._start_clock()
        elif m == 4: self._start_weather()
        elif m == 5: self._start_picture()
        elif m == 6: self._start_drawing()
        elif m == 7: self._start_text()
        elif m == 8: self._start_music()  # Custom Music (uses same display, backend serves different content)
        self.mode = m

    def apply_brightness(self, b: int):
        b = max(0, min(100, int(b)))
        if b == self.brightness:
            return
        print(f"[agent] brightness {self.brightness} -> {b}", flush=True)
        self.brightness = b
        # restart whichever is running to apply new brightness
        if self._is_running(self.mlb_proc):
            self.mlb_proc = self._stop("mlb", self.mlb_proc); self._start_mlb()
        if self._is_running(self.music_proc):
            self.music_proc = self._stop("music", self.music_proc); self._start_music()
        if self._is_running(self.clock_proc):
            self.clock_proc = self._stop("clock", self.clock_proc); self._start_clock()
        if self._is_running(self.weather_proc):
            self.weather_proc = self._stop("weather", self.weather_proc); self._start_weather()
        if self._is_running(self.picture_proc):
            self.picture_proc = self._stop("picture", self.picture_proc); self._start_picture()
        if self._is_running(self.drawing_proc):
            self.drawing_proc = self._stop("drawing", self.drawing_proc); self._start_drawing()
        if self._is_running(self.text_proc):
            self.text_proc = self._stop("text", self.text_proc); self._start_text()

    def apply_rotation(self, r: int):
        r = int(r)
        if r not in (0, 90, 180, 270):
            r = 0
        if r == self.rotation:
            return
        print(f"[agent] rotation {self.rotation} -> {r}", flush=True)
        self.rotation = r
        # restart current app to apply mapper
        self.apply_mode(self.mode)

    def restart_current(self):
        # helper: restart current mode (used by watchdog)
        self.apply_mode(self.mode)

def fetch_state():
    try:
        r = requests.get(f"{BACKEND_BASE}/state", headers=HEADERS, timeout=5)
        if r.ok:
            s = r.json()
            return int(s.get("mode", 0)), int(s.get("brightness", 60)), int(s.get("rotation", 0))
        else:
            print(f"[agent] GET /state not ok: {r.status_code}", flush=True)
    except Exception as e:
        print(f"[agent] poll error: {e}", flush=True)
    return None

async def watchdog_loop(runner: Runner, interval=5, stall_s=15):
    # checks: process alive AND heartbeat fresh
    while True:
        try:
            mode = runner.mode
            hb = heartbeat_path(mode)
            stale = False
            if os.path.exists(hb):
                age = _now() - os.path.getmtime(hb)
                stale = age > stall_s
            # determine current proc
            p = None
            if   mode == 1: p = runner.mlb_proc
            elif mode == 2: p = runner.music_proc
            elif mode == 3: p = runner.clock_proc
            elif mode == 4: p = runner.weather_proc
            elif mode == 5: p = runner.picture_proc
            elif mode == 6: p = runner.drawing_proc
            elif mode == 7: p = runner.text_proc
            elif mode == 8: p = runner.music_proc  # Custom Music uses same process

            if p is not None and not Runner._is_running(p):
                print(f"[agent] watchdog: mode {mode} process died; restarting", flush=True)
                runner.restart_current()
            elif stale:
                print(f"[agent] watchdog: mode {mode} heartbeat stale; restarting", flush=True)
                runner.restart_current()
        except Exception as e:
            print(f"[agent] watchdog error: {e}", flush=True)
        await asyncio.sleep(interval)

async def ws_loop():
    runner = Runner()

    # initial sync
    s = fetch_state()
    if s:
        m, b, rot = s
        runner.apply_mode(m)
        runner.apply_brightness(b)
        runner.apply_rotation(rot)

    # start watchdog
    asyncio.create_task(watchdog_loop(runner))

    while True:
        try:
            print(f"[agent] connecting WS {WS_URL}", flush=True)
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                print("[agent] WS connected", flush=True)
                await ws.send(json.dumps({"type":"hello","from":"pi"}))
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                    except Exception:
                        continue
                    if data.get("type") == "state":
                        if "mode" in data: runner.apply_mode(int(data["mode"]))
                        if "brightness" in data: runner.apply_brightness(int(data["brightness"]))
                        if "rotation" in data: runner.apply_rotation(int(data["rotation"]))
        except Exception as e:
            print(f"[agent] ws error: {e}", flush=True)
            # short poll during backoff
            for _ in range(3):
                s = fetch_state()
                if s:
                    m, b, rot = s
                    runner.apply_mode(m)
                    runner.apply_brightness(b)
                    runner.apply_rotation(rot)
                await asyncio.sleep(1)

def main():
    if not BACKEND_BASE or not WS_URL:
        print("[agent] Missing BACKEND_BASE/WS_URL in .env", flush=True)
        sys.exit(1)
    try:
        asyncio.run(ws_loop())
    except KeyboardInterrupt:
        print("[agent] interrupted", flush=True)

if __name__ == "__main__":
    main()
