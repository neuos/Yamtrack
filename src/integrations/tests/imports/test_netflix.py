from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import TV, Episode, MediaTypes, Movie, Season, Status
from integrations.imports import helpers, netflix

mock_path = Path(__file__).resolve().parent.parent / "mock_data"

METADATA_PATCH_TARGET = (
    "integrations.imports.tmdb_watch_history.services.get_media_metadata"
)
SEARCH_PATCH_TARGET = "integrations.imports.tmdb_watch_history.services.search"
DISCOVER_SESSION_TARGET = "integrations.imports.netflix.netflix_api.discover_session"
GET_ACTIVITY_TARGET = "integrations.imports.netflix.netflix_api.get_viewing_activity"


class ImportNetflix(TestCase):
    """Test importing media from Netflix viewing history CSV exports."""

    def setUp(self):
        """Create a test user for each import test."""
        self.user = get_user_model().objects.create_user(
            username="netflix-import-user",
        )

    @patch(METADATA_PATCH_TARGET)
    @patch(SEARCH_PATCH_TARGET)
    def test_import_netflix_csv(self, mock_search, mock_get_media_metadata):  # noqa: C901
        """Import a movie, a TV episode, and a movie misdetected as a show."""

        def fake_search(media_type, query, _page, _source=None):
            if media_type == MediaTypes.MOVIE.value and query == "Tenet":
                return {"results": [{"media_id": "100", "title": "Tenet", "image": ""}]}
            if media_type == MediaTypes.TV.value and query == "Stranger Things":
                return {
                    "results": [
                        {"media_id": "200", "title": "Stranger Things", "image": ""},
                    ],
                }
            if media_type == MediaTypes.TV.value and query == "Spenser Confidential":
                # Not a real show - the TV search should come up empty here.
                return {"results": []}
            if media_type == MediaTypes.MOVIE.value and query == "Spenser Confidential":
                return {
                    "results": [
                        {
                            "media_id": "300",
                            "title": "Spenser Confidential",
                            "image": "",
                        },
                    ],
                }
            return {"results": []}

        def fake_get_media_metadata(
            media_type,
            media_id,
            _source,
            season_numbers=None,
            episode_number=None,  # noqa: ARG001
        ):
            if media_type == MediaTypes.MOVIE.value and media_id == "100":
                return {"title": "Tenet", "image": "https://example.com/tenet.jpg"}
            if media_type == MediaTypes.MOVIE.value and media_id == "300":
                return {
                    "title": "Spenser Confidential",
                    "image": "https://example.com/spenser.jpg",
                }
            if media_type == MediaTypes.TV.value:
                return {
                    "title": "Stranger Things",
                    "image": "https://example.com/st.jpg",
                    "max_progress": 9,
                    "related": {"seasons": [{"season_number": 4}]},
                }
            if media_type == MediaTypes.SEASON.value:
                season_number = season_numbers[0]
                return {
                    "title": f"Season {season_number}",
                    "image": "https://example.com/season.jpg",
                    "max_progress": 9,
                    "episodes": [
                        {"episode_number": 1, "name": "Chapter One"},
                        {"episode_number": 2, "name": "Chapter Two"},
                    ],
                }
            if media_type == MediaTypes.EPISODE.value:
                return {
                    "title": "Stranger Things",
                    "image": "https://example.com/episode.jpg",
                }
            return None

        mock_search.side_effect = fake_search
        mock_get_media_metadata.side_effect = fake_get_media_metadata

        with Path(mock_path / "import_netflix.csv").open("rb") as file:
            imported_counts, warnings = netflix.importer(file, self.user, "new")

        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 2)
        self.assertEqual(imported_counts[MediaTypes.TV.value], 1)
        self.assertEqual(imported_counts[MediaTypes.SEASON.value], 1)
        self.assertEqual(imported_counts[MediaTypes.EPISODE.value], 1)
        self.assertIsNone(warnings)

        tenet = Movie.objects.get(user=self.user, item__media_id="100")
        self.assertEqual(tenet.status, Status.COMPLETED.value)
        self.assertEqual(tenet.progress, 1)

        # "Spenser Confidential: Part 2" looks like a show ("Part 2" matches the
        # season/episode heuristic) but only matches TMDB as a movie.
        spenser = Movie.objects.get(user=self.user, item__media_id="300")
        self.assertEqual(spenser.status, Status.COMPLETED.value)

        tv = TV.objects.get(user=self.user, item__media_id="200")
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

        season = Season.objects.get(
            user=self.user,
            item__media_id="200",
            item__season_number=4,
        )
        self.assertEqual(season.status, Status.IN_PROGRESS.value)

        episode = Episode.objects.get(
            related_season__user=self.user,
            item__media_id="200",
            item__season_number=4,
            item__episode_number=1,
        )
        self.assertIsNotNone(episode.end_date)

    def test_extract_show_name(self):
        """Titles are classified as TV or movie based on colon-segment shape."""
        importer_instance = netflix.NetflixImporter(None, self.user, "new")

        self.assertEqual(
            importer_instance._extract_show_name(
                "Stranger Things: Stranger Things 4: Chapter One",
            ),
            ("Stranger Things", True),
        )
        self.assertEqual(
            importer_instance._extract_show_name("Spenser Confidential: Part 2"),
            ("Spenser Confidential", True),
        )
        self.assertEqual(
            importer_instance._extract_show_name("Spider-Man: Homecoming"),
            ("Spider-Man: Homecoming", False),
        )
        self.assertEqual(
            importer_instance._extract_show_name("Tenet"),
            ("Tenet", False),
        )

    def test_parse_date(self):
        """Netflix dates are parsed in both M/D/YY and M/D/YYYY formats."""
        importer_instance = netflix.NetflixImporter(None, self.user, "new")

        self.assertEqual(importer_instance._parse_date("1/2/24").year, 2024)
        self.assertEqual(importer_instance._parse_date("1/2/2024").year, 2024)
        self.assertIsNone(importer_instance._parse_date(""))
        self.assertIsNone(importer_instance._parse_date("not-a-date"))


