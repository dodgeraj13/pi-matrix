#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weather display – 64x64 RGB LED matrix (Mode 4).

Layout (PIL-rendered):
  [ 16×16 icon ]  Condition text
  Large current temperature (color-graded)
  L: 58°                   H: 85°
  ─────────────────────────────────
  ☀ 6:32       💧 65%       8:15 ☽
"""

import os, sys, time, math, argparse, configparser, inspect, requests, gc
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont

def _add_path(p: str):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

_HOME = os.environ.get("HOME", "/home/pi_two")
_add_path(f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/bindings/python")

from rgbmatrix import RGBMatrix, RGBMatrixOptions

HEARTBEAT = "/tmp/matrix-heartbeat-4"

# ── Args / config ─────────────────────────────────────────────────────────────

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

# ── Weather fetch ─────────────────────────────────────────────────────────────

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
        "temp":     current.get("temp"),
        "tmin":     (daily0.get("temp") or {}).get("min"),
        "tmax":     (daily0.get("temp") or {}).get("max"),
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
        "temp":     main.get("temp"),
        "tmin":     main.get("temp_min"),
        "tmax":     main.get("temp_max"),
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

# ── Temperature utilities ─────────────────────────────────────────────────────

def normalize_temp_value(v, units):
    if v is None: return None
    try: v = float(v)
    except: return None
    u = (units or "").lower()
    if u.startswith("imp") or u.startswith("met"):
        return v
    return v - 273.15   # Kelvin fallback

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
    if temp <= mid:
        return lerp_rgb(c1, c2, (temp - lo) / (mid - lo))
    return lerp_rgb(c2, c3, (temp - mid) / (hi - mid))

# ── Time formatting ───────────────────────────────────────────────────────────

def fmt_hhmm(ts, tz_off):
    if not ts: return "--:--"
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        if tz_off:
            dt = dt + timedelta(seconds=int(tz_off))
        s = dt.strftime("%I:%M")
        return s.lstrip("0") or "0:00"
    except Exception:
        return "--:--"

# ── PIL fonts ─────────────────────────────────────────────────────────────────

_TTF_CANDIDATES = [
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

def _load_ttf(size):
    for p in _TTF_CANDIDATES:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except: pass
    return ImageFont.load_default()

_F_TEMP  = None   # large current temp
_F_MED   = None   # lo/hi temps
_F_SMALL = None   # condition, sunrise/sunset/humidity

def _init_fonts():
    global _F_TEMP, _F_MED, _F_SMALL
    _F_TEMP  = _load_ttf(15)
    _F_MED   = _load_ttf(9)
    _F_SMALL = _load_ttf(7)

def _tsz(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]

def _cx(draw, text, font, W=64):
    w, _ = _tsz(draw, text, font)
    return max(0, (W - w) // 2)

# ── Drawn symbols ─────────────────────────────────────────────────────────────

def draw_sun(draw, cx, cy, r=4):
    """Small sun: yellow filled circle + 8 short rays."""
    col = (255, 210, 0)
    draw.ellipse([cx - r + 1, cy - r + 1, cx + r - 1, cy + r - 1], fill=col)
    for deg in range(0, 360, 45):
        rad = math.radians(deg)
        x1 = cx + int(r * math.cos(rad))
        y1 = cy + int(r * math.sin(rad))
        x2 = cx + int((r + 2) * math.cos(rad))
        y2 = cy + int((r + 2) * math.sin(rad))
        draw.line([x1, y1, x2, y2], fill=col, width=1)

def draw_crescent(draw, cx, cy, r=4):
    """Clean crescent moon: outer disk minus offset cutout."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(200, 215, 255))
    cut_r = max(1, int(r * 0.82))
    cut_x = cx + int(r * 0.55)
    cut_y = cy - int(r * 0.25)
    draw.ellipse([cut_x - cut_r, cut_y - cut_r,
                  cut_x + cut_r, cut_y + cut_r], fill=(0, 0, 0))

def draw_raindrop(draw, cx, cy):
    """Tiny water drop (5×7 px)."""
    col = (80, 160, 255)
    draw.ellipse([cx - 2, cy, cx + 2, cy + 4], fill=col)
    draw.polygon([(cx, cy - 3), (cx - 2, cy + 1), (cx + 2, cy + 1)], fill=col)

# ── Weather icon (MLB scoreboard PNG assets, tinted) ─────────────────────────

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
        print(f"[weather] icon loaded: {code} → {os.path.basename(p)}", flush=True)
        return raw
    except Exception as e:
        print(f"[weather] icon load error {code}: {e}", flush=True)
        ICON_CACHE[key] = None
        return None

# ── Rendering ─────────────────────────────────────────────────────────────────

def _fit_condition(draw, text, max_w):
    """Split condition text into (line1, line2) each fitting max_w px at _F_SMALL."""
    words = (text or "").split()
    line1, line2 = "", ""
    for word in words:
        candidate = (line1 + " " + word).strip()
        if _tsz(draw, candidate, _F_SMALL)[0] <= max_w:
            line1 = candidate
        else:
            candidate2 = (line2 + " " + word).strip()
            if _tsz(draw, candidate2, _F_SMALL)[0] <= max_w:
                line2 = candidate2
            # else: word dropped (extremely long strings)
    return line1, line2


