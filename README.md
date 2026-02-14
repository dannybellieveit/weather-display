# Weather Display

A minimal weather station using Waveshare Triple LCD HAT on Raspberry Pi Zero.

![Waveshare Triple LCD HAT](https://www.waveshare.com/wiki/images/a/a5/Triple_LCD_HAT_A.jpg)

## Displays

- **Main (1.3" 240x240)**: Temperature, conditions, high/low
- **Left (0.96" 160x80)**: Humidity & wind
- **Right (0.96" 160x80)**: Sunrise & sunset

## Hardware

- Raspberry Pi Zero W/2W
- [Waveshare Triple LCD HAT (A)](https://www.waveshare.com/wiki/Triple_LCD_HAT_(A))

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

Edit `weather.py` to change:

```python
LAT, LON, CITY = 51.4279, -0.1255, "Streatham"  # Your location
BL_MAIN_DUTY = 90   # Main screen brightness (0-100)
BL_SIDE_DUTY = 45   # Side screen brightness (0-100)
UPDATE_SECONDS = 300  # Weather refresh interval
```

## Auto-Update

The install script sets up automatic updates. Every 5 minutes, the Pi checks for changes and restarts the service if needed.

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

Weather data from [Open-Meteo](https://open-meteo.com/) (free, no API key needed).
