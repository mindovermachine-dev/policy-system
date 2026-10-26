"""The page a browser lands on after passkey enrolment, `GET /enrolled`, of the REST API.

Authentik's enrolment flows (passkey recovery and invite enrolment) end on a static redirect
stage whose relative target is `ENROLMENT_DONE_PATH`; Authentik and PS Service share one public
host, so the browser lands here. The page is a fixed HTML document: the handler takes no
parameters, so no query, header or cookie value can reach it, and it shows no user data. The
route is on the REST bearer middleware's open list, declared a public route, and exempt from the
startup replay gate (the page reads no graph). The handler writes no log entry: there is no
decision, state or failure to record, and Authentik's own `login` event is the evidence that
enrolment finished.
"""

from __future__ import annotations

from typing import Final

from fastapi.responses import HTMLResponse

ENROLMENT_DONE_PATH: Final = "/enrolled"

# The page loads nothing, so the policy denies every fetch, frame, base URL and form target; the
# browser must not sniff another content type; and no referrer leaves the page.
_DONE_PAGE_HEADERS: Final = {
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_DONE_PAGE_HTML: Final = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Passkey registered - PS Service</title>
</head>
<body>
<h1>Passkey registered</h1>
<p>Your passkey is registered and you are signed in. You can close this tab and run
<code>ps-cli auth login</code>.</p>
</body>
</html>
"""


async def get_enrolment_done_page() -> HTMLResponse:
    """Serve the fixed page that tells the user their passkey enrolment finished.

    Purpose: give the browser a final page that reads as success and offers no link, after an
    Authentik enrolment flow redirects here.

    Returns:
        An HTML response with the fixed page and the headers `Content-Security-Policy` (deny
        all), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. It is
        always HTTP 200 and byte-identical for every request.

    Side Effects:
        None.
    """
    return HTMLResponse(content=_DONE_PAGE_HTML, headers=_DONE_PAGE_HEADERS)
