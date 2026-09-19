#!/bin/bash
# Weather Display - Improved Install Script
# https://github.com/dannybellieveit/weather-display

set -e

echo "╔════════════════════════════════════════╗"
echo "║     Weather Display - Installer        ║"
echo "╚════════════════════════════════════════╝"

REPO_DIR="$HOME/weather-display"
ACTUAL_USER="${SUDO_USER:-$USER}"
ACTUAL_HOME=$(eval echo ~$ACTUAL_USER)

# Check if we're in the repo directory or need to clone
if [ ! -f "$REPO_DIR/weather.py" ]; then
    echo "→ Cloning repository..."
    git clone https://github.com/dannybellieveit/weather-display.git "$REPO_DIR"
fi

cd "$REPO_DIR"

# The two Waveshare driver modules this project actually uses are vendored
# in vendor/waveshare/lib/ (tracked in git), so a normal clone already has
# them — weather.py prefers that path automatically. Only fall back to
# downloading Waveshare's zip if vendoring is somehow missing.
if [ ! -d "$REPO_DIR/vendor/waveshare/lib" ]; then
    echo "→ Vendored Waveshare library missing, downloading as a fallback..."
    WAVESHARE_DIR="$HOME/Zero_LCD_HAT_A_Demo/python"
    if [ ! -d "$WAVESHARE_DIR/lib" ]; then
        sudo apt-get update -qq
        sudo apt-get install -y -qq unzip
        cd "$HOME"
        wget -q https://files.waveshare.com/wiki/Zero-LCD-HAT-A/Zero_LCD_HAT_A_Demo.zip
        unzip -q -o Zero_LCD_HAT_A_Demo.zip
        rm -f Zero_LCD_HAT_A_Demo.zip
        cd "$REPO_DIR"
    fi
fi

# Install Python dependencies
echo "→ Installing Python packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq git python3-pip python3-pil python3-spidev python3-numpy python3-requests python3-rpi.gpio python3-gpiozero python3-lgpio

# Enable SPI + the extra overlays the triple-screen setup needs
BOOTCONFIG="/boot/config.txt"
[ -f /boot/firmware/config.txt ] && BOOTCONFIG="/boot/firmware/config.txt"
for LINE in "dtparam=spi=on" "dtoverlay=spi1-1cs" "dtoverlay=spi0-2cs"; do
    if ! grep -q "^${LINE}$" "$BOOTCONFIG" 2>/dev/null; then
        echo "→ Adding $LINE..."
        echo "$LINE" | sudo tee -a "$BOOTCONFIG" >/dev/null
        NEEDS_REBOOT=1
    fi
done
[ -n "$NEEDS_REBOOT" ] && echo "  ⚠ Boot config changed - reboot required after install"

# Create systemd service (run as actual user, not root)
echo "→ Creating systemd service..."
sudo tee /etc/systemd/system/weather.service > /dev/null << EOF
[Unit]
Description=Weather Display
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$ACTUAL_HOME/weather-display
Environment="HOME=$ACTUAL_HOME"
# GPIO19 (main-screen backlight) is claimed by the spi1-1cs overlay; only
# RPi.GPIO's direct-register writes can toggle it, lgpio fails on it. Do
# not remove this — the two buttons opt into an explicit lgpio factory
# locally in weather.py instead, so both needs are met simultaneously.
Environment="GPIOZERO_PIN_FACTORY=RPiGPIO"
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
# Pulls changes from GitHub and restarts service if updated.
# Two safety nets before trusting a new push:
#   1. The new weather.py must at least compile.
#   2. The service must stay running for 60s after restart.
# Either failing rolls back to the previous commit automatically.

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
    PREV_COMMIT="\$LOCAL"

    if ! git reset --hard origin/main >> "\$LOG_FILE" 2>&1; then
        echo "\$(date): ERROR - Failed to pull changes" >> "\$LOG_FILE"
        exit 1
    fi
    echo "\$(date): Successfully pulled changes" >> "\$LOG_FILE"

    # Safety net 1: does the new weather.py even parse?
    if ! python3 -m py_compile weather.py >> "\$LOG_FILE" 2>&1; then
        echo "\$(date): ERROR - New weather.py failed to compile, rolling back to \$PREV_COMMIT" >> "\$LOG_FILE"
        git reset --hard "\$PREV_COMMIT" >> "\$LOG_FILE" 2>&1
        exit 1
    fi

    if ! systemctl restart weather >> "\$LOG_FILE" 2>&1; then
        echo "\$(date): ERROR - Failed to restart service, rolling back to \$PREV_COMMIT" >> "\$LOG_FILE"
        git reset --hard "\$PREV_COMMIT" >> "\$LOG_FILE" 2>&1
        systemctl restart weather >> "\$LOG_FILE" 2>&1
        exit 1
    fi
    echo "\$(date): Service restarted, watching for a crash loop..." >> "\$LOG_FILE"

    # Safety net 2: a push that compiles but crashes at runtime would
    # otherwise sit broken until someone noticed. Give it 60s to prove
    # it's actually staying up before trusting the update.
    sleep 60
    if ! systemctl is-active --quiet weather; then
        echo "\$(date): ERROR - Service crashed within 60s of update, rolling back to \$PREV_COMMIT" >> "\$LOG_FILE"
        git reset --hard "\$PREV_COMMIT" >> "\$LOG_FILE" 2>&1
        systemctl restart weather >> "\$LOG_FILE" 2>&1
    else
        echo "\$(date): Service stayed up, update confirmed good" >> "\$LOG_FILE"
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
