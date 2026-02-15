#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
Weather Station - Waveshare Triple LCD HAT
Custom Design by Danny
https://github.com/dannybellieveit/weather-display

Main screen (1.3" 240x240): Current conditions with UV, high/low, time
Left screen  (0.96" 160x80): Humidity & Wind
Right screen (0.96" 160x80): Sun times
"""

import os, sys, time, logging, urllib.request, json, subprocess, math
import spidev as SPI

WAVESHARE_DIR = os.path.join(os.path.expanduser('~'), 'Zero_LCD_HAT_A_Demo', 'python')
sys.path.append(WAVESHARE_DIR)
from lib import LCD_1inch3, LCD_0inch96
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

# ── Pins ──────────────────────────────────────────────────────────────────────
RST_MAIN, DC_MAIN, BL_MAIN, BUS_MAIN, DEV_MAIN = 27, 22, 19, 1, 0
RST_L,    DC_L,    BL_L,    BUS_L,    DEV_L    = 24,  4, 13, 0, 0
RST_R,    DC_R,    BL_R,    BUS_R,    DEV_R    = 23,  5, 12, 0, 1

# ── Config ────────────────────────────────────────────────────────────────────
BL_MAIN_DUTY = 90   # Main screen brightness (0-100)
BL_SIDE_DUTY = 45   # Side screens brightness (0-100)

LAT, LON, CITY = 51.4279, -0.1255, "Streatham"
UPDATE_SECONDS = 300

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


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN SCREEN (240x240) - Final Design
# ══════════════════════════════════════════════════════════════════════════════
def render_main(w, wifi):
    img  = Image.new("RGB", (240, 240), (10, 10, 14))
    draw = ImageDraw.Draw(img)

    if not w['ok']:
        draw.text((80, 110), "No Data", font=f(18), fill=(80, 80, 90))
        return img

    # ── Top Left: City & Date ─────────────────────────────────────────────────
    draw.text((12, 21), CITY.upper(), font=f(13), fill=(80, 95, 95))
    draw.text((12, 37), time.strftime("%a %d %b"), font=f(11), fill=(55, 55, 70))

    # ── Top Right: Low & High ─────────────────────────────────────────────────
    # Low temp
    low_text = f"{w['low']}°"
    low_w = draw.textlength(low_text, font=f(18))
    draw.text((169 - low_w/2, 24), low_text, font=f(18), fill=(120, 180, 255))

    # High temp
    high_text = f"{w['high']}°"
    high_w = draw.textlength(high_text, font=f(18))
    draw.text((199 - high_w/2, 24), high_text, font=f(18), fill=(255, 160, 80))

    # WiFi indicator
    draw_wifi(draw, 216, 10, wifi)

    # ── Center: Large Temperature (TRULY centered now!) ───────────────────────
    tc = temp_col(w['temp'])
    temp_text = f"{w['temp']}°"
    # Calculate exact center position
    bbox = draw.textbbox((0, 0), temp_text, font=f(85))
    text_width = bbox[2] - bbox[0]
    draw.text((120 - text_width/2, 115), temp_text, font=f(85), fill=tc)

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

    # ── Bottom Left: UV Index (out of the way!) ──────────────────────────────
    uv_text = f"UV {w['uv']}"
    draw.text((12, 220), uv_text, font=f(16), fill=uv_col(w['uv']))

    # ── Bottom Center: Time (properly centered!) ──────────────────────────────
    time_text = time.strftime("%H:%M")
    bbox = draw.textbbox((0, 0), time_text, font=f(16))
    time_w = bbox[2] - bbox[0]
    draw.text((120 - time_w/2, 220), time_text, font=f(16), fill=(224, 224, 224))

    return img


# ══════════════════════════════════════════════════════════════════════════════
#  LEFT SCREEN (160x80) - Humidity & Wind (centered!)
# ══════════════════════════════════════════════════════════════════════════════
def render_left(w, wifi):
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

    # Wind (right side - centered in its space!)
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
#  RIGHT SCREEN (160x80) - Sunrise & Sunset
# ══════════════════════════════════════════════════════════════════════════════
def render_right(w, wifi):
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


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
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

    weather = {'ok': False}
    last_fetch = 0

    log.info("Weather station ready!")

    try:
        while True:
            now = time.time()
            if now - last_fetch >= UPDATE_SECONDS or last_fetch == 0:
                log.info("Fetching weather...")
                new = fetch_weather()
                if new['ok']:
                    weather = new
                    log.info(f"{weather['temp']}°C {WMO.get(weather['code'], '')}")
                last_fetch = now

            wifi = wifi_status()

            disp_main.ShowImage(render_main(weather, wifi))
            disp_left.ShowImage(render_left(weather, wifi))
            disp_right.ShowImage(render_right(weather, wifi))

            time.sleep(30)

    except KeyboardInterrupt:
        log.info("Exiting...")
        for d in [disp_main, disp_left, disp_right]:
            d.clear()
            d.module_exit()

if __name__ == '__main__':
    main()
