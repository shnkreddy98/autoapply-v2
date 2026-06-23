#!/bin/bash
# Start the virtual display + VNC stack, then the headed Playwright MCP.
set -e

export DISPLAY=:99

# Virtual framebuffer the headed browser renders into.
Xvfb :99 -screen 0 1280x900x24 -ac &
sleep 1

# Minimal window manager so the browser window has a frame.
fluxbox >/dev/null 2>&1 &

# Expose the display over VNC (no password — local/dev use only).
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -bg -quiet

# Web VNC client at http://localhost:6080/vnc.html
websockify --web /usr/share/novnc 6080 localhost:5900 &

# Playwright MCP is HEADED by default (omit --headless), so it renders to :99.
# --allowed-hosts '*' disables the host check so the proxy can reach us by the
# compose service name 'playwright' (local/dev use only).
# --output-mode stdout inlines the accessibility snapshot in the tool result
# (instead of writing a .yml file and returning a link) so the proxy can prune it.
# --allow-unrestricted-file-access lets browser_file_upload read /resume/*.pdf.
exec node /app/cli.js --browser chromium --no-sandbox \
  --port 8931 --host 0.0.0.0 --allowed-hosts '*' --output-mode stdout \
  --allow-unrestricted-file-access
