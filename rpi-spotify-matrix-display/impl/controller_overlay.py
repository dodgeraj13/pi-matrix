#!/usr/bin/env python3
"""
controller_overlay.py
─────────────────────
Drop-in replacement for controller_v3.py that adds seamless phone-notification
overlays without touching a single line of the original controller.

Usage (same flags as controller_v3.py):
  python controller_overlay.py [-f] [-e]

How it works
────────────
1. Pre-imports the matrix library (real hardware or emulator) and replaces
   RGBMatrix with a thin Python subclass whose SetImage intercepts every
   rendered frame before it hits the display.

2. Reads config.ini to build the backend WebSocket URL, then starts a
   _NotificationOverlay (background thread + PIL blend).

3. Calls controller_v3.main() — which now unknowingly uses the wrapped
   matrix class, giving us a transparent hook on every frame.

controller_v3.py is completely unmodified and can be git-pulled from
upstream at any time without merge conflicts.

One-time Pi setup
─────────────────
  pip install websocket-client

Then update your systemd service (or however you launch the controller) to
run controller_overlay.py instead of controller_v3.py.  All flags are
passed through unchanged.
"""

import os, sys, inspect, json, time, threading, queue, configparser
from pathlib import Path
from PIL import Image

# ── Locate repo root (same logic as controller_v3.py) ───────────────────────
_currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
_repo_root  = Path(_currentdir).parent

# ── Read config early so we have the backend URL before patching ─────────────
_config = configparser.ConfigParser()
_config.read(str(_repo_root / "config.ini"))
_backend_base = _config.get("Spotify", "backend_url", fallback="").rstrip("/")
_device_token = _config.get("Spotify", "device_token", fallback="")
_is_emulated  = "-e" in sys.argv or "--emulated" in sys.argv

# Add rgbmatrix Python bindings to path only if the system-level install isn't
# already available.  Inserting an uncompiled local copy ahead of the system
# package causes "No module named 'rgbmatrix.core'" at runtime.
_rgb_bindings = _repo_root / "rpi-rgb-led-matrix" / "bindings" / "python"
try:
    import rgbmatrix as _rgbmatrix_probe  # noqa: F401 — probe only
    del _rgbmatrix_probe
except ImportError:
    if _rgb_bindings.exists():
        sys.path.insert(0, str(_rgb_bindings))


# ── Notification overlay ──────────────────────────────────────────────────────

class _NotificationOverlay:
    """Background WebSocket listener + per-frame PIL blend.

    Connects to the same /ws endpoint used by the browser.  When the backend
    broadcasts a notification message the icon is queued; blend_onto() is
    called on every matrix frame to composite it with a smooth fade-in/out.
    """

    FADE_SECS = 0.35

    def __init__(self, ws_url: str):
        self._url     = ws_url
        self._q: queue.Queue = queue.Queue(maxsize=5)
        self._img     = None          # current notification PIL Image or None
        self._start   = 0.0           # time.time() when it started
        self._dur     = 2.5           # total display seconds
        self._enabled = True
        threading.Thread(target=self._listen, daemon=True).start()

    # ── background WebSocket thread ───────────────────────────────────────────

    def _listen(self):
        """Persistent connection with automatic reconnect."""
        while True:
            try:
                import websocket
                websocket.WebSocketApp(
                    self._url,
                    on_message=self._on_msg,
                    on_error=lambda ws, e: None,
                    on_close=lambda ws, c, m: None,
                ).run_forever(ping_interval=20, ping_timeout=10, reconnect=5)
            except ImportError:
                print("[notif] websocket-client not installed — run: pip install websocket-client")
                return
            except Exception as e:
                print(f"[notif] ws error: {e}")
            time.sleep(5)

    def _on_msg(self, ws, raw):
        try:
            data = json.loads(raw)
            if data.get("type") == "notification" and data.get("icon") and self._enabled:
                self._q.put_nowait(data)
            elif data.get("type") == "notification_settings":
                self._enabled = bool(data.get("enabled", True))
                self._dur     = float(data.get("duration", 2.5))
        except Exception:
            pass

    # ── main-loop API (called from the patched SetImage) ─────────────────────

    def tick(self):
        """Drain the queue and arm the pending icon.  Call once per frame."""
        try:
            data = self._q.get_nowait()
            import base64, io
            raw = base64.b64decode(data["icon"])
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            self._img   = img.resize((64, 64), Image.Resampling.LANCZOS)
            self._start = time.time()
            self._dur   = float(data.get("duration", self._dur))
        except queue.Empty:
            pass
        except Exception as e:
            print(f"[notif] decode error: {e}")
            self._img = None

    def blend_onto(self, frame: Image.Image) -> Image.Image:
        """Return frame with the notification icon composited on top, or frame unchanged."""
        if self._img is None:
            return frame
        elapsed = time.time() - self._start
        if elapsed >= self._dur:
            self._img = None
            return frame
        f = self.FADE_SECS
        if elapsed < f:
            alpha = elapsed / f                          # fade in
        elif elapsed > self._dur - f:
            alpha = max(0.0, (self._dur - elapsed) / f)  # fade out
        else:
            alpha = 1.0                                  # hold
        return Image.blend(frame.convert("RGB"), self._img, alpha)


# ── Global overlay instance ───────────────────────────────────────────────────
_overlay: _NotificationOverlay | None = None


# ── Wrap RGBMatrix.SetImage BEFORE controller_v3 imports the library ──────────
#
# Python module imports are cached in sys.modules.  By importing rgbmatrix here
# and replacing its RGBMatrix with a subclass, we guarantee that when
# controller_v3.main() does `from rgbmatrix import RGBMatrix` it gets our
# wrapped version — no source edits needed.

def _wrap_matrix_module(mod):
    """Replace mod.RGBMatrix with a subclass whose SetImage applies the overlay."""
    orig = getattr(mod, "RGBMatrix", None)
    if orig is None:
        return

    class _Wrapped(orig):
        def SetImage(self, frame, *args, **kwargs):
            if _overlay:
                _overlay.tick()
                frame = _overlay.blend_onto(frame)
            super().SetImage(frame, *args, **kwargs)

    mod.RGBMatrix = _Wrapped


if _is_emulated:
    try:
        import RGBMatrixEmulator as _mod
        _wrap_matrix_module(_mod)
        print("[overlay] RGBMatrixEmulator.RGBMatrix wrapped ✓")
    except ImportError:
        print("[overlay] RGBMatrixEmulator not found — skipping wrap")
else:
    try:
        import rgbmatrix as _mod
        _wrap_matrix_module(_mod)
        print("[overlay] rgbmatrix.RGBMatrix wrapped ✓")
    except ImportError:
        print("[overlay] rgbmatrix not found — skipping wrap (are you on Pi hardware?)")


# ── Start overlay (needs to happen before main() so _overlay is set) ──────────

if _backend_base and _device_token:
    _ws_url  = _backend_base.replace("https://", "wss://").replace("http://", "ws://")
    _ws_url += f"/ws?device={_device_token}"
    _overlay  = _NotificationOverlay(_ws_url)
    print(f"[notif] overlay started → {_ws_url}")
else:
    print("[notif] backend_url or device_token missing in config.ini — overlay disabled")


# ── Hand off to the original controller ──────────────────────────────────────

if __name__ == "__main__":
    import controller_v3
    controller_v3.main()
