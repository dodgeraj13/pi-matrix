#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Stocks / crypto display for 64x64 RGB matrix. (Mode 14)
# - Fetches configured symbols + cycle_time from backend /stocks-settings
# - Two sparklines: today (5m intraday) and 5-day weekly (1h intervals)
# - Cycle time is configurable from the frontend
# - Heartbeat file every 30s so agent can detect we're alive

import os, sys, time, argparse, gc
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

def fetch_settings(api_base, device_token=""):
    """Return (symbols, cycle_time_seconds) from backend."""
    try:
        headers = {}
        if device_token:
            headers["X-Device-Token"] = device_token
        r = _session.get(f"{api_base}/stocks-settings", headers=headers, timeout=5)
        if r.ok:
            data  = r.json()
            syms  = data.get("symbols", [])
            cycle = float(data.get("cycle_time", 5.0))
            if syms:
                return syms, max(1.0, cycle)
    except Exception as e:
        print(f"[stocks] settings fetch error: {e}", flush=True)
    return ["AAPL", "MSFT", "BTC-USD"], 5.0

# ── Yahoo Finance price fetch ─────────────────────────────────────────────────

def fetch_quote(symbol):
    """Return (price, pct_change, day_spark, week_spark) or None on failure.

    day_spark  — 5-minute intraday closes for today
    week_spark — 1-hour closes for the past 5 trading days
    """
    try:
        # ── Intraday (today, 5m bars) ──
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?interval=5m&range=1d&includePrePost=false")
        r = requests.get(url, headers=_YF_HEADERS, timeout=8)
        if not r.ok:
            return None
        data   = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        meta  = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            return None
        pct    = ((price - prev) / prev * 100) if prev else 0.0
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        day_spark = [c for c in closes if c is not None]

        # ── Weekly (5 days, 1h bars) ──
        url_wk = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                  f"?interval=1h&range=5d&includePrePost=false")
        r2 = requests.get(url_wk, headers=_YF_HEADERS, timeout=8)
        week_spark = []
        if r2.ok:
            data2   = r2.json()
            result2 = data2.get("chart", {}).get("result", [])
            if result2:
                wk_closes  = result2[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                week_spark = [c for c in wk_closes if c is not None]

        return price, pct, day_spark, week_spark
    except Exception as e:
        print(f"[stocks] quote fetch error for {symbol}: {e}", flush=True)
        return None

# ── Rendering ─────────────────────────────────────────────────────────────────

_font_sym   = None   # small ticker name
_font_price = None   # price
_font_pct   = None   # % change
_font_tiny  = None   # "1D" / "1W" sparkline labels

def _init_fonts():
    global _font_sym, _font_price, _font_pct, _font_tiny
    _font_sym   = _load_font(9)    # much smaller than original 16
    _font_price = _load_font(12)
    _font_pct   = _load_font(10)
    _font_tiny  = _load_font(7)

def _text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]