class ImportNetflixAPI(TestCase):
    """Test importing media from a live Netflix account via the Shakti API."""

    def setUp(self):
        """Create a test user for each import test."""
        self.user = get_user_model().objects.create_user(
            username="netflix-api-import-user",
        )

    def test_entry_to_row_prefers_datestr(self):
        """A raw entry with dateStr is used as-is."""
        importer_instance = netflix.NetflixAPIImporter(
            self.user,
            "new",
            helpers.encrypt("nid"),
            helpers.encrypt("snid"),
            "guid-1",
        )

        row = importer_instance._entry_to_row({"title": "Tenet", "dateStr": "1/2/24"})

        self.assertEqual(row, {"Title": "Tenet", "Date": "1/2/24"})

    def test_entry_to_row_formats_epoch_seconds_and_millis(self):
        """A raw entry with only a unix epoch is formatted to M/D/YY."""
        importer_instance = netflix.NetflixAPIImporter(
            self.user,
            "new",
            helpers.encrypt("nid"),
            helpers.encrypt("snid"),
            "guid-1",
        )

        seconds_row = importer_instance._entry_to_row(
            {"title": "Tenet", "date": 1704171600},
        )
        millis_row = importer_instance._entry_to_row(
            {"title": "Tenet", "date": 1704171600000},
        )

        self.assertEqual(seconds_row["Date"], millis_row["Date"])

    @patch(METADATA_PATCH_TARGET)
    @patch(SEARCH_PATCH_TARGET)
    @patch(GET_ACTIVITY_TARGET)
    @patch(DISCOVER_SESSION_TARGET)
    def test_import_netflix_api(
        self,
        mock_discover_session,
        mock_get_activity,
        mock_search,
        mock_get_media_metadata,
    ):
        """A live-account import normalizes API entries the same way as CSV rows."""
        mock_discover_session.return_value = {"build_id": "vabc123", "profiles": []}
        mock_get_activity.return_value = [{"title": "Tenet", "dateStr": "1/2/24"}]
        mock_search.return_value = {
            "results": [{"media_id": "100", "title": "Tenet", "image": ""}],
        }
        mock_get_media_metadata.return_value = {
            "title": "Tenet",
            "image": "https://example.com/tenet.jpg",
        }

        imported_counts, warnings = netflix.importer(
            None,
            self.user,
            "new",
            netflix_id=helpers.encrypt("nid"),
            secure_netflix_id=helpers.encrypt("snid"),
            profile_guid="guid-1",
        )

        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 1)
        self.assertIsNone(warnings)
        mock_discover_session.assert_called_once_with("nid", "snid")
        mock_get_activity.assert_called_once_with("nid", "snid", "guid-1", "vabc123")

        tenet = Movie.objects.get(user=self.user, item__media_id="100")
        self.assertEqual(tenet.status, Status.COMPLETED.value)
