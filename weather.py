#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
Weather Station - Waveshare Triple LCD HAT
https://github.com/dannybellieveit/weather-display

Main screen (1.3" 240x240): Current conditions
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

LAT  = float(os.environ.get('WEATHER_LAT', '51.5074'))
LON  = float(os.environ.get('WEATHER_LON', '-0.1278'))
CITY = os.environ.get('WEATHER_CITY', 'London')
UPDATE_SECONDS = 300

# ── Fonts ─────────────────────────────────────────────────────────────────────
FONT_DIR = os.path.join(WAVESHARE_DIR, 'Font')

def load_font(size):
    try:    return ImageFont.truetype(os.path.join(FONT_DIR, 'Font00.ttf'), size)
    except Exception: return ImageFont.load_default()

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

def wifi_status():
    try:
        out = subprocess.check_output(['iwconfig','wlan0'], stderr=subprocess.DEVNULL).decode()
        if 'ESSID:"' in out and 'off/any' not in out:
            return True
    except Exception: pass
    try:
        out = subprocess.check_output(['ip','route'], stderr=subprocess.DEVNULL).decode()
        if 'default' in out:
            return True
    except Exception: pass
    return False

def draw_wifi(draw, x, y, connected, col_on=(80,220,120), col_off=(180,60,60)):
    col = col_on if connected else col_off
    draw.ellipse([x+4,y+9,x+8,y+13], fill=col)
    draw.arc([x+1,y+4,x+11,y+14], start=210, end=330, fill=col, width=2)
    draw.arc([x-2,y,x+14,y+16], start=210, end=330, fill=col, width=2)

def draw_bar(draw, x, y, w, h, pct, fg, bg=(30,30,38)):
    draw.rectangle([x,y,x+w,y+h], fill=bg)
    fw = int(w * min(max(pct,0),1))
    if fw > 0:
        draw.rectangle([x,y,x+fw,y+h], fill=fg)

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
            'uv':      round(c.get('uv_index',0)),
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
#  MAIN SCREEN (240x240)
# ══════════════════════════════════════════════════════════════════════════════
def render_main(w, wifi):
    img  = Image.new("RGB", (240, 240), (10, 10, 14))
    draw = ImageDraw.Draw(img)
    
    if not w['ok']:
        draw_wifi(draw, 112, 80, wifi)
        msg = "No WiFi" if not wifi else "No Data"
        mw = draw.textlength(msg, font=load_font(18))
        draw.text(((240 - mw) / 2, 110), msg, font=load_font(18),
                  fill=(180, 60, 60) if not wifi else (80, 80, 90))
        return img

    tc = temp_col(w['temp'])
    
    # Header
    draw.text((12, 8), CITY.upper(), font=load_font(13), fill=(80, 80, 95))
    draw.text((12, 24), time.strftime("%a %d %b"), font=load_font(11), fill=(55, 55, 70))
    draw_wifi(draw, 216, 10, wifi)
    if not wifi:
        draw.text((178, 10), "OFFLINE", font=load_font(9), fill=(180, 60, 60))
    draw.line([(12, 42), (228, 42)], fill=(25, 25, 35), width=1)
    
    # Big temperature
    ts = f"{w['temp']}°"
    tw = draw.textlength(ts, font=load_font(85))
    draw.text(((240 - tw) / 2, 48), ts, font=load_font(85), fill=tc)
    
    # Condition
    cond = WMO.get(w['code'], 'Unknown')
    cw = draw.textlength(cond, font=load_font(16))
    draw.text(((240 - cw) / 2, 138), cond, font=load_font(16), fill=(200, 200, 210))
    
    # Feels like
    fl = f"Feels {w['feels']}°"
    fw = draw.textlength(fl, font=load_font(12))
    draw.text(((240 - fw) / 2, 160), fl, font=load_font(12), fill=(70, 70, 85))
    
    draw.line([(12, 182), (228, 182)], fill=(25, 25, 35), width=1)
    
    # High / Low
    hl_text = f"{w['high']}°"
    ll_text = f"{w['low']}°"
    center = 120
    spacing = 50
    
    hx = center - spacing
    draw.polygon([(hx-8, 204), (hx, 194), (hx+8, 204)], fill=(255, 140, 60))
    draw.text((hx - draw.textlength(hl_text, font=load_font(18))/2, 208), hl_text, font=load_font(18), fill=(255, 160, 80))
    
    lx = center + spacing
    draw.polygon([(lx-8, 194), (lx, 204), (lx+8, 194)], fill=(100, 160, 255))
    draw.text((lx - draw.textlength(ll_text, font=load_font(18))/2, 208), ll_text, font=load_font(18), fill=(120, 180, 255))
    
    # Clock
    clk = time.strftime("%H:%M")
    draw.text(((240 - draw.textlength(clk, font=load_font(10))) / 2, 228), clk, font=load_font(10), fill=(40, 40, 55))
    
    return img


# ══════════════════════════════════════════════════════════════════════════════
#  LEFT SCREEN (160x80)
# ══════════════════════════════════════════════════════════════════════════════
def render_left(w):
    img  = Image.new("RGB", (160, 80), (10, 10, 14))
    draw = ImageDraw.Draw(img)
    
    if not w['ok']:
        draw.text((60, 32), "--", font=load_font(14), fill=(60, 60, 70))
        return img
    
    # Humidity
    draw.text((8, 4), "HUM", font=load_font(10), fill=(50, 50, 65))
    draw.text((8, 16), f"{w['humidity']}%", font=load_font(24), fill=(60, 180, 180))
    draw_bar(draw, 8, 52, 60, 5, w['humidity']/100, (60, 180, 180))
    
    draw.line([(80, 8), (80, 72)], fill=(25, 25, 35), width=1)
    
    # Wind
    draw.text((88, 4), "WIND", font=load_font(10), fill=(50, 50, 65))
    draw.text((88, 16), f"{w['wind']}", font=load_font(24), fill=(160, 110, 220))
    draw.text((88, 46), "km/h", font=load_font(9), fill=(60, 60, 75))
    draw.text((88, 60), wind_dir(w['wdir']), font=load_font(12), fill=(160, 110, 220))
    
    return img


# ══════════════════════════════════════════════════════════════════════════════
#  RIGHT SCREEN (160x80)
# ══════════════════════════════════════════════════════════════════════════════
def render_right(w):
    img  = Image.new("RGB", (160, 80), (10, 10, 14))
    draw = ImageDraw.Draw(img)
    
    if not w['ok']:
        draw.text((60, 32), "--", font=load_font(14), fill=(60, 60, 70))
        return img
    
    # Sunrise
    draw_sunrise(draw, 32, 28, r=14)
    draw.text((32 - draw.textlength(w['sunrise'], font=load_font(14))/2, 50), 
              w['sunrise'], font=load_font(14), fill=(255, 190, 80))
    
    draw.line([(80, 8), (80, 72)], fill=(25, 25, 35), width=1)
    
    # Sunset
    draw_sunset(draw, 120, 28, r=14)
    draw.text((120 - draw.textlength(w['sunset'], font=load_font(14))/2, 50), 
              w['sunset'], font=load_font(14), fill=(255, 110, 60))
    
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
                    last_fetch = now
                    log.info(f"{weather['temp']}°C {WMO.get(weather['code'], '')}")
                else:
                    last_fetch = now - UPDATE_SECONDS + 60  # retry in 60s

            wifi = wifi_status()
            
            disp_main.ShowImage(render_main(weather, wifi))
            disp_left.ShowImage(render_left(weather))
            disp_right.ShowImage(render_right(weather))
            
            time.sleep(30)

    except KeyboardInterrupt:
        log.info("Exiting...")
    except Exception:
        log.exception("Unexpected error")
    finally:
        for d in [disp_main, disp_left, disp_right]:
            d.clear()
            d.module_exit()

if __name__ == '__main__':
    main()
