#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Drawing mode for 64x64 RGB matrix.
# - Downloads PNG from backend only when ETag changes
# - Keeps a single cached, pre-scaled 64x64 RGB image
# - Heartbeat file every 30s so agent can detect we're alive
# - Gentle on memory: closes responses/images and runs gc periodically

import os, sys, time, argparse, gc
from io import BytesIO

import requests
from PIL import Image

# ---------- Add rpi-rgb-led-matrix binding path ----------
def _add_path(p: str):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)
_add_path("/home/pi_two/rpi-spotify-matrix-display/rpi-rgb-led-matrix/bindings/python")
# ---------------------------------------------------------

from rgbmatrix import RGBMatrix, RGBMatrixOptions

# Global cache/state
_session = requests.Session()
_cached_etag: str | None = None
_cached_img: Image.Image | None = None  # 64x64 RGB, ready to blit

def parse_args():
    ap = argparse.ArgumentParser(prog="MatrixDrawing")
    ap.add_argument("--api-base", required=True, help="e.g. https://matrix-backend-xxx.onrender.com")
    ap.add_argument("--device-token", default="", help="Device UUID token for multi-device backend")
    ap.add_argument("--brightness", type=int, default=None)
    ap.add_argument("--hardware-mapping", type=str, default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown", type=int, default=2)
    ap.add_argument("--pixel-mapper", type=str, default=None)  # e.g. Rotate:90
    ap.add_argument("--interval", type=float, default=0.5, help="draw loop sleep seconds")
    ap.add_argument("--refresh-hz", type=int, default=120, help="limit refresh rate; 0 disables limit")
    return ap.parse_args()

def _scale_to_64(rgb_img: Image.Image) -> Image.Image:
    """
    Ensure a 64x64 RGB image. If the source isn't square or is larger/smaller,
    we letterbox/pillarbox with black to 64x64 and center the resized content.
    (Front-end already sends a square PNG, but this is defensive.)
    """
    W = H = 64
    src_w, src_h = rgb_img.size
    # Compute fit
    scale = min(W / src_w, H / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = rgb_img.resize((new_w, new_h), Image.NEAREST)
    # Paste onto black 64x64 canvas
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    off_x = (W - new_w) // 2
    off_y = (H - new_h) // 2
    canvas.paste(resized, (off_x, off_y))
    return canvas

def fetch_if_changed(api_base: str, device_token: str = ""):
    """
    If /image ETag changed, download once and update the cached 64x64 image.
    """
    global _cached_etag, _cached_img, _session
    r = None
    try:
        headers = {"Accept": "image/png"}
        if device_token:
            headers["X-Device-Token"] = device_token
        if _cached_etag:
            headers["If-None-Match"] = _cached_etag

        r = _session.get(f"{api_base}/image", headers=headers, timeout=6)
        # 204: no image yet
        if r.status_code == 204:
            return

        # 304: unchanged
        if r.status_code == 304:
            return

        # 200 image
        if r.ok and "image" in (r.headers.get("content-type") or ""):
            etag = r.headers.get("ETag")
            payload = r.content  # copy bytes before closing response
            # close early to free sockets
            r.close(); r = None

            # Decode -> convert -> scale once
            with Image.open(BytesIO(payload)) as im:
                im = im.convert("RGB")
                new_img = _scale_to_64(im)

            # Replace cache
            if _cached_img is not None:
                try:
                    _cached_img.close()
                except Exception:
                    pass
            _cached_img = new_img
            _cached_etag = etag
        # Other codes: ignore quietly
    except Exception as e:
        print(f"[drawing] fetch error: {e}", flush=True)
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass

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

    print(
        "Suggestion: to slightly improve display update, add\n"
        "        isolcpus=3\n"
        "at the end of /boot/cmdline.txt and reboot (see README.md)",
        flush=True,
    )

    last_gc = 0.0
    last_hb = 0.0
    last_poll = 0.0

    # Draw loop
    while True:
        now = time.time()

        # Poll backend for image at most ~4/sec (or slower if you want)
        if now - last_poll > 0.25:
            fetch_if_changed(args.api_base, args.device_token)
            last_poll = now

        # Heartbeat every 30s so agent can monitor mode 6
        if now - last_hb > 30.0:
            try:
                with open("/tmp/matrix-heartbeat-6", "w") as f:
                    f.write(str(now))
            except Exception:
                pass
            last_hb = now

        # GC every 10s to keep RSS stable on low-RAM Pis
        if now - last_gc > 10.0:
            gc.collect()
            last_gc = now

        # Draw the cached image if we have one
        try:
            if _cached_img is not None:
                # SetImage accepts a PIL Image in RGB
                matrix.SetImage(_cached_img, 0, 0)
        except Exception as e:
            print(f"[drawing] draw error: {e}", flush=True)

        time.sleep(max(0.05, float(args.interval)))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
