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

# Chrome's own prompts are browser UI, out of Playwright's reach: "Save password?"
# opened on every portal sign-in, and Chrome's address autofill could put stale values
# into fields before det reads them. Managed policies switch them off. Chrome for
# Testing reads this directory (found in its binary), not /etc/opt/chrome.
COPY chrome-policies.json /etc/opt/chrome_for_testing/policies/managed/autoapply.json

# 8931 = Playwright MCP (HTTP), 6080 = noVNC web viewer
EXPOSE 8931 6080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
