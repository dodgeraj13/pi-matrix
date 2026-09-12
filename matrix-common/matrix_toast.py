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
SLIDE_S     = 0.45      # slide in / slide out
ANIM_FPS    = 30.0      # frame rate a host loop should run at while animating
MAX_BANNER_H= 30        # under half a 64px panel: a notice, not a takeover
W = H = 64


def _ease_out(p):
    """Fast at first, settling at the end — reads as deceleration."""
    p = max(0.0, min(1.0, p))
    return 1.0 - (1.0 - p) ** 3

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

    # ── animation ──
    def _rows_visible(self, now):
        """How many rows of the banner are on screen right now.

        The banner slides up from below the bottom edge rather than fading.
        A slide needs no knowledge of what's underneath, so it works
        identically whether we can read the frame back or not — and it stays
        legible the whole way, where a half-faded banner just looks murky.
        Returns 0 when nothing should be drawn.
        """
        if self._banner is None:
            return 0
        elapsed = now - self._start
        if elapsed < 0 or elapsed > self._dur:
            self._banner = None
            return 0
        if elapsed < SLIDE_S:
            p = _ease_out(elapsed / SLIDE_S)
        elif elapsed > self._dur - SLIDE_S:
            p = _ease_out(max(0.0, (self._dur - elapsed) / SLIDE_S))
        else:
            p = 1.0
        return int(round(self._h * p))

    def animating(self, now=None):
        """True while the banner is sliding, so a host loop can draw faster."""
        if self._banner is None:
            return False
        e = (now or time.time()) - self._start
        return 0 <= e < SLIDE_S or (self._dur - SLIDE_S) < e <= self._dur

    # ── compositing ──
    def apply(self, frame):
        """Return `frame` with the banner composited in, or unchanged."""
        now = time.time()
        self._poll(now)
        n = self._rows_visible(now)
        if n <= 0:
            return frame
        try:
            out = frame.convert("RGB") if frame.mode != "RGB" else frame.copy()
            # Only the top n rows are on screen; the rest is still below the edge.
            out.paste(self._banner.crop((0, 0, W, n)), (0, H - n))
            return out
        except Exception:
            return frame


def _render_banner(doc):
    """Pre-render the banner once, so per-frame work is just a blend."""
    text  = (doc.get("text") or "").strip()
    title = (doc.get("title") or "").strip()
    icon_b64 = doc.get("icon") or ""

    f_title, f_text = _font(7), _font(8)
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    line_h  = tmp.textbbox((0, 0), "Ag", font=f_text)[3] + 1
    head_h  = 9 if title else 0
    # Keep the banner under half the panel — it's a notice, not a takeover, and
    # whatever is behind it should still be recognisable.
    room    = MAX_BANNER_H - 4 - head_h
    max_ln  = max(1, room // line_h)
    lines   = _wrap(tmp, text, f_text, W - 6)[:max_ln]
    # Size to the content so the banner never clips a line it drew.
    h = min(MAX_BANNER_H, max(13, 4 + head_h + len(lines) * line_h))

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
        y += head_h
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

_ATTACHED = []      # every Toast created by attach(), for frame_delay()


def frame_delay(default):
    """Sleep this long instead of `default` at the end of a draw loop.

    Some scripts idle at one or two frames a second, which is plenty for a
    clock but means an animation gets a single frame and looks like a jump
    cut. While a banner is sliding this returns a much shorter delay, then
    goes back to the script's own pace once it settles. Costs nothing when no
    notification is on screen.
    """
    try:
        now = time.time()
        if any(t.animating(now) for t in _ATTACHED):
            return min(default, 1.0 / ANIM_FPS)
    except Exception:
        pass
    return default


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
        _ATTACHED.append(toast)
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

    A FrameCanvas is write-only, so there is nothing to read back and blend
    against — which is exactly why the animation is a slide. Only the rows
    currently on screen get written, so the same motion works here as on the
    PIL path with no compromise.
    """
    now = time.time()
    toast._poll(now)
    n = toast._rows_visible(now)
    if n <= 0:
        return
    b = toast._banner
    px = b.load()
    top = H - n
    for yy in range(n):
        row = top + yy
        for xx in range(W):
            r, g, bl = px[xx, yy]
            canvas.SetPixel(xx, row, r, g, bl)
