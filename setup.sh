#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  Matrix Pi Setup Script
#  Installs everything needed for a new Pi + 64×64 LED matrix combo.
#  Run as your normal Pi user (NOT root).
#  Usage: bash setup.sh
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
section() { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Sanity checks ────────────────────────────────────────────────
[[ $EUID -eq 0 ]] && die "Do not run as root. Run as your normal Pi user."
command -v git   >/dev/null 2>&1 || die "git not found. Run: sudo apt-get install -y git"
command -v curl  >/dev/null 2>&1 || die "curl not found. Run: sudo apt-get install -y curl"
command -v python3 >/dev/null 2>&1 || die "python3 not found."

WHOAMI=$(whoami)
HOME_DIR="/home/$WHOAMI"
REPO_URL="https://github.com/dodgeraj13/pi_two.git"
REPO_DIR="$HOME_DIR/matrix-agent"

echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║      Matrix Pi — New Device Setup        ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Running as user : $WHOAMI"
echo "  Home directory  : $HOME_DIR"
echo "  Repo target dir : $REPO_DIR"
echo ""

# ── Gather config ─────────────────────────────────────────────────
section "Configuration"

read -rp "  Backend URL (e.g. https://matrix-backend-xxx.onrender.com): " BACKEND_URL
BACKEND_URL="${BACKEND_URL%/}"
[[ -z "$BACKEND_URL" ]] && die "Backend URL is required."

read -rp "  Admin API token (MY_SUPER_TOKEN_123 or custom): " ADMIN_TOKEN
[[ -z "$ADMIN_TOKEN" ]] && die "Admin token is required."

read -rp "  Device name for this Pi (e.g. living-room): " DEVICE_NAME
DEVICE_NAME="${DEVICE_NAME:-pi-$(hostname)}"

read -rp "  Display rotation (0 / 90 / 180 / 270) [90]: " ROTATION
ROTATION="${ROTATION:-90}"

read -rp "  OpenWeatherMap API key (leave blank to skip weather): " OWM_KEY
OWM_LOC=""
OWM_UNITS="imperial"
if [[ -n "$OWM_KEY" ]]; then
    read -rp "  Weather location (e.g. Chicago,il,us): " OWM_LOC
    read -rp "  Units (imperial / metric) [imperial]: " OWM_UNITS
    OWM_UNITS="${OWM_UNITS:-imperial}"
fi

echo ""

# ── Create device token via backend API ──────────────────────────
section "Creating device on backend"

HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"$DEVICE_NAME\"}" \
    "$BACKEND_URL/devices" 2>/dev/null) || die "Could not reach backend at $BACKEND_URL"

HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)
HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -1)

if [[ "$HTTP_CODE" != "200" && "$HTTP_CODE" != "201" ]]; then
    die "Backend returned $HTTP_CODE: $HTTP_BODY"
fi

DEVICE_TOKEN=$(echo "$HTTP_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token') or d.get('device_token'))" 2>/dev/null) \
    || die "Could not parse token from response: $HTTP_BODY"
[[ -z "$DEVICE_TOKEN" || "$DEVICE_TOKEN" == "None" ]] && die "Could not parse token from response: $HTTP_BODY"

ok "Device token: $DEVICE_TOKEN"

# ── System update + packages ──────────────────────────────────────
section "System update & packages"

sudo apt-get update -y
sudo apt-get upgrade -y
sudo apt-get install -y \
    git python3-dev python3-pip python3-venv python3-pillow \
    make gcc g++ cython3 cmake \
    libxml2-dev libxslt-dev libopenjp2-7 \
    libsdl2-dev libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 \
    libatlas-base-dev libfreetype6-dev \
    fonts-freefont-ttf
ok "System packages installed"

# ── Clone or update the repo ──────────────────────────────────────
section "Cloning repository"

if [[ -d "$REPO_DIR/.git" ]]; then
    info "Repo already exists — pulling latest..."
    git -C "$REPO_DIR" fetch origin
    git -C "$REPO_DIR" reset --hard origin/main
    ok "Repo updated"
else
    git clone "$REPO_URL" "$REPO_DIR"
    ok "Repo cloned to $REPO_DIR"
fi

# rpi-rgb-led-matrix is cloned directly in the "Build bindings" section below

# ── Handle /home/pi_two symlink for non-pi_two users ─────────────
if [[ "$WHOAMI" != "pi_two" ]]; then
    warn "Username '$WHOAMI' ≠ 'pi_two'. Creating /home/pi_two symlink."
    sudo ln -sfn "$HOME_DIR" /home/pi_two
    ok "/home/pi_two → $HOME_DIR"
fi

# ── Create app symlinks in home dir ──────────────────────────────
section "Creating app directory symlinks"

for APP in rpi-spotify-matrix-display mlb-led-scoreboard \
           matrix-clock matrix-weather matrix-picture \
           matrix-drawing matrix-text; do
    SRC="$REPO_DIR/$APP"
    DEST="$HOME_DIR/$APP"
    if [[ -d "$SRC" ]]; then
        ln -sfn "$SRC" "$DEST"
        ok "$DEST → $SRC"
    else
        warn "$SRC not found, skipping symlink"
    fi
