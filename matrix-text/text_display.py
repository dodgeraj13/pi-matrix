#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Text mode for 64x64 RGB matrix.
# - Fetches text config from backend
# - Renders text with optional scrolling
# - Supports multi-line text
# - Heartbeat file every 30s so agent can detect we're alive

import os, sys, time, argparse, gc, json
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

# ---------- Add rpi-rgb-led-matrix binding path ----------
def _add_path(p: str):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)
_HOME = os.environ.get("HOME", "/home/pi_two")
_add_path(f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/bindings/python")
# ---------------------------------------------------------

from rgbmatrix import RGBMatrix, RGBMatrixOptions

# Global cache/state
_session = requests.Session()
_text_config = {
    "text": "",
    "font": "6x12",
    "color": "#ffffff",
    "scrollMode": "static",
    "scrollSpeed": 5
}
_cached_etag: str | None = None

# Font mapping - BDF fonts with their pixel heights
FONT_MAP = {
    "4x6": {"file": "4x6.bdf", "height": 6},
    "5x7": {"file": "5x7.bdf", "height": 7},
    "5x8": {"file": "5x8.bdf", "height": 8},
    "6x9": {"file": "6x9.bdf", "height": 9},
    "6x10": {"file": "6x10.bdf", "height": 10},
    "6x12": {"file": "6x12.bdf", "height": 12},
    "6x13": {"file": "6x13.bdf", "height": 13},
    "7x13": {"file": "7x13.bdf", "height": 13},
    "7x14": {"file": "7x14.bdf", "height": 14},
    "8x13": {"file": "8x13.bdf", "height": 13},
    "9x15": {"file": "9x15.bdf", "height": 15},
    "10x20": {"file": "10x20.bdf", "height": 20},
    "tom-thumb": {"file": "tom-thumb.bdf", "height": 8},
}

FONT_DIR = f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/fonts"

def parse_args():
    ap = argparse.ArgumentParser(prog="MatrixText")
    ap.add_argument("--api-base", required=True, help="e.g. https://matrix-backend-xxx.onrender.com")
    ap.add_argument("--device-token", default="", help="Device UUID token for multi-device backend")
    ap.add_argument("--brightness", type=int, default=None)
    ap.add_argument("--hardware-mapping", type=str, default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown", type=int, default=2)
    ap.add_argument("--pixel-mapper", type=str, default=None)
    ap.add_argument("--interval", type=float, default=0.05, help="draw loop sleep seconds")
    ap.add_argument("--refresh-hz", type=int, default=120, help="limit refresh rate; 0 disables limit")
    return ap.parse_args()

def hex_to_rgb(hex_color: str) -> tuple:
    """Convert hex color to RGB tuple"""
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def get_font_height(font_name: str) -> int:
    """Get the pixel height of a font"""
    return FONT_MAP.get(font_name, {}).get("height", 12)

def wrap_text_static(text: str, font_name: str, max_width: int = 64) -> list:
    """Wrap text into lines that fit within max_width pixels for static mode"""
    if not text:
        return []

    lines = text.split('\n')
    wrapped = []

    font_info = FONT_MAP.get(font_name, {"height": 12})
    # Estimate characters that fit based on font width (roughly half of height for BDF fonts)
    char_width = max(1, font_info["height"] // 2)

    for line in lines:
        if not line:
            wrapped.append("")
            continue

        # Simple wrapping: estimate characters per line
        chars_per_line = max_width // char_width
        if chars_per_line >= len(line):
            wrapped.append(line)
        else:
            # Split into chunks
            words = line.split(' ')
            current_line = ""
            for word in words:
                test_line = current_line + (" " if current_line else "") + word
                if len(test_line) <= chars_per_line:
                    current_line = test_line
                else:
                    if current_line:
                        wrapped.append(current_line)
                    current_line = word
            if current_line:
                wrapped.append(current_line)

    return wrapped

def measure_text_width(text: str, font) -> int:
    """Measure the width of text in pixels"""
    try:
        # Try getting bbox for newer PIL versions
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]
    except AttributeError:
        # Fallback for older PIL
        try:
            return font.getsize(text)[0]
        except:
            return len(text) * 6  # Rough estimate

def render_text_frame(text_config: dict, scroll_offset: int = 0) -> Image.Image:
    """Render a single frame of text"""
    text = text_config.get("text", "")
    if not text:
        return Image.new("RGB", (64, 64), (0, 0, 0))

    font_name = text_config.get("font", "6x12")
    color = hex_to_rgb(text_config.get("color", "#ffffff"))
    scroll_mode = text_config.get("scrollMode", "static")

    # Get font info
    font_file = FONT_MAP.get(font_name, {"file": "6x12.bdf", "height": 12})["file"]
    font_height = FONT_MAP.get(font_name, {"height": 12})["height"]

    # Create canvas
    canvas = Image.new("RGB", (64, 64), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Load font
    font_path = os.path.join(FONT_DIR, font_file)
    try:
        font = ImageFont.load(font_path)
    except Exception as e:
        print(f"[text] font load error: {e}, using default", flush=True)
        font = ImageFont.load_default()

    if scroll_mode == "scroll":
        # Scroll mode - single line scrolling
        scroll_speed = int(text_config.get("scrollSpeed", 5))

        # Get total text width
        total_width = measure_text_width(text, font)

        # Always scroll even if text fits - create a scrolling canvas
        scroll_width = max(total_width + 64, 128)  # Extra space for scrolling
        scroll_canvas = Image.new("RGB", (scroll_width, 64), (0, 0, 0))
        scroll_draw = ImageDraw.Draw(scroll_canvas)

        # Draw text at starting position
        y = (64 - font_height) // 2
        scroll_draw.text((0, y), text, fill=color, font=font)

        # Calculate x position based on scroll offset (speed 1 = slowest, 10 = fastest)
        # Speed 1 = offset 0.1, Speed 10 = offset 2.0
        speed_factor = scroll_speed * 0.2
        x_pos = int(scroll_offset * speed_factor) % (total_width + 64)

        # Crop the appropriate portion of the scroll canvas
        if x_pos + 64 <= scroll_width:
            frame = scroll_canvas.crop((x_pos, 0, x_pos + 64, 64))
        else:
            # Wrap around - draw two copies
            frame = Image.new("RGB", (64, 64), (0, 0, 0))
            frame.paste(scroll_canvas.crop((x_pos, 0, scroll_width, 64)), (0, 0))
            remaining = 64 - (scroll_width - x_pos)
            if remaining > 0:
                frame.paste(scroll_canvas.crop((0, 0, remaining, 64)), (scroll_width - x_pos, 0))

        return frame

    else:
        # Static mode - multi-line with word wrap
        lines = wrap_text_static(text, font_name, max_width=60)

        if not lines:
            return canvas

        # Calculate vertical spacing
        total_height = len(lines) * (font_height + 2)
        start_y = max(0, (64 - total_height) // 2)

        for i, line in enumerate(lines):
            if not line:
                continue
            y = start_y + i * (font_height + 2)
            if y > 64:
                break
            # Center each line horizontally
            line_width = measure_text_width(line, font)
            x = max(4, (64 - line_width) // 2)
            draw.text((x, y), line, fill=color, font=font)

        return canvas

def fetch_text_config(api_base: str, device_token: str = ""):
    """Fetch text configuration from backend"""
    global _text_config, _cached_etag
    try:
        headers = {}
        if device_token:
            headers["X-Device-Token"] = device_token
        if _cached_etag:
            headers["If-None-Match"] = _cached_etag

        r = _session.get(f"{api_base}/text", headers=headers, timeout=6)

        if r.status_code == 304:
            return

        if r.ok:
            etag = r.headers.get("ETag")
            data = r.json()
            _text_config = {
                "text": data.get("text", ""),
                "font": data.get("font", "6x12"),
                "color": data.get("color", "#ffffff"),
                "scrollMode": data.get("scrollMode", "static"),
                "scrollSpeed": data.get("scrollSpeed", 5)
            }
            _cached_etag = etag
            print(f"[text] config updated: text='{_text_config['text'][:30]}...', font={_text_config['font']}, scroll={_text_config['scrollMode']}", flush=True)
    except Exception as e:
        print(f"[text] fetch error: {e}", flush=True)

def main():
    args = parse_args()

    # Matrix options
    opts = RGBMatrixOptions()
    opts.rows = 64; opts.cols = 64
    opts.hardware_mapping = args.hardware_mapping
    if args.brightness is not None:
        opts.brightness = max(0, min(100, int(args.brightness)))
    opts.gpio_slowdown = int(args.gpio_slowdown)
    opts.limit_refresh_rate_hz = int(args.refresh_hz) if int(args.refresh_hz) > 0 else 0
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper
    opts.drop_privileges = False

    matrix = RGBMatrix(options=opts)

    last_config_poll = 0.0
    last_hb = 0.0
    last_gc = 0.0
    scroll_offset = 0

    # Initial fetch
    fetch_text_config(args.api_base, args.device_token)

    print("[text] starting text display", flush=True)

    while True:
        now = time.time()

        # Poll backend for text config every 2 seconds
        if now - last_config_poll > 2.0:
            fetch_text_config(args.api_base, args.device_token)
            last_config_poll = now

        # Heartbeat every 30s
        if now - last_hb > 30.0:
            try:
                with open("/tmp/matrix-heartbeat-7", "w") as f:
                    f.write(str(now))
            except Exception:
                pass
            last_hb = now

        # GC every 10s
        if now - last_gc > 10.0:
            gc.collect()
            last_gc = now

        # Update scroll offset - always increment when in scroll mode
        if _text_config.get("scrollMode") == "scroll":
            scroll_offset += 1
            # Reset after a full cycle
            if scroll_offset > 500:
                scroll_offset = 0
        else:
            scroll_offset = 0

        # Render and display
        try:
            img = render_text_frame(_text_config, scroll_offset)
            matrix.SetImage(img, 0, 0)
        except Exception as e:
            print(f"[text] draw error: {e}", flush=True)

        time.sleep(max(0.02, float(args.interval)))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass