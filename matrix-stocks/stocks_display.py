#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Stocks / crypto display for 64x64 RGB matrix. (Mode 14)
# - Fetches configured symbols from backend /stocks-settings
# - Fetches prices from Yahoo Finance (free, no API key)
# - Cycles through symbols, showing price and % change
# - Sparkline at bottom shows day's price movement
# - Heartbeat file every 30s so agent can detect we're alive

import os, sys, time, argparse, gc, math
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

def _add_path(p):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

_HOME = os.environ.get("HOME", "/home/pi_two")
_add_path(f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/bindings/python")

from rgbmatrix import RGBMatrix, RGBMatrixOptions

HEARTBEAT = "/tmp/matrix-heartbeat-14"
W, H = 64, 64

# Yahoo Finance headers — mimics a browser request
_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36",
    "Accept": "application/json",
}

def parse_args():
    ap = argparse.ArgumentParser(prog="MatrixStocks")
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--device-token", default="")
    ap.add_argument("--brightness", type=int, default=None)
    ap.add_argument("--hardware-mapping", default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown", type=int, default=2)
    ap.add_argument("--pixel-mapper", default=None)
    ap.add_argument("--cycle-time", type=float, default=5.0,
                    help="Seconds to show each symbol")
    ap.add_argument("--price-refresh", type=float, default=60.0,
                    help="Seconds between price fetches")
    return ap.parse_args()

# ── Font loading ──────────────────────────────────────────────────────────────

def _load_font(size):
    candidates = [
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()

# ── Backend settings ──────────────────────────────────────────────────────────

_session = requests.Session()

def fetch_symbols(api_base, device_token=""):
    try:
        headers = {}
        if device_token:
            headers["X-Device-Token"] = device_token
        r = _session.get(f"{api_base}/stocks-settings", headers=headers, timeout=5)
        if r.ok:
            data = r.json()
            syms = data.get("symbols", [])
            if syms:
                return syms
    except Exception as e:
        print(f"[stocks] settings fetch error: {e}", flush=True)
    return ["AAPL", "MSFT", "BTC-USD"]

# ── Yahoo Finance price fetch ─────────────────────────────────────────────────

def fetch_quote(symbol):
    """Return (price, pct_change, sparkline_points) or None on failure."""
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?interval=5m&range=1d&includePrePost=false")
        r = requests.get(url, headers=_YF_HEADERS, timeout=8)
        if not r.ok:
            return None
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            return None
        pct = ((price - prev) / prev * 100) if prev else 0.0

        # Sparkline: intraday close prices, strip None values
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        spark = [c for c in closes if c is not None]

        return price, pct, spark
    except Exception as e:
        print(f"[stocks] quote fetch error for {symbol}: {e}", flush=True)
        return None

# ── Rendering ─────────────────────────────────────────────────────────────────

_font_sym   = None
_font_price = None
_font_pct   = None

def _init_fonts():
    global _font_sym, _font_price, _font_pct
    _font_sym   = _load_font(16)
    _font_price = _load_font(13)
    _font_pct   = _load_font(12)

def _text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]

def _centered_x(draw, text, font):
    w, _ = _text_size(draw, text, font)
    return max(0, (W - w) // 2)

def _draw_sparkline(draw, points, y_top, y_bot):
    if len(points) < 2:
        return
    px_h = y_bot - y_top
    mn, mx = min(points), max(points)
    rng = mx - mn if mx != mn else 1.0
    step = W / (len(points) - 1)
    color = (100, 200, 100)  # green default
    coords = []
    for i, v in enumerate(points):
        x = int(round(i * step))
        y = y_bot - int(round((v - mn) / rng * px_h))
        coords.append((x, y))
    for i in range(len(coords) - 1):
        draw.line([coords[i], coords[i + 1]], fill=color, width=1)

def render_symbol(symbol, price, pct, spark):
    img = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Symbol name
    sym_display = symbol.replace("-USD", "").replace("-", "/")
    x = _centered_x(draw, sym_display, _font_sym)
    draw.text((x, 2), sym_display, font=_font_sym, fill=(220, 220, 255))

    # Price — format nicely
    if price >= 10000:
        price_str = f"${price:,.0f}"
    elif price >= 100:
        price_str = f"${price:.2f}"
    elif price >= 1:
        price_str = f"${price:.3f}"
    else:
        price_str = f"${price:.5f}"

    x = _centered_x(draw, price_str, _font_price)
    draw.text((x, 22), price_str, font=_font_price, fill=(255, 255, 255))

    # % change
    arrow = "▲" if pct >= 0 else "▼"
    pct_str = f"{arrow}{abs(pct):.2f}%"
    color = (80, 220, 80) if pct >= 0 else (220, 80, 80)
    x = _centered_x(draw, pct_str, _font_pct)
    draw.text((x, 38), pct_str, font=_font_pct, fill=color)

    # Sparkline
    if spark:
        _draw_sparkline(draw, spark, 53, 62)

    return img

def render_loading(symbol):
    img = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    sym_display = symbol.replace("-USD", "").replace("-", "/")
    x = _centered_x(draw, sym_display, _font_sym)
    draw.text((x, 10), sym_display, font=_font_sym, fill=(180, 180, 255))
    msg = "Loading..."
    x2 = _centered_x(draw, msg, _font_pct)
    draw.text((x2, 34), msg, font=_font_pct, fill=(160, 160, 160))
    return img

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    _init_fonts()

    opts = RGBMatrixOptions()
    opts.rows = 64
    opts.cols = 64
    opts.hardware_mapping = args.hardware_mapping
    if args.brightness is not None:
        opts.brightness = max(0, min(100, args.brightness))
    opts.gpio_slowdown = args.gpio_slowdown
    opts.drop_privileges = False
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper

    matrix = RGBMatrix(options=opts)

    # State
    symbols    = fetch_symbols(args.api_base, args.device_token)
    quotes     = {}      # symbol → (price, pct, spark)
    sym_idx    = 0
    last_price_fetch = 0.0
    last_sym_fetch   = 0.0
    last_hb          = 0.0
    last_cycle       = 0.0
    last_gc          = 0.0

    while True:
        now = time.time()

        # Refresh symbol list every 5 minutes
        if now - last_sym_fetch > 300:
            symbols = fetch_symbols(args.api_base, args.device_token) or symbols
            last_sym_fetch = now

        # Refresh prices
        if now - last_price_fetch > args.price_refresh:
            for sym in symbols:
                result = fetch_quote(sym)
                if result:
                    quotes[sym] = result
            last_price_fetch = now
            print(f"[stocks] refreshed {len(quotes)}/{len(symbols)} quotes", flush=True)

        # Cycle to next symbol
        if now - last_cycle > args.cycle_time:
            sym_idx = (sym_idx + 1) % max(len(symbols), 1)
            last_cycle = now

        # Heartbeat
        if now - last_hb > 30.0:
            try:
                with open(HEARTBEAT, "w") as f:
                    f.write(str(now))
            except Exception:
                pass
            last_hb = now

        # GC
        if now - last_gc > 60.0:
            gc.collect()
            last_gc = now

        # Render
        if symbols:
            sym = symbols[sym_idx % len(symbols)]
            try:
                if sym in quotes:
                    price, pct, spark = quotes[sym]
                    img = render_symbol(sym, price, pct, spark)
                else:
                    img = render_loading(sym)
                matrix.SetImage(img, 0, 0)
            except Exception as e:
                print(f"[stocks] render error: {e}", flush=True)

        time.sleep(0.1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