done

# ── Build rpi-rgb-led-matrix (new pip/cmake build system) ────────
section "Building rpi-rgb-led-matrix LED driver"

# The hzeller/rpi-rgb-led-matrix repo now uses scikit-build-core + cmake
# (the old 'make build-python' target no longer exists on master).
# Strategy: build a wheel once, then install it into every venv that needs it.

MATRIX_SRC="$HOME_DIR/rpi-rgb-led-matrix"
# Pin to last commit before Pi 5 RP1 backend was added (commit e947417f, 2026-04-13).
# Master as of 2026-04-30 includes rp1_rio_backend.cc which uses 64-bit ARM instructions
# that fail to compile on 32-bit armhf (Pi 3/4).  This SHA works on all Pi models.
MATRIX_SHA="e947417f"
if [[ -d "$MATRIX_SRC/.git" ]]; then
    info "rpi-rgb-led-matrix already cloned — fetching and pinning to $MATRIX_SHA..."
    git -C "$MATRIX_SRC" fetch origin
    git -C "$MATRIX_SRC" checkout "$MATRIX_SHA"
else
    git clone https://github.com/hzeller/rpi-rgb-led-matrix.git "$MATRIX_SRC"
    git -C "$MATRIX_SRC" checkout "$MATRIX_SHA"
fi

info "Building rgbmatrix wheel (takes a few minutes — cmake + Cython compile)..."
WHEEL_DIR="/tmp/rgbmatrix-wheel"
rm -rf "$WHEEL_DIR"
pip3 wheel "$MATRIX_SRC" --no-deps -w "$WHEEL_DIR" 2>&1 | sed 's/^/  [wheel] /'
RGBMATRIX_WHL=$(ls "$WHEEL_DIR"/rgbmatrix*.whl 2>/dev/null | head -1)
[[ -z "$RGBMATRIX_WHL" ]] && die "Failed to build rgbmatrix wheel — check errors above"

# Install system-wide: MLB's main.py runs as sudo python3 (not in a venv)
sudo pip3 install "$RGBMATRIX_WHL" --break-system-packages --force-reinstall --quiet
ok "rgbmatrix installed system-wide"

# Symlink the full matrix repo so display scripts can find fonts at:
#   rpi-spotify-matrix-display/rpi-rgb-led-matrix/fonts
SPOTIFY_MATRIX="$REPO_DIR/rpi-spotify-matrix-display/rpi-rgb-led-matrix"
ln -sfn "$MATRIX_SRC" "$SPOTIFY_MATRIX"
ok "rpi-rgb-led-matrix repo linked: $SPOTIFY_MATRIX → $MATRIX_SRC"

mkdir -p "$REPO_DIR/mlb-led-scoreboard/submodules"
ln -sfn "$MATRIX_SRC" "$REPO_DIR/mlb-led-scoreboard/submodules/matrix"
ok "rpi-rgb-led-matrix repo linked for MLB"

# ── Agent venv (clock, weather, picture, drawing, text) ──────────
section "Agent Python venv"

cd "$REPO_DIR/matrix-agent"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install --quiet requests websockets python-dotenv pillow
.venv/bin/pip install --quiet "$RGBMATRIX_WHL"
ok "Agent venv ready at $REPO_DIR/matrix-agent/.venv"

# ── MLB Scoreboard setup ──────────────────────────────────────────
section "MLB scoreboard setup"

mkdir -p "$REPO_DIR/mlb-led-scoreboard/logs"   # install.sh opens logs/mlbled.log at startup
cd "$REPO_DIR/mlb-led-scoreboard"
info "Running MLB install.sh (skipping matrix — already installed above)..."
# --skip-python: apt packages installed above
# --skip-matrix: rgbmatrix already installed via pip above
# --skip-config: non-interactive
echo "n" | bash install.sh --skip-python --skip-matrix --skip-config 2>&1 | sed 's/^/  [mlb] /'

# install.sh creates mlb-led-scoreboard/venv — put rgbmatrix in there too
# (the shebang on main.py points at this venv's python)
MLB_VENV="$REPO_DIR/mlb-led-scoreboard/venv"
if [[ -f "$MLB_VENV/bin/pip" ]]; then
    "$MLB_VENV/bin/pip" install --quiet "$RGBMATRIX_WHL"
    ok "rgbmatrix installed into MLB venv"
else
    warn "MLB venv not found at $MLB_VENV — MLB may fail to import rgbmatrix"
fi
ok "MLB scoreboard ready"

# Blacklist snd_bcm2835 (conflicts with hardware PWM)
if [[ ! -f /etc/modprobe.d/blacklist-rgbmatrix.conf ]]; then
    info "Blacklisting snd_bcm2835 kernel module..."
    echo "blacklist snd_bcm2835" | sudo tee /etc/modprobe.d/blacklist-rgbmatrix.conf >/dev/null
    ok "snd_bcm2835 blacklisted"
fi

