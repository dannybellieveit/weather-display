#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
NASA Earth Photo Display - Waveshare Triple LCD HAT
Shows NASA EPIC (Earth Polychromatic Imaging Camera) latest Earth photo

Main screen (1.3" 240x240): NASA's latest Earth photo
Left screen  (0.96" 160x80): Date & time of photo
Right screen (0.96" 160x80): Location info
"""

import os, sys, time, logging, urllib.request, json, subprocess
from io import BytesIO
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

UPDATE_SECONDS = 3600  # Update Earth photo every hour

# ── Fonts ─────────────────────────────────────────────────────────────────────
FONT_DIR = os.path.join(WAVESHARE_DIR, 'Font')

def f(size):
    try:    return ImageFont.truetype(os.path.join(FONT_DIR, 'Font00.ttf'), size)
    except: return ImageFont.load_default()


# ══════════════════════════════════════════════════════════════════════════════
#  NASA EPIC API
# ══════════════════════════════════════════════════════════════════════════════
def fetch_earth_photo():
    """
    Fetch latest Earth photo from NASA EPIC API
    Returns dict with image data and metadata
    """
    try:
        # Get list of recent images (no API key needed!)
        log.info("Fetching latest Earth photo from NASA EPIC...")
        api_url = "https://epic.gsfc.nasa.gov/api/natural"

        with urllib.request.urlopen(api_url, timeout=15) as response:
            images = json.loads(response.read())

        if not images:
            log.warning("No images available from NASA EPIC")
            return {'ok': False}

        # Get the most recent image
        latest = images[0]
        image_name = latest['image']
        date = latest['date']

        # Parse date for image URL (format: YYYY-MM-DD HH:MM:SS -> YYYY/MM/DD)
        date_parts = date.split(' ')[0].split('-')
        year, month, day = date_parts[0], date_parts[1], date_parts[2]

        # Construct image URL
        # Format: https://epic.gsfc.nasa.gov/archive/natural/YYYY/MM/DD/png/imagename.png
        image_url = f"https://epic.gsfc.nasa.gov/archive/natural/{year}/{month}/{day}/png/{image_name}.png"

        log.info(f"Downloading Earth photo: {image_name}")

        # Download the image
        with urllib.request.urlopen(image_url, timeout=30) as img_response:
            image_data = img_response.read()

        # Load image from bytes
        earth_img = Image.open(BytesIO(image_data))

        # Get coordinates
        coords = latest.get('centroid_coordinates', {})
        lat = coords.get('lat', 0)
        lon = coords.get('lon', 0)

        return {
            'ok': True,
            'image': earth_img,
            'date': date,
            'lat': round(lat, 1),
            'lon': round(lon, 1),
            'caption': latest.get('caption', 'Earth')
        }

    except Exception as e:
        log.error(f"Failed to fetch Earth photo: {e}")
        return {'ok': False}


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN SCREEN (240x240) - Earth Photo
# ══════════════════════════════════════════════════════════════════════════════
def render_main(earth_data):
    img = Image.new("RGB", (240, 240), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    if not earth_data['ok']:
        # Error state
        draw.text((60, 110), "No Earth", font=f(18), fill=(80, 80, 90))
        draw.text((50, 130), "photo available", font=f(14), fill=(60, 60, 70))
        return img

    # Resize Earth image to fit screen (240x240)
    earth_img = earth_data['image']
    earth_img = earth_img.resize((240, 240), Image.LANCZOS)

    return earth_img


# ══════════════════════════════════════════════════════════════════════════════
#  LEFT SCREEN (160x80) - Date & Time Info
# ══════════════════════════════════════════════════════════════════════════════
def render_left(earth_data):
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
#  RIGHT SCREEN (160x80) - Location Info
# ══════════════════════════════════════════════════════════════════════════════
def render_right(earth_data):
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

    earth_data = {'ok': False}
    last_fetch = 0

    log.info("NASA Earth Photo Display ready!")

    try:
        while True:
            now = time.time()

            # Fetch new Earth photo periodically
            if now - last_fetch >= UPDATE_SECONDS or last_fetch == 0:
                new_data = fetch_earth_photo()
                if new_data['ok']:
                    earth_data = new_data
                    log.info(f"Earth photo updated: {earth_data['date']}")
                last_fetch = now

            # Render screens
            disp_main.ShowImage(render_main(earth_data))
            disp_left.ShowImage(render_left(earth_data))
            disp_right.ShowImage(render_right(earth_data))

            time.sleep(60)  # Update display every minute

    except KeyboardInterrupt:
        log.info("Exiting...")
        for d in [disp_main, disp_left, disp_right]:
            d.clear()
            d.module_exit()

if __name__ == '__main__':
    main()
