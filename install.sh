#!/bin/bash
# ═══════════════════════════════════════════
#  Seedbox Web Dashboard — Installer
#  Run once on the laptop as user amit
# ═══════════════════════════════════════════
set -e

INSTALL_DIR="$HOME/seedbox-web"
SERVICE_FILE="seedbox-web.service"

echo ""
echo "  🌊 Seedbox Web Dashboard Installer"
echo "  ──────────────────────────────────"
echo ""

# ── 1. Copy files ──────────────────────────
echo "  ▶ Copying files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR/templates"

copy_if_needed() {
    local src="$1"
    local dest="$2"
    if [ "$(readlink -f "$src")" = "$(readlink -f "$dest")" ]; then
        return 0
    fi
    cp "$src" "$dest"
}

copy_tree_if_needed() {
    local src="$1"
    local dest="$2"
    if [ "$(readlink -f "$src")" = "$(readlink -f "$dest")" ]; then
        return 0
    fi
    rm -rf "$dest"
    cp -r "$src" "$dest"
}

copy_if_needed app.py "$INSTALL_DIR/app.py"
copy_if_needed seedbox-root-helper "$INSTALL_DIR/seedbox-root-helper"
copy_if_needed requirements.txt "$INSTALL_DIR/requirements.txt"
copy_tree_if_needed templates "$INSTALL_DIR/templates"
chmod +x "$INSTALL_DIR/seedbox-root-helper"
echo "  ✅ Files copied"

# ── 2. Python venv ─────────────────────────
echo "  ▶ Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r requirements.txt
echo "  ✅ Virtual environment ready"

# ── 3. Systemd service ─────────────────────
echo "  ▶ Installing systemd service..."
sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo sed -i "s|/home/amit|$HOME|g" /etc/systemd/system/seedbox-web.service
sudo sed -i "s|User=amit|User=$USER|g" /etc/systemd/system/seedbox-web.service
sudo cp "$INSTALL_DIR/seedbox-root-helper" /usr/local/bin/seedbox-root-helper
sudo sed -i "s|__SEEDBOX_HOME__|$HOME|g" /usr/local/bin/seedbox-root-helper
sudo chmod 755 /usr/local/bin/seedbox-root-helper
echo "$USER ALL=(root) NOPASSWD: /usr/local/bin/seedbox-root-helper *" | sudo tee /etc/sudoers.d/seedbox-web >/dev/null
sudo chmod 440 /etc/sudoers.d/seedbox-web
sudo visudo -cf /etc/sudoers.d/seedbox-web
sudo systemctl daemon-reload
sudo systemctl enable seedbox-web
sudo systemctl restart seedbox-web
echo "  ✅ Service installed and started"

# ── 4. UFW firewall ────────────────────────
if command -v ufw &>/dev/null; then
    echo "  ▶ Opening port 5000 in firewall..."
    sudo ufw allow 5000/tcp &>/dev/null
    echo "  ✅ Port 5000 allowed"
fi

# ── Done ───────────────────────────────────
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "  ╔═══════════════════════════════════════════╗"
echo "  ║   ✅  Dashboard installed successfully!   ║"
echo "  ╠═══════════════════════════════════════════╣"
echo "  ║                                           ║"
echo "  ║  Open in browser:                         ║"
echo "  ║  → http://$LOCAL_IP:5000                  ║"
echo "  ║                                           ║"
echo "  ║  Check status:                            ║"
echo "  ║  → systemctl status seedbox-web           ║"
echo "  ║                                           ║"
echo "  ║  View logs:                               ║"
echo "  ║  → journalctl -u seedbox-web -f           ║"
echo "  ║                                           ║"
echo "  ╚═══════════════════════════════════════════╝"
echo ""