def _centered_x(draw, text, font):
    w, _ = _text_size(draw, text, font)
    return max(0, (W - w) // 2)

def _draw_sparkline(draw, points, x0, y_top, y_bot, color=(100, 200, 100)):
    """Draw a poly-line sparkline from x0..W-1 in y_top..y_bot."""
    if len(points) < 2:
        return
    x1   = W - 1
    px_w = x1 - x0
    px_h = y_bot - y_top
    mn, mx = min(points), max(points)
    rng  = mx - mn if mx != mn else 1.0
    step = px_w / (len(points) - 1)
    coords = []
    for i, v in enumerate(points):
        x = x0 + int(round(i * step))
        y = y_bot - int(round((v - mn) / rng * px_h))
        coords.append((x, y))
    for i in range(len(coords) - 1):
        draw.line([coords[i], coords[i + 1]], fill=color, width=1)

# ── Layout (all Y values are pixel rows, 0-indexed in a 64x64 frame) ──────────
#
#  0 ┌──────────────────────────────┐
#    │  AAPL   (sym, font=9)        │  Y=1
#    │  $182.34  (price, font=12)   │  Y=11
#    │  ▲1.23%   (pct,   font=10)   │  Y=24
# 33 ├── separator ─────────────────┤
#    │ 1D ~~~~~~~~~~~~~~~~~~~~~~~~  │  Y=35..44
# 45 ├── separator ─────────────────┤
#    │ 1W ~~~~~~~~~~~~~~~~~~~~~~~~  │  Y=46..62
# 63 └──────────────────────────────┘

_Y_SYM    = 1
_Y_PRICE  = 11
_Y_PCT    = 24
_Y_SEP1   = 33
_Y_1D_TOP = 35
_Y_1D_BOT = 44
_Y_SEP2   = 45
_Y_1W_TOP = 46
_Y_1W_BOT = 62
_X_LABEL  = 1    # x of "1D"/"1W" text
_X_SPARK  = 15   # x where sparkline begins (after label)


def render_symbol(symbol, price, pct, day_spark, week_spark):
    img  = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Ticker symbol ──
    sym_display = symbol.replace("-USD", "").replace("-", "/")
    x = _centered_x(draw, sym_display, _font_sym)
    draw.text((x, _Y_SYM), sym_display, font=_font_sym, fill=(200, 200, 255))

    # ── Price ──
    if price >= 10_000:
        price_str = f"${price:,.0f}"
    elif price >= 100:
        price_str = f"${price:.2f}"
    elif price >= 1:
        price_str = f"${price:.3f}"
    else:
        price_str = f"${price:.5f}"
    x = _centered_x(draw, price_str, _font_price)
    draw.text((x, _Y_PRICE), price_str, font=_font_price, fill=(255, 255, 255))

    # ── % change ──
    arrow   = "▲" if pct >= 0 else "▼"
    pct_str = f"{arrow}{abs(pct):.2f}%"
    clr_pct = (80, 220, 80) if pct >= 0 else (220, 80, 80)
    x = _centered_x(draw, pct_str, _font_pct)
    draw.text((x, _Y_PCT), pct_str, font=_font_pct, fill=clr_pct)

    # ── Separators ──
    draw.line([(0, _Y_SEP1), (W - 1, _Y_SEP1)], fill=(45, 45, 45))
    draw.line([(0, _Y_SEP2), (W - 1, _Y_SEP2)], fill=(45, 45, 45))

    # ── 1D sparkline (green/red matching pct) ──
    draw.text((_X_LABEL, _Y_1D_TOP), "1D", font=_font_tiny, fill=(140, 140, 140))
    clr_day = (80, 200, 80) if pct >= 0 else (200, 80, 80)
    if day_spark:
        _draw_sparkline(draw, day_spark, _X_SPARK, _Y_1D_TOP, _Y_1D_BOT, color=clr_day)

    # ── 1W sparkline (always blue) ──
    draw.text((_X_LABEL, _Y_1W_TOP), "1W", font=_font_tiny, fill=(140, 140, 140))
    if week_spark:
        _draw_sparkline(draw, week_spark, _X_SPARK, _Y_1W_TOP, _Y_1W_BOT, color=(80, 150, 220))

    return img


def render_loading(symbol):
    img  = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    sym_display = symbol.replace("-USD", "").replace("-", "/")
    x = _centered_x(draw, sym_display, _font_sym)
    draw.text((x, _Y_SYM), sym_display, font=_font_sym, fill=(180, 180, 255))
    msg = "Loading..."
    x2  = _centered_x(draw, msg, _font_pct)
    draw.text((x2, 28), msg, font=_font_pct, fill=(160, 160, 160))
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
    opts.gpio_slowdown   = args.gpio_slowdown
    opts.drop_privileges = False
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper

    matrix = RGBMatrix(options=opts)

    symbols, cycle_time = fetch_settings(args.api_base, args.device_token)
    quotes           = {}    # symbol -> (price, pct, day_spark, week_spark)
    sym_idx          = 0
    last_price_fetch = 0.0
    last_sym_fetch   = 0.0
    last_hb          = 0.0
    last_cycle       = 0.0
    last_gc          = 0.0

    while True:
        now = time.time()

        # Re-fetch symbols + cycle_time every 5 minutes
        if now - last_sym_fetch > 300:
            new_syms, new_cycle = fetch_settings(args.api_base, args.device_token)
            if new_syms:
                symbols    = new_syms
            cycle_time     = new_cycle
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
        if now - last_cycle > cycle_time:
            sym_idx    = (sym_idx + 1) % max(len(symbols), 1)
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
                    price, pct, day_spark, week_spark = quotes[sym]
                    img = render_symbol(sym, price, pct, day_spark, week_spark)
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
