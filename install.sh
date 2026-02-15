#!/bin/bash
# Weather Display - Improved Install Script
# https://github.com/dannybellieveit/weather-display

set -e

echo "╔════════════════════════════════════════╗"
echo "║     Weather Display - Installer        ║"
echo "╚════════════════════════════════════════╝"

REPO_DIR="$HOME/weather-display"
WAVESHARE_DIR="$HOME/Zero_LCD_HAT_A_Demo/python"
ACTUAL_USER="${SUDO_USER:-$USER}"
ACTUAL_HOME=$(eval echo ~$ACTUAL_USER)

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

# Create systemd service (run as actual user, not root)
echo "→ Creating systemd service..."
sudo tee /etc/systemd/system/weather.service > /dev/null << EOF
[Unit]
Description=Weather Display
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$ACTUAL_HOME/weather-display
Environment="HOME=$ACTUAL_HOME"
ExecStart=/usr/bin/python3 $ACTUAL_HOME/weather-display/weather.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Create improved update script
echo "→ Creating update script..."
sudo tee /usr/local/bin/weather-update > /dev/null << EOF
#!/bin/bash
# Weather Display Auto-Update Script
# Pulls changes from GitHub and restarts service if updated

REPO_DIR="$ACTUAL_HOME/weather-display"
LOG_FILE="/var/log/weather-update.log"

cd "\$REPO_DIR" || {
    echo "\$(date): ERROR - Could not cd to \$REPO_DIR" >> "\$LOG_FILE"
    exit 1
}

# Fetch latest changes
if ! git fetch -q origin main 2>&1; then
    echo "\$(date): ERROR - Failed to fetch from GitHub" >> "\$LOG_FILE"
    exit 1
fi

# Compare local and remote
LOCAL=\$(git rev-parse HEAD)
REMOTE=\$(git rev-parse origin/main)

if [ "\$LOCAL" != "\$REMOTE" ]; then
    echo "\$(date): Update available (\$LOCAL -> \$REMOTE)" >> "\$LOG_FILE"
    
    # Pull changes (reset --hard to avoid conflicts)
    if git reset --hard origin/main 2>&1; then
        echo "\$(date): Successfully pulled changes" >> "\$LOG_FILE"
        
        # Restart service
        if systemctl restart weather 2>&1; then
            echo "\$(date): Service restarted successfully" >> "\$LOG_FILE"
        else
            echo "\$(date): ERROR - Failed to restart service" >> "\$LOG_FILE"
            exit 1
        fi
    else
        echo "\$(date): ERROR - Failed to pull changes" >> "\$LOG_FILE"
        exit 1
    fi
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
echo "  Update:  sudo /usr/local/bin/weather-update"
echo ""
echo "  Auto-updates enabled (every 5 min)"
echo ""

# Check if reboot needed
if ! lsmod | grep -q spi_bcm2835; then
    echo "  ⚠ Please reboot to enable SPI: sudo reboot"
fi
