#!/bin/bash
# Weather Display - Install Script
# https://github.com/dannybellieveit/weather-display

set -e

echo "╔════════════════════════════════════════╗"
echo "║     Weather Display - Installer        ║"
echo "╚════════════════════════════════════════╝"

REPO_DIR="$HOME/weather-display"
WAVESHARE_DIR="$HOME/Zero_LCD_HAT_A_Demo/python"

# Check if we're in the repo directory or need to clone
if [ ! -f "$REPO_DIR/weather.py" ]; then
    echo "→ Cloning repository..."
    git clone https://github.com/dannybellieveit/weather-display.git "$REPO_DIR"
fi

cd "$REPO_DIR"

# Check for Waveshare library
if [ ! -d "$WAVESHARE_DIR/lib" ]; then
    echo "→ Waveshare library not found, cloning..."
    cd "$HOME"
    git clone https://github.com/waveshare/Zero_LCD_HAT_A_Demo.git
    cd "$REPO_DIR"
fi

# Install Python dependencies
echo "→ Installing Python packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-pil python3-spidev

# Enable SPI if not already
if ! grep -q "^dtparam=spi=on" /boot/config.txt 2>/dev/null && \
   ! grep -q "^dtparam=spi=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "→ Enabling SPI..."
    echo "dtparam=spi=on" | sudo tee -a /boot/config.txt >/dev/null
    echo "  ⚠ SPI enabled - reboot required after install"
fi

# Create systemd service
echo "→ Creating systemd service..."
sudo tee /etc/systemd/system/weather.service > /dev/null << EOF
[Unit]
Description=Weather Display
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$REPO_DIR
EnvironmentFile=-$REPO_DIR/.env
ExecStart=/usr/bin/python3 $REPO_DIR/weather.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Create update script
echo "→ Creating update script..."
sudo tee /usr/local/bin/weather-update > /dev/null << EOF
#!/bin/bash
cd "$REPO_DIR"
git fetch -q origin main
LOCAL=\$(git rev-parse HEAD)
REMOTE=\$(git rev-parse origin/main)
if [ "\$LOCAL" != "\$REMOTE" ]; then
    git pull -q
    systemctl restart weather
    echo "\$(date): Updated weather display" >> /var/log/weather-update.log
fi
EOF
sudo chmod +x /usr/local/bin/weather-update

# Create systemd timer for auto-updates
echo "→ Setting up auto-updates..."
sudo tee /etc/systemd/system/weather-update.service > /dev/null << EOF
[Unit]
Description=Update Weather Display from GitHub

[Service]
Type=oneshot
ExecStart=/usr/local/bin/weather-update
EOF

sudo tee /etc/systemd/system/weather-update.timer > /dev/null << EOF
[Unit]
Description=Check for Weather Display updates every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF

# Enable and start services
echo "→ Enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable weather.service
sudo systemctl enable weather-update.timer
sudo systemctl start weather-update.timer
sudo systemctl restart weather.service

echo ""
echo "╔════════════════════════════════════════╗"
echo "║            ✓ Install Complete          ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "  Status:  sudo systemctl status weather"
echo "  Logs:    journalctl -u weather -f"
echo "  Restart: sudo systemctl restart weather"
echo ""
echo "  Auto-updates enabled (every 5 min)"
echo ""

# Check if reboot needed
if ! lsmod | grep -q spi_bcm2835; then
    echo "  ⚠ Please reboot to enable SPI: sudo reboot"
fi
