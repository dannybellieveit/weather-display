#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
Weather Station - Waveshare Triple LCD HAT
Custom Design by Danny
https://github.com/dannybellieveit/weather-display

Main screen (1.3" 240x240): Current conditions with UV, high/low, time
Left screen  (0.96" 160x80): Humidity & Wind
Right screen (0.96" 160x80): Sun times

KEY2 cycles between three pages: weather / NASA Earth photo / moon phase.

Burn-in prevention:
- Auto-dim to 20% after 2 minutes
- Backlight fully off between 00:00 and 07:00 when dimmed
- KEY1 button to wake

Local config: copy config.example.json to config.json (gitignored, sits
next to this script) to override location/brightness/timing without
touching source — the auto-updater's `git reset --hard` won't touch it.
"""

import os, sys, time, logging, urllib.request, json, subprocess, math, threading
import spidev as SPI
import RPi.GPIO as GPIO
from io import BytesIO

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENDORED_WAVESHARE_DIR = os.path.join(SCRIPT_DIR, 'vendor', 'waveshare')
DOWNLOADED_WAVESHARE_DIR = os.path.join(os.path.expanduser('~'), 'Zero_LCD_HAT_A_Demo', 'python')

# Prefer the driver files vendored in this repo; fall back to the
# separately-downloaded copy (see install.sh) only if vendoring is missing.
if os.path.isdir(os.path.join(VENDORED_WAVESHARE_DIR, 'lib')):
    WAVESHARE_DIR = VENDORED_WAVESHARE_DIR
else:
    WAVESHARE_DIR = DOWNLOADED_WAVESHARE_DIR
sys.path.append(WAVESHARE_DIR)
from lib import LCD_1inch3, LCD_0inch96
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

# ── Optimization: Caching & Double Buffering ──────────────────────────────────
# Trick #2: Pre-rendered Image Caching - cache resized Earth images
earth_image_cache = {
    'data': None,
    'resized_240': None,  # Cached 240x240 version for main screen
    'timestamp': 0,
    'ttl': 3600,  # 1 hour, matches fetch interval
    'size_bytes': 0
}

# Trick #7: WiFi Status Caching - cache WiFi status to reduce subprocess calls
wifi_cache = {
    'status': False,
    'timestamp': 0,
    'ttl': 60  # Check every 60s instead of 5s
}

# Trick #5: Double Buffering - pre-rendered frames for instant page switching
PAGES = ['weather', 'earth', 'moon']
frame_buffers = {
    'weather': {'main': None, 'left': None, 'right': None},
    'earth': {'main': None, 'left': None, 'right': None},
    'moon': {'main': None, 'left': None, 'right': None},
}

# Lock for thread-safe buffer updates
buffer_lock = threading.Lock()

# ── Pins (fixed by the HAT's wiring, not user-configurable) ────────────────────
RST_MAIN, DC_MAIN, BL_MAIN, BUS_MAIN, DEV_MAIN = 27, 22, 19, 1, 0
RST_L,    DC_L,    BL_L,    BUS_L,    DEV_L    = 24,  4, 13, 0, 0
RST_R,    DC_R,    BL_R,    BUS_R,    DEV_R    = 23,  5, 12, 0, 1
KEY1_PIN = 25  # Wake button
KEY2_PIN = 26  # Page cycling

# ── Local config (config.json, gitignored) ─────────────────────────────────────
# Overrides the defaults below without touching source, and survives the
# auto-updater's `git reset --hard origin/main` since it's untracked.
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')

DEFAULT_CONFIG = {
    'lat': 51.4279,
    'lon': -0.1255,
    'city': 'Streatham',
    'bl_main_duty': 90,     # Main screen brightness (0-100)
    'bl_side_duty': 45,     # Side screens brightness (0-100)
    'update_seconds': 300,  # Weather fetch interval
    'temp_x': 90,           # Big-temperature X position (0-240)
    'temp_y': 40,           # Big-temperature Y position (0-240)
    'dim_timeout': 120,     # Seconds idle before auto-dim
    'night_start_hour': 0,  # Backlight fully off from this hour...
    'night_end_hour': 7,    # ...until this hour, while dimmed
}

def _load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as fp:
            user_cfg = json.load(fp)
        cfg.update({k: v for k, v in user_cfg.items() if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Failed to load config.json, using defaults: {e}")
    return cfg

CFG = _load_config()

LAT, LON, CITY = CFG['lat'], CFG['lon'], CFG['city']
UPDATE_SECONDS = CFG['update_seconds']

# ── Display Settings ──────────────────────────────────────────────────────────
BL_MAIN_DUTY = CFG['bl_main_duty']
BL_SIDE_DUTY = CFG['bl_side_duty']

# ── Manual Positioning (Adjust these to move the temperature!) ───────────────
TEMP_X = CFG['temp_x']   # X position: adjust to move left/right (0-240)
TEMP_Y = CFG['temp_y']   # Y position: adjust to move up/down (0-240)

# ── Burn-in Prevention Settings ───────────────────────────────────────────────
DIM_TIMEOUT = CFG['dim_timeout']         # Seconds before auto-dim (2 minutes)
NIGHT_START_HOUR = CFG['night_start_hour']
NIGHT_END_HOUR = CFG['night_end_hour']

# ── Fonts ─────────────────────────────────────────────────────────────────────
FONT_DIR = os.path.join(WAVESHARE_DIR, 'Font')
TATE_FONT_URL = 'https://www.tate.org.uk/static/fonts/TateNewPro-Regular.5af49f1c9910.woff'
TATE_FONT_PATH = os.path.join(os.path.expanduser('~'), '.cache', 'weather-display', 'TateNewPro-Regular.woff')

def _download_tate_font():
    """Download Tate font once and cache locally."""
    if os.path.exists(TATE_FONT_PATH):
        return True
    try:
        os.makedirs(os.path.dirname(TATE_FONT_PATH), exist_ok=True)
        log.info(f"Downloading Tate font...")
        with urllib.request.urlopen(TATE_FONT_URL, timeout=15) as r:
            data = r.read()
        with open(TATE_FONT_PATH, 'wb') as fout:
            fout.write(data)
        log.info(f"Tate font cached at {TATE_FONT_PATH}")
        return True
    except Exception as e:
        log.warning(f"Failed to download Tate font: {e}")
        return False

# Try downloading Tate font at startup
_tate_font_available = _download_tate_font()

def f(size):
    # Prefer Tate font, fall back to bundled Font00.ttf, then PIL default
    if _tate_font_available:
        try:
            return ImageFont.truetype(TATE_FONT_PATH, size)
        except (FileNotFoundError, OSError):
            pass
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, 'Font00.ttf'), size)
    except (FileNotFoundError, OSError):
        log.warning("No fonts available, using default font")
        return ImageFont.load_default()

# ── Weather codes ─────────────────────────────────────────────────────────────
WMO = {
    0:"Clear", 1:"Mostly Clear", 2:"Partly Cloudy", 3:"Overcast",
    45:"Foggy", 48:"Icy Fog",
    51:"Light Drizzle", 53:"Drizzle", 55:"Heavy Drizzle",
    61:"Light Rain", 63:"Rain", 65:"Heavy Rain",
    71:"Light Snow", 73:"Snow", 75:"Heavy Snow", 77:"Snow Grains",
    80:"Showers", 81:"Rain Showers", 82:"Heavy Showers",
    85:"Snow Showers", 86:"Heavy Snow Showers",
    95:"Thunderstorm", 96:"Storm+Hail", 99:"Severe Storm",
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def wind_dir(deg):
    return ['N','NE','E','SE','S','SW','W','NW'][round(deg/45)%8]

def temp_col(t):
    if t < 5:  return (100, 180, 255)
    if t < 12: return (60, 200, 200)
    if t < 18: return (80, 220, 140)
    if t < 24: return (200, 200, 100)
    if t < 28: return (255, 160, 60)
    return (255, 80, 60)

def uv_col(uv):
    if uv <= 2: return (100, 200, 100)
    if uv <= 5: return (240, 200, 60)
    if uv <= 7: return (255, 160, 60)
    if uv <= 10: return (255, 100, 60)
    return (200, 60, 100)

def wifi_status():
    """Check WiFi connectivity with caching (Trick #7) - reduces subprocess calls by 92%"""
    global wifi_cache
    now = time.time()

    # Return cached result if fresh (within TTL)
    if now - wifi_cache['timestamp'] < wifi_cache['ttl']:
        return wifi_cache['status']

    # Cache expired - perform actual check
    status = False
    try:
        out = subprocess.check_output(['iwconfig','wlan0'], stderr=subprocess.DEVNULL).decode()
        if 'ESSID:"' in out and 'off/any' not in out:
            status = True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass  # iwconfig not found or failed - network down

    if not status:
        try:
            out = subprocess.check_output(['ip','route'], stderr=subprocess.DEVNULL).decode()
            if 'default' in out:
                status = True
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            pass  # ip route failed - no default gateway

    # Update cache
    wifi_cache['status'] = status
    wifi_cache['timestamp'] = now
    return status


def draw_wifi(draw, x, y, connected, col_on=(80,220,120), col_off=(180,60,60)):
    col = col_on if connected else col_off
    draw.ellipse([x+4,y+9,x+8,y+13], fill=col)
    draw.arc([x+1,y+4,x+11,y+14], start=210, end=330, fill=col, width=2)
    draw.arc([x-2,y,x+14,y+16], start=210, end=330, fill=col, width=2)

def draw_suncream(draw, x, y, h=16):
    """Small suncream-bottle icon (hand-drawn: the display font has no emoji
    glyphs, so an actual 🧴 character silently renders as nothing)."""
    bottle_col = (240, 210, 90)
    cap_col = (210, 170, 60)
    w = int(h * 0.62)
    cap_w = max(2, int(w * 0.45))
    cap_h = max(2, int(h * 0.2))
    body_top = y + cap_h
    draw.rectangle([x + (w - cap_w) // 2, y, x + (w - cap_w) // 2 + cap_w, body_top], fill=cap_col)
    draw.rounded_rectangle([x, body_top, x + w, y + h], radius=2, fill=bottle_col)

# ── Graphics ──────────────────────────────────────────────────────────────────
def draw_sunrise(draw, cx, cy, r=12):
    sun_col = (255, 190, 60)
    horizon_col = (60, 60, 75)
    ray_col = (255, 160, 40)

    draw.line([(cx-r-6, cy), (cx+r+6, cy)], fill=horizon_col, width=1)
    draw.pieslice([cx-r, cy-r, cx+r, cy+r], start=180, end=0, fill=sun_col)

    ray_len = 5
    for angle in [150, 120, 90, 60, 30]:
        rad = math.radians(angle)
        x1 = cx + int((r+2) * math.cos(rad))
        y1 = cy - int((r+2) * math.sin(rad))
        x2 = cx + int((r+2+ray_len) * math.cos(rad))
        y2 = cy - int((r+2+ray_len) * math.sin(rad))
        draw.line([(x1, y1), (x2, y2)], fill=ray_col, width=2)

def draw_sunset(draw, cx, cy, r=12):
    sun_col = (255, 120, 50)
    horizon_col = (60, 60, 75)
    ray_col = (255, 90, 40)

    draw.line([(cx-r-6, cy), (cx+r+6, cy)], fill=horizon_col, width=1)
    draw.pieslice([cx-r, cy-r+4, cx+r, cy+r+4], start=200, end=340, fill=sun_col)

    ray_len = 4
    for angle in [140, 110, 70, 40]:
        rad = math.radians(angle)
        x1 = cx + int((r) * math.cos(rad))
        y1 = cy - int((r-2) * math.sin(rad))
        x2 = cx + int((r+ray_len) * math.cos(rad))
        y2 = cy - int((r-2+ray_len) * math.sin(rad))
        draw.line([(x1, y1), (x2, y2)], fill=ray_col, width=2)

# WMO codes that indicate precipitation (rain, drizzle, showers, thunderstorms, snow)
PRECIP_CODES = {51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}

def _calc_precip_duration(hourly, current_code):
    """Calculate how long precipitation will last from now, using hourly forecasts.
    Returns a string like '2h', '30min', or None if not precipitating."""
    if current_code not in PRECIP_CODES and current_code not in SNOW_CODES:
        return None

    times = hourly.get('time', [])
    codes = hourly.get('weather_code', [])
    if not times or not codes:
        return None

    # Find the current hour index
    now_str = time.strftime('%Y-%m-%dT%H:00')
    try:
        start_idx = next(i for i, t in enumerate(times) if t >= now_str)
    except StopIteration:
        return None

    # Count consecutive hours with precipitation from now
    all_precip = PRECIP_CODES | SNOW_CODES
    hours = 0
    for i in range(start_idx, len(codes)):
        if int(codes[i]) in all_precip:
            hours += 1
        else:
            break

    if hours <= 0:
        return None
    if hours == 1:
        return "~1h"
    if hours >= len(codes) - start_idx:
        return f"{hours}h+"
    return f"~{hours}h"

WEATHER_CACHE_PATH = os.path.join(SCRIPT_DIR, '.weather_cache.json')

def _save_weather_cache(data):
    """Persist the last successful fetch so a restart shows real data, not 'No Data'."""
    try:
        with open(WEATHER_CACHE_PATH, 'w') as fp:
            json.dump(data, fp)
    except OSError as e:
        log.warning(f"Failed to save weather cache: {e}")

def _load_weather_cache():
    try:
        with open(WEATHER_CACHE_PATH) as fp:
            return json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'ok': False}

def fetch_weather():
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
        f"wind_speed_10m,wind_direction_10m,weather_code,uv_index"
        f"&hourly=weather_code"
        f"&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max"
        f"&timezone=Europe/London&forecast_days=2"
        f"&models=ukmo_seamless"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read())
        c = d['current']
        dl = d['daily']
        code = int(c['weather_code'])
        precip_duration = _calc_precip_duration(d.get('hourly', {}), code)
        return {
            'temp':    round(c['temperature_2m']),
            'feels':   round(c['apparent_temperature']),
            'humidity':round(c['relative_humidity_2m']),
            'wind':    round(c['wind_speed_10m']),
            'wdir':    round(c['wind_direction_10m']),
            'code':    code,
            'uv':      round(dl.get('uv_index_max', [0])[0]),
            'high':    round(dl['temperature_2m_max'][0]),
            'low':     round(dl['temperature_2m_min'][0]),
            'sunrise': dl['sunrise'][0][11:16],
            'sunset':  dl['sunset'][0][11:16],
            'precip_duration': precip_duration,
            'ok': True,
        }
    except Exception as e:
        log.warning(f"Fetch failed: {e}")
        return {'ok': False}

MAX_EARTH_PHOTOS = 12          # Number of images to rotate through
UPDATE_LIST_SECONDS = 43200   # Refresh photo list every 12 hours

def invalidate_earth_cache():
    """Clear earth image cache on error or expiry (Trick #10)"""
    global earth_image_cache
    earth_image_cache['data'] = None
    earth_image_cache['resized_240'] = None
    earth_image_cache['timestamp'] = 0
    earth_image_cache['size_bytes'] = 0
    log.info("Earth cache invalidated")

def fetch_photos_list():
    """
    Fetch metadata for the last MAX_EARTH_PHOTOS images from NASA EPIC API.
    Returns a list of metadata dicts (no images downloaded).
    """
    try:
        log.info("Fetching NASA EPIC image list...")
        api_url = "https://epic.gsfc.nasa.gov/api/natural"

        with urllib.request.urlopen(api_url, timeout=15) as response:
            images = json.loads(response.read())

        if not images:
            log.warning("No images available from NASA EPIC")
            return []

        photos = images[:MAX_EARTH_PHOTOS]
        log.info(f"Got {len(photos)} images from NASA EPIC")
        return photos

    except Exception as e:
        log.error(f"Failed to fetch photo list: {e}")
        return []

def fetch_earth_photo(meta, index, total):
    """
    Download a single NASA EPIC photo given its metadata dict (Trick #2 caching).
    index/total are 1-based for display.
    """
    try:
        image_name = meta['image']
        date = meta['date']

        # Parse date for image URL
        date_parts = date.split(' ')[0].split('-')
        year, month, day = date_parts[0], date_parts[1], date_parts[2]

        # Construct image URL (use JPG to save bandwidth: ~200KB vs ~2MB PNG)
        image_url = f"https://epic.gsfc.nasa.gov/archive/natural/{year}/{month}/{day}/jpg/{image_name}.jpg"

        log.info(f"Downloading Earth photo {index}/{total}: {image_name}.jpg")

        with urllib.request.urlopen(image_url, timeout=30) as img_response:
            image_data = img_response.read()

        earth_img = Image.open(BytesIO(image_data))

        coords = meta.get('centroid_coordinates', {})
        lat = coords.get('lat', 0)
        lon = coords.get('lon', 0)

        # OPTIMIZATION: Cache the resized image ONCE (Trick #2)
        log.info("Caching resized Earth image (240x240)...")
        earth_image_cache['data'] = earth_img
        earth_image_cache['resized_240'] = earth_img.resize((240, 240), Image.LANCZOS)
        earth_image_cache['timestamp'] = time.time()
        earth_image_cache['size_bytes'] = len(image_data)
        log.info(f"Cached Earth image {index}/{total} (size: {len(image_data)//1024}KB)")

        return {
            'ok': True,
            'image': earth_img,
            'date': date,
            'lat': round(lat, 1),
            'lon': round(lon, 1),
            'index': index,
            'total': total,
        }

    except Exception as e:
        log.error(f"Failed to fetch Earth photo: {e}")
        invalidate_earth_cache()
        return {'ok': False}


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN SCREEN (240x240)
# ══════════════════════════════════════════════════════════════════════════════
def render_main(w, wifi):
    img  = Image.new("RGB", (240, 240), (10, 10, 14))
    draw = ImageDraw.Draw(img)

    if not w['ok']:
        draw.text((80, 110), "No Data", font=f(18), fill=(80, 80, 90))
        return img

    # Top Left: City & Date
    draw.text((12, 7), CITY.upper(), font=f(18), fill=(200, 200, 200))
    draw.text((12, 24), time.strftime("%a %d %b"), font=f(16), fill=(150, 150, 150))

    # Top Right: Low & High
    low_text = f"{w['low']}°"
    bbox = draw.textbbox((0, 0), low_text, font=f(18))
    low_w = bbox[2] - bbox[0]
    draw.text((169 - low_w/2, 24), low_text, font=f(18), fill=(120, 180, 255))

    high_text = f"{w['high']}°"
    bbox = draw.textbbox((0, 0), high_text, font=f(18))
    high_w = bbox[2] - bbox[0]
    draw.text((199 - high_w/2, 24), high_text, font=f(18), fill=(255, 160, 80))

    # WiFi indicator
    draw_wifi(draw, 216, 10, wifi)

    # Large Temperature - ADJUST TEMP_X and TEMP_Y AT TOP OF FILE TO POSITION
    tc = temp_col(w['temp'])
    temp_text = f"{w['temp']}°"
    # Calculate text width and center it
    bbox = draw.textbbox((0, 0), temp_text, font=f(85))
    temp_w = bbox[2] - bbox[0]
    adjustment_factor = 0.175  # Experiment with this value
    draw.text((TEMP_X - temp_w * adjustment_factor, TEMP_Y), temp_text, font=f(85), fill=tc)


    # Feels like (centered)
    feels_text = f"Feels {w['feels']}°"
    bbox = draw.textbbox((0, 0), feels_text, font=f(18))
    feels_w = bbox[2] - bbox[0]
    draw.text((120 - feels_w/2, 138), feels_text, font=f(18), fill=(200, 200, 200))

    # Condition with precipitation duration on one line (e.g. "Rain for ~3h")
    cond = WMO.get(w['code'], 'Unknown')
    precip_dur = w.get('precip_duration')
    if precip_dur:
        cond = f"{cond} for {precip_dur}"
    bbox = draw.textbbox((0, 0), cond, font=f(20))
    cond_w = bbox[2] - bbox[0]
    draw.text((120 - cond_w/2, 158), cond, font=f(20), fill=(200, 200, 210))

    # Bottom Left: UV Index (WHO recommends sunscreen from UV 3 upward)
    uv_text = f"UV {w['uv']}"
    draw.text((12, 210), uv_text, font=f(20), fill=uv_col(w['uv']))
    if w['uv'] >= 3:
        bbox = draw.textbbox((0, 0), uv_text, font=f(20))
        uv_w = bbox[2] - bbox[0]
        draw_suncream(draw, 12 + uv_w + 8, 211, h=18)

    # Bottom Center: Time (properly centered)
    time_text = time.strftime("%H:%M")
    bbox = draw.textbbox((0, 0), time_text, font=f(30))
    time_w = bbox[2] - bbox[0]
    draw.text((120 - time_w/2, 200), time_text, font=f(30), fill=(224, 224, 224))

    return img


# ══════════════════════════════════════════════════════════════════════════════
#  LEFT/RIGHT SCREENS - Humidity & Wind
# ══════════════════════════════════════════════════════════════════════════════
def render_humidity_wind(w, wifi):
    img  = Image.new("RGB", (160, 80), (10, 10, 14))
    draw = ImageDraw.Draw(img)

    if not w['ok']:
        draw.text((60, 32), "--", font=f(14), fill=(60, 60, 70))
        return img

    # Humidity (left side)
    draw.text((8, 8), "HUM", font=f(10), fill=(50, 50, 65))
    hum_text = f"{w['humidity']}%"
    bbox = draw.textbbox((0, 0), hum_text, font=f(28))
    hum_w = bbox[2] - bbox[0]
    draw.text((40 - hum_w/2, 28), hum_text, font=f(28), fill=(60, 180, 180))

    # Separator line
    draw.line([(80, 10), (80, 70)], fill=(25, 25, 35), width=1)

    # Wind (right side - centered)
    draw.text((88, 8), "WIND", font=f(10), fill=(50, 50, 65))
    wind_text = f"{w['wind']}"
    bbox = draw.textbbox((0, 0), wind_text, font=f(28))
    wind_w = bbox[2] - bbox[0]
    draw.text((120 - wind_w/2, 28), wind_text, font=f(28), fill=(160, 110, 220))

    # Direction and units (centered below)
    dir_text = f"{wind_dir(w['wdir'])} km/h"
    bbox = draw.textbbox((0, 0), dir_text, font=f(10))
    dir_w = bbox[2] - bbox[0]
    draw.text((120 - dir_w/2, 58), dir_text, font=f(10), fill=(80, 80, 95))

    return img


# ══════════════════════════════════════════════════════════════════════════════
#  LEFT/RIGHT SCREENS - Sunrise & Sunset
# ══════════════════════════════════════════════════════════════════════════════
def render_sun_times(w, wifi):
    img  = Image.new("RGB", (160, 80), (10, 10, 14))
    draw = ImageDraw.Draw(img)

    if not w['ok']:
        draw.text((60, 32), "--", font=f(14), fill=(60, 60, 70))
        return img

    # Sunrise (left side)
    draw_sunrise(draw, 40, 28, r=14)
    bbox = draw.textbbox((0, 0), w['sunrise'], font=f(14))
    sunrise_w = bbox[2] - bbox[0]
    draw.text((40 - sunrise_w/2, 50), w['sunrise'], font=f(14), fill=(255, 190, 80))

    # Separator line
    draw.line([(80, 10), (80, 70)], fill=(25, 25, 35), width=1)

    # Sunset (right side)
    draw_sunset(draw, 120, 28, r=14)
    bbox = draw.textbbox((0, 0), w['sunset'], font=f(14))
    sunset_w = bbox[2] - bbox[0]
    draw.text((120 - sunset_w/2, 50), w['sunset'], font=f(14), fill=(255, 110, 60))

    return img


# ══════════════════════════════════════════════════════════════════════════════
#  EARTH PHOTO PAGE - MAIN SCREEN (240x240)
# ══════════════════════════════════════════════════════════════════════════════
def render_main_earth(earth_data):
    img = Image.new("RGB", (240, 240), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    if not earth_data['ok']:
        draw.text((60, 110), "No Earth", font=f(18), fill=(80, 80, 90))
        draw.text((50, 130), "photo available", font=f(14), fill=(60, 60, 70))
        return img

    # OPTIMIZATION: Use cached resized image with TTL validation (Tricks #2, #10)
    # This eliminates expensive LANCZOS resampling on every 5-second cycle
    now = time.time()
    cache_age = now - earth_image_cache['timestamp']
    cache_valid = (earth_image_cache['resized_240'] is not None and
                   cache_age < earth_image_cache['ttl'])

    if cache_valid:
        return earth_image_cache['resized_240']
    else:
        # Cache expired or empty
        if earth_image_cache['resized_240'] is not None:
            log.info(f"Cache expired (age: {cache_age:.0f}s > TTL: {earth_image_cache['ttl']}s)")
            invalidate_earth_cache()

        # Fallback: resize on-the-fly if cache is empty
        if earth_data.get('image'):
            log.warning("Resizing Earth image on-the-fly (cache miss)")
            earth_img = earth_data['image']
            resized_img = earth_img.resize((240, 240), Image.LANCZOS)
            # Update cache in fallback path to prevent redundant resizing
            earth_image_cache['resized_240'] = resized_img
            earth_image_cache['timestamp'] = now
            return resized_img
        else:
            # No image available - return stale cache if available, otherwise blank
            return earth_image_cache.get('resized_240') or img


# ══════════════════════════════════════════════════════════════════════════════
#  EARTH PHOTO PAGE - LEFT SCREEN (160x80) - Date & Time Info
# ══════════════════════════════════════════════════════════════════════════════
def render_left_earth(earth_data):
    img = Image.new("RGB", (160, 80), (10, 10, 14))
    draw = ImageDraw.Draw(img)

    if not earth_data['ok']:
        draw.text((50, 32), "--", font=f(14), fill=(60, 60, 70))
        return img

    # Title with image index
    idx = earth_data.get('index', 1)
    total = earth_data.get('total', MAX_EARTH_PHOTOS)
    draw.text((8, 6), f"NASA EPIC {idx}/{total}", font=f(11), fill=(100, 150, 255))

    # Parse date from "YYYY-MM-DD HH:MM:SS"
    date_str = earth_data['date']
    date_part = date_str.split(' ')[0]  # YYYY-MM-DD
    time_part = date_str.split(' ')[1][:5]  # HH:MM

    # Display date
    draw.text((8, 28), date_part, font=f(14), fill=(200, 200, 210))

    # Display time (UTC)
    draw.text((8, 48), f"{time_part} UTC", font=f(12), fill=(150, 150, 160))

    return img


# ══════════════════════════════════════════════════════════════════════════════
#  EARTH PHOTO PAGE - RIGHT SCREEN (160x80) - Location Info
# ══════════════════════════════════════════════════════════════════════════════
def render_right_earth(earth_data):
    img = Image.new("RGB", (160, 80), (10, 10, 14))
    draw = ImageDraw.Draw(img)

    if not earth_data['ok']:
        draw.text((50, 32), "--", font=f(14), fill=(60, 60, 70))
        return img

    # Title
    draw.text((8, 6), "CENTER", font=f(10), fill=(80, 80, 95))

    # Latitude
    lat = earth_data['lat']
    lat_dir = 'N' if lat >= 0 else 'S'
    draw.text((8, 26), f"LAT: {abs(lat)}° {lat_dir}", font=f(12), fill=(100, 200, 150))

    # Longitude
    lon = earth_data['lon']
    lon_dir = 'E' if lon >= 0 else 'W'
    draw.text((8, 46), f"LON: {abs(lon)}° {lon_dir}", font=f(12), fill=(100, 200, 150))

    return img


# ══════════════════════════════════════════════════════════════════════════════
#  MOON PHASE — pure local calendar math, no network call
# ══════════════════════════════════════════════════════════════════════════════
SYNODIC_MONTH = 29.530588861  # days per lunar cycle
_KNOWN_NEW_MOON = 946_845_240  # 2000-01-06 18:14 UTC, a reference new moon (unix epoch)

def moon_phase(t=None):
    """Return (phase_name, illumination 0-1, days_into_cycle) for time t (default: now)."""
    if t is None:
        t = time.time()
    days = ((t - _KNOWN_NEW_MOON) / 86400.0) % SYNODIC_MONTH
    illumination = (1 - math.cos(2 * math.pi * days / SYNODIC_MONTH)) / 2
    if days < 1.84566:
        name = "New Moon"
    elif days < 5.53699:
        name = "Waxing Crescent"
    elif days < 9.22831:
        name = "First Quarter"
    elif days < 12.91963:
        name = "Waxing Gibbous"
    elif days < 16.61096:
        name = "Full Moon"
    elif days < 20.30228:
        name = "Waning Gibbous"
    elif days < 23.99361:
        name = "Last Quarter"
    else:
        name = "Waning Crescent"
    return name, illumination, days

def next_moon_events(t=None):
    """Return (next_full_moon_str, next_new_moon_str) as '%d %b' dates."""
    if t is None:
        t = time.time()
    _, _, days = moon_phase(t)
    days_to_full = (14.76529 - days) % SYNODIC_MONTH
    days_to_new = (SYNODIC_MONTH - days) % SYNODIC_MONTH
    full_str = time.strftime('%d %b', time.localtime(t + days_to_full * 86400))
    new_str = time.strftime('%d %b', time.localtime(t + days_to_new * 86400))
    return full_str, new_str

def draw_moon(draw, cx, cy, r, illumination, waxing):
    """Hand-drawn moon disc with a terminator shadow, in the style of the sun icons."""
    moon_col = (225, 222, 205)
    shadow_col = (14, 14, 22)

    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=moon_col)

    if illumination >= 0.999:
        return  # full moon: no shadow to draw
    if illumination <= 0.001:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=shadow_col)
        return

    # Two overlapping shapes approximate the lunar terminator: a half-disc of
    # shadow, then a centred ellipse (width set by illumination) painted back
    # over it — shrinking the shadow for a crescent, or restoring lit colour
    # for a gibbous — to produce the correct waxing/waning silhouette.
    # Width is 0 at quarter (a straight-line terminator, pure half/half) and
    # a full diameter at new/full (where the "ellipse" coincides with the
    # disc edge itself, pushing the whole disc to one colour).
    terminator_w = int(r * 2 * abs(1 - 2 * illumination))
    # Northern-hemisphere convention: waxing shows its growing sliver/shadow
    # on the right, so the shadow half starts on the LEFT for waxing.
    shadow_on_right = not waxing

    if shadow_on_right:
        draw.pieslice([cx - r, cy - r, cx + r, cy + r], -90, 90, fill=shadow_col)
    else:
        draw.pieslice([cx - r, cy - r, cx + r, cy + r], 90, 270, fill=shadow_col)

    # Below 50% lit: push further toward dark (crescent). At/above 50%: push
    # back toward lit (gibbous) — restoring lit colour over the shadow half.
    fill_col = shadow_col if illumination < 0.5 else moon_col
    draw.ellipse([cx - terminator_w // 2, cy - r, cx + terminator_w // 2, cy + r], fill=fill_col)

def render_main_moon():
    img = Image.new("RGB", (240, 240), (6, 6, 14))
    draw = ImageDraw.Draw(img)

    name, illumination, days = moon_phase()
    draw_moon(draw, 120, 100, r=65, illumination=illumination, waxing=(days < SYNODIC_MONTH / 2))

    pct_text = f"{round(illumination * 100)}%"
    bbox = draw.textbbox((0, 0), pct_text, font=f(22))
    w = bbox[2] - bbox[0]
    draw.text((120 - w / 2, 190), pct_text, font=f(22), fill=(210, 210, 225))

    bbox = draw.textbbox((0, 0), name, font=f(16))
    w = bbox[2] - bbox[0]
    draw.text((120 - w / 2, 216), name, font=f(16), fill=(150, 150, 170))

    return img

def render_left_moon():
    img = Image.new("RGB", (160, 80), (10, 10, 14))
    draw = ImageDraw.Draw(img)
    full_date, _ = next_moon_events()
    draw.text((8, 8), "NEXT FULL", font=f(10), fill=(80, 80, 95))
    draw.text((8, 28), full_date, font=f(22), fill=(220, 218, 200))
    return img

def render_right_moon():
    img = Image.new("RGB", (160, 80), (10, 10, 14))
    draw = ImageDraw.Draw(img)
    _, new_date = next_moon_events()
    draw.text((8, 8), "NEXT NEW", font=f(10), fill=(80, 80, 95))
    draw.text((8, 28), new_date, font=f(22), fill=(150, 150, 175))
    return img


# ══════════════════════════════════════════════════════════════════════════════
#  OPTIMIZATION HELPERS (Tricks #1, #4, #5)
# ══════════════════════════════════════════════════════════════════════════════

# Retry backoff: 30s, 60s, 120s... capped at `cap` seconds (the normal interval)
def _backoff_seconds(fail_count, cap):
    if fail_count <= 0:
        return cap
    return min(cap, 30 * (2 ** (fail_count - 1)))

# Trick #4: Async background fetching - prevents blocking on network timeouts
def fetch_weather_async(weather_ref):
    """Async wrapper for weather fetching. Caller stamps last_attempt before
    calling this (see main loop) so the retry gate updates immediately,
    not only after the network call completes — otherwise a slow/failing
    fetch lets the 5s loop spawn a new thread every tick indefinitely."""
    def _fetch():
        log.info("Fetching weather (async)...")
        new = fetch_weather()
        if new['ok']:
            weather_ref['data'] = new
            weather_ref['fail_count'] = 0
            _save_weather_cache(new)
            log.info(f"{new['temp']}°C {WMO.get(new['code'], '')}")
        else:
            weather_ref['fail_count'] += 1

    threading.Thread(target=_fetch, daemon=True).start()


def fetch_earth_async(earth_ref, meta, index, total, target_hour):
    """Async wrapper for Earth photo fetching – downloads one photo by metadata.
    Only advances last_photo_hour on success, so a failed download retries
    with backoff instead of waiting a full hour for the next attempt."""
    def _fetch():
        log.info(f"Fetching Earth photo {index}/{total} (async)...")
        new_earth = fetch_earth_photo(meta, index, total)
        if new_earth['ok']:
            earth_ref['data'] = new_earth
            earth_ref['last_photo_hour'] = target_hour
            earth_ref['photo_fail_count'] = 0
            log.info(f"Earth photo {index}/{total} updated: {new_earth['date']}")
        else:
            earth_ref['photo_fail_count'] += 1

    threading.Thread(target=_fetch, daemon=True).start()


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    # State tracking
    last_activity = time.time()
    is_dimmed = False
    was_night_dim = False  # tracks whether last dim used night-off (duty=0)
    current_page = 'weather'  # one of PAGES: 'weather' / 'earth' / 'moon'

    log.info("Initialising displays...")
    disp_main = LCD_1inch3.LCD_1inch3(
        spi=SPI.SpiDev(BUS_MAIN, DEV_MAIN), spi_freq=10000000,
        rst=RST_MAIN, dc=DC_MAIN, bl=BL_MAIN)
    disp_left = LCD_0inch96.LCD_0inch96(
        spi=SPI.SpiDev(BUS_L, DEV_L), spi_freq=10000000,
        rst=RST_L, dc=DC_L, bl=BL_L)
    disp_right = LCD_0inch96.LCD_0inch96(
        spi=SPI.SpiDev(BUS_R, DEV_R), spi_freq=10000000,
        rst=RST_R, dc=DC_R, bl=BL_R)

    for d in [disp_main, disp_left, disp_right]:
        d.Init()
        d.clear()

    disp_main.bl_DutyCycle(BL_MAIN_DUTY)
    disp_left.bl_DutyCycle(BL_SIDE_DUTY)
    disp_right.bl_DutyCycle(BL_SIDE_DUTY)

    # Setup buttons using Waveshare's GPIO library
    from gpiozero import DigitalInputDevice
    from gpiozero.pins.lgpio import LGPIOFactory
    _button_factory = LGPIOFactory()
    key1 = DigitalInputDevice(KEY1_PIN, pull_up=None, active_state=True, pin_factory=_button_factory)
    key2 = DigitalInputDevice(KEY2_PIN, pull_up=None, active_state=True, pin_factory=_button_factory)

    # Data references for async fetching (Trick #4). Warm-start weather from
    # the last successful fetch so a restart shows real data, not "No Data".
    weather_ref = {'data': _load_weather_cache(), 'last_attempt': 0, 'fail_count': 0}
    earth_ref = {
        'data': {'ok': False},
        'photos_list': [],       # Metadata for up to MAX_EARTH_PHOTOS images
        'list_last_attempt': 0,  # When the photo list was last attempted
        'list_fail_count': 0,
        'last_photo_hour': -1,   # Hour index of the last successfully downloaded photo
        'photo_last_attempt': 0,
        'photo_fail_count': 0,
    }

    # Trick #8: Dirty Flag Rendering - state tracking to skip unnecessary renders
    last_render_state = {'hash': None, 'time': None}
    last_displayed_page = None

    def compute_state_hash(weather, earth_data, wifi):
        """Compute hash of visible state for dirty flag detection (Trick #8)"""
        weather_tuple = (
            weather.get('temp'),
            weather.get('feels'),
            weather.get('humidity'),
            weather.get('wind'),
            weather.get('wdir'),
            weather.get('code'),
            weather.get('uv'),
            weather.get('high'),
            weather.get('low'),
            weather.get('sunrise'),
            weather.get('sunset'),
            weather.get('precip_duration'),
            weather.get('ok')
        )
        earth_tuple = (
            earth_data.get('ok'),
            earth_data.get('date'),
            earth_data.get('lat'),
            earth_data.get('lon'),
            earth_data.get('index'),
        )
        # Include today's date so the moon page still refreshes once daily
        # even if weather/earth data happens to be unchanged across midnight.
        return (weather_tuple, earth_tuple, wifi, time.strftime('%Y-%m-%d'))

    # Trick #5 & #8: Double Buffering with Dirty Flag - only render when state changes
    def update_frame_buffers():
        """Pre-render both pages into buffers only when data changes (Tricks #5, #8).

        Returns 'data' if weather/earth data changed (all screens need updating),
        'time' if only the clock minute changed (main screen only needs updating),
        or False if nothing changed.
        """
        nonlocal last_render_state

        with buffer_lock:
            wifi = wifi_status()
            weather = weather_ref['data']
            earth_data = earth_ref['data']

            # Compute current state hash (data only — time tracked separately)
            current_state = compute_state_hash(weather, earth_data, wifi)
            current_time = time.strftime("%H:%M")

            data_changed = (current_state != last_render_state['hash'])
            time_changed = (current_time != last_render_state['time'])

            if data_changed:
                log.debug("Rendering: data state changed")

                # Render weather page
                frame_buffers['weather']['main'] = render_main(weather, wifi)
                frame_buffers['weather']['left'] = render_humidity_wind(weather, wifi)
                frame_buffers['weather']['right'] = render_sun_times(weather, wifi)

                # Render earth page
                frame_buffers['earth']['main'] = render_main_earth(earth_data)
                frame_buffers['earth']['left'] = render_left_earth(earth_data)
                frame_buffers['earth']['right'] = render_right_earth(earth_data)

                # Render moon page (pure local math, negligible cost)
                frame_buffers['moon']['main'] = render_main_moon()
                frame_buffers['moon']['left'] = render_left_moon()
                frame_buffers['moon']['right'] = render_right_moon()

                last_render_state['hash'] = current_state
                last_render_state['time'] = current_time
                return 'data'

            elif time_changed:
                # Only the clock changed. Nothing but the weather page's main
                # screen shows a clock, and there's no point rendering into a
                # blacked-out/dimmed display or a page that isn't even shown —
                # the SPI push is already gated the same way below.
                if current_page != 'weather' or is_dimmed:
                    return False
                log.debug("Rendering: time-only change, main screen only")
                frame_buffers['weather']['main'] = render_main(weather, wifi)
                last_render_state['time'] = current_time
                return 'time'

            else:
                log.debug("Skipped render: state unchanged")
                return False

    # Trick #1: Event-Driven Rendering - immediate render on button press
    def render_current_page_now():
        """Immediately render and display current page (called on button press)"""
        with buffer_lock:
            page_buffer = frame_buffers.get(current_page)
            if page_buffer and page_buffer['main']:
                disp_main.ShowImage(page_buffer['main'])
                disp_left.ShowImage(page_buffer['left'])
                disp_right.ShowImage(page_buffer['right'])
                log.info(f"✓ Instant page render: {current_page}")

    # Button callbacks
    def key1_callback():
        nonlocal last_activity
        last_activity = time.time()
        log.info("✓ KEY1 pressed - wake button")

    def key2_callback():
        nonlocal current_page, last_activity
        current_page = PAGES[(PAGES.index(current_page) + 1) % len(PAGES)]
        last_activity = time.time()
        log.info(f"✓ KEY2 pressed - switched to {current_page} page")
        # Trick #1: Immediate render on button press - no waiting for loop!
        render_current_page_now()

    # Attach callbacks to buttons
    key1.when_activated = key1_callback
    key2.when_activated = key2_callback

    log.info("Weather station ready! (Burn-in protection + OPTIMIZATIONS enabled)")
    log.info(f"- Auto-dim after {DIM_TIMEOUT}s")
    log.info(f"- NASA EPIC: rotating {MAX_EARTH_PHOTOS} images, one per hour")
    log.info(f"- Press KEY2 to cycle: {' → '.join(PAGES)}")
    log.info("✓ Buttons ready using Waveshare GPIO library")
    log.info("✓ OPTIMIZATIONS: Image caching, WiFi caching, async fetching, double buffering,")
    log.info("✓                dirty flag rendering, cache invalidation, instant page switching")

    # Trick #6: Reduce loop sleep from 30s to 5s for more responsive updates
    LOOP_INTERVAL = 5  # seconds (was 30)
    loop_count = 0

    try:
        while True:
            now = time.time()
            loop_count += 1

            # Trick #4: Async fetch weather data (every UPDATE_SECONDS, backing off on failure).
            # last_attempt is stamped here — synchronously, before spawning — so the gate
            # updates immediately rather than only after the network call finishes; otherwise
            # a slow/failing fetch lets this 5s loop spawn a new thread every tick forever.
            weather_interval = _backoff_seconds(weather_ref['fail_count'], UPDATE_SECONDS)
            if now - weather_ref['last_attempt'] >= weather_interval:
                weather_ref['last_attempt'] = now
                fetch_weather_async(weather_ref)

            # 12-image hourly rotation: refresh photo list every 12h, rotate photo each hour
            current_hour = int(now // 3600)
            list_interval = _backoff_seconds(earth_ref['list_fail_count'], UPDATE_LIST_SECONDS)
            if now - earth_ref['list_last_attempt'] >= list_interval:
                earth_ref['list_last_attempt'] = now  # set immediately to prevent duplicate spawns
                def _refresh_list(ref=earth_ref):
                    new_list = fetch_photos_list()
                    if new_list:
                        ref['photos_list'] = new_list
                        ref['last_photo_hour'] = -1  # force photo reload on new list
                        ref['list_fail_count'] = 0
                        log.info(f"Photo list refreshed: {len(new_list)} images available")
                    else:
                        ref['list_fail_count'] += 1
                threading.Thread(target=_refresh_list, daemon=True).start()

            if earth_ref['photos_list'] and current_hour != earth_ref['last_photo_hour']:
                # Retry within the hour with backoff on failure, rather than
                # stamping last_photo_hour up front and stalling a full hour.
                photo_interval = _backoff_seconds(earth_ref['photo_fail_count'], 3600)
                if now - earth_ref['photo_last_attempt'] >= photo_interval:
                    earth_ref['photo_last_attempt'] = now
                    photos = earth_ref['photos_list']
                    idx = current_hour % len(photos)
                    human_idx = idx + 1
                    total = len(photos)
                    fetch_earth_async(earth_ref, photos[idx], human_idx, total, current_hour)

            # Check for auto-dim / night schedule
            inactive_time = now - last_activity
            should_be_dimmed = inactive_time >= DIM_TIMEOUT
            is_night = NIGHT_START_HOUR <= time.localtime(now).tm_hour < NIGHT_END_HOUR

            if should_be_dimmed:
                # Re-apply if not yet dimmed, or night status changed while dimmed
                if not is_dimmed or is_night != was_night_dim:
                    if is_night:
                        log.info("Night schedule: turning backlight off (00:00–07:00)")
                        disp_main.bl_DutyCycle(0)
                        disp_left.bl_DutyCycle(0)
                        disp_right.bl_DutyCycle(0)
                    else:
                        log.info("Auto-dimming displays")
                        disp_main.bl_DutyCycle(int(BL_MAIN_DUTY * 0.2))
                        disp_left.bl_DutyCycle(int(BL_SIDE_DUTY * 0.2))
                        disp_right.bl_DutyCycle(int(BL_SIDE_DUTY * 0.2))
                    is_dimmed = True
                    was_night_dim = is_night

            elif is_dimmed:
                log.info("Restoring brightness")
                disp_main.bl_DutyCycle(BL_MAIN_DUTY)
                disp_left.bl_DutyCycle(BL_SIDE_DUTY)
                disp_right.bl_DutyCycle(BL_SIDE_DUTY)
                is_dimmed = False
                was_night_dim = False

            # Trick #5: Update double buffers (pre-render both pages)
            buffers_changed = update_frame_buffers()

            # Trick #3: Lazy Rendering - only push to displays when content changed
            page_changed = (current_page != last_displayed_page)
            if buffers_changed == 'data' or page_changed:
                # Data changed or page switched: update all three screens
                with buffer_lock:
                    page_buffer = frame_buffers.get(current_page)
                    if page_buffer and page_buffer['main']:
                        disp_main.ShowImage(page_buffer['main'])
                        disp_left.ShowImage(page_buffer['left'])
                        disp_right.ShowImage(page_buffer['right'])
                        last_displayed_page = current_page
            elif buffers_changed == 'time' and current_page == 'weather':
                # Clock ticked: only the main screen needs updating.
                # Side screens don't show the time, so leave them alone.
                with buffer_lock:
                    if frame_buffers['weather']['main']:
                        disp_main.ShowImage(frame_buffers['weather']['main'])
                        last_displayed_page = current_page

            # Trick #6: Faster loop = more responsive button handling (5s vs 30s)
            time.sleep(LOOP_INTERVAL)

    except KeyboardInterrupt:
        log.info("Exiting...")
        GPIO.cleanup()
        for d in [disp_main, disp_left, disp_right]:
            d.clear()
            d.module_exit()

if __name__ == '__main__':
    main()
