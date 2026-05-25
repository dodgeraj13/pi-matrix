import os
from PIL import Image

_HOME = os.environ.get("HOME", "/home/pi_two")
ICON_DIR = f"{_HOME}/mlb-led-scoreboard/assets/weather"

# Exact OWM code -> filename mapping (all files exist in your folder)
EXACT_CODE_MAP = {
    "01d": "01d.png",  # clear day
    "01n": "01n.png",  # clear night
    "02d": "02d.png",  # few clouds day
    "02n": "02n.png",  # few clouds night
    "03d": "03d.png",  # scattered clouds day
    "03n": "03n.png",
    "04d": "04d.png",  # broken/overcast day
    "04n": "04n.png",
    "09d": "09d.png",  # shower rain day
    "09n": "09n.png",
    "10d": "10d.png",  # rain day
    "10n": "10n.png",
    "11d": "11d.png",  # thunderstorm day
    "11n": "11n.png",
    "13d": "13d.png",  # snow day
    "13n": "13n.png",
    "50d": "50d.png",  # mist/haze day
    "50n": "50n.png",
}

def _list_icons():
    if not os.path.isdir(ICON_DIR):
        return set()
    return set(f for f in os.listdir(ICON_DIR) if f.lower().endswith((".png",".bmp",".jpg",".jpeg")))

def _pick_filename(icon_code: str) -> str|None:
    if not icon_code:
        return None
    code = icon_code.lower()
    fn = EXACT_CODE_MAP.get(code)
    if not fn:
        return None
    files = _list_icons()
    if fn in files:
        return os.path.join(ICON_DIR, fn)
    return None

def _paste_rgba(canvas, img, x, y):
    """Paste RGBA image to rgbmatrix canvas (alpha-aware, no blur)."""
    px = img.load()
    w,h = img.size
    for yy in range(h):
        for xx in range(w):
            r,g,b,a = px[xx,yy]
            if a:
                canvas.SetPixel(x+xx, y+yy, r,g,b)

def draw_icon_if_available(canvas, x:int, y:int, icon_code:str, _condition_unused:str="", box=(16,16)) -> bool:
    """
    Draws the exact OWM icon PNG if present. Returns True if drawn.
    - Scales down to 'box' with NEAREST (crisp)
    - Does NOT scale up (keeps icons clean)
    """
    path = _pick_filename(icon_code)
    if not path:
        return False
    try:
        im = Image.open(path).convert("RGBA")
        w,h = im.size
        tgt_w, tgt_h = box
        # only scale DOWN if needed
        if w > tgt_w or h > tgt_h:
            scale = min(tgt_w/float(w), tgt_h/float(h))
            nw, nh = max(1,int(w*scale)), max(1,int(h*scale))
            im = im.resize((nw,nh), resample=Image.NEAREST)
        _paste_rgba(canvas, im, x, y)
        return True
    except Exception:
        return False
