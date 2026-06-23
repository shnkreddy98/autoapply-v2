# Playwright MCP, but headed and visible over noVNC.
#
# The official image runs Chromium headless. We add a virtual display (Xvfb),
# a VNC server (x11vnc), a window manager (fluxbox), and a web VNC client
# (noVNC via websockify) so the browser can be watched at http://localhost:6080.
FROM mcr.microsoft.com/playwright/mcp

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc fluxbox novnc websockify procps \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 8931 = Playwright MCP (HTTP), 6080 = noVNC web viewer
EXPOSE 8931 6080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
