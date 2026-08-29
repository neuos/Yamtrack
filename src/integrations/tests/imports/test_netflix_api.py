import json
from unittest.mock import patch

from django.test import TestCase

from integrations.imports import netflix_api
from integrations.imports.helpers import MediaImportError


def _browse_html(react_context):
    blob = json.dumps(react_context)
    return f"<html><body><script>netflix.reactContext = {blob};</script></body></html>"


FAKE_REACT_CONTEXT = {
    "models": {
        "serverDefs": {"data": {"BUILD_IDENTIFIER": "vabc123"}},
        "profilesList": {
            "profiles": {
                "p1": {
                    "summary": {
                        "guid": "guid-1",
                        "profileName": "Alex",
                        "isKids": False,
                    },
                },
                "p2": {
                    "summary": {
                        "guid": "guid-2",
                        "profileName": "Kids",
                        "isKids": True,
                    },
                },
            },
        },
    },
}
FAKE_BROWSE_HTML = _browse_html(FAKE_REACT_CONTEXT)

API_REQUEST_TARGET = "integrations.imports.netflix_api.services.api_request"


class DiscoverSessionTests(TestCase):
    """Test parsing the Netflix build id and profile list."""

    @patch(API_REQUEST_TARGET)
    def test_discover_session_parses_build_id_and_profiles(self, mock_api_request):
        """A valid /browse response yields the build id and every profile."""
        mock_api_request.return_value = FAKE_BROWSE_HTML

        session = netflix_api.discover_session("nid", "snid")

        self.assertEqual(session["build_id"], "vabc123")
        self.assertEqual(
            session["profiles"],
            [
                {"guid": "guid-1", "name": "Alex", "is_kids": False},
                {"guid": "guid-2", "name": "Kids", "is_kids": True},
            ],
        )
        self.assertIn("Cookie", mock_api_request.call_args.kwargs["headers"])
        self.assertIn(
            "NetflixId=nid",
            mock_api_request.call_args.kwargs["headers"]["Cookie"],
        )

    @patch(API_REQUEST_TARGET)
    def test_discover_session_rejects_unparseable_response(self, mock_api_request):
        """A response without the expected embedded JSON raises a clear error."""
        mock_api_request.return_value = "<html>login page</html>"

        with self.assertRaises(MediaImportError):
            netflix_api.discover_session("nid", "snid")

    @patch(API_REQUEST_TARGET)
    def test_discover_session_rejects_empty_profile_list(self, mock_api_request):
        """A parseable response with no profiles is still an error."""
        mock_api_request.return_value = (
            '<script>netflix.reactContext = {"models":{"serverDefs":'
            '{"data":{"BUILD_IDENTIFIER":"v1"}},"profilesList":{"profiles":{}}}};'
            "</script>"
        )

        with self.assertRaises(MediaImportError):
            netflix_api.discover_session("nid", "snid")


class GetViewingActivityTests(TestCase):
    """Test paginating the Shakti viewingactivity endpoint."""

    @patch(API_REQUEST_TARGET)
    def test_paginates_until_a_short_page(self, mock_api_request):
        """Fetching stops once a page comes back shorter than the page size."""
        full_page = [
            {"title": f"Item {i}"}
            for i in range(netflix_api.VIEWING_ACTIVITY_PAGE_SIZE)
        ]
        short_page = [{"title": "Last Item"}]
        mock_api_request.side_effect = [
            {"viewedItems": full_page},
            {"viewedItems": short_page},
        ]

        entries = netflix_api.get_viewing_activity("nid", "snid", "guid-1", "vabc123")

        self.assertEqual(len(entries), len(full_page) + len(short_page))
        self.assertEqual(mock_api_request.call_count, 2)

    @patch(API_REQUEST_TARGET)
    def test_stops_on_empty_page(self, mock_api_request):
        """An empty first page yields no entries and makes only one request."""
        mock_api_request.return_value = {"viewedItems": []}

        entries = netflix_api.get_viewing_activity("nid", "snid", "guid-1", "vabc123")

        self.assertEqual(entries, [])
        mock_api_request.assert_called_once()
