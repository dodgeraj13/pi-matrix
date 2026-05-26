import numpy as np, requests, math, time, threading, os, sys
from PIL import Image, ImageFont, ImageDraw
from io import BytesIO
from datetime import datetime

# Derive repo root dynamically: apps_v2/ → impl/ → rpi-spotify-matrix-display/ → repo root
_d = os.path.dirname
_REPO_DIR = _d(_d(_d(_d(os.path.realpath(__file__)))))
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)
try:
    from clock_utils import (
        CLOCK_CENTER, CLOCK_RADIUS, NUM_TICKS,
        RING_COLOR, TICK_COLOR, NUMBER_COLOR,
        HOUR_HAND_COLOR, MINUTE_HAND_COLOR, CENTER_COLOR,
        HOUR_HAND_LENGTH, MINUTE_HAND_LENGTH,
        HOUR_HAND_WIDTH, MINUTE_HAND_WIDTH, CENTER_DOT_RADIUS,
        polar_point, get_hand_angles
    )
    USE_CLOCK_UTILS = True
except ImportError:
    USE_CLOCK_UTILS = False
    # Fallback local definition
    def polar_point_local(cx, cy, r, deg):
        a = math.radians(deg)
        return (cx + int(round(r * math.cos(a))), cy + int(round(r * math.sin(a))))

