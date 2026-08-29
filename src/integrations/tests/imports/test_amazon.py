from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import TV, Episode, MediaTypes, Movie, Season, Status
from integrations.imports import amazon

mock_path = Path(__file__).resolve().parent.parent / "mock_data"

METADATA_PATCH_TARGET = (
    "integrations.imports.tmdb_watch_history.services.get_media_metadata"
)
SEARCH_PATCH_TARGET = "integrations.imports.tmdb_watch_history.services.search"


class ImportAmazonPrime(TestCase):
    """Test importing media from Amazon Prime CSV exports."""

    def setUp(self):
        """Create a test user for each import test."""
        self.user = get_user_model().objects.create_user(
            username="amazon-import-user",
        )

    @patch(METADATA_PATCH_TARGET)
    @patch(SEARCH_PATCH_TARGET)
    def test_import_amazon_prime_csv(self, mock_search, mock_get_media_metadata):
        """Import movie plays and a TV episode from an Amazon Prime CSV export."""

        def fake_search(media_type, query, _page, _source=None):
            if media_type == MediaTypes.MOVIE.value and query == "Tenet":
                return {
                    "results": [
                        {"media_id": "100", "title": "Tenet", "image": ""},
                    ],
                }

            if media_type == MediaTypes.TV.value and query == "The Boys":
                return {
                    "results": [
                        {"media_id": "200", "title": "The Boys", "image": ""},
                    ],
                }

            return {"results": []}

        def fake_get_media_metadata(
            media_type,
            _media_id,
            _source,
            season_numbers=None,
            episode_number=None,  # noqa: ARG001
        ):
            if media_type == MediaTypes.MOVIE.value:
                return {
                    "title": "Tenet",
                    "image": "https://example.com/movie.jpg",
                }

            if media_type == MediaTypes.TV.value:
                return {
                    "title": "The Boys",
                    "image": "https://example.com/show.jpg",
                    "max_progress": 32,
                    "related": {
                        "seasons": [{"season_number": 4}, {"season_number": 3}],
                    },
                }

            if media_type == MediaTypes.SEASON.value:
                season_number = season_numbers[0]
                return {
                    "title": f"Season {season_number}",
                    "image": "https://example.com/season.jpg",
                    "max_progress": 8,
                }

            if media_type == MediaTypes.EPISODE.value:
                return {
                    "title": "The Boys",
                    "episode_title": "Department of Dirty Tricks",
                    "image": "https://example.com/episode.jpg",
                }

            return None

        mock_search.side_effect = fake_search
        mock_get_media_metadata.side_effect = fake_get_media_metadata

        with Path(mock_path / "import_amazon.csv").open("rb") as file:
            imported_counts, warnings = amazon.importer(file, self.user, "new")

        # The fixture has two Tenet rows at different timestamps: each play is
        # its own event-based record, so both are kept (not deduped to one).
        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 2)
        self.assertEqual(imported_counts[MediaTypes.TV.value], 1)
        self.assertEqual(imported_counts[MediaTypes.SEASON.value], 1)
        self.assertEqual(imported_counts[MediaTypes.EPISODE.value], 1)
        self.assertIsNone(warnings)

        movies = Movie.objects.filter(user=self.user, item__media_id="100")
        self.assertEqual(movies.count(), 2)
        for movie in movies:
            self.assertEqual(movie.status, Status.COMPLETED.value)
            self.assertEqual(movie.progress, 1)

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
        self.assertEqual(episode.end_date.year, 2024)

    @patch(METADATA_PATCH_TARGET)
    @patch(SEARCH_PATCH_TARGET)
    def test_reimporting_same_play_is_a_no_op(
        self,
        mock_search,
        mock_get_media_metadata,
    ):
        """Re-importing the exact same CSV twice doesn't duplicate the plays."""
        mock_search.return_value = {
            "results": [{"media_id": "100", "title": "Tenet", "image": ""}],
        }
        mock_get_media_metadata.return_value = {
            "title": "Tenet",
            "image": "https://example.com/movie.jpg",
        }

        with Path(mock_path / "import_amazon.csv").open("rb") as file:
            amazon.importer(file, self.user, "new")
        with Path(mock_path / "import_amazon.csv").open("rb") as file:
            amazon.importer(file, self.user, "new")

        self.assertEqual(
            Movie.objects.filter(user=self.user, item__media_id="100").count(),
            2,
        )
