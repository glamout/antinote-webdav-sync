#!/bin/bash
set -e

INSTALL_DIR="$HOME/Library/Application Support/AntinoteWebDAVSync"
APP_BUNDLE="$HOME/Applications/AntinoteWebDAVSync.app"
PLIST_LABEL="io.antinote.webdav-sync"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Installing Antinote -> WebDAV Real-Time Mirror Service ==="
mkdir -p "$INSTALL_DIR/mirror_db"
mkdir -p "$HOME/Applications"
mkdir -p "$HOME/Library/LaunchAgents"

# 1. Stop any running or legacy instances
for LBL in "$PLIST_LABEL"; do
  launchctl bootout "gui/$(id -u)/${LBL}" 2>/dev/null || launchctl unload "$HOME/Library/LaunchAgents/${LBL}.plist" 2>/dev/null || true
  if [ "$LBL" != "$PLIST_LABEL" ]; then
    rm -f "$HOME/Library/LaunchAgents/${LBL}.plist"
  fi
done
pkill -f "antinote_webdav_sync.py" 2>/dev/null || true
pkill -f "AntinoteWebDAVSync" 2>/dev/null || true
rm -f "/tmp/antinote-webdav-sync.err" "/tmp/antinote-webdav-sync.log"
sleep 0.5

# 2. Install Python sync engine & uninstaller
cp "$SCRIPT_DIR/src/antinote_webdav_sync.py" "$INSTALL_DIR/antinote_webdav_sync.py"
chmod +x "$INSTALL_DIR/antinote_webdav_sync.py"
cp "$SCRIPT_DIR/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
chmod +x "$INSTALL_DIR/uninstall.sh"

# 3. Initialize or preserve config.json
if [ -f "$SCRIPT_DIR/config.json" ] && ! grep -q "your-webdav-server.com" "$SCRIPT_DIR/config.json"; then
  cp "$SCRIPT_DIR/config.json" "$INSTALL_DIR/config.json"
  chmod 600 "$INSTALL_DIR/config.json"
elif [ -f "$INSTALL_DIR/config.json" ] && ! grep -q "your-webdav-server.com" "$INSTALL_DIR/config.json"; then
  echo "Preserved existing configuration in $INSTALL_DIR/config.json"
elif [ -f "$SCRIPT_DIR/config.json" ]; then
  cp "$SCRIPT_DIR/config.json" "$INSTALL_DIR/config.json"
  chmod 600 "$INSTALL_DIR/config.json"
else
  cp "$SCRIPT_DIR/config.example.json" "$INSTALL_DIR/config.json"
  chmod 600 "$INSTALL_DIR/config.json"
  echo "⚠️  Created $INSTALL_DIR/config.json from config.example.json."
  echo "   Please edit $INSTALL_DIR/config.json with your WebDAV credentials."
fi

# Auto-detect Antinote DB path in Terminal context and persist into config.json
DETECTED_DB="$(/usr/bin/python3 "$INSTALL_DIR/antinote_webdav_sync.py" --detect-db || true)"
if [ -n "$DETECTED_DB" ]; then
  /usr/bin/python3 -c '
import json, sys, os
cfg_p, db_p = sys.argv[1], sys.argv[2]
if os.path.isfile(cfg_p):
    d = json.load(open(cfg_p, "r", encoding="utf-8"))
    if not d.get("antinote_db_path"):
        d["antinote_db_path"] = db_p
    d.setdefault("poll_interval_seconds", 2)
    json.dump(d, open(cfg_p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
' "$INSTALL_DIR/config.json" "$DETECTED_DB"
fi

# 4. Compile native C container watcher app bundle (AntinoteWebDAVSync.app)
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"

cat > "$APP_BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>AntinoteWebDAVSync</string>
  <key>CFBundleIdentifier</key>
  <string>io.antinote.AntinoteWebDAVSync</string>
  <key>CFBundleName</key>
  <string>AntinoteWebDAVSync</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>2.3.0</string>
  <key>CFBundleVersion</key>
  <string>2.3.0</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
EOF

clang -Wall -Wextra -O2 "$SCRIPT_DIR/src/daemon.c" -o "$APP_BUNDLE/Contents/MacOS/AntinoteWebDAVSync"
codesign --force --deep --sign - "$APP_BUNDLE" 2>/dev/null || true

# 5. Register macOS LaunchAgent
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-W</string>
    <string>-g</string>
    <string>-a</string>
    <string>${APP_BUNDLE}</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || launchctl load -w "$PLIST_PATH" || true
sleep 2

echo "✅ AntinoteWebDAVSync v2.3.0 installed!"
echo "   - Runtime directory: $INSTALL_DIR"
echo "   - App bundle:        $APP_BUNDLE"
echo "   - Config file:       $INSTALL_DIR/config.json"
echo "   - Log files:         /tmp/antinote-webdav-sync.log | /tmp/antinote-webdav-sync.err"
