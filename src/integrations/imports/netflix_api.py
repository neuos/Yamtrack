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


def _cookie_header(netflix_id, secure_netflix_id):
    """Build the Cookie header value from the two session cookie values."""
    return f"NetflixId={netflix_id}; SecureNetflixId={secure_netflix_id}"


def _parse_react_context(html):
    """Extract the `netflix.reactContext` JSON blob embedded in a Netflix page."""
    match = _REACT_CONTEXT_RE.search(html)
    if not match:
        raise MediaImportError(_INVALID_SESSION_MSG)

    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise MediaImportError(_INVALID_SESSION_MSG) from error


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
        headers={"Cookie": _cookie_header(netflix_id, secure_netflix_id)},
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
    headers = {"Cookie": _cookie_header(netflix_id, secure_netflix_id)}
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
