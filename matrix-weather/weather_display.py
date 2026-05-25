#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Weather display with MLB PNG condition icons; offscreen canvas reused to prevent memory leaks.
# Tweaks: prefer medium clock font (12x24.bdf) in patched fonts folder.

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

# ---------------- args/config ----------------

def parse_args():
    ap = argparse.ArgumentParser(prog="MatrixWeatherDisplay")
    ap.add_argument("--brightness", type=int, default=None)
    ap.add_argument("--pixel-mapper", type=str, default=None)
    ap.add_argument("--hardware-mapping", type=str, default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown", type=int, default=2)
    ap.add_argument("--update-interval", type=int, default=180)
    return ap.parse_args()

def load_config():
    cfg = configparser.ConfigParser()
    here = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    ini_path = os.path.join(here, "weather.ini")
    if os.path.exists(ini_path):
        cfg.read(ini_path)
    section = cfg["Weather"] if "Weather" in cfg else {}
    api_key  = os.getenv("WEATHER_API_KEY")  or section.get("api_key", "")
    location = os.getenv("WEATHER_LOCATION") or section.get("location", "Los Angeles,US")
    units    = os.getenv("WEATHER_UNITS")    or section.get("units", "imperial")
    provider = os.getenv("WEATHER_PROVIDER") or section.get("provider", "openweathermap")
    return {"api_key": api_key, "location": location, "units": units, "provider": provider}

def _is_numberlike(s: str):
    try:
        float(s); return True
    except Exception:
        return False

def parse_location(loc: str):
    loc = (loc or "").strip()
    if "," in loc:
        a, b = [x.strip() for x in loc.split(",", 1)]
        if _is_numberlike(a) and _is_numberlike(b):
            return {"lat": a, "lon": b}
        return {"q": loc}
    return {"q": loc}

# ---------------- weather fetch ----------------

def get_coords_for_location(api_key: str, location: str, units: str):
    base = "https://api.openweathermap.org/data/2.5/weather"
    params = {"appid": api_key, "units": units}
    params.update(parse_location(location))
    r = requests.get(base, params=params, timeout=8)
    r.raise_for_status()
    data = r.json()
    coord = data.get("coord") or {}
    tz_off = data.get("timezone")
    return (coord.get("lat"), coord.get("lon")), tz_off, data

def ow_onecall_try(api_key: str, lat: float, lon: float, units: str):
    base = "https://api.openweathermap.org/data/2.5/onecall"
    params = {"appid": api_key, "lat": lat, "lon": lon, "units": units, "exclude": "minutely,hourly,alerts"}
    r = requests.get(base, params=params, timeout=8)
    r.raise_for_status()
    return r.json()

def normalize_from_onecall(one, units: str):
    current = one.get("current", {})
    daily0 = (one.get("daily") or [{}])[0]
    tz_off = int(one.get("timezone_offset", 0))
    temp = current.get("temp")
    wlist = current.get("weather") or daily0.get("weather") or [{}]
    icon = wlist[0].get("icon", "")
    desc = wlist[0].get("description", "") or wlist[0].get("main", "")
    tmin = (daily0.get("temp") or {}).get("min", None)
    tmax = (daily0.get("temp") or {}).get("max", None)
    sunrise = daily0.get("sunrise", current.get("sunrise"))
    sunset  = daily0.get("sunset",  current.get("sunset"))
    humidity = current.get("humidity")
    return {
        "temp": temp, "tmin": tmin, "tmax": tmax,
        "condition": (desc or "").title(),
        "icon": icon, "sunrise": sunrise, "sunset": sunset,
        "humidity": humidity, "tz_offset": tz_off, "units": units
    }

def normalize_from_current(current, units: str, tz_off_guess=None):
    main = current.get("main", {})
    sysb = current.get("sys", {})
    w = (current.get("weather") or [{}])[0]
    off = 0 if tz_off_guess is None else int(tz_off_guess)
    return {
        "temp": main.get("temp"),
        "tmin": main.get("temp_min"),
        "tmax": main.get("temp_max"),
        "condition": (w.get("description") or w.get("main") or "").title(),
        "icon": w.get("icon", ""),
        "sunrise": sysb.get("sunrise"),
        "sunset":  sysb.get("sunset"),
        "humidity": main.get("humidity"),
        "tz_offset": off,
        "units": units
    }

def fetch_weather(cfg):
    api_key = cfg["api_key"]
    if not api_key:
        raise RuntimeError("OpenWeatherMap requires WEATHER_API_KEY")
    units = cfg["units"]
    loc = parse_location(cfg["location"])
    if "lat" in loc and "lon" in loc:
        try:
            return normalize_from_onecall(ow_onecall_try(api_key, float(loc["lat"]), float(loc["lon"]), units), units)
        except Exception:
            pass
    (latlon, tz_off, cur_data) = get_coords_for_location(api_key, cfg["location"], units)
    lat, lon = latlon
    if lat is not None and lon is not None:
        try:
            return normalize_from_onecall(ow_onecall_try(api_key, float(lat), float(lon), units), units)
        except Exception:
            pass
    return normalize_from_current(cur_data, units, tz_off_guess=tz_off)

# ---------------- temp normalization/colors ----------------

def k_to_c(k): return k - 273.15

def normalize_temp_value(v, units: str):
    if v is None:
        return None
    u = (units or "").lower()
    try:
        v = float(v)
    except Exception:
        return None
    # OpenWeather values are already correct when you pass units=imperial/metric.
    if u.startswith("imp") or u.startswith("met"):
        return v
    # Fallback: Kelvin -> Celsius
    return k_to_c(v)

def lerp(a, b, t): return a + (b - a) * t
def lerp_rgb(c1, c2, t):
    return (int(lerp(c1[0], c2[0], t)),
            int(lerp(c1[1], c2[1], t)),
            int(lerp(c1[2], c2[2], t)))

def temp_color(temp, units: str):
    if temp is None:
        return (220, 230, 255)
    if (units or "").lower().startswith("imp"):
        lo, mid, hi = 20.0, 70.0, 100.0
    else:
        lo, mid, hi = -10.0, 21.0, 38.0
    c1, c2, c3 = (80, 160, 255), (255, 210, 0), (255, 80, 60)
    if temp <= lo: return c1
    if temp >= hi: return c3
    if temp <= mid:
        t = max(0, min(1, (temp - lo) / (mid - lo)))
        return lerp_rgb(c1, c2, t)
    t = max(0, min(1, (temp - mid) / (hi - mid)))
    return lerp_rgb(c2, c3, t)

def gcol(tup): return graphics.Color(tup[0], tup[1], tup[2])

# ---------------- fonts ----------------

FONT_ROOTS = [
    f"{_HOME}/mlb-led-scoreboard/assets/fonts/patched",
    f"{_HOME}/mlb-led-scoreboard/assets/fonts",
    f"{_HOME}/mlb-led-scoreboard/rpi-rgb-led-matrix/fonts",
    f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/fonts",
    f"{_HOME}/rpi-rgb-led-matrix/fonts",
]

# Prefer your new medium font:
CLOCK_BDFS = ["12x24.bdf", "10x20.bdf", "9x18B.bdf", "9x18.bdf"]
CURR_BDFS  = ["12x24.bdf", "10x20.bdf", "9x18B.bdf"]
SMALL_BDFS = ["7x13.bdf", "6x12.bdf", "6x10.bdf", "5x8.bdf"]

def find_font_path(candidates):
    for root in FONT_ROOTS:
        for name in candidates:
            p = os.path.join(root, name)
            if os.path.exists(p):
                return p
    return None

def load_font(candidates):
    p = find_font_path(candidates)
    f = graphics.Font()
    if p:
        f.LoadFont(p)
        return f, p
    p_fallback = find_font_path(["6x10.bdf"]) or find_font_path(["5x8.bdf"])
    if p_fallback:
        f.LoadFont(p_fallback)
        return f, p_fallback
    return f, "none-found"

# ---------------- time formatting ----------------

def fmt_hhmm(ts, tz_off):
    if not ts:
        return "--:--"
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        if tz_off:
            dt = dt + timedelta(seconds=int(tz_off))
        s = dt.strftime("%I:%M")
        return s[1:] if s.startswith("0") else s
    except Exception:
        return "--:--"

def now_local_string(tz_off):
    try:
        dt = datetime.utcnow().replace(tzinfo=timezone.utc)
        if tz_off:
            dt = dt + timedelta(seconds=int(tz_off))
        s = dt.strftime("%I:%M")
        return s[1:] if s.startswith("0") else s
    except Exception:
        s = datetime.now().strftime("%I:%M")
        return s[1:] if s.startswith("0") else s

# ---------------- icons ----------------

ICON_DIR = f"{_HOME}/mlb-led-scoreboard/assets/weather"
ICON_CACHE = {}
ICON_SIZE = 14

def _icon_path_for(code: str):
    code = (code or "").strip()
    if len(code) >= 3 and code[:2].isdigit():
        exact = os.path.join(ICON_DIR, f"{code[:3]}.png")
        if os.path.exists(exact): return exact
        day = os.path.join(ICON_DIR, f"{code[:2]}d.png")
        if os.path.exists(day): return day
    dflt = os.path.join(ICON_DIR, "01d.png")
    return dflt if os.path.exists(dflt) else None

def _load_icon(code: str):
    key = code or "default"
    if key in ICON_CACHE:
        return ICON_CACHE[key]
    p = _icon_path_for(code)
    if not p:
        ICON_CACHE[key] = None
        print(f"[weather] icon {code!r} -> none", flush=True)
        return None
    try:
        img = Image.open(p).convert("RGBA").resize((ICON_SIZE, ICON_SIZE), Image.NEAREST)
        ICON_CACHE[key] = img
        print(f"[weather] icon {code!r} -> {os.path.basename(p)}", flush=True)
        return img
    except Exception as e:
        print(f"[weather] icon load failed for {p}: {e}", flush=True)
        ICON_CACHE[key] = None
        return None

def _tint_for_code(code: str):
    head = (code or "")[:2]
    is_night = (len(code or "") >= 3 and (code[2] == "n"))
    if head == "01":
        base = (255, 220, 0)
    elif head in ("02", "03", "04"):
        base = (210, 220, 235)
    elif head in ("09", "10"):
        base = (90, 150, 255)
    elif head == "11":
        base = (255, 230, 90)
    elif head == "13":
        base = (230, 240, 255)
    elif head == "50":
        base = (190, 200, 210)
    else:
        base = (255, 220, 0)
    if is_night:
        base = tuple(int(c * 0.85) for c in base)
    return base

def _blit_tinted_rgba(canvas, img, x, y, tint_rgb):
    if img is None:
        return
    tr, tg, tb = tint_rgb
    w, h = img.size
    px = img.load()
    for j in range(h):
        yy = y + j
        if yy < 0 or yy >= 64:
            continue
        for i in range(w):
            xx = x + i
            if xx < 0 or xx >= 64:
                continue
            _, _, _, a = px[i, j]
            if a > 16:
                canvas.SetPixel(xx, yy, tr, tg, tb)

# ---------------- tiny drawings ----------------

def draw_sun_small(canvas, x, y):
    col = graphics.Color(255, 210, 0)
    canvas.SetPixel(x, y, col.red, col.green, col.blue)
    for k in range(8):
        a = k * (math.pi / 4)
        x2 = x + int(2 * math.cos(a))
        y2 = y + int(2 * math.sin(a))
        graphics.DrawLine(canvas, x, y, x2, y2, col)

def draw_moon_small(canvas, x, y):
    """
    Crescent moon centered around (x,y).
    """
    r = 4
    cut_dx = 2
    cut_dy = -1
    crescent = graphics.Color(235, 240, 255)
    black = graphics.Color(0, 0, 0)

    cx, cy = x, y

    def fill_circle(ccx, ccy, rr, col):
        for yy in range(-rr, rr + 1):
            for xx in range(-rr, rr + 1):
                if xx*xx + yy*yy <= rr*rr:
                    px = ccx + xx
                    py = ccy + yy
                    if 0 <= px < 64 and 0 <= py < 64:
                        canvas.SetPixel(px, py, col.red, col.green, col.blue)

    fill_circle(cx, cy, r, crescent)
    fill_circle(cx + cut_dx, cy + cut_dy, r, black)

    # small highlight
    if 0 <= cx - 2 < 64 and 0 <= cy - 2 < 64:
        canvas.SetPixel(cx - 2, cy - 2, 255, 255, 255)

def clear_canvas(canvas):
    black = graphics.Color(0, 0, 0)
    for y in range(64):
        graphics.DrawLine(canvas, 0, y, 63, y, black)

def measure_text_on(canvas, font, text):
    return graphics.DrawText(canvas, font, -9999, -9999, graphics.Color(0, 0, 0), text)

# ---------------- render ----------------

def draw_frame(offscreen, fonts, wdata):
    W = 64
    clear_canvas(offscreen)

    units   = wdata.get("units")
    cur_raw = wdata.get("temp")
    tmin_raw= wdata.get("tmin")
    tmax_raw= wdata.get("tmax")
    icon_cd = wdata.get("icon")
    sunrise = wdata.get("sunrise")
    sunset  = wdata.get("sunset")
    tz_off  = wdata.get("tz_offset") or 0
    humid   = wdata.get("humidity")

    cur  = normalize_temp_value(cur_raw,  units)
    tmin = normalize_temp_value(tmin_raw, units)
    tmax = normalize_temp_value(tmax_raw, units)

    f_small, f_clock, f_curr = fonts["small"], fonts["clock"], fonts["curr"]
    col_clock = graphics.Color(220, 230, 255)

    # Top-left icon
    icon_img = _load_icon(icon_cd)
    if icon_img is None:
        draw_sun_small(offscreen, 8, 8)
    else:
        _blit_tinted_rgba(offscreen, icon_img, 2, 2, _tint_for_code(icon_cd))

    # Humidity (top-right)
    hum_txt = ("%d%%" % int(humid)) if humid is not None else "--%"
    graphics.DrawText(offscreen, f_small, W - 22, 12, graphics.Color(170, 210, 255), hum_txt)

    # Clock (center)
    clock_txt = now_local_string(tz_off)
    cw = measure_text_on(offscreen, f_clock, clock_txt)
    clk_x = (W - cw) // 2
    clk_y = 31  # tuned for 12x24; adjust +/- 1 if needed
    graphics.DrawText(offscreen, f_clock, clk_x, clk_y, col_clock, clock_txt)

    # Sunrise / Sunset
    base_y = 61
    f_tiny, _ = load_font(["5x8.bdf", "6x10.bdf"])
    sr_txt = fmt_hhmm(sunrise, tz_off)
    ss_txt = fmt_hhmm(sunset,  tz_off)

    draw_sun_small(offscreen, 6, base_y - 3)
    graphics.DrawText(offscreen, f_tiny, 11, base_y, graphics.Color(240, 220, 120), sr_txt)

    ss_w = measure_text_on(offscreen, f_tiny, ss_txt)
    ss_text_x = W - 2 - ss_w
    # Place the moon LEFT of the sunset time so it doesn't collide.
    draw_moon_small(offscreen, ss_text_x - 8, base_y - 4)
    graphics.DrawText(offscreen, f_tiny, ss_text_x - 2, base_y, graphics.Color(220, 200, 255), ss_txt)

    # Temps row
    t_lo = "--" if tmin is None else str(int(round(tmin)))
    t_hi = "--" if tmax is None else str(int(round(tmax)))
    t_cu = "--" if cur  is None else str(int(round(cur)))

    col_lo = gcol(temp_color(tmin, units))
    col_hi = gcol(temp_color(tmax, units))
    col_cu = gcol(temp_color(cur,  units))

    w_lo = measure_text_on(offscreen, f_small, t_lo)
    w_hi = measure_text_on(offscreen, f_small, t_hi)
    w_cu = measure_text_on(offscreen, f_curr,  t_cu)

    gap = 6
    total = w_lo + gap + w_cu + gap + w_hi
    x = (W - total) // 2

    y_row = 52
    graphics.DrawText(offscreen, f_small, x, y_row, col_lo, t_lo)
    x += w_lo + gap
    graphics.DrawText(offscreen, f_curr,  x, y_row, col_cu, t_cu)
    x += w_cu + gap
    graphics.DrawText(offscreen, f_small, x, y_row, col_hi, t_hi)

# ---------------- main loop ----------------

def main():
    args = parse_args()
    cfg  = load_config()

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
    off = matrix.CreateFrameCanvas()   # create once & reuse

    f_small, small_path = load_font(SMALL_BDFS)
    f_clock, clock_path = load_font(CLOCK_BDFS)
    f_curr,  curr_path  = load_font(CURR_BDFS)
    fonts = {"small": f_small, "clock": f_clock, "curr": f_curr}
    print(f"[weather] using fonts -> clock: {clock_path}, curr: {curr_path}, small: {small_path}", flush=True)

    last_fetch = 0.0
    last_hb    = 0.0
    last_drawn_minute = -1   # track which minute is currently on screen
    weather_dirty = True     # force a draw on startup
    cache = None
    interval = max(60, int(args.update_interval))

    try:
        while True:
            now = time.time()

            # Heartbeat every 30s
            if now - last_hb > 30.0:
                try:
                    with open("/tmp/matrix-heartbeat-4", "w") as f:
                        f.write(str(now))
                except Exception:
                    pass
                last_hb = now

            # Fetch weather periodically
            if now - last_fetch > interval or cache is None:
                try:
                    cache = fetch_weather(cfg)
                    weather_dirty = True
                    try:
                        import json
                        with open("/tmp/weather.json", "w") as wf:
                            json.dump({
                                "temp": str(int(cache.get("temp", 0))) if cache.get("temp") else "--",
                                "condition": cache.get("condition", "unknown"),
                                "icon": cache.get("icon", "01d")
                            }, wf)
                    except Exception:
                        pass
                except Exception as e:
                    sys.stderr.write("[weather] fetch failed: %s\n" % e)
                finally:
                    last_fetch = now

            # Only redraw when the displayed minute changes or new weather arrived.
            # The display is otherwise static — redrawing every 80ms just causes flicker.
            current_minute = int(now) // 60
            if weather_dirty or current_minute != last_drawn_minute:
                try:
                    draw_frame(off, fonts, cache or {})
                    off = matrix.SwapOnVSync(off)
                    last_drawn_minute = current_minute
                    weather_dirty = False
                except Exception as e:
                    sys.stderr.write("[weather] draw error: %s\n" % e)

            time.sleep(1.0)   # 1-second tick is plenty for a static weather display
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
