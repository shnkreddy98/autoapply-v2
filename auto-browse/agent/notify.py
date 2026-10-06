"""Push notifications for runs that need a person: a captcha, a code that is not in
the inbox, a sign-in that failed. Sent through ntfy (https://ntfy.sh): the phone app
subscribes to a topic, nothing to sign up for.

    NTFY_TOPIC       the topic, e.g. autoapply-3f9c1e...  (unset: pushes are skipped)
    NTFY_SERVER      default https://ntfy.sh (self-hosted ntfy works the same)
    NOTIFY_VNC_URL   what tapping the push opens; default http://localhost:6080/vnc.html,
                     set it to this machine's LAN / Tailscale address for a phone

Anyone who knows the topic can read it, so a push carries the job, the reason and
the VNC link — never a code, a password or profile data. A failed push never stops a
run: it is logged and the run waits as it would without one.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def vnc_url() -> str:
    return os.getenv("NOTIFY_VNC_URL", "http://localhost:6080/vnc.html")


def _header(text: str) -> str:
    # HTTP header values are latin-1; ntfy titles are short
    return text.encode("latin-1", "replace").decode("latin-1")[:180]


def push(title: str, message: str, *, click: str | None = None, priority: str = "high",
         tags: str = "warning") -> bool:
    """Send one push; True when ntfy accepted it, False when skipped or failed."""
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": _header(title), "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    try:
        resp = httpx.post(f"{server}/{topic}", content=message.encode("utf-8"), headers=headers, timeout=10)
        if not resp.is_success:
            log.warning("ntfy push refused: %s %s", resp.status_code, resp.text[:120])
        return resp.is_success
    except Exception as exc:
        log.warning("ntfy push failed: %s", exc)
        return False
