# -*- coding: utf-8 -*-
#!/usr/bin/env python3
import asyncio, datetime, json, os, signal, subprocess, sys, time
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

HOME_DIR     = os.getenv("HOME_DIR", f"/home/{os.getenv('USER', 'pi_two')}")
REPO_ROOT    = str(BASE.parent)  # one level up from matrix-agent/ = repo root

MLB_DIR      = os.getenv("MLB_DIR",     f"{REPO_ROOT}/mlb-led-scoreboard")
MUSIC_DIR    = os.getenv("MUSIC_DIR",   f"{HOME_DIR}/rpi-spotify-matrix-display")  # separate repo
MUSIC_IMPL   = os.path.join(MUSIC_DIR, "impl")
CLOCK_DIR    = os.getenv("CLOCK_DIR",   f"{REPO_ROOT}/matrix-clock")
WEATHER_DIR  = os.getenv("WEATHER_DIR", f"{REPO_ROOT}/matrix-weather")
PICTURE_DIR  = os.getenv("PICTURE_DIR", f"{REPO_ROOT}/matrix-picture")
DRAWING_DIR  = os.getenv("DRAWING_DIR", f"{REPO_ROOT}/matrix-drawing")
TEXT_DIR     = os.getenv("TEXT_DIR",    f"{REPO_ROOT}/matrix-text")
MAP_DIR        = os.getenv("MAP_DIR",        WEATHER_DIR)  # map_display.py lives alongside weather_display.py
COUNTDOWN_DIR  = os.getenv("COUNTDOWN_DIR",  f"{REPO_ROOT}/matrix-countdown")
SCREENSAVER_DIR= os.getenv("SCREENSAVER_DIR",f"{REPO_ROOT}/matrix-screensaver")
STOPWATCH_DIR  = os.getenv("STOPWATCH_DIR",  f"{REPO_ROOT}/matrix-stopwatch")
GIF_DIR        = os.getenv("GIF_DIR",        f"{REPO_ROOT}/matrix-gif")
STOCKS_DIR     = os.getenv("STOCKS_DIR",     f"{REPO_ROOT}/matrix-stocks")

HEADERS = {}
if DEVICE_TOKEN:
    HEADERS["X-Device-Token"] = DEVICE_TOKEN
elif API_TOKEN:
    HEADERS["Authorization"] = f"Bearer {API_TOKEN}"

def heartbeat_path(mode:int) -> str:
    return f"/tmp/matrix-heartbeat-{mode}"

def _now() -> float:
    return time.time()

# Maps mode int → (proc_attr_name, start_method_name)
# Modes 2 and 8 share the same process (music_proc / _start_music).
_MODES: dict[int, tuple[str, str]] = {
    1:  ("mlb_proc",         "_start_mlb"),
    2:  ("music_proc",       "_start_music"),
    3:  ("clock_proc",       "_start_clock"),
    4:  ("weather_proc",     "_start_weather"),
    5:  ("picture_proc",     "_start_picture"),
    6:  ("drawing_proc",     "_start_drawing"),
    7:  ("text_proc",        "_start_text"),
    8:  ("music_proc",       "_start_music"),
    9:  ("map_proc",         "_start_map"),
    10: ("countdown_proc",   "_start_countdown"),
    11: ("screensaver_proc", "_start_screensaver"),
    12: ("stopwatch_proc",   "_start_stopwatch"),
    13: ("gif_proc",         "_start_gif"),
    14: ("stocks_proc",      "_start_stocks"),
}

# Ordered list of unique proc attributes for kill-all / brightness loops
_ALL_PROCS = list(dict.fromkeys(attr for attr, _ in _MODES.values()))


