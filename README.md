# Weather Display

A minimal weather station using Waveshare Triple LCD HAT on Raspberry Pi Zero.

![Waveshare Triple LCD HAT](https://www.waveshare.com/wiki/images/a/a5/Triple_LCD_HAT_A.jpg)

## Displays

Two physical buttons: **KEY1** wakes the display from auto-dim, **KEY2** cycles between three pages:

1. **Weather**
   - Main (1.3" 240x240): current temperature (colour-coded), feels-like, condition + precipitation duration, UV index (🧴 shown when sunscreen is recommended, UV ≥ 3), time
   - Left (0.96" 160x80): humidity & wind
   - Right (0.96" 160x80): sunrise & sunset
2. **Earth photo** — the latest NASA EPIC natural-colour photo of Earth, rotating through the last 12 available images (one new download per hour), with capture date/time and centroid lat/lon on the side screens
3. **Moon phase** — current phase name and illumination %, computed locally (no network call), with next full/new moon dates on the side screens

Weather data comes from [Open-Meteo](https://open-meteo.com/) (free, no API key needed) using the UK Met Office (UKMO) model.

## Burn-in / power protection

- Auto-dims to 20% brightness after 2 minutes idle
- Backlight fully off overnight (00:00–07:00 by default, configurable)
- KEY1 wakes it back up

## Hardware

- Raspberry Pi Zero (original, single-core ARMv6) — the Waveshare demo library and install steps also work on Zero W/2W
- [Waveshare Triple LCD HAT (A)](https://www.waveshare.com/wiki/Zero_LCD_HAT_(A))

The two Waveshare display driver files this project actually uses are vendored in `vendor/waveshare/lib/` (tracked in git), so a normal clone already has everything needed — `install.sh` only falls back to downloading Waveshare's zip if that's somehow missing.

## Quick Install

```bash
curl -sSL https://raw.githubusercontent.com/dannybellieveit/weather-display/main/install.sh | bash
```

Or manually:

```bash
cd ~
git clone https://github.com/dannybellieveit/weather-display.git
cd weather-display
./install.sh
```

## Configuration

Copy `config.example.json` to `config.json` (in the same directory as `weather.py`) and edit it — this file is gitignored, so local changes survive the auto-updater's `git reset --hard`, unlike editing `weather.py` directly.

```json
{
  "lat": 51.4279,
  "lon": -0.1255,
  "city": "Streatham",
  "bl_main_duty": 90,
  "bl_side_duty": 45,
  "update_seconds": 300,
  "temp_x": 90,
  "temp_y": 40,
  "dim_timeout": 120,
  "night_start_hour": 0,
  "night_end_hour": 7
}
```

Any field you omit falls back to the default shown above. Restart the service after editing (`sudo systemctl restart weather`).

## Auto-Update

The install script sets up automatic updates. Every 5 minutes, the Pi checks for changes and restarts the service if needed. Two safety nets protect against a bad push: the new `weather.py` must compile, and the service must stay running for 60 seconds after restart — either failing rolls back to the previous commit automatically. See `/var/log/weather-update.log` for the history.

To disable auto-updates:
```bash
sudo systemctl disable weather-update.timer
```

## Manual Control

```bash
# Check status
sudo systemctl status weather

# Restart
sudo systemctl restart weather

# View logs
journalctl -u weather -f

# Stop
sudo systemctl stop weather
```

## API

Weather data from [Open-Meteo](https://open-meteo.com/) (free, no API key needed), using the UK Met Office (UKMO) model. Earth photos from [NASA EPIC](https://epic.gsfc.nasa.gov/).
