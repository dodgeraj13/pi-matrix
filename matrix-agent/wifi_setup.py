"""
WiFi Provisioning Script for Matrix Raspberry Pi
=================================================

Checks for internet connectivity. If offline, creates a WiFi hotspot with a
captive portal so the user can provision credentials from any phone or laptop.

How it works for end users:
  1. Pi boots with no WiFi configured
  2. This script runs — creates "Matrix-Setup" hotspot
  3. User connects phone to "Matrix-Setup" (password: matrix1234)
  4. Phone detects captive portal, shows "Sign in to network" popup
  5. User picks their home WiFi, enters password, taps Connect
  6. Pi connects to home WiFi, script exits, agent starts

Testing (from a machine NOT connected over WiFi to the Pi):
  sudo python3 wifi_setup.py --force-setup

REQUIREMENTS:
  - NetworkManager (nmcli) installed and managing wlan0
  - NOPASSWD sudo for nmcli (set up by setup.sh)
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
HOTSPOT_SSID       = "Matrix-Setup"
HOTSPOT_PASSWORD   = "matrix1234"
HOTSPOT_CON_NAME   = "matrix-hotspot"
HOTSPOT_IP         = "10.42.0.1"
HTTP_PORT          = 80

# NetworkManager writes a shared dnsmasq config here when in hotspot mode.
# Adding address=/#/<ip> makes every DNS query resolve to our IP so phones
# automatically show the "Sign in to network" captive portal popup.
NM_DNSMASQ_DIR     = "/etc/NetworkManager/dnsmasq-shared.d"
NM_CAPTIVE_CONF    = os.path.join(NM_DNSMASQ_DIR, "matrix-captive.conf")

# ──────────────────────────────────────────────
# Connectivity check
# ──────────────────────────────────────────────

def is_online() -> bool:
    try:
        s = socket.create_connection(("8.8.8.8", 53), timeout=3)
        s.close()
        return True
    except OSError:
        return False

# ──────────────────────────────────────────────
# nmcli helpers
# ──────────────────────────────────────────────

def _nmcli(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = ["sudo", "-n", "nmcli"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def _run(*args, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)

def wlan0_exists() -> bool:
    return _run("ip", "link", "show", "wlan0").returncode == 0

# ──────────────────────────────────────────────
# Captive portal DNS redirect
# ──────────────────────────────────────────────

def _write_captive_conf() -> None:
    """
    Tell NM's shared dnsmasq to resolve every domain to our hotspot IP.
    This triggers the "Sign in to network" popup on Android, iOS, and Windows.
    """
    try:
        os.makedirs(NM_DNSMASQ_DIR, exist_ok=True)
        conf = f"address=/#/{HOTSPOT_IP}\n"
        result = subprocess.run(
            ["sudo", "-n", "tee", NM_CAPTIVE_CONF],
            input=conf, capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"[wifi] Captive portal DNS config written → {NM_CAPTIVE_CONF}")
        else:
            print(f"[wifi] Warning: could not write captive conf: {result.stderr.strip()}")
    except Exception as e:
        print(f"[wifi] Warning: captive conf error: {e}")

def _remove_captive_conf() -> None:
    try:
        subprocess.run(["sudo", "-n", "rm", "-f", NM_CAPTIVE_CONF],
                       capture_output=True, text=True)
    except Exception:
        pass

# ──────────────────────────────────────────────
# Hotspot lifecycle
# ──────────────────────────────────────────────

def create_hotspot() -> bool:
    print(f"[wifi] Creating hotspot '{HOTSPOT_SSID}' ...")
    result = _nmcli(
        "device", "wifi", "hotspot",
        "ifname", "wlan0",
        "ssid", HOTSPOT_SSID,
        "password", HOTSPOT_PASSWORD,
        "con-name", HOTSPOT_CON_NAME,
    )
    if result.returncode != 0:
        print(f"[wifi] Failed: {result.stderr.strip()}")
        return False
    print(f"[wifi] Hotspot up — SSID: {HOTSPOT_SSID}  password: {HOTSPOT_PASSWORD}  IP: {HOTSPOT_IP}")
    return True

def stop_hotspot() -> None:
    _nmcli("con", "down", HOTSPOT_CON_NAME)
    _nmcli("con", "delete", HOTSPOT_CON_NAME)
    print("[wifi] Hotspot stopped.")

def scan_networks() -> list:
    _nmcli("device", "wifi", "rescan")
    time.sleep(2)
    result = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")
    seen = set()
    networks = []
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        ssid     = parts[0].strip()
        signal   = parts[1].strip()
        security = ":".join(parts[2:]).strip()
        if not ssid or ssid == HOTSPOT_SSID or ssid in seen:
            continue
        seen.add(ssid)
        try:
            sig_int = int(signal)
        except ValueError:
            sig_int = 0
        networks.append({"ssid": ssid, "signal": sig_int, "security": security})
    networks.sort(key=lambda n: n["signal"], reverse=True)
    return networks

def connect_to_network(ssid: str, password: str):
    print(f"[wifi] Connecting to '{ssid}' ...")
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    try:
        result = _nmcli(*args, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "Timed out after 30 s"
    if result.returncode == 0:
        print(f"[wifi] Connected to '{ssid}'.")
        return True, ""
    err = result.stderr.strip() or result.stdout.strip()
    print(f"[wifi] Failed: {err}")
    return False, err

# ──────────────────────────────────────────────
# Captive-portal-aware HTTP handler
# ──────────────────────────────────────────────

# URLs that Android / iOS / Windows use to detect captive portals.
# We serve our setup page for all of them so the OS shows the popup.
_CAPTIVE_PROBE_PATHS = {
    "/generate_204",           # Android
    "/gen_204",                # Android fallback
    "/hotspot-detect.html",    # iOS / macOS
    "/library/test/success.html",  # iOS
    "/ncsi.txt",               # Windows
    "/connecttest.txt",        # Windows 10+
    "/redirect",               # Windows
}

_server_state = {"success": False}


class ProvisionHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str, status: int = 200) -> None:
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in _CAPTIVE_PROBE_PATHS:
            # Redirect OS captive portal probes to our setup page
            self._redirect(f"http://{HOTSPOT_IP}/")
        elif path == "/":
            self._html(HTML_PAGE)
        elif path == "/scan":
            self._json(scan_networks())
        else:
            self._redirect(f"http://{HOTSPOT_IP}/")

    def do_POST(self):
        if self.path != "/connect":
            self.send_error(404)
            return

        length   = int(self.headers.get("Content-Length", 0))
        raw      = self.rfile.read(length).decode()
        params   = urllib.parse.parse_qs(raw)
        ssid     = params.get("ssid",     [""])[0].strip()
        password = params.get("password", [""])[0]

        if not ssid:
            self._json({"ok": False, "error": "No SSID provided"})
            return

        print("[http] Stopping hotspot to attempt connection...")
        stop_hotspot()
        time.sleep(2)

        ok, err = connect_to_network(ssid, password)
        if ok:
            _server_state["success"] = True
            self._json({"ok": True})
        else:
            print("[http] Failed — restarting hotspot so user can retry...")
            create_hotspot()
            self._json({"ok": False, "error": err})


# ──────────────────────────────────────────────
# HTML setup page
# ──────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Matrix WiFi Setup</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         display:flex;justify-content:center;align-items:flex-start;min-height:100vh;padding:2rem 1rem}
    .card{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:2rem;width:100%;max-width:420px}
    h1{font-size:1.5rem;margin-bottom:.4rem;color:#fff}
    p.sub{color:#888;font-size:.9rem;margin-bottom:1.5rem}
    label{display:block;font-size:.85rem;color:#aaa;margin-bottom:.3rem}
    select,input[type=password]{width:100%;padding:.65rem .75rem;background:#111;border:1px solid #444;
      border-radius:8px;color:#e0e0e0;font-size:1rem;margin-bottom:1rem}
    button{width:100%;padding:.75rem;border:none;border-radius:8px;font-size:1rem;
           cursor:pointer;margin-bottom:.75rem;transition:opacity .2s}
    button:disabled{opacity:.45;cursor:not-allowed}
    .scan{background:#2a2a2a;color:#ccc;border:1px solid #555}
    .conn{background:#4a90d9;color:#fff}
    #st{margin-top:1rem;padding:.75rem;border-radius:8px;font-size:.9rem;display:none}
    #st.ok{background:#1a3a1a;color:#6fcf6f;border:1px solid #3a7a3a}
    #st.err{background:#3a1a1a;color:#cf6f6f;border:1px solid #7a3a3a}
    #st.inf{background:#1a2a3a;color:#6fafcf;border:1px solid #3a5a7a}
  </style>
</head>
<body>
<div class="card">
  <h1>Matrix WiFi Setup</h1>
  <p class="sub">Connect your Matrix display to your home network.</p>
  <label>Network</label>
  <select id="net"><option value="">— tap Scan to find networks —</option></select>
  <label>Password</label>
  <input type="password" id="pw" placeholder="WiFi password"/>
  <button class="scan" id="scanBtn" onclick="doScan()">Scan for networks</button>
  <button class="conn" id="connBtn" onclick="doConnect()" disabled>Connect</button>
  <div id="st"></div>
</div>
<script>
const sel=document.getElementById('net'),connBtn=document.getElementById('connBtn'),st=document.getElementById('st');
function show(msg,t){st.textContent=msg;st.className=t;st.style.display='block'}
sel.onchange=()=>{connBtn.disabled=sel.value===''};
async function doScan(){
  const btn=document.getElementById('scanBtn');
  btn.disabled=true;btn.textContent='Scanning…';
  show('Scanning for networks…','inf');
  try{
    const nets=await(await fetch('/scan')).json();
    sel.innerHTML='<option value="">— choose a network —</option>';
    if(!nets.length){show('No networks found. Try again.','inf')}
    else{
      nets.forEach(n=>{
        const o=document.createElement('option');
        o.value=n.ssid;
        o.textContent=n.ssid+'  ('+n.signal+'%)'+(n.security?'  🔒':'');
        sel.appendChild(o);
      });
      show('Found '+nets.length+' network(s).','inf');
    }
  }catch(e){show('Scan failed: '+e.message,'err')}
  finally{btn.disabled=false;btn.textContent='Scan for networks'}
}
async function doConnect(){
  const ssid=sel.value,pw=document.getElementById('pw').value;
  if(!ssid){show('Please select a network.','err');return}
  connBtn.disabled=true;
  show('Connecting to "'+ssid+'"… up to 30 seconds.','inf');
  const form=new URLSearchParams();form.append('ssid',ssid);form.append('password',pw);
  try{
    const d=await(await fetch('/connect',{method:'POST',body:form})).json();
    if(d.ok){show('Connected! Your Matrix display is online. You can close this page.','ok')}
    else{show('Failed: '+(d.error||'unknown error'),'err');connBtn.disabled=false}
  }catch(e){show('Error: '+e.message,'err');connBtn.disabled=false}
}
</script>
</body>
</html>
"""