class Runner:
    def __init__(self):
        self.mode = 0
        self.normal_brightness = 60   # user's setting (from /state)
        self.brightness = 60          # effective brightness applied to hardware
        self.idle_brightness = 20     # brightness when music is paused / dim schedule active
        self.dim_schedule_enabled = False
        self.dim_start = ""           # "HH:MM" when dim schedule begins
        self.dim_end   = ""           # "HH:MM" when dim schedule ends
        self.rotation = 0  # 0,90,180,270
        self.location = ""          # e.g. "Chicago, IL" — set from /settings
        self.units = "imperial"     # "imperial" | "metric"
        self.map_address_a = ""     # origin for map mode
        self.map_address_b = ""     # destination for map mode
        self.map_label_a   = ""     # friendly label for origin  (e.g. "Home")
        self.map_label_b   = ""     # friendly label for destination (e.g. "Work")
        self.map_submode   = "alternate"  # "basic" | "map" | "alternate"
        self.schedule_enabled = False
        self.schedule_slots   = []   # [{"id":..,"start":"HH:MM","end":"HH:MM","mode":int}]
        self.mlb_proc: subprocess.Popen | None = None
        self.music_proc: subprocess.Popen | None = None
        self.clock_proc: subprocess.Popen | None = None
        self.weather_proc: subprocess.Popen | None = None
        self.picture_proc: subprocess.Popen | None = None
        self.drawing_proc: subprocess.Popen | None = None
        self.text_proc: subprocess.Popen | None = None
        self.map_proc: subprocess.Popen | None = None
        self.countdown_proc: subprocess.Popen | None = None
        self.screensaver_proc: subprocess.Popen | None = None
        self.stopwatch_proc: subprocess.Popen | None = None
        self.timer_end_time: float = 0.0
        self.timer_duration: float = 0.0
        self.stopwatch_start_time: float = 0.0
        self.screensaver_animations: str = "rain,fire,plasma"
        self.screensaver_cycle_time: float = 25.0
        self.screensaver_fade_time: float = 2.0
        self.gif_proc: subprocess.Popen | None = None
        self.stocks_proc: subprocess.Popen | None = None

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
        # Wait for process to fully exit so the new process can acquire the LED hardware
        try:
            p.wait(timeout=4)
            print(f"[agent] {name} exited cleanly", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[agent] {name} did not exit in time — sending SIGKILL", flush=True)
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                pass
        return None

    def get_effective_brightness(self) -> int:
        """Return idle_brightness when dim schedule is active, else normal_brightness."""
        if self.dim_schedule_enabled and self.dim_start and self.dim_end:
            ct = datetime.datetime.now().strftime("%H:%M")
            s, e = self.dim_start, self.dim_end
            in_dim = (s <= ct < e) if s <= e else (ct >= s or ct < e)
            if in_dim:
                return self.idle_brightness
        return self.normal_brightness

    def _pixel_mapper(self):
        if self.rotation in (90, 180, 270):
            return [f"--pixel-mapper", f"Rotate:{self.rotation}"]  # capital R
        return []

    def _child_env(self):
        # unbuffered logs; stable HOME/CACHE; inject weather settings
        env = os.environ.copy()
        env["HOME"] = HOME_DIR
        env["XDG_CACHE_HOME"] = f"{HOME_DIR}/.cache"
        env["PYTHONUNBUFFERED"] = "1"
        if self.location:
            env["WEATHER_LOCATION"] = self.location
        if self.units:
            env["WEATHER_UNITS"] = self.units
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
        # Inject device token so spotify_module sends X-Device-Token
        if DEVICE_TOKEN:
            if not cfg.has_section("Spotify"):
                cfg.add_section("Spotify")
            cfg.set("Spotify", "device_token", DEVICE_TOKEN)
            cfg.set("Spotify", "use_backend", "true")
            if BACKEND_BASE and not cfg.has_option("Spotify", "backend_url"):
                cfg.set("Spotify", "backend_url", BACKEND_BASE)
        with open(ini_path, "w") as f:
            cfg.write(f)

    def _launch(self, name: str, script: str, extra_args=(), extra_env=(), cwd=None) -> "subprocess.Popen | None":
        """Start a standard LED display script under sudo with shared hardware args."""
        # Always use the same interpreter that's running the agent — this guarantees
        # the same venv/packages (rgbmatrix, PIL, requests) regardless of BASE location.
        py = sys.executable
        env_prefix = [
            f"HOME={HOME_DIR}", f"XDG_CACHE_HOME={HOME_DIR}/.cache", "PYTHONUNBUFFERED=1",
            *extra_env,
        ]
        cmd = [
            "sudo", "-n", "/usr/bin/env", *env_prefix,
            py, script,
            "--hardware-mapping", "adafruit-hat-pwm",
            "--gpio-slowdown", "2",
            "--brightness", str(self.brightness),
            *self._pixel_mapper(),
            *extra_args,
        ]
        try:
            print(f"[agent] starting {name} ...", flush=True)
            return subprocess.Popen(cmd, cwd=cwd, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] {name} start error: {e}", flush=True)
            return None

    def _start_mlb(self):
        if self._is_running(self.mlb_proc): return
        try:
            fetch_and_write_mlb_config(self)
            print("[agent] starting MLB ...", flush=True)
            # Use the venv python + explicit script path so we don't depend on
            # main.py having the execute bit or a working shebang.
            mlb_py = os.path.join(MLB_DIR, "venv", "bin", "python3")
            if not os.path.exists(mlb_py):
                mlb_py = sys.executable
            cmd = [
                "sudo","-n", mlb_py, os.path.join(MLB_DIR, "main.py"),
                "--led-rows=64","--led-cols=64",
                "--led-gpio-mapping=adafruit-hat-pwm",
                f"--led-brightness={self.brightness}",
                "--led-slowdown-gpio=2",
            ]
            # MLB tool rotates internally via --led-pixel-mapper if supported:
            if self.rotation in (90,180,270):
                cmd.append(f"--led-pixel-mapper=Rotate:{self.rotation}")
            self.mlb_proc = subprocess.Popen(cmd, cwd=MLB_DIR, start_new_session=True, env=self._child_env())
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
                "sudo","-n","env", f"HOME={HOME_DIR}", f"XDG_CACHE_HOME={HOME_DIR}/.cache","PYTHONUNBUFFERED=1",
                py, "controller_v3.py"
            ]
            self.music_proc = subprocess.Popen(cmd, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] Music start error: {e}", flush=True)
            self.music_proc = None

    def _start_clock(self):
        if self._is_running(self.clock_proc): return
        self.clock_proc = self._launch("clock", os.path.join(CLOCK_DIR, "clock_display.py"))

    def _start_weather(self):
        if self._is_running(self.weather_proc): return
        extra = []
        for key, val in [("WEATHER_API_KEY", os.getenv("WEATHER_API_KEY", "")),
                         ("WEATHER_LOCATION", self.location),
                         ("WEATHER_UNITS", self.units)]:
            if val:
                extra.append(f"{key}={val}")
        self.weather_proc = self._launch("weather", os.path.join(WEATHER_DIR, "weather_display.py"),
                                         extra_env=extra)

    def _backend_args(self):
        args = ["--api-base", BACKEND_BASE]
        if DEVICE_TOKEN:
            args += ["--device-token", DEVICE_TOKEN]
        return args

    def _start_picture(self):
        if self._is_running(self.picture_proc): return
        self.picture_proc = self._launch("picture", os.path.join(PICTURE_DIR, "picture.py"),
                                         extra_args=self._backend_args())

    def _start_drawing(self):
        if self._is_running(self.drawing_proc): return
        self.drawing_proc = self._launch("drawing", os.path.join(DRAWING_DIR, "drawing_display.py"),
                                          extra_args=self._backend_args())

    def _start_text(self):
        if self._is_running(self.text_proc): return
        self.text_proc = self._launch("text", os.path.join(TEXT_DIR, "text_display.py"),
                                       extra_args=self._backend_args())

    def _start_map(self):
        if self._is_running(self.map_proc): return
        if not self.map_address_a or not self.map_address_b:
            print("[agent] Map mode: MAP_ADDRESS_A/B not set, skipping", flush=True)
            return
        extra = [
            f"MAP_ADDRESS_A={self.map_address_a}", f"MAP_ADDRESS_B={self.map_address_b}",
            f"MAP_LABEL_A={self.map_label_a}",     f"MAP_LABEL_B={self.map_label_b}",
            f"MAP_SUBMODE={self.map_submode}",      f"WEATHER_UNITS={self.units}",
        ]
        for key in ("WEATHER_API_KEY", "MAPBOX_TOKEN"):
            if v := os.getenv(key, ""):
                extra.append(f"{key}={v}")
        py = sys.executable
        env_prefix = [f"HOME={HOME_DIR}", f"XDG_CACHE_HOME={HOME_DIR}/.cache", "PYTHONUNBUFFERED=1", *extra]
        cmd = ["sudo", "-n", "/usr/bin/env", *env_prefix, py,
               os.path.join(MAP_DIR, "map_display.py"), *self._pixel_mapper()]
        try:
            print("[agent] starting map ...", flush=True)
            self.map_proc = subprocess.Popen(cmd, cwd=MAP_DIR, start_new_session=True, env=self._child_env())
        except Exception as e:
            print(f"[agent] map start error: {e}", flush=True)
            self.map_proc = None

    def _start_countdown(self):
        if self._is_running(self.countdown_proc): return
        self.countdown_proc = self._launch(
            "countdown", os.path.join(COUNTDOWN_DIR, "countdown_display.py"),
            extra_args=["--end-time", str(self.timer_end_time), "--duration", str(self.timer_duration)])

    def _start_screensaver(self):
        if self._is_running(self.screensaver_proc): return
        self.screensaver_proc = self._launch(
            "screensaver", os.path.join(SCREENSAVER_DIR, "screensaver_display.py"),
            extra_args=["--animations", self.screensaver_animations,
                        "--cycle-time", str(self.screensaver_cycle_time),
                        "--fade-time",  str(self.screensaver_fade_time)])

    def _start_stopwatch(self):
        if self._is_running(self.stopwatch_proc): return
        self.stopwatch_proc = self._launch(
            "stopwatch", os.path.join(STOPWATCH_DIR, "stopwatch_display.py"),
            extra_args=["--start-time", str(self.stopwatch_start_time)])

    def _start_gif(self):
        if self._is_running(self.gif_proc): return
        self.gif_proc = self._launch("gif", os.path.join(GIF_DIR, "gif_display.py"),
                                     extra_args=self._backend_args())

    def _start_stocks(self):
        if self._is_running(self.stocks_proc): return
        self.stocks_proc = self._launch("stocks", os.path.join(STOCKS_DIR, "stocks_display.py"),
                                        extra_args=self._backend_args())

    def _kill_all(self):
        for attr in _ALL_PROCS:
            setattr(self, attr, self._stop(attr.replace("_proc", ""), getattr(self, attr)))
        # Remove stale heartbeat files. Display scripts run as root (sudo), so
        # the files are root-owned and os.remove() fails for the pi_two agent.
        # Use sudo rm to ensure deletion regardless of ownership.
        hb_files = [heartbeat_path(m) for m in range(1, 15)]
        try:
            subprocess.run(
                ["sudo", "-n", "rm", "-f", *hb_files],
                capture_output=True, timeout=5
            )
        except Exception:
            pass

    def _dispatch(self, m: int):
        """Start the process for mode m (does NOT kill others — caller must)."""
        if m in _MODES:
            getattr(self, _MODES[m][1])()

    def apply_mode(self, m: int):
        if m == self.mode:
            return
        print(f"[agent] mode {self.mode} -> {m}", flush=True)
        self._kill_all()
        self._dispatch(m)
        self.mode = m

    def apply_brightness(self, b: int):
        """Store user's normal brightness and apply the effective brightness to hardware."""
        self.normal_brightness = max(0, min(100, int(b)))
        self._apply_effective_brightness()

    def _apply_effective_brightness(self):
        """Compute and apply effective brightness; restart running procs only if it changed."""
        eff = self.get_effective_brightness()
        if eff == self.brightness:
            return
        print(f"[agent] effective brightness {self.brightness} -> {eff}", flush=True)
        self.brightness = eff
        for attr in _ALL_PROCS:
            p = getattr(self, attr)
            if self._is_running(p):
                name = attr.replace("_proc", "")
                setattr(self, attr, self._stop(name, p))
                # Find which mode uses this proc and call its start method.
                # Use the first matching entry in _MODES (covers mode 2/8 sharing music_proc).
                for m_entry, (m_attr, m_start) in _MODES.items():
                    if m_attr == attr:
                        getattr(self, m_start)()
                        break

    def _force_restart(self):
        """Kill whatever is running and restart the current mode with current settings."""
        if self.mode == 0:
            return
        print(f"[agent] force-restarting mode {self.mode}", flush=True)
        self._kill_all()
        self._dispatch(self.mode)

    def apply_rotation(self, r: int):
        r = int(r)
        if r not in (0, 90, 180, 270):
            r = 0
        if r == self.rotation:
            return
        print(f"[agent] rotation {self.rotation} -> {r}", flush=True)
        self.rotation = r
        self._force_restart()

    def restart_current(self):
        # helper: restart current mode (used by watchdog)
        self._force_restart()

    def _check_schedule(self):
        """Apply mode based on current time. Called every 30 s."""
        if not self.schedule_enabled or not self.schedule_slots:
            return
        ct = datetime.datetime.now().strftime("%H:%M")
        for slot in self.schedule_slots:
            start = slot.get("start", "")
            end   = slot.get("end",   "")
            mode  = int(slot.get("mode", 0))
            if not start or not end:
                continue
            # Support midnight-spanning slots (start > end)
            if start <= end:
                in_slot = start <= ct < end
            else:
                in_slot = ct >= start or ct < end
            if in_slot:
                if self.mode != mode:
                    print(f"[schedule] {start}–{end} → mode {mode}", flush=True)
                    self.apply_mode(mode)
                return
        # No active slot — turn off if schedule is controlling display
        if self.mode != 0:
            print(f"[schedule] no active slot → off", flush=True)
            self.apply_mode(0)

