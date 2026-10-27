#!/bin/bash
set -e

INSTALL_DIR="$HOME/Library/Application Support/AntinoteWebDAVSync"
APP_BUNDLE="$HOME/Applications/AntinoteWebDAVSync.app"

echo "=== Uninstalling Antinote -> WebDAV Real-Time Mirror Service ==="
for LBL in "io.antinote.webdav-sync"; do
  launchctl bootout "gui/$(id -u)/${LBL}" 2>/dev/null || launchctl unload "$HOME/Library/LaunchAgents/${LBL}.plist" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/${LBL}.plist"
done

pkill -f "antinote_webdav_sync.py" 2>/dev/null || true
pkill -f "AntinoteWebDAVSync" 2>/dev/null || true
rm -rf "$INSTALL_DIR"
rm -rf "$APP_BUNDLE"
rm -f "/tmp/antinote-webdav-sync.log" "/tmp/antinote-webdav-sync.err"
echo "✅ Uninstalled cleanly."