# isolcpus=3 for smoother display
CMDLINE_FILE=""
[[ -f /boot/firmware/cmdline.txt ]] && CMDLINE_FILE="/boot/firmware/cmdline.txt"
[[ -f /boot/cmdline.txt ]]         && CMDLINE_FILE="/boot/cmdline.txt"
if [[ -n "$CMDLINE_FILE" ]] && ! grep -q "isolcpus=3" "$CMDLINE_FILE"; then
    sudo sed -i '$ s/$/ isolcpus=3/' "$CMDLINE_FILE"
    ok "Added isolcpus=3 to $CMDLINE_FILE"
fi

# ── Spotify display venv ──────────────────────────────────────────
section "Spotify display venv"

cd "$REPO_DIR/rpi-spotify-matrix-display"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/pip install --quiet "$RGBMATRIX_WHL"
ok "Spotify display venv ready"

# ── Write config files ────────────────────────────────────────────
section "Writing configuration files"

# .env for the agent
WS_URL="${BACKEND_URL/https:/wss:}/ws"
cat > "$REPO_DIR/matrix-agent/.env" <<EOF
# Generated by setup.sh — edit as needed

BACKEND_BASE=$BACKEND_URL
WS_URL=$WS_URL

DEVICE_TOKEN=$DEVICE_TOKEN

ROTATION=$ROTATION

HOME_DIR=$HOME_DIR
MLB_DIR=$HOME_DIR/mlb-led-scoreboard
MUSIC_DIR=$HOME_DIR/rpi-spotify-matrix-display
CLOCK_DIR=$HOME_DIR/matrix-clock
WEATHER_DIR=$HOME_DIR/matrix-weather
PICTURE_DIR=$HOME_DIR/matrix-picture
DRAWING_DIR=$HOME_DIR/matrix-drawing
TEXT_DIR=$HOME_DIR/matrix-text
EOF
ok ".env written"

# Spotify config.ini
cat > "$REPO_DIR/rpi-spotify-matrix-display/config.ini" <<EOF
[Spotify]
use_backend = true
device_token = $DEVICE_TOKEN
backend_url = $BACKEND_URL

[Matrix]
hardware_mapping = adafruit-hat-pwm
brightness = 60
gpio_slowdown = 2
limit_refresh_rate_hz = 0
shutdown_delay = 999999999
pixel_mapper_config = Rotate:$ROTATION
EOF
ok "Spotify config.ini written"

# MLB config.json (copy example if not present)
if [[ ! -f "$REPO_DIR/mlb-led-scoreboard/config.json" ]]; then
    cp "$REPO_DIR/mlb-led-scoreboard/config.json.example" \
       "$REPO_DIR/mlb-led-scoreboard/config.json"
    chmod 666 "$REPO_DIR/mlb-led-scoreboard/config.json"
    ok "MLB config.json created from example"
fi

# Weather config
if [[ -n "$OWM_KEY" ]]; then
    cat > "$REPO_DIR/matrix-weather/weather.ini" <<EOF
[Weather]
api_key = $OWM_KEY
location = $OWM_LOC
units = $OWM_UNITS
provider = openweathermap
EOF
    ok "Weather config written"
fi

# ── sudoers ───────────────────────────────────────────────────────
section "Configuring sudoers (NOPASSWD for display scripts)"

SUDOERS_FILE="/etc/sudoers.d/matrix-$WHOAMI"
echo "$WHOAMI ALL=(ALL) NOPASSWD: ALL" | sudo tee "$SUDOERS_FILE" >/dev/null
sudo chmod 440 "$SUDOERS_FILE"
sudo visudo -c -f "$SUDOERS_FILE" >/dev/null && ok "sudoers entry valid" || die "sudoers syntax error!"

# ── systemd service ───────────────────────────────────────────────
section "Installing systemd service"

sudo tee /etc/systemd/system/matrix-agent.service >/dev/null <<EOF
[Unit]
Description=Matrix Agent (Backend <-> Pi LED Display)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$WHOAMI
WorkingDirectory=$REPO_DIR/matrix-agent
ExecStart=$REPO_DIR/matrix-agent/.venv/bin/python agent.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
EnvironmentFile=$REPO_DIR/matrix-agent/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable matrix-agent
sudo systemctl start matrix-agent
ok "matrix-agent service installed and started"

# ── Done ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║           Setup complete! 🎉             ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Device token   : $DEVICE_TOKEN"
echo "  Frontend URL   : $BACKEND_URL  →  /d/$DEVICE_TOKEN"
echo "  Agent service  : sudo systemctl status matrix-agent"
echo "  Agent logs     : journalctl -u matrix-agent -f"
echo ""
echo -e "  ${YELLOW}Next steps:${NC}"
echo "  1. Visit the frontend and enter your device token"
echo "  2. Authorize Spotify via the frontend (for music mode)"
echo "  3. Set your preferred MLB teams in the MLB Config editor"
if [[ -z "$OWM_KEY" ]]; then
echo "  4. Add an OpenWeatherMap API key to matrix-weather/weather.ini"
echo "     for weather mode"
fi
echo ""
echo -e "  ${YELLOW}A reboot is recommended:${NC}  sudo reboot"
echo ""