def _fetch(path: str) -> dict:
    """GET {BACKEND_BASE}{path}, return parsed JSON or {} on any failure."""
    if not BACKEND_BASE or not HEADERS:
        return {}
    try:
        r = requests.get(f"{BACKEND_BASE}{path}", headers=HEADERS, timeout=5)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[agent] {path} fetch error: {e}", flush=True)
    return {}


def fetch_and_write_mlb_config(runner: "Runner | None" = None):
    """Fetch MLB config from backend and write to local config files.
    Also patches weather.location / weather.apikey from device settings."""
    if not BACKEND_BASE or not HEADERS:
        return
    try:
        r = requests.get(f"{BACKEND_BASE}/mlb-config", headers=HEADERS, timeout=5)
        if not r.ok:
            print(f"[agent] MLB config fetch failed: {r.status_code}", flush=True)
            return
        data = r.json()

        # Write config.json — optionally patch weather block with device location + API key
        config = data.get("config") or {}
        if config:
            weather_api_key = os.getenv("WEATHER_API_KEY", "")
            location = (runner.location if runner else "") or os.getenv("WEATHER_LOCATION", "")
            units = (runner.units if runner else "imperial") or "imperial"
            # Only touch the weather block if we actually have something to write.
            # Never create an empty weather block — that gives pyowm "Nothing to geocode".
            if weather_api_key or location:
                if "weather" not in config or not isinstance(config["weather"], dict):
                    config["weather"] = {}
                if weather_api_key:
                    config["weather"]["apikey"] = weather_api_key
                if location:
                    config["weather"]["location"] = location
                    config["weather"]["metric_units"] = (units == "metric")
            config_path = os.path.join(MLB_DIR, "config.json")
            with open(config_path, "w") as f:
                json.dump(config, f, indent="\t")
            print("[agent] MLB config.json written", flush=True)

        # Write colors/scoreboard.json
        sb_colors = data.get("scoreboard_colors")
        if sb_colors:
            colors_dir = os.path.join(MLB_DIR, "colors")
            os.makedirs(colors_dir, exist_ok=True)
            with open(os.path.join(colors_dir, "scoreboard.json"), "w") as f:
                json.dump(sb_colors, f, indent=2)
            print("[agent] MLB scoreboard colors written", flush=True)

    except Exception as e:
        print(f"[agent] MLB config fetch error: {e}", flush=True)


