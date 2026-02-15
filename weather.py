#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
Weather Station - Waveshare Triple LCD HAT
Custom Design by Danny
https://github.com/dannybellieveit/weather-display

Main screen (1.3" 240x240): Current conditions with UV, high/low, time
Left screen  (0.96" 160x80): Humidity & Wind / Sun times (swaps hourly)
Right screen (0.96" 160x80): Sun times / Humidity & Wind (swaps hourly)

Burn-in prevention:
- Auto-dim to 20% after 2 minutes
- KEY1 button to wake
- Screens swap every hour
"""

import os, sys, time, logging, urllib.request, json, subprocess, math, threading
import spidev as SPI
import RPi.GPIO as GPIO
from io import BytesIO

WAVESHARE_DIR = os.path.join(os.path.expanduser('~'), 'Zero_LCD_HAT_A_Demo', 'python')
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
    'timestamp': 0
}

# Trick #5: Double Buffering - pre-rendered frames for instant page switching
frame_buffers = {
    'weather': {'main': None, 'left': None, 'right': None},
    'earth': {'main': None, 'left': None, 'right': None}
}

# Lock for thread-safe buffer updates
buffer_lock = threading.Lock()

# ── Pins ──────────────────────────────────────────────────────────────────────
RST_MAIN, DC_MAIN, BL_MAIN, BUS_MAIN, DEV_MAIN = 27, 22, 19, 1, 0
RST_L,    DC_L,    BL_L,    BUS_L,    DEV_L    = 24,  4, 13, 0, 0
RST_R,    DC_R,    BL_R,    BUS_R,    DEV_R    = 23,  5, 12, 0, 1
KEY1_PIN = 25  # Wake button
KEY2_PIN = 26  # Reserved for page cycling

# ── Display Settings ──────────────────────────────────────────────────────────
BL_MAIN_DUTY = 90   # Main screen brightness (0-100)
BL_SIDE_DUTY = 45   # Side screens brightness (0-100)

LAT, LON, CITY = 51.4279, -0.1255, "Streatham"
UPDATE_SECONDS = 300

# ── Manual Positioning (Adjust these to move the temperature!) ───────────────
TEMP_X = 90   # X position: adjust to move left/right (0-240)
TEMP_Y = 45  # Y position: adjust to move up/down (0-240)

# ── Burn-in Prevention Settings ───────────────────────────────────────────────
DIM_TIMEOUT = 120    # Seconds before auto-dim (2 minutes)
SWAP_INTERVAL = 3600  # Seconds between screen swaps (1 hour)

# ── Fonts ─────────────────────────────────────────────────────────────────────
FONT_DIR = os.path.join(WAVESHARE_DIR, 'Font')

def f(size):
    try:    return ImageFont.truetype(os.path.join(FONT_DIR, 'Font00.ttf'), size)
    except: return ImageFont.load_default()

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
    try:
        out = subprocess.check_output(['iwconfig','wlan0'], stderr=subprocess.DEVNULL).decode()
        if 'ESSID:"' in out and 'off/any' not in out:
            return True
    except: pass
    try:
        out = subprocess.check_output(['ip','route'], stderr=subprocess.DEVNULL).decode()
        if 'default' in out:
            return True
    except: pass
    return False


def draw_wifi(draw, x, y, connected, col_on=(80,220,120), col_off=(180,60,60)):
    col = col_on if connected else col_off
    draw.ellipse([x+4,y+9,x+8,y+13], fill=col)
    draw.arc([x+1,y+4,x+11,y+14], start=210, end=330, fill=col, width=2)
    draw.arc([x-2,y,x+14,y+16], start=210, end=330, fill=col, width=2)

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

def fetch_weather():
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
        f"wind_speed_10m,wind_direction_10m,weather_code,uv_index"
        f"&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
        f"&timezone=Europe/London&forecast_days=1"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read())
        c = d['current']
        dl = d['daily']
        return {
            'temp':    round(c['temperature_2m']),
            'feels':   round(c['apparent_temperature']),
            'humidity':round(c['relative_humidity_2m']),
            'wind':    round(c['wind_speed_10m']),
            'wdir':    round(c['wind_direction_10m']),
            'code':    int(c['weather_code']),
            'uv':      round(c.get('uv_index', 0)),
            'high':    round(dl['temperature_2m_max'][0]),
            'low':     round(dl['temperature_2m_min'][0]),
            'sunrise': dl['sunrise'][0][11:16],
            'sunset':  dl['sunset'][0][11:16],
            'ok': True,
        }
    except Exception as e:
        log.warning(f"Fetch failed: {e}")
        return {'ok': False}

def fetch_earth_photo():
    """Fetch latest Earth photo from NASA EPIC API with caching (Trick #2)"""
    try:
        log.info("Fetching latest Earth photo from NASA EPIC...")
        api_url = "https://epic.gsfc.nasa.gov/api/natural"

        with urllib.request.urlopen(api_url, timeout=15) as response:
            images = json.loads(response.read())

        if not images:
            return {'ok': False}

        latest = images[0]
        image_name = latest['image']
        date = latest['date']

        # Parse date for image URL
        date_parts = date.split(' ')[0].split('-')
        year, month, day = date_parts[0], date_parts[1], date_parts[2]

        # Construct image URL (use JPG instead of PNG to save bandwidth)
        # JPG is ~200KB vs PNG ~2MB for same 2048x2048 image
        image_url = f"https://epic.gsfc.nasa.gov/archive/natural/{year}/{month}/{day}/jpg/{image_name}.jpg"

        log.info(f"Downloading Earth photo: {image_name}.jpg")

        # Download the image (loads directly into memory, not saved to disk)
        with urllib.request.urlopen(image_url, timeout=30) as img_response:
            image_data = img_response.read()

        # Load from memory using BytesIO - no files created on disk
        earth_img = Image.open(BytesIO(image_data))

        coords = latest.get('centroid_coordinates', {})
        lat = coords.get('lat', 0)
        lon = coords.get('lon', 0)

        # OPTIMIZATION: Cache the resized image ONCE instead of resizing every render
        log.info("Caching resized Earth image (240x240)...")
        earth_image_cache['data'] = earth_img
        earth_image_cache['resized_240'] = earth_img.resize((240, 240), Image.LANCZOS)
        earth_image_cache['timestamp'] = time.time()

        return {
            'ok': True,
            'image': earth_img,
            'date': date,
            'lat': round(lat, 1),
            'lon': round(lon, 1)
        }

    except Exception as e:
        log.error(f"Failed to fetch Earth photo: {e}")
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
    draw.text((12, 21), CITY.upper(), font=f(13), fill=(80, 95, 95))
    draw.text((12, 37), time.strftime("%a %d %b"), font=f(11), fill=(55, 55, 70))

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
    draw.text((TEMP_X, TEMP_Y), temp_text, font=f(85), fill=tc)

    # Feels like (centered)
    feels_text = f"Feels {w['feels']}°"
    bbox = draw.textbbox((0, 0), feels_text, font=f(12))
    feels_w = bbox[2] - bbox[0]
    draw.text((120 - feels_w/2, 138), feels_text, font=f(12), fill=(70, 70, 85))

    # Condition (centered)
    cond = WMO.get(w['code'], 'Unknown')
    bbox = draw.textbbox((0, 0), cond, font=f(16))
    cond_w = bbox[2] - bbox[0]
    draw.text((120 - cond_w/2, 158), cond, font=f(16), fill=(200, 200, 210))

    # Bottom Left: UV Index
    uv_text = f"UV {w['uv']}"
    draw.text((12, 220), uv_text, font=f(16), fill=uv_col(w['uv']))

    # Bottom Center: Time (properly centered)
    time_text = time.strftime("%H:%M")
    bbox = draw.textbbox((0, 0), time_text, font=f(16))
    time_w = bbox[2] - bbox[0]
    draw.text((120 - time_w/2, 220), time_text, font=f(16), fill=(224, 224, 224))

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

    # OPTIMIZATION: Use cached resized image instead of resizing every render (Trick #2)
    # This eliminates expensive LANCZOS resampling on every 5-second cycle
    if earth_image_cache['resized_240'] is not None:
        return earth_image_cache['resized_240']
    else:
        # Fallback: resize on-the-fly if cache is empty
        earth_img = earth_data['image']
        earth_img = earth_img.resize((240, 240), Image.LANCZOS)
        return earth_img


# ══════════════════════════════════════════════════════════════════════════════
#  EARTH PHOTO PAGE - LEFT SCREEN (160x80) - Date & Time Info
# ══════════════════════════════════════════════════════════════════════════════
def render_left_earth(earth_data):
    img = Image.new("RGB", (160, 80), (10, 10, 14))
    draw = ImageDraw.Draw(img)

    if not earth_data['ok']:
        draw.text((50, 32), "--", font=f(14), fill=(60, 60, 70))
        return img

    # Title
    draw.text((8, 6), "NASA EPIC", font=f(12), fill=(100, 150, 255))

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
#  OPTIMIZATION HELPERS (Tricks #1, #4, #5)
# ══════════════════════════════════════════════════════════════════════════════

# Trick #4: Async background fetching - prevents blocking on network timeouts
def fetch_weather_async(weather_ref, last_fetch_ref):
    """Async wrapper for weather fetching"""
    def _fetch():
        log.info("Fetching weather (async)...")
        new = fetch_weather()
        if new['ok']:
            weather_ref['data'] = new
            weather_ref['last_fetch'] = time.time()
            log.info(f"{new['temp']}°C {WMO.get(new['code'], '')}")

    threading.Thread(target=_fetch, daemon=True).start()


def fetch_earth_async(earth_ref, last_fetch_ref):
    """Async wrapper for Earth photo fetching"""
    def _fetch():
        log.info("Fetching Earth photo (async)...")
        new_earth = fetch_earth_photo()
        if new_earth['ok']:
            earth_ref['data'] = new_earth
            earth_ref['last_fetch'] = time.time()
            log.info(f"Earth photo updated: {new_earth['date']}")

    threading.Thread(target=_fetch, daemon=True).start()


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    # State tracking
    last_activity = time.time()
    is_dimmed = False
    screens_swapped = False
    last_swap_time = time.time()
    current_page = 'weather'  # 'weather' or 'earth'

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
    key1 = disp_left.gpio_mode(KEY1_PIN, disp_left.INPUT, None)
    key2 = disp_left.gpio_mode(KEY2_PIN, disp_left.INPUT, None)

    # Data references for async fetching (Trick #4)
    weather_ref = {'data': {'ok': False}, 'last_fetch': 0}
    earth_ref = {'data': {'ok': False}, 'last_fetch': 0}

    # Trick #5: Double Buffering - helper to update frame buffers
    def update_frame_buffers():
        """Pre-render both pages into buffers for instant switching"""
        with buffer_lock:
            wifi = wifi_status()
            weather = weather_ref['data']
            earth_data = earth_ref['data']

            # Render weather page
            frame_buffers['weather']['main'] = render_main(weather, wifi)
            if screens_swapped:
                frame_buffers['weather']['left'] = render_sun_times(weather, wifi)
                frame_buffers['weather']['right'] = render_humidity_wind(weather, wifi)
            else:
                frame_buffers['weather']['left'] = render_humidity_wind(weather, wifi)
                frame_buffers['weather']['right'] = render_sun_times(weather, wifi)

            # Render earth page
            frame_buffers['earth']['main'] = render_main_earth(earth_data)
            frame_buffers['earth']['left'] = render_left_earth(earth_data)
            frame_buffers['earth']['right'] = render_right_earth(earth_data)

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
        current_page = 'earth' if current_page == 'weather' else 'weather'
        last_activity = time.time()
        log.info(f"✓ KEY2 pressed - switched to {current_page} page")
        # Trick #1: Immediate render on button press - no waiting for loop!
        render_current_page_now()

    # Attach callbacks to buttons
    key1.when_activated = key1_callback
    key2.when_activated = key2_callback

    log.info("Weather station ready! (Burn-in protection + OPTIMIZATIONS enabled)")
    log.info(f"- Auto-dim after {DIM_TIMEOUT}s")
    log.info(f"- Screen swap every {SWAP_INTERVAL}s")
    log.info(f"- Press KEY2 to cycle between weather and Earth photo")
    log.info("✓ Buttons ready using Waveshare GPIO library")
    log.info("✓ OPTIMIZATIONS: Image caching, async fetching, double buffering, instant page switching")

    # Trick #6: Reduce loop sleep from 30s to 5s for more responsive updates
    LOOP_INTERVAL = 5  # seconds (was 30)
    loop_count = 0

    try:
        while True:
            now = time.time()
            loop_count += 1

            # Trick #4: Async fetch weather data (every 300s = 60 cycles at 5s interval)
            if now - weather_ref['last_fetch'] >= UPDATE_SECONDS or weather_ref['last_fetch'] == 0:
                fetch_weather_async(weather_ref, None)

            # Trick #4: Async fetch Earth photo data (every 3600s = 720 cycles at 5s interval)
            if now - earth_ref['last_fetch'] >= 3600 or earth_ref['last_fetch'] == 0:
                fetch_earth_async(earth_ref, None)

            # Check for screen swap
            if now - last_swap_time >= SWAP_INTERVAL:
                screens_swapped = not screens_swapped
                last_swap_time = now
                log.info(f"Swapping screens (now: {'swapped' if screens_swapped else 'normal'})")

            # Check for auto-dim
            inactive_time = now - last_activity
            should_be_dimmed = inactive_time >= DIM_TIMEOUT

            if should_be_dimmed and not is_dimmed:
                log.info("Auto-dimming displays")
                disp_main.bl_DutyCycle(int(BL_MAIN_DUTY * 0.2))
                disp_left.bl_DutyCycle(int(BL_SIDE_DUTY * 0.2))
                disp_right.bl_DutyCycle(int(BL_SIDE_DUTY * 0.2))
                is_dimmed = True

            elif not should_be_dimmed and is_dimmed:
                log.info("Restoring brightness")
                disp_main.bl_DutyCycle(BL_MAIN_DUTY)
                disp_left.bl_DutyCycle(BL_SIDE_DUTY)
                disp_right.bl_DutyCycle(BL_SIDE_DUTY)
                is_dimmed = False

            # Trick #5: Update double buffers (pre-render both pages)
            update_frame_buffers()

            # Trick #3: Lazy Rendering - only display the current page from buffer
            with buffer_lock:
                page_buffer = frame_buffers.get(current_page)
                if page_buffer and page_buffer['main']:
                    disp_main.ShowImage(page_buffer['main'])
                    disp_left.ShowImage(page_buffer['left'])
                    disp_right.ShowImage(page_buffer['right'])

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
