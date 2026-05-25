#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# GIF player mode for 64x64 RGB matrix. (Mode 13)
# - Downloads GIF from backend only when ETag changes
# - Loops through frames at the GIF's native frame timing
# - Heartbeat file every 30s so agent can detect we're alive

import hashlib, os, sys, time, argparse, gc
from io import BytesIO

import requests
from PIL import Image

def _add_path(p):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

_HOME = os.environ.get("HOME", "/home/pi_two")
_add_path(f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/bindings/python")

from rgbmatrix import RGBMatrix, RGBMatrixOptions

HEARTBEAT = "/tmp/matrix-heartbeat-13"

_session = requests.Session()
_cached_etag = None
_cached_hash = None   # MD5 of last downloaded GIF bytes — avoids re-parsing unchanged content
_frames: list = []   # list of (PIL.Image 64x64 RGB, duration_seconds)

def parse_args():
    ap = argparse.ArgumentParser(prog="MatrixGIF")
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--device-token", default="")
    ap.add_argument("--brightness", type=int, default=None)
    ap.add_argument("--hardware-mapping", default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown", type=int, default=2)
    ap.add_argument("--pixel-mapper", default=None)
    return ap.parse_args()

def _scale_to_64(img):
    W = H = 64
    src_w, src_h = img.size
    scale = min(W / src_w, H / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    canvas.paste(resized, ((W - new_w) // 2, (H - new_h) // 2))
    return canvas

def fetch_if_changed(api_base, device_token=""):
    global _cached_etag, _cached_hash, _frames
    try:
        headers = {"Accept": "image/gif"}
        if device_token:
            headers["X-Device-Token"] = device_token
        if _cached_etag:
            headers["If-None-Match"] = _cached_etag

        r = _session.get(f"{api_base}/gif", headers=headers, timeout=8)
        if r.status_code in (204, 304):
            r.close()
            return
        if not r.ok:
            r.close()
            return

        etag = r.headers.get("ETag")
        data = r.content
        r.close()

        # Skip re-parsing if content hasn't changed (backend may not send ETags)
        content_hash = hashlib.md5(data).hexdigest()
        if content_hash == _cached_hash and _frames:
            return

        gif = Image.open(BytesIO(data))
        new_frames = []
        try:
            # Composite frames onto a canvas so delta/additive GIFs animate correctly.
            # PIL's seek() gives you only the changed pixels per frame, not a full frame.
            # Alpha-compositing accumulates changes, producing the correct visible frame.
            canvas = Image.new("RGBA", gif.size, (0, 0, 0, 255))
            i = 0
            while True:
                gif.seek(i)
                duration_ms = gif.info.get("duration", 100)
                frame_rgba = gif.copy().convert("RGBA")
                new_canvas = Image.alpha_composite(canvas, frame_rgba)
                new_frames.append((_scale_to_64(new_canvas.convert("RGB")),
                                   max(0.02, duration_ms / 1000.0)))
                # disposal_method 2 = restore to background before next frame
                disposal = getattr(gif, "disposal_method", 1)
                canvas = Image.new("RGBA", gif.size, (0, 0, 0, 255)) if disposal == 2 else new_canvas
                i += 1
        except EOFError:
            pass

        if new_frames:
            _frames = new_frames
            _cached_etag = etag
            _cached_hash = content_hash
            dur0 = new_frames[0][1]
            print(f"[gif] loaded {len(_frames)} frames (frame duration: {dur0*1000:.0f}ms)", flush=True)
    except Exception as e:
        print(f"[gif] fetch error: {e}", flush=True)

def main():
    args = parse_args()

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

    last_hb = 0.0
    last_poll = 0.0
    last_gc = 0.0
    frame_idx = 0

    while True:
        now = time.time()

        if now - last_poll > 5.0:
            fetch_if_changed(args.api_base, args.device_token)
            last_poll = now

        if now - last_hb > 30.0:
            try:
                with open(HEARTBEAT, "w") as f:
                    f.write(str(now))
            except Exception:
                pass
            last_hb = now

        if now - last_gc > 30.0:
            gc.collect()
            last_gc = now

        if _frames:
            frame_img, duration = _frames[frame_idx % len(_frames)]
            try:
                matrix.SetImage(frame_img, 0, 0)
            except Exception as e:
                print(f"[gif] draw error: {e}", flush=True)
            time.sleep(duration)
            frame_idx += 1
        else:
            time.sleep(0.5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