def _do_update():
    """Force-sync to origin/main and reboot if anything changed."""
    try:
        repo_dir = str(BASE.parent)
        print(f"[agent] checking for updates in {repo_dir}", flush=True)

        # Get current HEAD before updating
        before = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()

        # Fetch latest from remote
        fetch = subprocess.run(
            ["git", "-C", repo_dir, "fetch", "origin"],
            capture_output=True, text=True, timeout=60
        )
        if fetch.returncode != 0:
            print(f"[agent] git fetch failed: {fetch.stderr.strip()}", flush=True)
            return

        # Hard-reset to origin/main (never fails due to divergent branches)
        reset = subprocess.run(
            ["git", "-C", repo_dir, "reset", "--hard", "origin/main"],
            capture_output=True, text=True, timeout=30
        )
        if reset.returncode != 0:
            print(f"[agent] git reset failed: {reset.stderr.strip()}", flush=True)
            return

        after = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()

        if before == after:
            print("[agent] already up to date — no reboot needed", flush=True)
        else:
            print(f"[agent] updated {before[:7]} → {after[:7]} — rebooting in 3 seconds...", flush=True)
            time.sleep(3)
            subprocess.run(["sudo", "reboot"], check=False)
    except Exception as e:
        print(f"[agent] update error: {e}", flush=True)


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

