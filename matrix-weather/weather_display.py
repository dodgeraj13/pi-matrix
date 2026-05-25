#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weather display – 64x64 RGB LED matrix (Mode 4).

Rendering:
  · Text          → rgbmatrix.graphics BDF bitmap fonts (crisp, pixel-perfect)
  · Weather icon  → PNG asset loaded via PIL, blitted pixel-by-pixel
  · Sun/Moon/Drop → drawn directly on canvas via SetPixel

Layout (64×64):
  y= 1-16  : 16×16 weather icon, centered
  y=18-33  : Current temperature (9x15B, large, color-graded)
  y=35-43  : L:XX°   H:XX°  (5x8)
  y=45     : separator line
  y=47-52  : ☀ 6:32          7:45 ☽  (4x6)
  y=56-61  : 💧 65%              6:32  (4x6, humidity + current time)
"""

import os, sys, time, math, argparse, configparser, inspect, requests, gc
from datetime import datetime, timezone, timedelta
from PIL import Image

def _add_path(p: str):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

_HOME = os.environ.get("HOME", "/home/pi_two")
_add_path(f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/bindings/python")

from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

HEARTBEAT = "/tmp/matrix-heartbeat-4"

# ── BDF fonts ──────────────────────────────────────────────────────────────────
_FONT_DIR = f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/fonts"

_F_LARGE = None   # 9x15B – current temperature
_F_MED   = None   # 5x8   – L/H temps
_F_SMALL = None   # 4x6   – bottom bar (sunrise/sunset/humidity/time)


def _load_bdf(name):
    f = graphics.Font()
    p = os.path.join(_FONT_DIR, name)
    if os.path.exists(p):
        f.LoadFont(p)
    else:
        print(f"[weather] font not found: {p}", flush=True)
    return f


def _init_fonts():
    global _F_LARGE, _F_MED, _F_SMALL
    _F_LARGE = _load_bdf("9x15B.bdf")
    _F_MED   = _load_bdf("5x8.bdf")
    _F_SMALL = _load_bdf("4x6.bdf")


# ── Args / config ──────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser(prog="MatrixWeatherDisplay")
    ap.add_argument("--brightness",       type=int, default=None)
    ap.add_argument("--pixel-mapper",     type=str, default=None)
    ap.add_argument("--hardware-mapping", type=str, default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown",    type=int, default=2)
    ap.add_argument("--update-interval",  type=int, default=180)
    return ap.parse_args()


def load_config():
    cfg = configparser.ConfigParser()
    here = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    ini_path = os.path.join(here, "weather.ini")
    if os.path.exists(ini_path):
        cfg.read(ini_path)
    section = cfg["Weather"] if "Weather" in cfg else {}
    return {
        "api_key":  os.getenv("WEATHER_API_KEY")  or section.get("api_key",  ""),
        "location": os.getenv("WEATHER_LOCATION") or section.get("location", "Los Angeles,US"),
        "units":    os.getenv("WEATHER_UNITS")    or section.get("units",    "imperial"),
        "provider": os.getenv("WEATHER_PROVIDER") or section.get("provider", "openweathermap"),
    }


def _is_numberlike(s):
    try: float(s); return True
    except: return False


def parse_location(loc):
    loc = (loc or "").strip()
    if "," in loc:
        a, b = [x.strip() for x in loc.split(",", 1)]
        if _is_numberlike(a) and _is_numberlike(b):
            return {"lat": a, "lon": b}
        return {"q": loc}
    return {"q": loc}


# ── Weather fetch ──────────────────────────────────────────────────────────────

def get_coords_for_location(api_key, location, units):
    params = {"appid": api_key, "units": units}
    params.update(parse_location(location))
    r = requests.get("https://api.openweathermap.org/data/2.5/weather",
                     params=params, timeout=8)
    r.raise_for_status()
    data = r.json()
    coord = data.get("coord") or {}
    return (coord.get("lat"), coord.get("lon")), data.get("timezone"), data


def ow_onecall_try(api_key, lat, lon, units):
    r = requests.get("https://api.openweathermap.org/data/2.5/onecall",
                     params={"appid": api_key, "lat": lat, "lon": lon,
                             "units": units, "exclude": "minutely,hourly,alerts"},
                     timeout=8)
    r.raise_for_status()
    return r.json()


def normalize_from_onecall(one, units):
    current = one.get("current", {})
    daily0  = (one.get("daily") or [{}])[0]
    tz_off  = int(one.get("timezone_offset", 0))
    wlist   = current.get("weather") or daily0.get("weather") or [{}]
    icon    = wlist[0].get("icon", "")
    desc    = wlist[0].get("description", "") or wlist[0].get("main", "")
    return {
        "temp":      current.get("temp"),
        "tmin":      (daily0.get("temp") or {}).get("min"),
        "tmax":      (daily0.get("temp") or {}).get("max"),
        "condition": (desc or "").title(),
        "icon":      icon,
        "sunrise":   daily0.get("sunrise", current.get("sunrise")),
        "sunset":    daily0.get("sunset",  current.get("sunset")),
        "humidity":  current.get("humidity"),
        "tz_offset": tz_off,
        "units":     units,
    }


def normalize_from_current(data, units, tz_off_guess=None):
    main = data.get("main", {})
    sysb = data.get("sys", {})
    w    = (data.get("weather") or [{}])[0]
    return {
        "temp":      main.get("temp"),
        "tmin":      main.get("temp_min"),
        "tmax":      main.get("temp_max"),
        "condition": (w.get("description") or w.get("main") or "").title(),
        "icon":      w.get("icon", ""),
        "sunrise":   sysb.get("sunrise"),
        "sunset":    sysb.get("sunset"),
        "humidity":  main.get("humidity"),
        "tz_offset": int(tz_off_guess or 0),
        "units":     units,
    }


def fetch_weather(cfg):
    api_key = cfg["api_key"]
    if not api_key:
        raise RuntimeError("WEATHER_API_KEY not set")
    units = cfg["units"]
    loc   = parse_location(cfg["location"])
    if "lat" in loc and "lon" in loc:
        try:
            return normalize_from_onecall(
                ow_onecall_try(api_key, float(loc["lat"]), float(loc["lon"]), units), units)
        except Exception:
            pass
    (latlon, tz_off, cur_data) = get_coords_for_location(api_key, cfg["location"], units)
    lat, lon = latlon
    if lat is not None:
        try:
            return normalize_from_onecall(
                ow_onecall_try(api_key, float(lat), float(lon), units), units)
        except Exception:
            pass
    return normalize_from_current(cur_data, units, tz_off_guess=tz_off)


# ── Temperature utilities ──────────────────────────────────────────────────────

def normalize_temp_value(v, units):
    if v is None: return None
    try: v = float(v)
    except: return None
    u = (units or "").lower()
    if u.startswith("imp") or u.startswith("met"):
        return v
    return v - 273.15


def lerp(a, b, t): return a + (b - a) * t


def lerp_rgb(c1, c2, t):
    return (int(lerp(c1[0], c2[0], t)),
            int(lerp(c1[1], c2[1], t)),
            int(lerp(c1[2], c2[2], t)))


def temp_color(temp, units):
    if temp is None: return (220, 230, 255)
    if (units or "").lower().startswith("imp"):
        lo, mid, hi = 20.0, 70.0, 100.0
    else:
        lo, mid, hi = -10.0, 21.0, 38.0
    c1, c2, c3 = (80, 160, 255), (255, 210, 0), (255, 80, 60)
    if temp <= lo:  return c1
    if temp >= hi:  return c3
    if temp <= mid: return lerp_rgb(c1, c2, (temp - lo) / (mid - lo))
    return lerp_rgb(c2, c3, (temp - mid) / (hi - mid))


# ── Time formatting ────────────────────────────────────────────────────────────

def fmt_hhmm(ts, tz_off):
    """Format a UTC timestamp as local H:MM."""
    if not ts: return "--:--"
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        if tz_off:
            dt = dt + timedelta(seconds=int(tz_off))
        s = dt.strftime("%I:%M")
        return s.lstrip("0") or "0:00"
    except Exception:
        return "--:--"


def fmt_current_time(tz_off):
    """Format the current local time as H:MM."""
    try:
        dt = datetime.now(tz=timezone.utc) + timedelta(seconds=int(tz_off or 0))
        s = dt.strftime("%I:%M")
        return s.lstrip("0") or "0:00"
    except Exception:
        return "--:--"


# ── Weather icon (PNG asset, tinted) ──────────────────────────────────────────

ICON_DIR   = f"{_HOME}/mlb-led-scoreboard/assets/weather"
ICON_SIZE  = 16
ICON_CACHE: dict = {}


def _icon_path(code):
    code = (code or "").strip()
    if len(code) >= 3 and code[:2].isdigit():
        p = os.path.join(ICON_DIR, f"{code[:3]}.png")
        if os.path.exists(p): return p
        p = os.path.join(ICON_DIR, f"{code[:2]}d.png")
        if os.path.exists(p): return p
    p = os.path.join(ICON_DIR, "01d.png")
    return p if os.path.exists(p) else None


def _tint_for_code(code):
    head  = (code or "")[:2]
    night = len(code or "") >= 3 and (code or "")[2] == "n"
    tints = {
        "01": (255, 220, 0),
        "02": (210, 225, 240), "03": (200, 215, 235), "04": (180, 200, 225),
        "09": (80, 140, 255),  "10": (100, 160, 255),
        "11": (255, 230, 80),
        "13": (230, 240, 255),
        "50": (185, 195, 210),
    }
    base = tints.get(head, (255, 220, 0))
    if night:
        base = tuple(int(c * 0.75) for c in base)
    return base


def _load_icon(code):
    key = code or "default"
    if key in ICON_CACHE:
        return ICON_CACHE[key]
    p = _icon_path(code)
    if not p:
        ICON_CACHE[key] = None
        return None
    try:
        raw  = Image.open(p).convert("RGBA").resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
        tint = _tint_for_code(code)
        tr, tg, tb = tint
        px = raw.load()
        for yy in range(ICON_SIZE):
            for xx in range(ICON_SIZE):
                r, g, b, a = px[xx, yy]
                if a > 16:
                    px[xx, yy] = (tr, tg, tb, a)
        ICON_CACHE[key] = raw
        print(f"[weather] icon loaded: {code}", flush=True)
        return raw
    except Exception as e:
        print(f"[weather] icon load error {code}: {e}", flush=True)
        ICON_CACHE[key] = None
        return None


# ── Pixel drawing helpers ──────────────────────────────────────────────────────

def _px(canvas, x, y, r, g, b):
    if 0 <= x < 64 and 0 <= y < 64:
        canvas.SetPixel(x, y, r, g, b)


def _hline(canvas, x0, x1, y, r, g, b):
    for x in range(x0, x1 + 1):
        _px(canvas, x, y, r, g, b)


def _blit_icon(canvas, pil_rgba, ox, oy):
    """Blit a tinted RGBA PIL image onto the canvas pixel-by-pixel."""
    px = pil_rgba.load()
    w, h = pil_rgba.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 16:
                _px(canvas, ox + x, oy + y, r, g, b)


def _draw_sun(canvas, cx, cy, r=4):
    """Filled circle + 8 short rays."""
    col = (255, 210, 0)
    cr  = max(1, r - 1)
    for dy in range(-cr, cr + 1):
        for dx in range(-cr, cr + 1):
            if dx * dx + dy * dy <= cr * cr:
                _px(canvas, cx + dx, cy + dy, *col)
    for deg in range(0, 360, 45):
        rad = math.radians(deg)
        x1 = cx + int(round(r * math.cos(rad)))
        y1 = cy + int(round(r * math.sin(rad)))
        x2 = cx + int(round((r + 2) * math.cos(rad)))
        y2 = cy + int(round((r + 2) * math.sin(rad)))
        steps = max(abs(x2 - x1), abs(y2 - y1), 1)
        for s in range(steps + 1):
            t = s / steps
            _px(canvas, int(x1 + t * (x2 - x1)), int(y1 + t * (y2 - y1)), *col)


def _draw_crescent(canvas, cx, cy, r=4):
    """Crescent moon via disk-minus-offset-disk pixel op."""
    cut_r  = max(1, int(r * 0.80))
    cut_cx = cx + int(r * 0.55)
    cut_cy = cy - int(r * 0.15)
    col = (200, 215, 255)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                ddx = (cx + dx) - cut_cx
                ddy = (cy + dy) - cut_cy
                if ddx * ddx + ddy * ddy > cut_r * cut_r:
                    _px(canvas, cx + dx, cy + dy, *col)


def _draw_raindrop(canvas, cx, cy):
    """Tiny 3×5 raindrop."""
    col = (80, 160, 255)
    for dy, row in [(-2, (0,)), (-1, (-1, 0, 1)), (0, (-1, 0, 1)),
                    (1, (-1, 0, 1)), (2, (0,))]:
        for dx in row:
            _px(canvas, cx + dx, cy + dy, *col)


# ── Layout constants ───────────────────────────────────────────────────────────
#
#  y= 1-16  : 16×16 icon, centered at x=24
#  y=18-33  : current temp  9x15B (baseline=33, ~15px ascent)
#  y=35-43  : L/H temps     5x8   (baseline=43, ~8px ascent)
#  y=45     : separator
#  y=47-52  : sunrise | sunset   4x6 (baseline=52); icons at cy=49
#  y=56-61  : humidity | time    4x6 (baseline=61); drop at cy=58
#
_Y_ICON_TOP   = 1
_X_ICON_LEFT  = 24   # (64 - 16) // 2
_Y_TEMP_BL    = 33   # 9x15B baseline
_Y_LH_BL      = 43   # 5x8 baseline
_Y_SEP        = 45
_Y_SUNMOON_BL = 52   # 4x6 baseline – sunrise/sunset row
_Y_BOT_BL     = 61   # 4x6 baseline – humidity/time row


def draw_frame(canvas, wdata, now_ts=None):
    """Draw all weather elements directly onto a matrix FrameCanvas."""
    canvas.Clear()

    units   = wdata.get("units", "imperial")
    cur_raw = wdata.get("temp")
    tmin_r  = wdata.get("tmin")
    tmax_r  = wdata.get("tmax")
    icon_cd = wdata.get("icon", "01d")
    sunrise = wdata.get("sunrise")
    sunset  = wdata.get("sunset")
    tz_off  = wdata.get("tz_offset") or 0
    humid   = wdata.get("humidity")

    cur  = normalize_temp_value(cur_raw, units)
    tmin = normalize_temp_value(tmin_r,  units)
    tmax = normalize_temp_value(tmax_r,  units)

    # ── Weather icon ──────────────────────────────────────────────────────────
    icon_img = _load_icon(icon_cd)
    if icon_img:
        _blit_icon(canvas, icon_img, _X_ICON_LEFT, _Y_ICON_TOP)
    else:
        is_night = len(icon_cd or "") >= 3 and icon_cd[2] == "n"
        if is_night:
            _draw_crescent(canvas, 32, 9, r=6)
        else:
            _draw_sun(canvas, 32, 9, r=6)

    # ── Current temperature (large, color-graded) ─────────────────────────────
    t_cur = "--" if cur is None else f"{int(round(cur))}\xb0"
    cr, cg, cb = temp_color(cur, units)
    col_cur = graphics.Color(cr, cg, cb)
    # 9x15B: ~9px per char; center in 64px
    tw    = len(t_cur) * 9
    x_cur = max(0, (64 - tw) // 2)
    graphics.DrawText(canvas, _F_LARGE, x_cur, _Y_TEMP_BL, col_cur, t_cur)

    # ── Low / High (5x8, ~5px per char) ──────────────────────────────────────
    t_lo = "--" if tmin is None else f"L:{int(round(tmin))}\xb0"
    t_hi = "--" if tmax is None else f"H:{int(round(tmax))}\xb0"
    cr_lo, cg_lo, cb_lo = temp_color(tmin, units)
    cr_hi, cg_hi, cb_hi = temp_color(tmax, units)
    graphics.DrawText(canvas, _F_MED, 1, _Y_LH_BL,
                      graphics.Color(cr_lo, cg_lo, cb_lo), t_lo)
    x_hi = 63 - len(t_hi) * 5
    graphics.DrawText(canvas, _F_MED, x_hi, _Y_LH_BL,
                      graphics.Color(cr_hi, cg_hi, cb_hi), t_hi)

    # ── Separator ─────────────────────────────────────────────────────────────
    _hline(canvas, 0, 63, _Y_SEP, 40, 40, 40)

    # ── Sunrise / Sunset row (4x6, ~4px per char) ────────────────────────────
    sr_txt  = fmt_hhmm(sunrise, tz_off)
    ss_txt  = fmt_hhmm(sunset,  tz_off)
    icon_cy1 = _Y_SUNMOON_BL - 3   # icon center for this row

    # Left: tiny sun + sunrise time
    _draw_sun(canvas, 3, icon_cy1, r=2)
    graphics.DrawText(canvas, _F_SMALL, 8, _Y_SUNMOON_BL,
                      graphics.Color(240, 220, 120), sr_txt)

    # Right: sunset time + tiny crescent
    _draw_crescent(canvas, 61, icon_cy1, r=2)
    x_ss = 58 - len(ss_txt) * 4
    graphics.DrawText(canvas, _F_SMALL, x_ss, _Y_SUNMOON_BL,
                      graphics.Color(200, 190, 255), ss_txt)

    # ── Humidity / Current time row (4x6) ────────────────────────────────────
    hum_txt  = f"{int(humid)}%" if humid is not None else "--%"
    time_txt = fmt_current_time(tz_off)
    icon_cy2 = _Y_BOT_BL - 3   # icon center for this row

    # Left: tiny raindrop + humidity
    _draw_raindrop(canvas, 3, icon_cy2)
    graphics.DrawText(canvas, _F_SMALL, 8, _Y_BOT_BL,
                      graphics.Color(100, 185, 255), hum_txt)

    # Right: current time (right-aligned)
    x_time = 63 - len(time_txt) * 4
    graphics.DrawText(canvas, _F_SMALL, x_time, _Y_BOT_BL,
                      graphics.Color(160, 165, 185), time_txt)


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    cfg  = load_config()
    _init_fonts()

    opts = RGBMatrixOptions()
    opts.rows = 64
    opts.cols = 64
    opts.hardware_mapping      = args.hardware_mapping
    if args.brightness is not None:
        opts.brightness = max(0, min(100, int(args.brightness)))
    opts.gpio_slowdown         = int(args.gpio_slowdown)
    opts.limit_refresh_rate_hz = 0
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper
    opts.drop_privileges = False

    matrix = RGBMatrix(options=opts)
    canvas = matrix.CreateFrameCanvas()

    last_fetch        = 0.0
    last_hb           = 0.0
    last_drawn_minute = -1
    weather_dirty     = True
    cache             = None
    interval          = max(60, int(args.update_interval))

    try:
        while True:
            now = time.time()

            # Heartbeat
            if now - last_hb > 30.0:
                try:
                    with open(HEARTBEAT, "w") as f:
                        f.write(str(now))
                except Exception:
                    pass
                last_hb = now

            # Fetch weather
            if now - last_fetch > interval or cache is None:
                try:
                    cache = fetch_weather(cfg)
                    weather_dirty = True
                    try:
                        import json
                        with open("/tmp/weather.json", "w") as wf:
                            json.dump({
                                "temp":      str(int(cache.get("temp", 0))) if cache.get("temp") else "--",
                                "condition": cache.get("condition", "unknown"),
                                "icon":      cache.get("icon", "01d"),
                            }, wf)
                    except Exception:
                        pass
                except Exception as e:
                    sys.stderr.write(f"[weather] fetch failed: {e}\n")
                finally:
                    last_fetch = now

            # Redraw every minute (time ticks) or on fresh weather data
            current_minute = int(now) // 60
            if weather_dirty or current_minute != last_drawn_minute:
                try:
                    draw_frame(canvas, cache or {}, now)
                    canvas = matrix.SwapOnVSync(canvas)
                    last_drawn_minute = current_minute
                    weather_dirty     = False
                except Exception as e:
                    sys.stderr.write(f"[weather] draw error: {e}\n")

            time.sleep(1.0)

    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
