"""
notify.py
---------
POST a finished run's summary to a URL, for a scraper that runs unattended.

THREE PROPERTIES, and each exists because the obvious version gets it wrong.

**It cannot fail the run.** The scrape is the work; telling somebody about it
is not. A webhook that raises turns a successful 3,000-product run into exit
1, and an unreachable notification endpoint is a normal Tuesday. Every error
is caught and logged as a warning, and the run's own exit code is returned
untouched.

**It cannot leak the URL.** A webhook URL is usually a credential: Slack,
Discord and most alerting services put the secret in the path
(`hooks.slack.com/services/T000/B000/XXXXXXXX`), not in a header. So it is
never logged, not even on failure — only its scheme and host are, which is
what a person needs to know ("the Slack one failed") and is not the secret.
This is the same rule the proxy pool follows and for the same reason, and it
matters more here: `requests` puts the FULL URL, query string included, into
the text of HTTPError and of every connection error, so the naive
`logger.warning("%s", e)` publishes the token the moment anything goes wrong.

**It is bounded.** A hung endpoint must not hold the process open after the
data is written. Ten seconds, once — no retries: a notification that arrives
three minutes late is not worth a run that cannot exit, and the sidecar on
disk is the durable record either way.

IT FIRES ON FAILURE TOO, which is the main use. `finish_run` deliberately
writes NO sidecar for a run that gathered nothing, so a webhook keyed on the
sidecar would be silent for exactly the runs somebody wants to hear about.
The payload is built from the same fields regardless.
"""

import json
import logging
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

TIMEOUT_S = 10


def describe(url: str) -> str:
    """A webhook URL with everything secret removed.

    Scheme and host only: enough to say WHICH endpoint, never enough to post
    to it. Most webhook URLs carry their token in the path.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.scheme or not parts.netloc:
        return "<malformed url>"
    host = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme}://{host}/…"


def payload(meta: dict, exit_code: int) -> dict:
    """What gets posted: the run metadata plus the exit code.

    The exit code is included because it is the field an alerting rule
    actually branches on, and it is not otherwise in the sidecar — `status`
    and `stop_reason` describe the run, while the exit code is the contract a
    pipeline was written against.
    """
    return dict(meta, exit_code=exit_code)


def send(url: Optional[str], meta: dict, exit_code: int) -> bool:
    """POST the summary. Returns whether it was delivered; never raises."""
    if not url:
        return False
    # Imported here rather than at module scope: this module is imported by
    # every engine, and a notification feature should not make `requests` a
    # hard import for a run that does not use it. (requests IS in
    # requirements.txt, so this is about honesty of dependency, not about a
    # missing package.)
    import requests

    where = describe(url)
    try:
        resp = requests.post(
            url,
            data=json.dumps(payload(meta, exit_code)).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001 — see the module docstring
        # `e` is NOT interpolated: requests puts the full URL into the text of
        # connection errors and of HTTPError, so printing it here would
        # publish the token. The exception TYPE is the useful half and carries
        # no secret.
        logger.warning("Webhook to %s failed (%s) — the run's own result is "
                       "unaffected and %s is on disk.",
                       where, type(e).__name__, "the output")
        return False

    if resp.status_code >= 400:
        logger.warning("Webhook to %s returned HTTP %d — the run's own result "
                       "is unaffected.", where, resp.status_code)
        return False
    logger.info("Webhook delivered to %s (HTTP %d).", where, resp.status_code)
    return True