async def brightness_schedule_loop(runner: Runner, interval: int = 60):
    """Re-evaluate effective brightness every minute to handle dim schedule transitions."""
    while True:
        try:
            runner._apply_effective_brightness()
        except Exception as e:
            print(f"[agent] brightness schedule error: {e}", flush=True)
        await asyncio.sleep(interval)


async def schedule_loop(runner: Runner, interval: int = 30):
    """Check schedule every 30 s and apply mode if needed."""
    while True:
        try:
            runner._check_schedule()
        except Exception as e:
            print(f"[agent] schedule error: {e}", flush=True)
        await asyncio.sleep(interval)

async def watchdog_loop(runner: Runner, interval=5, stall_s=90):
    while True:
        try:
            mode = runner.mode
            stale = False
            hb = heartbeat_path(mode)
            if os.path.exists(hb):
                stale = (_now() - os.path.getmtime(hb)) > stall_s
            p = getattr(runner, _MODES[mode][0], None) if mode in _MODES else None
            if p is not None and not Runner._is_running(p):
                print(f"[agent] watchdog: mode {mode} process died; restarting", flush=True)
                runner.restart_current()
            elif stale:
                print(f"[agent] watchdog: mode {mode} heartbeat stale; restarting", flush=True)
                runner.restart_current()
        except Exception as e:
            print(f"[agent] watchdog error: {e}", flush=True)
        await asyncio.sleep(interval)

