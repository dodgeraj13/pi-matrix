#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Map mode display (mode 9).

Two alternating screens:

  BASIC  (every ~9 s):
    - Scrolling destination name
    - Current temperature at destination (large, centered)
    - Low / High temps
    - Estimated drive time in large font

  MAP VIEW (every ~9 s):
    - Mapbox Static Images API: traffic-day-v2 style, 64×64
    - Route drawn as GeoJSON overlay (blue line)
    - Live traffic colours visible underneath

Env vars:
  MAP_ADDRESS_A   origin address  (e.g. "123 Main St, San Francisco, CA")
  MAP_ADDRESS_B   destination     (e.g. "456 Sunset Blvd, Los Angeles, CA")
  MAP_LABEL_A     friendly label for origin   (e.g. "Home")
  MAP_LABEL_B     friendly label for destination (e.g. "Work")
  WEATHER_API_KEY OpenWeatherMap API key
  WEATHER_UNITS   imperial | metric   (default: imperial)
  MAPBOX_TOKEN    Mapbox public token (for Map View submode)

Args (passed by agent):
  --pixel-mapper  e.g. Rotate:90
"""

import argparse, io, os, sys, time

import requests

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("[map] Pillow not installed — Map View disabled. Install with: pip install Pillow", flush=True)

# ── Matrix binding path ───────────────────────────────────────────────────────
_HOME = os.getenv("HOME", "/home/pi_two")
for _p in [
    f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/bindings/python",
    f"{_HOME}/rpi-rgb-led-matrix/bindings/python",
]:
    if os.path.exists(_p) and _p not in sys.path:
        sys.path.append(_p)

from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

# ── Args ──────────────────────────────────────────────────────────────────────
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pixel-mapper",      type=str, default=None)
    ap.add_argument("--hardware-mapping",  type=str, default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown",     type=int, default=2)
    ap.add_argument("--brightness",        type=int, default=None)
    return ap.parse_args()

# ── Env ───────────────────────────────────────────────────────────────────────
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
WEATHER_UNITS   = os.getenv("WEATHER_UNITS", "imperial")
MAP_ADDRESS_A   = os.getenv("MAP_ADDRESS_A", "").strip()
MAP_ADDRESS_B   = os.getenv("MAP_ADDRESS_B", "").strip()
MAP_LABEL_A     = os.getenv("MAP_LABEL_A",   "").strip()   # e.g. "Home"
MAP_LABEL_B     = os.getenv("MAP_LABEL_B",   "").strip()   # e.g. "Work"
MAPBOX_TOKEN    = os.getenv("MAPBOX_TOKEN",  "").strip()
MAP_SUBMODE     = os.getenv("MAP_SUBMODE",   "alternate").strip()  # "basic" | "map" | "alternate"

HEARTBEAT_FILE     = "/tmp/matrix-heartbeat-9"
HEARTBEAT_INTERVAL = 30    # seconds
UPDATE_INTERVAL    = 300   # re-fetch data every 5 minutes
SCROLL_DELAY       = 0.05  # seconds per scroll tick
SUBMODE_INTERVAL   = 9     # seconds before switching between Basic and Map View

# ── APIs ──────────────────────────────────────────────────────────────────────
NOMINATIM         = "https://nominatim.openstreetmap.org/search"
OSRM              = "http://router.project-osrm.org/route/v1/driving"
OWM               = "https://api.openweathermap.org/data/2.5/weather"
MAPBOX            = "https://api.mapbox.com/styles/v1/dodgeraj/cmp4j7gvu000k01suhg6l5ylw/static"
MAPBOX_DIRECTIONS = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic"


def geocode(address: str):
    """Return (lat, lon, short_city_name) or None."""
    try:
        r = requests.get(NOMINATIM, params={
            "q": address, "format": "json", "limit": 1, "addressdetails": 1,
        }, headers={"User-Agent": "matrix-map-display/1.0"}, timeout=10)
        hits = r.json()
        if hits:
            d    = hits[0]
            addr = d.get("address", {})
            city = (addr.get("city") or addr.get("town") or addr.get("village")
                    or addr.get("county") or d["display_name"].split(",")[0])
            return float(d["lat"]), float(d["lon"]), city.strip()
    except Exception as e:
        print(f"[map] geocode error '{address}': {e}", flush=True)
    return None


_CONGESTION_COLOR = {
    "low":      (30,  200, 100),   # green  — free flow
    "moderate": (255, 200,   0),   # amber  — some traffic
    "heavy":    (255, 100,   0),   # orange — heavy traffic
    "severe":   (220,  50,  50),   # red    — standstill
}
_FREE_FLOW_COLOR = (30, 120, 255)  # blue   — unknown / no data


def get_route(lat_a, lon_a, lat_b, lon_b):
    """Return (duration_seconds, coords, seg_colors) or (None, None, []).

    seg_colors is a list of (r,g,b) tuples — one per consecutive coord pair.

    If MAPBOX_TOKEN is set, uses Mapbox Directions driving-traffic profile for
    real-time congestion colours.  Falls back to OSRM + theoretical road speeds.
    """
    if MAPBOX_TOKEN:
        try:
            url = (
                f"{MAPBOX_DIRECTIONS}"
                f"/{lon_a:.6f},{lat_a:.6f};{lon_b:.6f},{lat_b:.6f}"
                f"?geometries=geojson&overview=full&annotations=congestion"
                f"&access_token={MAPBOX_TOKEN}"
            )
            r = requests.get(url, timeout=15)
            d = r.json()
            if d.get("code") == "Ok":
                route      = d["routes"][0]
                dur        = route["duration"]
                coords     = route["geometry"]["coordinates"]
                congestion = route["legs"][0]["annotation"].get("congestion", [])
                colors     = [_CONGESTION_COLOR.get(c, _FREE_FLOW_COLOR) for c in congestion]
                print(f"[map] Mapbox directions OK — {len(coords)} pts, {len(colors)} segments", flush=True)
                return dur, coords, colors
        except Exception as e:
            print(f"[map] Mapbox directions error (falling back to OSRM): {e}", flush=True)

    # ── OSRM fallback ──────────────────────────────────────────────────────────
    try:
        url = (
            f"{OSRM}/{lon_a:.6f},{lat_a:.6f};{lon_b:.6f},{lat_b:.6f}"
            "?overview=full&geometries=geojson&annotations=speed"
        )
        r = requests.get(url, timeout=15)
        d = r.json()
        if d.get("code") == "Ok":
            route  = d["routes"][0]
            dur    = route["duration"]
            coords = route["geometry"]["coordinates"]
            speeds = route["legs"][0]["annotation"].get("speed", [])
            colors = [speed_to_color(s) for s in speeds]
            print(f"[map] OSRM OK — {len(coords)} pts, {len(colors)} segments", flush=True)
            return dur, coords, colors
    except Exception as e:
        print(f"[map] OSRM routing error: {e}", flush=True)

    return None, None, []


def get_weather(lat, lon, units="imperial"):
    """Return {temp, tmin, tmax, condition} or None."""
    if not WEATHER_API_KEY:
        return None
    try:
        r = requests.get(OWM, params={
            "lat": lat, "lon": lon, "appid": WEATHER_API_KEY, "units": units,
        }, timeout=10)
        d    = r.json()
        main = d.get("main", {})
        w    = (d.get("weather") or [{}])[0]
        return {
            "temp":      round(main.get("temp", 0)),
            "tmin":      round(main.get("temp_min", 0)),
            "tmax":      round(main.get("temp_max", 0)),
            "condition": (w.get("description") or w.get("main") or "").title(),
        }
    except Exception as e:
        print(f"[map] weather error: {e}", flush=True)
    return None


def simplify_route(coords, seg_colors, max_points=60):
    """Downsample coords to at most max_points; pick representative color per segment.

    Returns (simplified_coords, simplified_colors) where simplified_colors has
    len(simplified_coords)-1 entries — one (r,g,b) tuple per drawn segment.
    """
    n = len(coords)
    if n <= max_points:
        return coords, seg_colors

    step    = n / max_points
    sampled = [coords[int(i * step)] for i in range(max_points)]
    sampled.append(coords[-1])

    out_colors = []
    for i in range(len(sampled) - 1):
        # pick the color at the midpoint of this simplified segment's original span
        mid = int((i + 0.5) * step)
        mid = min(mid, len(seg_colors) - 1) if seg_colors else 0
        out_colors.append(seg_colors[mid] if seg_colors else _FREE_FLOW_COLOR)

    return sampled, out_colors


def bbox_center_zoom(coords, tile_px=64):
    """Calculate Mapbox center lon/lat and integer zoom to fit all coords.

    Mapbox uses 512px tiles at zoom 0.  For a tile_px-wide image:
      zoom = log2(tile_px * 360 / (512 * lon_span_deg))
    Latitude uses the Mercator cos() correction for accuracy.
    """
    import math
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)

    # 18 % padding on each side so the pins aren't clipped
    pad_lon = max((max_lon - min_lon) * 0.18, 0.005)
    pad_lat = max((max_lat - min_lat) * 0.18, 0.005)
    min_lon -= pad_lon; max_lon += pad_lon
    min_lat -= pad_lat; max_lat += pad_lat

    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2

    lon_span = max_lon - min_lon
    lat_span = max_lat - min_lat

    # Mercator latitude compression at the centre latitude
    lat_cos = math.cos(math.radians(center_lat))

    # zoom = log2(tile_px / 512 * 360 / lon_span)
    #       (tile_px/512 is the fraction of a full Mapbox tile we're filling)
    zoom_lon = math.log2(tile_px * 360 / (512 * lon_span)) if lon_span > 0 else 10
    zoom_lat = math.log2(tile_px * 360 * lat_cos / (512 * lat_span)) if lat_span > 0 else 10

    zoom = max(1, min(14, math.floor(min(zoom_lon, zoom_lat)) + 1))

    return center_lon, center_lat, zoom


def speed_to_color(mps):
    """Map OSRM speed (m/s) to a traffic RGB colour.

    OSRM speeds are road-type theoretical, not live traffic, but still give a
    useful relative signal: motorways fast, city streets slow.
      > 80 km/h  → blue/green (free flow)
      40–80 km/h → yellow     (moderate)
      < 40 km/h  → red        (slow / urban)
    """
    kmh = mps * 3.6
    if kmh > 80:
        return (30, 200, 100)   # green-blue — fast
    elif kmh > 40:
        return (255, 200, 0)    # amber — moderate
    else:
        return (220, 50, 50)    # red — slow


# ── PIL route drawing helpers ─────────────────────────────────────────────────

def _lonlat_to_px(lon, lat, clon, clat, zoom, tile_px, retina=2):
    """Mercator lon/lat → pixel (x, y) within a tile_px-square Mapbox tile.

    retina=2 because we fetch @2x: the 128×128 physical image covers the same
    geographic area as a 64×64 logical image, so each degree of displacement
    equals twice as many physical pixels compared to a plain 512-px world map.
    """
    import math
    scale = 512 * (2 ** zoom) * retina   # physical pixels per 360° at this zoom
    px = (lon - clon) * scale / 360 + tile_px / 2
    def _merc(d): return math.log(math.tan(math.pi / 4 + math.radians(d) / 2))
    py = -(_merc(lat) - _merc(clat)) * scale / (2 * math.pi) + tile_px / 2
    return int(round(px)), int(round(py))


def _draw_route(img, coords, clon, clat, zoom, speeds=None):
    """Draw the route polyline + origin/destination circles onto img (in-place).

    Each segment is coloured using OSRM speed annotations (speeds list, m/s):
      green-blue = fast, amber = moderate, red = slow.
    Falls back to blue if no speed data.
    """
    from PIL import ImageDraw

    draw      = ImageDraw.Draw(img)
    tile      = img.width                                  # 128 at @2x
    pixels    = [_lonlat_to_px(c[0], c[1], clon, clat, zoom, tile) for c in coords]
    FREE_FLOW = (30, 120, 255)

    if len(pixels) >= 2:
        for i in range(len(pixels) - 1):
            p1, p2 = pixels[i], pixels[i + 1]
            col = speeds[i] if speeds and i < len(speeds) else FREE_FLOW
            draw.line([p1, p2], fill=col, width=3)

    # Origin dot — red with white outline  (r=6 at 128px → ~3px on 64px LED)
    ox, oy = pixels[0]
    for col, rad in [((255, 255, 255), 7), ((220, 60, 60), 6)]:
        draw.ellipse([ox-rad, oy-rad, ox+rad, oy+rad], fill=col)

    # Destination dot — green with white outline
    dx, dy = pixels[-1]
    for col, rad in [((255, 255, 255), 7), ((60, 210, 90), 6)]:
        draw.ellipse([dx-rad, dy-rad, dx+rad, dy+rad], fill=col)


def fetch_map_image(route_coords, route_speeds=None):
    """Fetch a 64×64 Mapbox static tile and draw the route on top.

    Steps:
      1. Fetch tile at @2x (128×128) — custom style, no attribution
      2. Draw speed-coloured route polyline + origin/destination dots in PIL
      3. Resize 128→64 for the LED matrix
    The custom Mapbox style is used as-is; no colour filtering applied.
    """
    if not HAS_PIL or not MAPBOX_TOKEN or not route_coords:
        return None
    try:
        simplified, seg_speeds = simplify_route(route_coords, route_speeds or [])
        clon, clat, zoom       = bbox_center_zoom(simplified)

        url = (
            f"{MAPBOX}"
            f"/{clon:.5f},{clat:.5f},{zoom},0"
            f"/64x64@2x"
            f"?attribution=false&logo=false&access_token={MAPBOX_TOKEN}"
        )
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            print(f"[map] Mapbox HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
            return None

        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        _draw_route(img, simplified, clon, clat, zoom, seg_speeds)
        img = img.resize((64, 64), Image.LANCZOS)
        print(f"[map] Map View image fetched OK (zoom={zoom})", flush=True)
        return img
    except Exception as e:
        print(f"[map] map image error: {e}", flush=True)
    return None


def blit_image(canvas, img):
    """Copy a PIL RGB Image pixel-by-pixel to the LED matrix canvas."""
    pixels = img.load()
    for y in range(64):
        for x in range(64):
            r, g, b = pixels[x, y]
            canvas.SetPixel(x, y, r, g, b)


def fmt_duration(secs):
    if secs is None:
        return "N/A"
    h = int(secs) // 3600
    m = (int(secs) % 3600) // 60
    return f"{h}h {m}m" if h else f"{m}m"


# ── Fonts ─────────────────────────────────────────────────────────────────────
def _font_roots():
    h = _HOME
    return [
        f"{h}/mlb-led-scoreboard/assets/fonts/patched",
        f"{h}/mlb-led-scoreboard/assets/fonts",
        f"{h}/mlb-led-scoreboard/rpi-rgb-led-matrix/fonts",
        f"{h}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/fonts",
        f"{h}/rpi-rgb-led-matrix/fonts",
    ]


def load_font(candidates):
    f = graphics.Font()
    for root in _font_roots():
        for name in candidates:
            p = os.path.join(root, name)
            if os.path.exists(p):
                f.LoadFont(p)
                return f
    for root in _font_roots():
        p = os.path.join(root, "5x8.bdf")
        if os.path.exists(p):
            f.LoadFont(p)
            return f
    return f


# ── Helpers ───────────────────────────────────────────────────────────────────
def clear_canvas(canvas):
    black = graphics.Color(0, 0, 0)
    for y in range(64):
        graphics.DrawLine(canvas, 0, y, 63, y, black)


def text_w(canvas, font, text):
    return graphics.DrawText(canvas, font, -9999, -9999, graphics.Color(0, 0, 0), text)


def draw_centered(canvas, font, y, color, text):
    w = text_w(canvas, font, text)
    x = max(0, (64 - w) // 2)
    graphics.DrawText(canvas, font, x, y, color, text)


def draw_line(canvas, y, r=40, g=40, b=50):
    graphics.DrawLine(canvas, 0, y, 63, y, graphics.Color(r, g, b))


# ── Scroller ──────────────────────────────────────────────────────────────────
class Scroller:
    """Smooth ping-pong text scroller."""
    def __init__(self, pause_frames=25):
        self.offset     = 0
        self._dir       = 1
        self._hold      = 0
        self._pause     = pause_frames
        self._last_tick = 0.0

    def tick(self, content_w, window_w, now):
        if content_w <= window_w:
            self.offset = 0
            return 0
        if now - self._last_tick < SCROLL_DELAY:
            return self.offset
        self._last_tick = now
        if self._hold > 0:
            self._hold -= 1
            return self.offset
        max_off = content_w - window_w + 2
        self.offset += self._dir
        if self.offset >= max_off:
            self.offset = max_off
            self._dir   = -1
            self._hold  = self._pause
        elif self.offset <= 0:
            self.offset = 0
            self._dir   = 1
            self._hold  = self._pause
        return self.offset


# ── Render: Basic screen ──────────────────────────────────────────────────────
# Layout (64×64):
#   y= 0- 9  |  "TO: [scrolling destination name]"   5x8
#   y=10     |  ── divider ──
#   y=11-30  |  Current temperature (10x20, large, centered)
#   y=31-33  |  ── divider ──
#   y=34-41  |  L:XX   H:XX  (low/high, 5x8)
#   y=42-43  |  ── divider ──
#   y=44-63  |  Drive time (9x18B, large, centered — ~20px tall)

def draw_basic(canvas, fonts, data, scrollers, now):
    clear_canvas(canvas)

    f_large = fonts["large"]   # 10x20 for temperature
    f_drive = fonts["drive"]   # 9x18B for drive time (bigger)
    f_small = fonts["small"]   # 5x8   for labels

    dest    = data.get("dest_name", "…")
    dur     = data.get("duration")
    wx      = data.get("weather") or {}
    units   = data.get("units", "imperial")
    loading = data.get("loading", False)
    error   = data.get("error", "")

    unit_sym = "F" if "imp" in units else "C"

    c_label = graphics.Color(130, 130, 160)
    c_dest  = graphics.Color(255, 200,  50)
    c_temp  = graphics.Color(255, 255, 255)
    c_lo    = graphics.Color( 80, 160, 255)
    c_hi    = graphics.Color(255, 110,  60)
    c_drive = graphics.Color( 80, 220, 120)
    c_err   = graphics.Color(255,  70,  70)

    # ── Row 1-8: "[label]: [destination scrolling]" ──────────────────────
    prefix     = (MAP_LABEL_B + ":") if MAP_LABEL_B else "TO:"
    prefix_w   = text_w(canvas, f_small, prefix)
    graphics.DrawText(canvas, f_small, 1, 8, c_label, prefix)
    dest_start = 1 + prefix_w + 3          # 3px gap after prefix
    dest_avail = 64 - dest_start
    dw = text_w(canvas, f_small, dest)
    sx = scrollers["dest"].tick(dw, dest_avail, now)
    graphics.DrawText(canvas, f_small, dest_start - sx, 8, c_dest, dest)

    draw_line(canvas, 10)

    # ── Loading / error ───────────────────────────────────────────────────
    if loading:
        draw_centered(canvas, f_small, 28, c_label, "Loading")
        draw_centered(canvas, f_small, 38, c_label, "route...")
        return
    if error:
        draw_centered(canvas, f_small, 28, c_err, "No data")
        draw_centered(canvas, f_small, 38, c_err, "check addr")
        return

    # ── Rows 12-30: Big current temperature ──────────────────────────────
    temp = wx.get("temp")
    if temp is not None:
        temp_str = str(temp)
        tw = text_w(canvas, f_large, temp_str)
        tx = max(0, (64 - tw) // 2)
        graphics.DrawText(canvas, f_large, tx, 30, c_temp, temp_str)
        # small °F/°C to the right of the number
        deg_x = min(tx + tw + 1, 57)
        graphics.DrawText(canvas, f_small, deg_x, 20, c_label, f"\xb0{unit_sym}")
    else:
        draw_centered(canvas, f_small, 24, c_label, "--")

    # ── Row 32-41: Low / High ─────────────────────────────────────────────
    draw_line(canvas, 32)
    tmin = wx.get("tmin")
    tmax = wx.get("tmax")
    if tmin is not None:
        graphics.DrawText(canvas, f_small, 1, 40, c_lo, f"L {tmin}")
    if tmax is not None:
        hi_str = f"H {tmax}"
        hw = text_w(canvas, f_small, hi_str)
        graphics.DrawText(canvas, f_small, 63 - hw, 40, c_hi, hi_str)

    # ── Rows 43-63: Drive time (large) ───────────────────────────────────
    draw_line(canvas, 42)
    dur_str = fmt_duration(dur)
    dw2 = text_w(canvas, f_drive, dur_str)
    if dw2 > 62:
        draw_centered(canvas, f_small, 56, c_drive, dur_str)
    else:
        dx = max(0, (64 - dw2) // 2)
        graphics.DrawText(canvas, f_drive, dx, 63, c_drive, dur_str)


# ── Render: Map View screen ───────────────────────────────────────────────────
def draw_map_view(canvas, data):
    """Blit the cached Mapbox map image. Clears canvas if image not ready."""
    map_img = data.get("map_img")
    if map_img is not None:
        blit_image(canvas, map_img)
    else:
        clear_canvas(canvas)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    args = parse_args()

    if not MAP_ADDRESS_A or not MAP_ADDRESS_B:
        print("[map] MAP_ADDRESS_A and MAP_ADDRESS_B must be set", flush=True)
        sys.exit(1)

    print(f"[map] from={MAP_ADDRESS_A!r}  to={MAP_ADDRESS_B!r}", flush=True)
    if MAPBOX_TOKEN:
        print("[map] Map View enabled (Mapbox token present)", flush=True)
    else:
        print("[map] Map View disabled (no MAPBOX_TOKEN)", flush=True)

    opts = RGBMatrixOptions()
    opts.rows             = 64
    opts.cols             = 64
    opts.hardware_mapping = args.hardware_mapping
    opts.gpio_slowdown    = args.gpio_slowdown
    opts.drop_privileges  = False
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper
        print(f"[map] pixel mapper: {args.pixel_mapper}", flush=True)
    if args.brightness is not None:
        opts.brightness = args.brightness

    matrix    = RGBMatrix(options=opts)
    offscreen = matrix.CreateFrameCanvas()

    fonts = {
        "large": load_font(["10x20.bdf", "9x18B.bdf", "9x18.bdf"]),
        "drive": load_font(["9x18B.bdf", "9x18.bdf",  "7x13B.bdf", "7x13.bdf"]),
        "small": load_font(["5x8.bdf",   "6x10.bdf"]),
    }
    scrollers = {"dest": Scroller()}

    data            = {"loading": True, "dest_name": MAP_ADDRESS_B}
    last_fetch      = 0.0
    last_hb         = 0.0
    # submode cycling: 0 = Basic screen, 1 = Map View screen
    # MAP_SUBMODE env controls whether we cycle, pin to basic, or pin to map
    submode         = 0 if MAP_SUBMODE != "map" else 1
    last_switch     = 0.0
    print(f"[map] submode={MAP_SUBMODE!r}", flush=True)

    while True:
        now = time.time()

        # ── Heartbeat ─────────────────────────────────────────────────────
        if now - last_hb >= HEARTBEAT_INTERVAL:
            try:
                with open(HEARTBEAT_FILE, "w") as fh:
                    fh.write(str(now))
            except Exception:
                pass
            last_hb = now

        # ── Fetch / refresh data ──────────────────────────────────────────
        if now - last_fetch >= UPDATE_INTERVAL:
            last_fetch = now
            try:
                geo_a = geocode(MAP_ADDRESS_A)
                geo_b = geocode(MAP_ADDRESS_B)
                if geo_a and geo_b:
                    la, lna, _      = geo_a
                    lb, lnb, name_b = geo_b

                    dur, route_coords, route_colors = get_route(la, lna, lb, lnb)
                    wx                              = get_weather(lb, lnb, WEATHER_UNITS)

                    # Fetch map image if Mapbox is configured
                    map_img = None
                    if MAPBOX_TOKEN and HAS_PIL and route_coords:
                        map_img = fetch_map_image(route_coords, route_colors)

                    data = {
                        "dest_name":    name_b,
                        "duration":     dur,
                        "weather":      wx,
                        "units":        WEATHER_UNITS,
                        "loading":      False,
                        "error":        "" if wx else "weather err",
                        "map_img":      map_img,
                        "route_coords": route_coords,
                    }
                    print(
                        f"[map] {name_b} | drive={fmt_duration(dur)} | wx={wx} "
                        f"| map_img={'yes' if map_img else 'no'}",
                        flush=True,
                    )
                else:
                    data = {
                        "dest_name": MAP_ADDRESS_B,
                        "loading":   False,
                        "error":     "geocode failed",
                        "map_img":   None,
                    }
                    print("[map] geocode failed", flush=True)
            except Exception as e:
                print(f"[map] fetch exception: {e}", flush=True)
                data["loading"] = False
                data.setdefault("error", str(e))

        # ── Submode cycling ───────────────────────────────────────────────
        map_view_available = MAPBOX_TOKEN and HAS_PIL and data.get("map_img") is not None
        if MAP_SUBMODE == "basic":
            submode = 0
        elif MAP_SUBMODE == "map":
            submode = 1 if map_view_available else 0
        else:  # "alternate"
            if map_view_available:
                if now - last_switch >= SUBMODE_INTERVAL:
                    submode     = 1 - submode
                    last_switch = now
            else:
                submode     = 0
                last_switch = now

        # ── Render ────────────────────────────────────────────────────────
        if submode == 1:
            draw_map_view(offscreen, data)
        else:
            draw_basic(offscreen, fonts, data, scrollers, now)

        offscreen = matrix.SwapOnVSync(offscreen)
        time.sleep(0.05)  # ~20fps for smooth scrolling


if __name__ == "__main__":
    main()
