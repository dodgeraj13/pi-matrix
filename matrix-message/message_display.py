#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Message mode for the 64x64 matrix. (Mode 15)
#
# Shows the oldest undismissed message sent from a paired display: either text,
# or a picture the sender drew or picked from their gallery. It stays up until
# the recipient dismisses it in the app, at which point the agent switches the
# panel back to whatever it was showing before.
#
# Run as a subprocess via agent.py — never directly (needs sudo).

import os, sys, time, argparse, signal, gc
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

def _add_path(p):
    p = os.path.abspath(p)
    if os.path.exists(p) and p not in sys.path:
        sys.path.append(p)

_HOME = os.environ.get("HOME", "/home/pi_two")
_add_path(f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/bindings/python")
_add_path(f"{_HOME}/rpi-rgb-led-matrix/bindings/python")

from rgbmatrix import RGBMatrix, RGBMatrixOptions

HEARTBEAT = "/tmp/matrix-heartbeat-15"
W = H = 64

FONT_DIRS = [
    f"{_HOME}/mlb-led-scoreboard/assets/fonts/patched",
    f"{_HOME}/rpi-spotify-matrix-display/rpi-rgb-led-matrix/fonts",
    f"{_HOME}/rpi-rgb-led-matrix/fonts",
]

_session = requests.Session()


def parse_args():
    ap = argparse.ArgumentParser(prog="MatrixMessage")
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--device-token", default="")
    ap.add_argument("--brightness", type=int, default=None)
    ap.add_argument("--hardware-mapping", default="adafruit-hat-pwm")
    ap.add_argument("--gpio-slowdown", type=int, default=2)
    ap.add_argument("--pixel-mapper", default=None)
    return ap.parse_args()


# ── Fonts ─────────────────────────────────────────────────────────────────────

def _load_font(size):
    """Prefer a real TTF; fall back to PIL's builtin so we always render
    something rather than crashing on a bare install."""
    for d in FONT_DIRS:
        for name in ("04B_03__.TTF", "DejaVuSans.ttf", "Roboto-Regular.ttf"):
            p = os.path.join(d, name)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ── Fetching ──────────────────────────────────────────────────────────────────

def fetch_current(api_base, token):
    """Oldest undismissed message, or None. Returns the tuple we render from."""
    try:
        headers = {"X-Device-Token": token} if token else {}
        r = _session.get(f"{api_base}/messages", headers=headers, timeout=6)
        if not r.ok:
            return None
        msgs = r.json().get("messages") or []
        return msgs[0] if msgs else None
    except Exception as e:
        print(f"[message] fetch error: {e}", flush=True)
        return None


def fetch_image(api_base, token, msg_id):
    try:
        headers = {"X-Device-Token": token} if token else {}
        r = _session.get(f"{api_base}/messages/{msg_id}/image", headers=headers, timeout=8)
        if not r.ok:
            return None
        img = Image.open(BytesIO(r.content)).convert("RGB")
        if img.size != (W, H):
            img = img.resize((W, H), Image.LANCZOS)
        return img
    except Exception as e:
        print(f"[message] image error: {e}", flush=True)
        return None


# ── Rendering ─────────────────────────────────────────────────────────────────

def wrap_to_width(draw, text, font, max_w):
    """Greedy word wrap, splitting any single word too long to fit."""
    words, lines, cur = text.split(), [], ""
    def width(s):
        return draw.textbbox((0, 0), s, font=font)[2]
    for w in words:
        trial = f"{cur} {w}".strip()
        if width(trial) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while width(w) > max_w and len(w) > 1:
            cut = len(w)
            while cut > 1 and width(w[:cut]) > max_w:
                cut -= 1
            lines.append(w[:cut])
            w = w[cut:]
        cur = w
    if cur:
        lines.append(cur)
    return lines


def render_text(msg, font_from, font_body, scroll):
    frame = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(frame)

    # Sender strip along the top so you know who it's from at a glance.
    d.rectangle([0, 0, W - 1, 8], fill=(20, 60, 120))
    sender = (msg.get("from") or "").strip() or "Display"
    while sender and d.textbbox((0, 0), sender, font=font_from)[2] > W - 4:
        sender = sender[:-1]
    d.text((2, 1), sender, font=font_from, fill=(190, 220, 255))

    lines = wrap_to_width(d, msg.get("text") or "", font_body, W - 4)
    line_h = (d.textbbox((0, 0), "Ag", font=font_body)[3]) + 2
    area_top, area_bot = 11, H - 1
    visible = max(1, (area_bot - area_top) // line_h)

    if len(lines) <= visible:
        y = area_top + max(0, ((area_bot - area_top) - len(lines) * line_h) // 2)
        for ln in lines:
            d.text((2, y), ln, font=font_body, fill=(255, 255, 255))
            y += line_h
    else:
        # Too tall to fit: scroll the block vertically, pausing at each end.
        span = len(lines) * line_h - (area_bot - area_top)
        cycle = span + 40                     # 20 frames of dwell top and bottom
        pos = scroll % (cycle * 2)
        off = pos if pos <= cycle else cycle * 2 - pos
        off = max(0, min(span, off - 20))
        y = area_top - off
        for ln in lines:
            if -line_h < y < H:
                d.text((2, y), ln, font=font_body, fill=(255, 255, 255))
            y += line_h
        # Mask anything that bled into the sender strip.
        d.rectangle([0, 0, W - 1, 8], fill=(20, 60, 120))
        d.text((2, 1), sender, font=font_from, fill=(190, 220, 255))
    return frame


def render_image(img, msg, font_from):
    frame = img.copy()
    caption = (msg.get("text") or "").strip()
    d = ImageDraw.Draw(frame)
    sender = (msg.get("from") or "").strip() or "Display"
    label = f"{sender}: {caption}" if caption else sender
    while label and d.textbbox((0, 0), label, font=font_from)[2] > W - 4:
        label = label[:-1]
    # Dark strip keeps the name legible over a bright picture.
    d.rectangle([0, H - 9, W - 1, H - 1], fill=(0, 0, 0))
    d.text((2, H - 8), label, font=font_from, fill=(190, 220, 255))
    return frame


def render_waiting(font_body):
    frame = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(frame)
    d.text((6, 26), "no message", font=font_body, fill=(70, 70, 70))
    return frame


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    opts = RGBMatrixOptions()
    opts.rows = H
    opts.cols = W
    opts.hardware_mapping = args.hardware_mapping
    if args.brightness is not None:
        opts.brightness = max(1, min(100, args.brightness))
    opts.gpio_slowdown = args.gpio_slowdown
    opts.drop_privileges = False
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper

    matrix = RGBMatrix(options=opts)

    font_from = _load_font(7)
    font_body = _load_font(9)

    current = None          # message dict currently displayed
    current_img = None      # decoded picture for image messages
    last_poll = 0.0
    last_hb = 0.0
    scroll = 0
    frame_ctr = 0
    running = [True]

    def _stop(sig, frm):
        running[0] = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print("[message] started", flush=True)

    while running[0]:
        now = time.time()

        # Poll for a new/dismissed message. The agent switches modes on the WS
        # signal, so this only needs to catch the message *changing* underneath
        # us — every few seconds is plenty.
        if now - last_poll > 4.0:
            last_poll = now
            msg = fetch_current(args.api_base, args.device_token)
            new_id = msg.get("id") if msg else None
            cur_id = current.get("id") if current else None
            if new_id != cur_id:
                current = msg
                current_img = None
                scroll = 0
                if msg and msg.get("kind") in ("drawing", "gallery"):
                    current_img = fetch_image(args.api_base, args.device_token, msg["id"])

        if now - last_hb > 30.0:
            try:
                with open(HEARTBEAT, "w") as f:
                    f.write(str(now))
            except Exception:
                pass
            last_hb = now

        try:
            if current is None:
                frame = render_waiting(font_body)
            elif current_img is not None:
                frame = render_image(current_img, current, font_from)
            else:
                frame = render_text(current, font_from, font_body, scroll)
            matrix.SetImage(frame, 0, 0)
        except Exception as e:
            print(f"[message] draw error: {e}", flush=True)

        scroll += 1
        frame_ctr += 1
        if frame_ctr % 300 == 0:
            gc.collect()

        time.sleep(0.05)

    print("[message] exited cleanly", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