# ──────────────────────────────────────────────
# HTTP server
# ──────────────────────────────────────────────

class _ReuseServer(HTTPServer):
    allow_reuse_address = True

def start_http_server() -> HTTPServer:
    server = _ReuseServer(("0.0.0.0", HTTP_PORT), ProvisionHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[http] Listening on port {HTTP_PORT}")
    return server


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main() -> int:
    force = "--force-setup" in sys.argv

    if not force:
        if is_online():
            print("[wifi] Online — nothing to do.")
            return 0
        print("[wifi] Offline — retrying in 10 s...")
        time.sleep(10)
        if is_online():
            print("[wifi] Online after retry.")
            return 0
        print("[wifi] Still offline — starting provisioning.")
    else:
        print("[wifi] --force-setup: entering hotspot mode.")

    if not wlan0_exists():
        print("[wifi] ERROR: wlan0 not found.")
        return 1

    # Write captive portal DNS config BEFORE creating hotspot so NM picks it up
    _write_captive_conf()

    if not create_hotspot():
        _remove_captive_conf()
        return 1

    time.sleep(1)
    server = start_http_server()

    print(f"[wifi] Ready — phone should show 'Sign in to network' automatically.")
    print(f"[wifi] Or manually connect to '{HOTSPOT_SSID}' and open http://{HOTSPOT_IP}/")

    try:
        while True:
            time.sleep(3)
            if _server_state["success"] or is_online():
                print("[wifi] Online — shutting down provisioning.")
                break
    except KeyboardInterrupt:
        print("\n[wifi] Interrupted.")
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        _nmcli("con", "delete", HOTSPOT_CON_NAME)
        _remove_captive_conf()
        print("[wifi] Done.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