class SpotifyScreen:
    def __init__(self, config, modules, fullscreen):
        self.modules = modules
        self.config = config

        self.font = ImageFont.truetype("fonts/tiny.otf", 5)

        # Load larger font for clock
        try:
            self.clock_font = ImageFont.truetype("fonts/4x6.bdf", 6)
        except:
            self.clock_font = ImageFont.truetype("fonts/tiny.otf", 5)

        self.canvas_width = 64
        self.canvas_height = 64
        self.title_color = (255,255,255)
        self.artist_color = (255,255,255)
        self.play_color = (255, 255, 255)

        self.full_screen_always = fullscreen

        self.current_art_url = ''
        self.current_art_img = None
        self.current_title = ''
        self.current_artist = ''

        # For smooth fade transitions
        self.previous_art_img = None
        self.fade_progress = 1.0  # 1.0 = fully showing new image, 0.0 = showing old image
        self.fade_speed = 0.15  # How much to blend per frame (lower = slower fade)
        self.fade_steps = 0  # Count steps for consistent fade timing
        self.target_fade_steps = 10  # Total fade steps (higher = smoother but slower)

        self.title_animation_cnt = 0
        self.artist_animation_cnt = 0
        self.last_title_reset = math.floor(time.time())
        self.last_artist_reset = math.floor(time.time())
        self.scroll_delay = 4

        self.paused = True
        self.paused_time = math.floor(time.time())
        self.paused_delay = 5

        self.is_playing = False

        self.last_fetch_time = math.floor(time.time())
        self.fetch_interval = 1
        self.spotify_module = self.modules['spotify']

        # Idle fallback state
        self.idle_fallback = ""  # "", "digital_clock", "analog_clock", "last_album", "custom"
        self.idle_delay = 5  # Seconds before switching to fallback
        self.idle_start_time = None  # When idle started
        self.custom_album_art = None
        self.custom_album_url = ""
        self.last_custom_fetch = 0
        self.custom_fetch_interval = 30  # Fetch custom album art every 30 seconds max

        # Backend URL for fetching custom album
        if config is not None and 'Spotify' in config and 'backend_url' in config['Spotify']:
            self.backend_url = config['Spotify']['backend_url']
        else:
            self.backend_url = os.environ.get('BACKEND_URL', 'https://matrix-backend-lv4k.onrender.com')

        self.response = None
        self.thread = threading.Thread(target=self.getCurrentPlaybackAsync)
        self.thread.start()

    def getCurrentPlaybackAsync(self):
        # delay spotify fetches
        time.sleep(3)
        while True:
            self.response = self.spotify_module.getCurrentPlayback()
            time.sleep(1)

    def generateClockFrame(self):
        """Generate a digital clock frame for idle fallback"""
        frame = Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))
        draw = ImageDraw.Draw(frame)

        now = datetime.now()
        time_str = now.strftime("%I:%M")
        date_str = now.strftime("%m/%d")

        # Draw time (large, centered)
        try:
            # Try to use a larger font if available
            time_font = ImageFont.truetype("fonts/6x12.bdf", 12)
        except:
            time_font = self.font

        # Calculate center position for time
        bbox = draw.textbbox((0, 0), time_str, font=time_font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (self.canvas_width - text_width) // 2
        y = (self.canvas_height - text_height) // 2 - 4

        draw.text((x, y), time_str, fill=(255, 255, 255), font=time_font)

        # Draw date (smaller, below time)
        date_bbox = draw.textbbox((0, 0), date_str, font=self.font)
        date_width = date_bbox[2] - date_bbox[0]
        date_x = (self.canvas_width - date_width) // 2
        date_y = y + text_height + 4

        draw.text((date_x, date_y), date_str, fill=(150, 150, 150), font=self.font)

        return frame

    def polar_point(self, cx, cy, r, deg):
        """Convert polar to cartesian coordinates"""
        a = math.radians(deg)
        return (cx + int(round(r * math.cos(a))), cy + int(round(r * math.sin(a))))

    def generateAnalogClockFrame(self):
        """Generate an analog clock frame for idle fallback using shared constants"""
        frame = Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))
        draw = ImageDraw.Draw(frame)

        # Use shared constants if available, otherwise fallback
        cx, cy = CLOCK_CENTER if USE_CLOCK_UTILS else (32, 32)
        radius = CLOCK_RADIUS if USE_CLOCK_UTILS else 30

        # Draw outer ring
        ring_color = RING_COLOR if USE_CLOCK_UTILS else (220, 230, 255)
        tick_color = TICK_COLOR if USE_CLOCK_UTILS else (102, 102, 102)
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=ring_color, width=1)

        # Draw tick marks (12 hourly marks)
        num_ticks = NUM_TICKS if USE_CLOCK_UTILS else 12
        for i in range(num_ticks):
            ang = i * 30
            r1 = radius - 5
            r2 = radius
            x1, y1 = polar_point(cx, cy, r1, ang - 90) if USE_CLOCK_UTILS else self.polar_point(cx, cy, r1, ang - 90)
            x2, y2 = polar_point(cx, cy, r2, ang - 90) if USE_CLOCK_UTILS else self.polar_point(cx, cy, r2, ang - 90)
            draw.line((x1, y1, x2, y2), fill=tick_color)

        # Draw hour numbers (12, 3, 6, 9)
        try:
            num_font = ImageFont.truetype("fonts/4x6.bdf", 6)
        except:
            num_font = self.font

        num_color = NUMBER_COLOR if USE_CLOCK_UTILS else (204, 204, 204)
        num_offset = radius - 12

        # 12 at top
        draw.text((cx - 4, cx - num_offset - 2), "12", fill=num_color, font=num_font)
        # 3 at right
        draw.text((cx + num_offset - 2, cy - 3), "3", fill=num_color, font=num_font)
        # 6 at bottom
        draw.text((cx - 2, cy + num_offset - 6), "6", fill=num_color, font=num_font)
        # 9 at left
        draw.text((cx - num_offset, cy - 3), "9", fill=num_color, font=num_font)

        # Draw hands (no seconds hand)
        now = datetime.now()
        hour = now.hour % 12
        minute = now.minute

        hour_color = HOUR_HAND_COLOR if USE_CLOCK_UTILS else (255, 215, 140)
        minute_color = MINUTE_HAND_COLOR if USE_CLOCK_UTILS else (120, 200, 255)
        center_color = CENTER_COLOR if USE_CLOCK_UTILS else (255, 255, 255)

        # Calculate angles
        if USE_CLOCK_UTILS:
            hour_angle, minute_angle = get_hand_angles(hour, minute)
        else:
            minute_angle = (minute % 60) * 6 - 90
            hour_angle = ((hour % 12) + minute / 60.0) * 30 - 90

        # Hand lengths
        min_len = MINUTE_HAND_LENGTH if USE_CLOCK_UTILS else 26
        hr_len = HOUR_HAND_LENGTH if USE_CLOCK_UTILS else 18

        # Minute hand (longer)
        mx, my = polar_point(cx, cy, min_len, minute_angle) if USE_CLOCK_UTILS else self.polar_point(cx, cy, min_len, minute_angle)
        draw.line((cx, cy, mx, my), fill=minute_color, width=MINUTE_HAND_WIDTH if USE_CLOCK_UTILS else 2)

        # Hour hand (shorter, thicker)
        hx, hy = polar_point(cx, cy, hr_len, hour_angle) if USE_CLOCK_UTILS else self.polar_point(cx, cy, hr_len, hour_angle)
        draw.line((cx, cy, hx, hy), fill=hour_color, width=HOUR_HAND_WIDTH if USE_CLOCK_UTILS else 3)

        # Center dot
        dot_r = CENTER_DOT_RADIUS if USE_CLOCK_UTILS else 2
        draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=center_color)

        return frame

    def fetchCustomAlbumArt(self):
        """Fetch custom album art from backend for idle fallback"""
        # Rate limit
        current_time = time.time()
        if current_time - self.last_custom_fetch < self.custom_fetch_interval:
            return self.custom_album_art

        self.last_custom_fetch = current_time

        try:
            # Fetch custom album from backend
            resp = requests.get(f"{self.backend_url}/now-playing?mode=custom", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                art_url = data.get('album_art')
                if art_url and art_url != self.custom_album_url:
                    self.custom_album_url = art_url
                    # Download the image
                    img_resp = requests.get(art_url, timeout=5)
                    img = Image.open(BytesIO(img_resp.content))
                    self.custom_album_art = img.resize((self.canvas_width, self.canvas_height), Image.LANCZOS)
                    print(f"[spotify_player] Loaded custom album art for idle fallback")
        except Exception as e:
            print(f"[spotify_player] Failed to fetch custom album art: {e}")

        return self.custom_album_art

    def generateIdleFrame(self, fallback_mode):
        """Generate frame for idle state based on fallback mode"""
        if fallback_mode == "digital_clock":
            return self.generateClockFrame()
        elif fallback_mode == "analog_clock":
            return self.generateAnalogClockFrame()
        elif fallback_mode == "last_album":
            # Keep showing the last album art
            if self.current_art_img:
                frame = Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))
                frame.paste(self.current_art_img, (0, 0))
                return frame
            return Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))
        elif fallback_mode == "custom":
            art = self.fetchCustomAlbumArt()
            if art:
                frame = Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))
                frame.paste(art, (0, 0))
                return frame
        # Default: black screen
        return Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))

    def generate(self):
        if not self.spotify_module.queue.empty():
            self.response = self.spotify_module.queue.get()
            self.spotify_module.queue.queue.clear()
        return self.generateFrame(self.response)

    def generateFrame(self, response):
        # Handle idle fallback response
        if response is not None and isinstance(response, tuple) and len(response) >= 2 and response[0] == 'idle':
            fallback_mode = response[1]
            idle_delay = response[2] if len(response) > 2 else 5

            # Persist latest fallback config so rate-limited frames can use it
            self.idle_fallback = fallback_mode
            self.idle_delay = idle_delay

            self.is_playing = False

            # Track idle start time
            if self.idle_start_time is None:
                self.idle_start_time = time.time()

            # Check if we should show fallback yet
            idle_elapsed = time.time() - self.idle_start_time
            if idle_elapsed >= idle_delay:
                # Past grace period — show fallback at idle brightness
                idle_frame = self.generateIdleFrame(fallback_mode)
                return (idle_frame, False, True)   # is_idle_active=True → idle brightness
            else:
                # Grace period — still show last album art at normal brightness
                frame = Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))
                if self.current_art_img:
                    frame.paste(self.current_art_img, (0, 0))
                return (frame, False, False)        # is_idle_active=False → normal brightness

        # Rate-limit sentinel (spotify_module returns True when skipping API call).
        # Do NOT reset idle_start_time — preserve the grace period so it can expire.
        if response is True:
            frame = Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))
            if self.current_art_img:
                frame.paste(self.current_art_img, (0, 0))
            if self.idle_start_time is not None:
                idle_elapsed = time.time() - self.idle_start_time
                if idle_elapsed >= self.idle_delay:
                    idle_frame = self.generateIdleFrame(self.idle_fallback)
                    return (idle_frame, False, True)   # grace period expired → idle brightness
                return (frame, False, False)           # still in grace period → normal brightness
            return (frame, self.is_playing, False)

        # Reset idle timer when we get a real playing response
        self.idle_start_time = None

        # determine state
        if response is not None:
            artist, title, art_url, self.is_playing, progress_ms, duration_ms = response

            # load/update album art if URL changed
            if art_url != self.current_art_url:
                # Start fade transition
                if self.current_art_img is not None:
                    self.previous_art_img = self.current_art_img
                    self.fade_progress = 0.0
                    self.fade_steps = 0

                self.current_art_url = art_url
                try:
                    resp = requests.get(art_url, timeout=5)
                    img = Image.open(BytesIO(resp.content))
                    self.current_art_img = img.resize((self.canvas_width, self.canvas_height), Image.LANCZOS)
                except Exception as e:
                    print(f"[spotify_player] Failed to load album art: {e}")
                    # Keep previous image if load fails
                    if self.previous_art_img:
                        self.current_art_img = self.previous_art_img
                        self.fade_progress = 1.0

            # Update fade progress
            if self.fade_progress < 1.0 and self.previous_art_img is not None:
                self.fade_steps += 1
                self.fade_progress = min(1.0, self.fade_steps / self.target_fade_steps)
                if self.fade_progress >= 1.0:
                    # Fade complete, clear previous
                    self.previous_art_img = None

            # start with black bg
            frame = Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))

            # Draw with fade transition
            if self.fade_progress < 1.0 and self.previous_art_img is not None and self.current_art_img is not None:
                # Blend old and new images
                # Create blended image using alpha composite
                old_alpha = int(255 * (1 - self.fade_progress))
                new_alpha = int(255 * self.fade_progress)

                # Paste old image with reduced opacity
                frame.paste(self.previous_art_img, (0, 0))

                # Blend new image on top
                blend_frame = Image.new("RGBA", (self.canvas_width, self.canvas_height), (0, 0, 0, 0))
                blend_frame.paste(self.current_art_img, (0, 0))
                blend_frame.putalpha(new_alpha)
                frame = Image.alpha_composite(frame.convert("RGBA"), blend_frame).convert("RGB")
            elif self.current_art_img:
                frame.paste(self.current_art_img, (0, 0))

            # if playing, draw progress bar
            if self.is_playing and duration_ms:
                draw = ImageDraw.Draw(frame)
                bar_y = self.canvas_height - 1
                play_w = int((progress_ms / duration_ms) * self.canvas_width)
                # background bar
                draw.line((0, bar_y, self.canvas_width, bar_y), fill=(50,50,50))
                # progress
                draw.line((0, bar_y, play_w, bar_y), fill=self.play_color)

            return (frame, self.is_playing, False)

        # no response at all: still show last art (or blank)
        frame = Image.new("RGB", (self.canvas_width, self.canvas_height), (0, 0, 0))
        if self.current_art_img:
            frame.paste(self.current_art_img, (0, 0))
        return (frame, False, False)


def drawPlayPause(draw, is_playing, color):
    x = 10
    y = -16
    if not is_playing:
        draw.line((x+45,y+19,x+45,y+25), fill = color)
        draw.line((x+46,y+20,x+46,y+24), fill = color)
        draw.line((x+47,y+20,x+47,y+24), fill = color)
        draw.line((x+48,y+21,x+48,y+23), fill = color)
        draw.line((x+49,y+21,x+49,y+23), fill = color)
        draw.line((x+50,y+22,x+50,y+22), fill = color)
    else:
        draw.line((x+45,y+19,x+45,y+25), fill = color)
        draw.line((x+46,y+19,x+46,y+25), fill = color)
        draw.line((x+49,y+19,x+49,y+25), fill = color)
        draw.line((x+50,y+19,x+50,y+25), fill = color)