def draw_frame(wdata):
    """Return a 64×64 RGB PIL Image for the given weather data."""
    W, H = 64, 64
    img  = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    units    = wdata.get("units", "imperial")
    cur_raw  = wdata.get("temp")
    tmin_raw = wdata.get("tmin")
    tmax_raw = wdata.get("tmax")
    icon_cd  = wdata.get("icon", "01d")
    sunrise  = wdata.get("sunrise")
    sunset   = wdata.get("sunset")
    tz_off   = wdata.get("tz_offset") or 0
    humid    = wdata.get("humidity")
    cond     = wdata.get("condition", "")

    cur  = normalize_temp_value(cur_raw,  units)
    tmin = normalize_temp_value(tmin_raw, units)
    tmax = normalize_temp_value(tmax_raw, units)

    # ── Top section: icon + condition ────────────────────────────────────────
    icon_img = _load_icon(icon_cd)
    if icon_img:
        img.paste(icon_img, (1, 1), icon_img)
    else:
        # fallback drawn sun (or moon if night code)
        is_night = len(icon_cd or "") >= 3 and icon_cd[2] == "n"
        if is_night:
            draw_crescent(draw, 9, 9, r=6)
        else:
            draw_sun(draw, 9, 9, r=5)

    # Condition text: right of icon
    x_cond  = 1 + ICON_SIZE + 3   # x=20
    avail_w = W - x_cond - 1      # ~43 px
    l1, l2  = _fit_condition(draw, cond, avail_w)
    cond_col = (175, 190, 215)
    if l2:
        draw.text((x_cond, 2),  l1, font=_F_SMALL, fill=cond_col)
        draw.text((x_cond, 10), l2, font=_F_SMALL, fill=cond_col)
    else:
        # vertically center single line in icon height
        _, th = _tsz(draw, l1, _F_SMALL)
        y_l1 = 1 + (ICON_SIZE - th) // 2
        draw.text((x_cond, y_l1), l1, font=_F_SMALL, fill=cond_col)

    # ── Current temperature (large, color-graded) ────────────────────────────
    y_temp = 19
    t_cu   = "--" if cur  is None else f"{int(round(cur))}°"
    col_cu = temp_color(cur, units)
    draw.text((_cx(draw, t_cu, _F_TEMP), y_temp), t_cu, font=_F_TEMP, fill=col_cu)

    # ── Low / High (flanking, small, colored) ────────────────────────────────
    _, th_temp = _tsz(draw, t_cu, _F_TEMP)
    y_lh  = y_temp + th_temp + 1
    t_lo  = "--" if tmin is None else f"L:{int(round(tmin))}°"
    t_hi  = "--" if tmax is None else f"H:{int(round(tmax))}°"
    col_lo = temp_color(tmin, units)
    col_hi = temp_color(tmax, units)
    draw.text((2, y_lh), t_lo, font=_F_MED, fill=col_lo)
    w_hi, _ = _tsz(draw, t_hi, _F_MED)
    draw.text((W - 2 - w_hi, y_lh), t_hi, font=_F_MED, fill=col_hi)

    # ── Separator ────────────────────────────────────────────────────────────
    _, th_lh = _tsz(draw, t_lo, _F_MED)
    y_sep = y_lh + th_lh + 2
    draw.line([(0, y_sep), (W - 1, y_sep)], fill=(40, 40, 40))

    # ── Bottom bar: sunrise | humidity | sunset ───────────────────────────────
    # Three equal thirds of the 64px width (~21px each).
    y_bot  = y_sep + 3
    icon_r = 3                      # radius for tiny sun/moon
    icon_cy = y_bot + icon_r + 1    # vertical center for icons

    sr_txt  = fmt_hhmm(sunrise, tz_off)
    ss_txt  = fmt_hhmm(sunset,  tz_off)
    hum_txt = f"{int(humid)}%" if humid is not None else "--%"

    third = W // 3   # 21

    # — Sunrise (left third) —
    sun_cx = 1 + icon_r
    draw_sun(draw, sun_cx, icon_cy, r=icon_r)
    draw.text((sun_cx + icon_r + 2, y_bot), sr_txt,
              font=_F_SMALL, fill=(240, 220, 120))

    # — Humidity (middle third) —
    drop_cx = third + 2
    draw_raindrop(draw, drop_cx, icon_cy - 2)
    draw.text((drop_cx + 5, y_bot), hum_txt,
              font=_F_SMALL, fill=(100, 185, 255))

    # — Sunset (right third) —
    w_ss, _ = _tsz(draw, ss_txt, _F_SMALL)
    moon_cx = W - 1 - icon_r
    ss_x    = moon_cx - icon_r - 2 - w_ss
    draw.text((ss_x, y_bot), ss_txt, font=_F_SMALL, fill=(200, 190, 255))
    draw_crescent(draw, moon_cx, icon_cy, r=icon_r)

    return img

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    cfg  = load_config()
    _init_fonts()

    opts = RGBMatrixOptions()
    opts.rows = 64
    opts.cols = 64
    opts.hardware_mapping  = args.hardware_mapping
    if args.brightness is not None:
        opts.brightness = max(0, min(100, int(args.brightness)))
    opts.gpio_slowdown        = int(args.gpio_slowdown)
    opts.limit_refresh_rate_hz = 0
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper
    opts.drop_privileges = False

    matrix = RGBMatrix(options=opts)

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

            # Redraw only when minute changes or new data arrived
            current_minute = int(now) // 60
            if weather_dirty or current_minute != last_drawn_minute:
                try:
                    pil_img = draw_frame(cache or {})
                    matrix.SetImage(pil_img, 0, 0)
                    last_drawn_minute = current_minute
                    weather_dirty     = False
                except Exception as e:
                    sys.stderr.write(f"[weather] draw error: {e}\n")

            time.sleep(1.0)

    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