def _apply_settings(runner: Runner, settings: dict):
    """Apply fetched device settings (location, units) to the runner."""
    changed = False
    loc = settings.get("location", "").strip()
    units = settings.get("units", "imperial").strip() or "imperial"
    if loc != runner.location:
        runner.location = loc
        changed = True
        print(f"[agent] location set to {loc!r}", flush=True)
    if units != runner.units:
        runner.units = units
        changed = True
        print(f"[agent] units set to {units!r}", flush=True)
    return changed


async def ws_loop():
    runner = Runner()

    loop = asyncio.get_event_loop()

    # Load device settings (location, units) before starting modes
    settings = await loop.run_in_executor(None, _fetch, "/settings")
    _apply_settings(runner, settings)

    # Load map config
    map_cfg = await loop.run_in_executor(None, _fetch, "/map-config")
    if map_cfg.get("address_a"):
        runner.map_address_a = map_cfg["address_a"].strip()
    if map_cfg.get("address_b"):
        runner.map_address_b = map_cfg["address_b"].strip()
    if map_cfg.get("label_a") is not None:
        runner.map_label_a = map_cfg["label_a"].strip()
    if map_cfg.get("label_b") is not None:
        runner.map_label_b = map_cfg["label_b"].strip()
    if map_cfg.get("submode") in ("basic", "map", "alternate"):
        runner.map_submode = map_cfg["submode"]

    # Load timer config
    timer_cfg = await loop.run_in_executor(None, _fetch, "/timer-config")
    if timer_cfg.get("end_time"):
        runner.timer_end_time = float(timer_cfg["end_time"])
        runner.timer_duration = float(timer_cfg.get("duration", 0))
        print(f"[agent] timer config loaded: end_time={runner.timer_end_time}, duration={runner.timer_duration}", flush=True)

    # Load brightness config (idle brightness + dim schedule) — before applying state
    bc_cfg = await loop.run_in_executor(None, _fetch, "/brightness-config")
    if bc_cfg:
        runner.idle_brightness        = int(bc_cfg.get("idle_brightness", 20))
        runner.dim_schedule_enabled   = bool(bc_cfg.get("dim_schedule_enabled", False))
        runner.dim_start              = bc_cfg.get("dim_start", "")
        runner.dim_end                = bc_cfg.get("dim_end", "")
        print(f"[agent] brightness config: idle={runner.idle_brightness} "
              f"dim_enabled={runner.dim_schedule_enabled} {runner.dim_start}→{runner.dim_end}", flush=True)

    # Load screensaver config
    ss_cfg = await loop.run_in_executor(None, _fetch, "/screensaver-config")
    if ss_cfg:
        if ss_cfg.get("animations"):
            runner.screensaver_animations = ss_cfg["animations"]
        if ss_cfg.get("cycle_time"):
            runner.screensaver_cycle_time = float(ss_cfg["cycle_time"])
        if ss_cfg.get("fade_time") is not None:
            runner.screensaver_fade_time = float(ss_cfg["fade_time"])
        print(f"[agent] screensaver config: anims={runner.screensaver_animations} "
              f"cycle={runner.screensaver_cycle_time}s fade={runner.screensaver_fade_time}s", flush=True)

    # Load stopwatch config
    sw_cfg = await loop.run_in_executor(None, _fetch, "/stopwatch-config")
    if sw_cfg.get("start_time"):
        runner.stopwatch_start_time = float(sw_cfg["start_time"])
        print(f"[agent] stopwatch config loaded: start_time={runner.stopwatch_start_time}", flush=True)

    # Load schedule
    schedule_cfg = await loop.run_in_executor(None, _fetch, "/schedule")
    if schedule_cfg:
        runner.schedule_enabled = schedule_cfg.get("enabled", False)
        runner.schedule_slots   = schedule_cfg.get("slots", [])
        print(f"[agent] schedule loaded: enabled={runner.schedule_enabled}, {len(runner.schedule_slots)} slots", flush=True)

    # initial sync
    s = fetch_state()
    if s:
        m, b, rot = s
        runner.apply_mode(m)
        runner.apply_brightness(b)
        runner.apply_rotation(rot)

    # start watchdog, mode schedule, and brightness schedule
    asyncio.create_task(watchdog_loop(runner))
    asyncio.create_task(schedule_loop(runner))
    asyncio.create_task(brightness_schedule_loop(runner))

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
                        if data.get("force"): runner._force_restart()
                    elif data.get("type") == "settings":
                        changed = _apply_settings(runner, data)
                        if changed and runner.mode in (1, 4):
                            # Restart weather or MLB so new location takes effect
                            runner._force_restart()
                    elif data.get("type") == "map_config":
                        addr_a   = data.get("address_a",   "").strip()
                        addr_b   = data.get("address_b",   "").strip()
                        label_a  = data.get("label_a",     "").strip()
                        label_b  = data.get("label_b",     "").strip()
                        submode  = data.get("submode",     "alternate").strip()
                        if submode not in ("basic", "map", "alternate"):
                            submode = "alternate"
                        changed  = (addr_a   != runner.map_address_a or
                                    addr_b   != runner.map_address_b or
                                    label_a  != runner.map_label_a   or
                                    label_b  != runner.map_label_b   or
                                    submode  != runner.map_submode)
                        if changed:
                            runner.map_address_a = addr_a
                            runner.map_address_b = addr_b
                            runner.map_label_a   = label_a
                            runner.map_label_b   = label_b
                            runner.map_submode   = submode
                            print(f"[agent] map config updated: A={addr_a!r} B={addr_b!r} submode={submode!r}", flush=True)
                            if runner.mode == 9:
                                runner._force_restart()
                    elif data.get("type") == "mlb_config":
                        # Re-fetch and write config; restart MLB if it's currently running
                        print("[agent] mlb_config update received — rewriting config", flush=True)
                        fetch_and_write_mlb_config(runner)
                        if runner.mode == 1:
                            print("[agent] MLB is running — restarting to apply new config", flush=True)
                            runner.mlb_proc = runner._stop("mlb", runner.mlb_proc)
                            runner._start_mlb()
                    elif data.get("type") == "timer_config":
                        runner.timer_end_time = float(data.get("end_time", 0))
                        runner.timer_duration = float(data.get("duration", 0))
                        print(f"[agent] timer_config: end={runner.timer_end_time} dur={runner.timer_duration}", flush=True)
                        if runner.mode == 10:
                            # Restart countdown with new end time
                            runner.countdown_proc = runner._stop("countdown", runner.countdown_proc)
                            runner._start_countdown()
                    elif data.get("type") == "timer_expired":
                        # Backend signals that the timer just expired — switch to mode 10
                        print("[agent] timer_expired: switching to mode 10", flush=True)
                        runner.apply_mode(10)
                    elif data.get("type") == "brightness_config":
                        runner.idle_brightness      = int(data.get("idle_brightness", 20))
                        runner.dim_schedule_enabled = bool(data.get("dim_schedule_enabled", False))
                        runner.dim_start            = data.get("dim_start", "")
                        runner.dim_end              = data.get("dim_end", "")
                        print(f"[agent] brightness_config: idle={runner.idle_brightness} "
                              f"dim={runner.dim_schedule_enabled} {runner.dim_start}→{runner.dim_end}", flush=True)
                        runner._apply_effective_brightness()
                    elif data.get("type") == "screensaver_config":
                        anims     = data.get("animations", "rain,fire,plasma")
                        cycle_t   = float(data.get("cycle_time", 25.0))
                        fade_t    = float(data.get("fade_time", 2.0))
                        changed   = (anims   != runner.screensaver_animations or
                                     cycle_t != runner.screensaver_cycle_time  or
                                     fade_t  != runner.screensaver_fade_time)
                        runner.screensaver_animations = anims
                        runner.screensaver_cycle_time = cycle_t
                        runner.screensaver_fade_time  = fade_t
                        print(f"[agent] screensaver_config: anims={anims} cycle={cycle_t}s fade={fade_t}s", flush=True)
                        if changed and runner.mode == 11:
                            runner.screensaver_proc = runner._stop("screensaver", runner.screensaver_proc)
                            runner._start_screensaver()
                    elif data.get("type") == "stopwatch_config":
                        runner.stopwatch_start_time = float(data.get("start_time", 0))
                        print(f"[agent] stopwatch_config: start={runner.stopwatch_start_time}", flush=True)
                        if runner.mode == 12:
                            # Restart stopwatch with new start time
                            runner.stopwatch_proc = runner._stop("stopwatch", runner.stopwatch_proc)
                            runner._start_stopwatch()
                    elif data.get("type") == "schedule":
                        runner.schedule_enabled = bool(data.get("enabled", False))
                        runner.schedule_slots   = data.get("slots", [])
                        print(f"[agent] schedule updated: enabled={runner.schedule_enabled}, {len(runner.schedule_slots)} slots", flush=True)
                        runner._check_schedule()
                    elif data.get("type") == "stocks_config":
                        print("[agent] stocks_config updated — restarting if running", flush=True)
                        if runner.mode == 14:
                            runner.stocks_proc = runner._stop("stocks", runner.stocks_proc)
                            runner._start_stocks()
                    elif data.get("type") == "cmd" and data.get("cmd") == "update":
                        _do_update()
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
