#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matrix_toast — composite short-lived notifications over a running display.

Every display script funnels its output through one of two calls: SetImage()
for the PIL-based ones, or SwapOnVSync() for the ones that draw with the
graphics API onto a FrameCanvas. That single choke point is what makes toasts
possible without restarting anything: wrap the matrix object, and each frame
on its way to the panel can have a banner blended onto it first.

The agent drops notifications at /tmp/matrix-notify.json (see write_notification
in agent.py). Nothing here talks to the network.

Usage — one line, right after the matrix is created:

    from matrix_toast import attach
    matrix = attach(RGBMatrix(options=opts))

attach() is deliberately forgiving: if anything at all goes wrong — missing
file, bad JSON, no PIL, an unexpected matrix class — it returns the original
object untouched and the display carries on exactly as before. A notification
is never worth breaking the panel over.
"""

import json
import os
import time

NOTIFY_FILE = os.environ.get("MATRIX_NOTIFY_FILE", "/tmp/matrix-notify.json")
POLL_S      = 0.25      # how often to stat the file; a stat is ~free
FADE_S      = 0.30      # in and out
W = H = 64

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAVE_PIL = True
except Exception:                                    # pragma: no cover
    _HAVE_PIL = False


# ── Font ──────────────────────────────────────────────────────────────────────

_FONT_CACHE = {}

def _font(size):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    f = None
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                break
            except Exception:
                pass
    if f is None:
        f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


# ── The toast itself ──────────────────────────────────────────────────────────

class Toast:
    """Watches the notify file and paints the current banner, if any."""

    def __init__(self, path=NOTIFY_FILE):
        self._path     = path
        self._mtime    = -1.0
        self._last_poll = 0.0
        self._seq      = None
        self._start    = 0.0
        self._dur      = 0.0
        self._banner   = None       # pre-rendered RGB banner
        self._h        = 0

        # Ignore whatever is already on disk at startup, so a notification from
        # an hour ago doesn't reappear every time a mode switches.
        try:
            self._mtime = os.path.getmtime(self._path)
            with open(self._path) as f:
                self._seq = json.load(f).get("seq")
        except Exception:
            pass

    # ── polling ──
    def _poll(self, now):
        if now - self._last_poll < POLL_S:
            return
        self._last_poll = now
        try:
            m = os.path.getmtime(self._path)
        except OSError:
            return                                   # no file yet — normal
        if m == self._mtime:
            return
        self._mtime = m
        try:
            with open(self._path) as f:
                doc = json.load(f)
        except Exception:
            return                                   # mid-write or malformed
        seq = doc.get("seq")
        if seq is None or seq == self._seq:
            return
        self._seq = seq
        self._arm(doc, now)

    def _arm(self, doc, now):
        try:
            self._banner, self._h = _render_banner(doc)
            self._dur   = max(1.0, float(doc.get("duration") or 4.0))
            self._start = now
        except Exception:
            self._banner = None

    # ── compositing ──
    def apply(self, frame):
        """Return `frame` with the banner blended in, or unchanged."""
        now = time.time()
        self._poll(now)
        if self._banner is None:
            return frame
        elapsed = now - self._start
        if elapsed > self._dur:
            self._banner = None
            return frame

        # Ease in, hold, ease out.
        if elapsed < FADE_S:
            a = elapsed / FADE_S
        elif elapsed > self._dur - FADE_S:
            a = max(0.0, (self._dur - elapsed) / FADE_S)
        else:
            a = 1.0
        if a <= 0.01:
            return frame

        try:
            out = frame.convert("RGB") if frame.mode != "RGB" else frame.copy()
            y = H - self._h
            strip = out.crop((0, y, W, H))
            out.paste(Image.blend(strip, self._banner, a), (0, y))
            return out
        except Exception:
            return frame


def _render_banner(doc):
    """Pre-render the banner once, so per-frame work is just a blend."""
    text  = (doc.get("text") or "").strip()
    title = (doc.get("title") or "").strip()
    icon_b64 = doc.get("icon") or ""

    f_title, f_text = _font(8), _font(9)
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    lines = _wrap(tmp, text, f_text, W - 6)[:2]      # two lines is the limit
    line_h = tmp.textbbox((0, 0), "Ag", font=f_text)[3] + 1
    h = 4 + (10 if title else 0) + len(lines) * line_h
    h = max(14, min(34, h))

    banner = Image.new("RGB", (W, h), (10, 16, 30))
    d = ImageDraw.Draw(banner)
    d.line([(0, 0), (W - 1, 0)], fill=(95, 212, 255))   # accent edge

    x = 3
    if icon_b64:
        try:
            import base64
            from io import BytesIO
            ic = Image.open(BytesIO(base64.b64decode(icon_b64))).convert("RGB")
            side = h - 6
            ic = ic.resize((side, side), Image.LANCZOS)
            banner.paste(ic, (3, 3))
            x = 3 + side + 3
        except Exception:
            pass

    y = 2
    if title:
        d.text((x, y), title[:18], font=f_title, fill=(120, 190, 240))
        y += 10
    for ln in lines:
        d.text((x, y), ln, font=f_text, fill=(255, 255, 255))
        y += line_h
    return banner, h


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    def wide(s):
        return draw.textbbox((0, 0), s, font=font)[2]
    for w in words:
        t = f"{cur} {w}".strip()
        if wide(t) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


# ── Attaching to a live matrix ────────────────────────────────────────────────

def attach(matrix, path=NOTIFY_FILE):
    """Wrap `matrix` so every frame passes through the toast compositor.

    Handles both drawing styles:
      * SetImage(PIL image)  — blended directly
      * SwapOnVSync(canvas)  — the canvas is already on its way to hardware and
        can't be read back, so a toast is drawn onto it with SetPixel before
        the swap.

    Returns the original object unchanged if anything is missing or unexpected.
    """
    if not _HAVE_PIL:
        return matrix
    try:
        toast = Toast(path)
    except Exception:
        return matrix

    if hasattr(matrix, "SetImage"):
        _orig_set = matrix.SetImage
        def SetImage(img, *a, **kw):
            try:
                img = toast.apply(img)
            except Exception:
                pass
            return _orig_set(img, *a, **kw)
        try:
            matrix.SetImage = SetImage
        except Exception:
            return matrix       # C extension with read-only attrs

    if hasattr(matrix, "SwapOnVSync"):
        _orig_swap = matrix.SwapOnVSync
        def SwapOnVSync(canvas, *a, **kw):
            try:
                _stamp(canvas, toast)
            except Exception:
                pass
            return _orig_swap(canvas, *a, **kw)
        try:
            matrix.SwapOnVSync = SwapOnVSync
        except Exception:
            pass

    return matrix


def _stamp(canvas, toast):
    """Draw the toast onto a FrameCanvas pixel by pixel.

    A FrameCanvas is write-only, so there's nothing to blend against — the
    banner is drawn at full opacity once it has faded in far enough to look
    deliberate rather than flickering.
    """
    now = time.time()
    toast._poll(now)
    if toast._banner is None:
        return
    elapsed = now - toast._start
    if elapsed > toast._dur:
        toast._banner = None
        return
    if elapsed < FADE_S * 0.5 or elapsed > toast._dur - FADE_S * 0.5:
        return                                   # skip the faint edges
    b = toast._banner
    top = H - b.height
    px = b.load()
    for yy in range(b.height):
        for xx in range(W):
            r, g, bl = px[xx, yy]
            canvas.SetPixel(xx, top + yy, r, g, bl)
