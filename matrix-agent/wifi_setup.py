"""
WiFi Provisioning Script for Matrix Raspberry Pi
=================================================

This script checks for internet connectivity and, if offline, creates a
WiFi hotspot so the user can provision network credentials via a web UI.

TESTING:
  - To test hotspot mode (even when online):
      sudo python3 wifi_setup.py --force-setup
  - To test when actually offline:
      python3 wifi_setup.py

REQUIREMENTS:
  - NetworkManager (nmcli) installed
  - wlan0 interface present
  - Script run as root or pi user with NOPASSWD sudo for nmcli
  - For LED display: rpi-rgb-led-matrix installed at /home/pi_two/rpi-rgb-led-matrix
"""

import sys
import os
import time
import socket
import subprocess
import threading
import json
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
HOTSPOT_SSID     = "Matrix-Setup"
HOTSPOT_PASSWORD = "matrix1234"
HOTSPOT_CON_NAME = "matrix-hotspot"
HOTSPOT_IP       = "10.42.0.1"
HTTP_PORT        = 80
# Resolve the real user's home even when run via sudo
_REAL_USER       = os.environ.get("SUDO_USER") or os.environ.get("USER") or "pi"
_HOME            = os.path.expanduser(f"~{_REAL_USER}")
FONT_PATH        = os.path.join(_HOME, "rpi-rgb-led-matrix", "fonts", "6x10.bdf")
SCROLL_TEXT      = "  Connect to WiFi: Matrix-Setup  pw: matrix1234  then visit 10.42.0.1  "

# ──────────────────────────────────────────────
# Connectivity check
# ──────────────────────────────────────────────

def is_online() -> bool:
    """Return True if we can reach 8.8.8.8:53 (DNS)."""
    try:
        sock = socket.create_connection(("8.8.8.8", 53), timeout=3)
        sock.close()
        return True
    except OSError:
        return False


# ──────────────────────────────────────────────
# nmcli helpers (prefix with sudo -n for rootless use)
# ──────────────────────────────────────────────

