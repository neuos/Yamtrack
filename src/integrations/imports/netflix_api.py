"""Client for Netflix's unofficial "Shakti" viewing-activity API.

Netflix has no public API for viewing history. This module authenticates the
same way a logged-in browser tab does - with the `NetflixId`/`SecureNetflixId`
session cookies - and reads the same data Netflix's own web app reads: a
`BUILD_IDENTIFIER` and profile list embedded as JSON in the `/browse` page,
then the paginated `viewingactivity` endpoint keyed by that build id.

This is unofficial and undocumented. Netflix can change the page/response
shape at any time, which would surface here as a `MediaImportError` (parsing
failure) or as broken pagination, not a silent crash.
"""

import json
import logging
import re

from app.providers import services
from integrations.imports.helpers import MediaImportError

logger = logging.getLogger(__name__)

NETFLIX_BASE_URL = "https://www.netflix.com"
VIEWING_ACTIVITY_PAGE_SIZE = 20

_INVALID_SESSION_MSG = (
    "Invalid or expired Netflix session cookies. Reconnect your Netflix account."
)

_REACT_CONTEXT_RE = re.compile(
    r"netflix\.reactContext\s*=\s*(\{.*?\});\s*</script>",
    re.DOTALL,
)

# A browser-like User-Agent; Netflix serves different content to obvious
# non-browser clients (e.g. the default python-requests User-Agent).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _request_headers(netflix_id, secure_netflix_id):
    """Build browser-like request headers carrying the session cookies."""
    return {
        **_BROWSER_HEADERS,
        "Cookie": f"NetflixId={netflix_id}; SecureNetflixId={secure_netflix_id}",
    }


def _parse_react_context(html):
    """Extract the `netflix.reactContext` JSON blob embedded in a Netflix page."""
    match = _REACT_CONTEXT_RE.search(html)
    if not match:
        # Never log cookies or full page content (may include account details) -
        # just enough to tell a login-page bounce from a page-format mismatch.
        logger.warning(
            "Netflix /browse response (%s chars) had no reactContext blob. "
            "Contains 'reactContext': %s. Contains 'login': %s. Prefix: %r",
            len(html),
            "reactContext" in html,
            "login" in html.lower(),
            html[:200],
        )
        raise MediaImportError(_INVALID_SESSION_MSG)

    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.exception("Netflix reactContext blob was not valid JSON")
        raise MediaImportError(_INVALID_SESSION_MSG) from None


def discover_session(netflix_id, secure_netflix_id):
    """Validate Netflix session cookies and discover the build id + profiles.

    Args:
        netflix_id (str): Decrypted `NetflixId` cookie value.
        secure_netflix_id (str): Decrypted `SecureNetflixId` cookie value.

    Returns:
        dict: `{"build_id": str, "profiles": [{"guid", "name", "is_kids"}, ...]}`
    """
    html = services.api_request(
        "NETFLIX",
        "GET",
        f"{NETFLIX_BASE_URL}/browse",
        headers=_request_headers(netflix_id, secure_netflix_id),
        response_format="text",
    )
    context = _parse_react_context(html)

    try:
        build_id = context["models"]["serverDefs"]["data"]["BUILD_IDENTIFIER"]
        profiles_data = context["models"]["profilesList"]["profiles"]
    except KeyError as error:
        msg = (
            "Couldn't read Netflix account data. "
            "Netflix may have changed its page format."
        )
        raise MediaImportError(msg) from error

    profiles = [
        {
            "guid": profile["summary"]["guid"],
            "name": profile["summary"]["profileName"],
            "is_kids": profile["summary"].get("isKids", False),
        }
        for profile in profiles_data.values()
    ]

    if not profiles:
        msg = "No Netflix profiles found for this account."
        raise MediaImportError(msg)

    logger.info("Discovered %s Netflix profile(s)", len(profiles))
    return {"build_id": build_id, "profiles": profiles}


def get_viewing_activity(netflix_id, secure_netflix_id, profile_guid, build_id):
    """Fetch the full viewing activity for a Netflix profile, newest first."""
    headers = _request_headers(netflix_id, secure_netflix_id)
    entries = []
    page = 0

    while True:
        response = services.api_request(
            "NETFLIX",
            "GET",
            f"{NETFLIX_BASE_URL}/api/shakti/{build_id}/viewingactivity",
            params={
                "pg": page,
                "pgSize": VIEWING_ACTIVITY_PAGE_SIZE,
                "profileGuid": profile_guid,
            },
            headers=headers,
        )

        page_entries = response.get("viewedItems", [])
        if not page_entries:
            break

        entries.extend(page_entries)
        logger.info(
            "Retrieved page %s of Netflix viewing activity (%s items)",
            page,
            len(page_entries),
        )

        if len(page_entries) < VIEWING_ACTIVITY_PAGE_SIZE:
            break
        page += 1

    logger.info("Retrieved %s total Netflix viewing activity entries", len(entries))
    return entries