def _nmcli(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run nmcli with sudo -n so the script can be invoked as a normal user."""
    cmd = ["sudo", "-n", "nmcli"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def wlan0_exists() -> bool:
    """Check that wlan0 is available."""
    result = subprocess.run(
        ["ip", "link", "show", "wlan0"],
        capture_output=True, text=True
    )
    return result.returncode == 0


def create_hotspot() -> bool:
    """Create the Matrix-Setup WiFi hotspot. Returns True on success."""
    print("[wifi] Creating hotspot …")
    result = _nmcli(
        "device", "wifi", "hotspot",
        "ifname", "wlan0",
        "ssid",   HOTSPOT_SSID,
        "password", HOTSPOT_PASSWORD,
        "con-name", HOTSPOT_CON_NAME,
    )
    if result.returncode != 0:
        print(f"[wifi] Failed to create hotspot: {result.stderr.strip()}")
        return False
    print(f"[wifi] Hotspot '{HOTSPOT_SSID}' up. IP: {HOTSPOT_IP}")
    return True


def stop_hotspot() -> None:
    """Bring down and delete the hotspot connection profile."""
    _nmcli("con", "down", HOTSPOT_CON_NAME)
    _nmcli("con", "delete", HOTSPOT_CON_NAME)
    print("[wifi] Hotspot stopped and deleted.")


def scan_networks() -> list[dict]:
    """
    Rescan and return a deduplicated list of visible networks,
    sorted by signal strength (descending), excluding our own hotspot.
    """
    # Trigger a rescan (ignore errors — device may be busy)
    _nmcli("device", "wifi", "rescan")
    time.sleep(2)

    result = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")
    seen: set[str] = set()
    networks: list[dict] = []

    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        ssid     = parts[0].strip()
        signal   = parts[1].strip()
        security = ":".join(parts[2:]).strip()

        if not ssid or ssid == HOTSPOT_SSID:
            continue
        if ssid in seen:
            continue
        seen.add(ssid)

        try:
            sig_int = int(signal)
        except ValueError:
            sig_int = 0

        networks.append({"ssid": ssid, "signal": sig_int, "security": security})

    networks.sort(key=lambda n: n["signal"], reverse=True)
    return networks


def connect_to_network(ssid: str, password: str) -> tuple[bool, str]:
    """
    Attempt to connect to a WiFi network.
    Returns (success: bool, error_message: str).
    """
    print(f"[wifi] Attempting connection to '{ssid}' …")
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]

    try:
        result = _nmcli(*args, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "Connection attempt timed out after 30 s"

    if result.returncode == 0:
        print(f"[wifi] Connected to '{ssid}'.")
        return True, ""
    else:
        err = result.stderr.strip() or result.stdout.strip()
        print(f"[wifi] Connection failed: {err}")
        return False, err


# ──────────────────────────────────────────────
# LED matrix display (optional)
# ──────────────────────────────────────────────

def start_led_scroll(stop_event: threading.Event) -> None:
    """
    Start a daemon thread that scrolls setup instructions on the LED matrix.
    Falls back to a plain print if rgbmatrix is not available.
    """
    try:
        # Import inside try/except so script works without the library
        from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics  # type: ignore
        _run_led_scroll(stop_event, RGBMatrix, RGBMatrixOptions, graphics)
    except ImportError:
        print("[led] rgbmatrix not available — skipping LED display.")
        print(f"[led] {SCROLL_TEXT.strip()}")


def _run_led_scroll(stop_event, RGBMatrix, RGBMatrixOptions, graphics) -> None:
    """Internal: configure matrix and scroll text in a daemon thread."""

    def _thread_body():
        try:
            options = RGBMatrixOptions()
            options.rows                = 64
            options.cols                = 64
            options.hardware_mapping    = "adafruit-hat-pwm"
            options.gpio_slowdown       = 2
            options.disable_hardware_pulsing = True

            matrix = RGBMatrix(options=options)
            canvas = matrix.CreateFrameCanvas()

            font = graphics.Font()
            if not os.path.exists(FONT_PATH):
                print(f"[led] Font not found at {FONT_PATH} — skipping LED display.")
                return
            font.LoadFont(FONT_PATH)

            white = graphics.Color(255, 255, 255)
            text  = SCROLL_TEXT
            pos   = canvas.width  # start off-screen to the right

            while not stop_event.is_set():
                canvas.Clear()
                text_len = graphics.DrawText(canvas, font, pos, 48, white, text)
                pos -= 1
                if pos + text_len < 0:
                    pos = canvas.width   # reset scroll

                canvas = matrix.SwapOnVSync(canvas)
                time.sleep(0.03)  # ~33 fps scroll speed

            matrix.Clear()
        except Exception as e:
            print(f"[led] Error in LED thread: {e}")

    t = threading.Thread(target=_thread_body, daemon=True)
    t.start()


# ──────────────────────────────────────────────
# HTML page (inline, no templates)
# ──────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Matrix WiFi Setup</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0d0d0d;
      color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      display: flex;
      justify-content: center;
      align-items: flex-start;
      min-height: 100vh;
      padding: 2rem 1rem;
    }
    .card {
      background: #1a1a1a;
      border: 1px solid #333;
      border-radius: 12px;
      padding: 2rem;
      width: 100%;
      max-width: 420px;
    }
    h1 { font-size: 1.5rem; margin-bottom: 0.4rem; color: #fff; }
    p.sub { color: #888; font-size: 0.9rem; margin-bottom: 1.5rem; }
    label { display: block; font-size: 0.85rem; color: #aaa; margin-bottom: 0.3rem; }
    select, input[type="password"] {
      width: 100%;
      padding: 0.65rem 0.75rem;
      background: #111;
      border: 1px solid #444;
      border-radius: 8px;
      color: #e0e0e0;
      font-size: 1rem;
      margin-bottom: 1rem;
    }
    button {
      width: 100%;
      padding: 0.75rem;
      border: none;
      border-radius: 8px;
      font-size: 1rem;
      cursor: pointer;
      margin-bottom: 0.75rem;
      transition: opacity 0.2s;
    }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .btn-scan    { background: #2a2a2a; color: #ccc; border: 1px solid #555; }
    .btn-connect { background: #4a90d9; color: #fff; }
    #status {
      margin-top: 1rem;
      padding: 0.75rem;
      border-radius: 8px;
      font-size: 0.9rem;
      display: none;
    }
    #status.ok  { background: #1a3a1a; color: #6fcf6f; border: 1px solid #3a7a3a; }
    #status.err { background: #3a1a1a; color: #cf6f6f; border: 1px solid #7a3a3a; }
    #status.inf { background: #1a2a3a; color: #6fafcf; border: 1px solid #3a5a7a; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Matrix WiFi Setup</h1>
    <p class="sub">Connect your Matrix device to your home network.</p>

    <label for="network">Network</label>
    <select id="network">
      <option value="">— scan for networks —</option>
    </select>

    <label for="password">Password</label>
    <input type="password" id="password" placeholder="WiFi password"/>

    <button class="btn-scan" id="scanBtn" onclick="scanNetworks()">Scan for networks</button>
    <button class="btn-connect" id="connectBtn" onclick="connectNetwork()" disabled>Connect</button>

    <div id="status"></div>
  </div>

  <script>
    const sel       = document.getElementById('network');
    const connectBtn = document.getElementById('connectBtn');
    const statusDiv = document.getElementById('status');

    function showStatus(msg, type) {
      statusDiv.textContent = msg;
      statusDiv.className   = type;
      statusDiv.style.display = 'block';
    }

    sel.addEventListener('change', () => {
      connectBtn.disabled = sel.value === '';
    });

    async function scanNetworks() {
      const btn = document.getElementById('scanBtn');
      btn.disabled = true;
      btn.textContent = 'Scanning…';
      showStatus('Scanning for networks, please wait…', 'inf');
      try {
        const resp = await fetch('/scan');
        const nets = await resp.json();
        sel.innerHTML = '<option value="">— choose a network —</option>';
        if (nets.length === 0) {
          showStatus('No networks found. Try scanning again.', 'inf');
        } else {
          nets.forEach(n => {
            const opt = document.createElement('option');
            opt.value       = n.ssid;
            opt.textContent = n.ssid + '  (' + n.signal + '%)' + (n.security ? '  🔒' : '');
            sel.appendChild(opt);
          });
          showStatus('Found ' + nets.length + ' network(s). Select one above.', 'inf');
        }
      } catch(e) {
        showStatus('Scan failed: ' + e.message, 'err');
      } finally {
        btn.disabled    = false;
        btn.textContent = 'Scan for networks';
      }
    }

    async function connectNetwork() {
      const ssid = sel.value;
      const pw   = document.getElementById('password').value;
      if (!ssid) { showStatus('Please select a network first.', 'err'); return; }

      connectBtn.disabled = true;
      showStatus('Connecting to "' + ssid + '"… this may take up to 30 seconds.', 'inf');

      const form = new FormData();
      form.append('ssid',     ssid);
      form.append('password', pw);

      try {
        const resp = await fetch('/connect', { method: 'POST', body: new URLSearchParams(form) });
        const data = await resp.json();
        if (data.ok) {
          showStatus('Connected! The device will now restart its agent. You can close this page.', 'ok');
        } else {
          showStatus('Connection failed: ' + (data.error || 'unknown error'), 'err');
          connectBtn.disabled = false;
        }
      } catch(e) {
        showStatus('Error: ' + e.message, 'err');
        connectBtn.disabled = false;
      }
    }
  </script>
</body>
</html>
"""


# ──────────────────────────────────────────────
# HTTP request handler
# ──────────────────────────────────────────────

# Shared mutable state between HTTP handler and main thread
_server_state = {
    "success": False,
}


class ProvisionHandler(BaseHTTPRequestHandler):
    """Handles GET / (HTML), GET /scan (JSON), POST /connect (form)."""

    # Silence the default request logging
    def log_message(self, fmt, *args):  # noqa: N802
        print(f"[http] {self.address_string()} — {fmt % args}")

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/":
            self._send_html(HTML_PAGE)
        elif self.path == "/scan":
            nets = scan_networks()
            self._send_json(nets)
        else:
            self.send_error(404, "Not found")

    def do_POST(self):  # noqa: N802
        if self.path != "/connect":
            self.send_error(404, "Not found")
            return

        length  = int(self.headers.get("Content-Length", 0))
        raw     = self.rfile.read(length).decode()
        params  = urllib.parse.parse_qs(raw)
        ssid    = params.get("ssid", [""])[0].strip()
        password = params.get("password", [""])[0]

        if not ssid:
            self._send_json({"ok": False, "error": "No SSID provided"})
            return

        # Take the hotspot down so wlan0 is free to connect
        print("[http] Stopping hotspot to attempt connection…")
        stop_hotspot()
        time.sleep(2)

        ok, err = connect_to_network(ssid, password)
        if ok:
            _server_state["success"] = True
            self._send_json({"ok": True})
        else:
            # Restore hotspot so the user can try again
            print("[http] Connection failed — restarting hotspot…")
            create_hotspot()
            self._send_json({"ok": False, "error": err})


# ──────────────────────────────────────────────
# HTTP server lifecycle
# ──────────────────────────────────────────────

class _ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True  # must be set before bind() in __init__


def start_http_server() -> HTTPServer:
    """Start the provisioning HTTP server in a daemon thread."""
    server = _ReusableHTTPServer(("0.0.0.0", HTTP_PORT), ProvisionHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[http] Server listening on port {HTTP_PORT}")
    return server


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────

def main() -> int:
    force_setup = "--force-setup" in sys.argv

    # ── 1. Quick connectivity check ──────────────────────────────────────
    if not force_setup:
        if is_online():
            print("[wifi] Already online — nothing to do.")
            return 0

        print("[wifi] Offline — waiting 10 s before retry…")
        time.sleep(10)

        if is_online():
            print("[wifi] Online after retry — nothing to do.")
            return 0

        print("[wifi] Still offline — starting provisioning mode.")
    else:
        print("[wifi] --force-setup flag detected — entering hotspot mode directly.")

    # ── 2. Sanity-check wlan0 ────────────────────────────────────────────
    if not wlan0_exists():
        print("[wifi] ERROR: wlan0 interface not found. Cannot create hotspot.")
        return 1

    # ── 3. Create hotspot ────────────────────────────────────────────────
    if not create_hotspot():
        print("[wifi] ERROR: Failed to create hotspot — check nmcli / sudo permissions.")
        return 1

    # ── 4. Start LED scroll (daemon thread) ──────────────────────────────
    led_stop = threading.Event()
    led_thread = threading.Thread(
        target=start_led_scroll, args=(led_stop,), daemon=True
    )
    led_thread.start()

    # ── 5. Give hotspot time to assign IPs, then start HTTP server ───────
    time.sleep(1)
    server = start_http_server()

    print(f"[wifi] Provisioning active. Connect to '{HOTSPOT_SSID}' "
          f"(pw: {HOTSPOT_PASSWORD}) then open http://{HOTSPOT_IP}/")

    # ── 6. Poll for connectivity (main thread) ────────────────────────────
    try:
        while True:
            time.sleep(3)
            if _server_state["success"] or is_online():
                print("[wifi] Internet connection detected — shutting down provisioning.")
                break
    except KeyboardInterrupt:
        print("\n[wifi] Interrupted — cleaning up.")
    finally:
        # ── 7. Cleanup ───────────────────────────────────────────────────
        led_stop.set()
        try:
            server.shutdown()
        except Exception:
            pass
        # Always delete the hotspot profile, even if it was already stopped
        # by a successful /connect request.
        _nmcli("con", "delete", HOTSPOT_CON_NAME)
        print("[wifi] Cleanup complete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
