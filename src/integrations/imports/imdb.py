import logging
from collections import defaultdict
from csv import DictReader

from django.utils import timezone
from django.utils.dateparse import parse_datetime

import app
import app.providers
from app.models import TV, MediaTypes, Sources, Status
from app.providers.services import ProviderAPIError
from integrations.imports.helpers import (
    MediaImportError,
    MediaImportUnexpectedError,
    join_with_commas_and,
)
from integrations.imports.tmdb_watch_history import (
    TMDBWatchHistoryImportMixin,
    WatchEntry,
)

logger = logging.getLogger(__name__)

# Mapping of IMDB title types to media types
IMDB_TYPE_MAPPING = {
    "Movie": MediaTypes.MOVIE,
    "TV Series": MediaTypes.TV,
    "Short": MediaTypes.MOVIE,
    "TV Mini Series": MediaTypes.TV,
    "TV Movie": MediaTypes.MOVIE,
    "TV Special": MediaTypes.MOVIE,
    "Video": MediaTypes.MOVIE,
}

# IMDB title types we don't support
UNSUPPORTED_TYPES = {
    "TV Episode",
    "TV Short",
    "Video Game",
    "Music Video",
    "Podcast Series",
    "Podcast Episode",
}


def importer(file, user, mode):
    """Import media from IMDB CSV file."""
    imdb_importer = IMDBImporter(file, user, mode)
    return imdb_importer.import_data()


class IMDBImporter(TMDBWatchHistoryImportMixin):
    """Class to handle importing user data from IMDB CSV."""

    def __init__(self, file, user, mode):
        """Initialize the importer with file, user, and mode.

        Args:
            file: Uploaded CSV file
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
        """
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []

        self._init_watch_history_state()

        logger.info(
            "Initialized IMDB importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import all user data from CSV."""
        try:
            decoded_file = self.file.read().decode("utf-8").splitlines()
        except UnicodeDecodeError as e:
            msg = "Invalid file format. Please upload a CSV file."
            raise MediaImportError(msg) from e

        reader = DictReader(decoded_file)
        rows = list(reader)

        # Track media IDs and their titles from the import file
        media_id_counts = defaultdict(int)
        media_id_titles = defaultdict(list)

        # First pass: identify duplicates and validate entries
        for row in rows:
            try:
                self._process_first_pass(row, media_id_counts, media_id_titles)
            except Exception as error:
                error_msg = f"Error processing entry: {row}"
                raise MediaImportUnexpectedError(error_msg) from error

        # Second pass: process non-duplicate entries
        for row in rows:
            try:
                self._process_second_pass(row, media_id_counts)
            except Exception as error:
                error_msg = f"Error processing entry: {row}"
                raise MediaImportUnexpectedError(error_msg) from error

        # Add consolidated warnings for duplicates
        self._add_duplicate_warnings(media_id_counts, media_id_titles)

        return self.finalize_watch_history_import()

    def _process_first_pass(self, row, media_id_counts, media_id_titles):
        """First pass to identify duplicate entries and validate data."""
        imdb_id = self._extract_imdb_id(row)

        title = row.get("Title", "").strip()

        if not imdb_id:
            self.warnings.append(f"{title}: Invalid or missing IMDB ID")
            return

        title_type = row.get("Title Type", "").strip()

        if not self._is_supported_type(title_type):
            if title_type in UNSUPPORTED_TYPES:
                self.warnings.append(
                    f"{title}: Unsupported title type '{title_type}' - skipped",
                )
            else:
                self.warnings.append(
                    f"{title}: Unknown title type '{title_type}' - skipped",
                )
            return

        tmdb_data = self._lookup_in_tmdb(imdb_id, title_type)

        if not tmdb_data:
            self.warnings.append(
                f"{title}: Couldn't find a match in {Sources.TMDB.label}",
            )
            return

        media_id = tmdb_data["media_id"]
        media_id_counts[media_id] += 1
        media_id_titles[media_id].append(title)

    def _process_second_pass(self, row, media_id_counts):
        """Second pass to process non-duplicate entries."""
        imdb_id = self._extract_imdb_id(row)
        if not imdb_id:
            return  # Already added warning in first pass

        title_type = row.get("Title Type", "").strip()
        if not self._is_supported_type(title_type):
            return  # Already added warning in first pass

        tmdb_data = self._lookup_in_tmdb(imdb_id, title_type)
        if not tmdb_data:
            return  # Already added warning in first pass

        media_id = tmdb_data["media_id"]

        # Skip if this media_id appears more than once
        if media_id_counts[media_id] > 1:
            return

        media_type = IMDB_TYPE_MAPPING[title_type]
        entry = self._build_entry(row, tmdb_data, media_type)

        if media_type == MediaTypes.MOVIE.value:
            self.process_movie_entry(entry)
        else:
            self.process_single_media_entry(entry, TV)

    def _build_entry(self, row, tmdb_data, media_type):
        """Build a WatchEntry from an IMDB row already resolved against TMDB."""
        title = row.get("Title", "").strip()
        rating = self._parse_rating(row.get("Your Rating", ""))
        status = Status.COMPLETED.value if rating is not None else Status.PLANNING.value

        date_created = self._parse_date(row.get("Created", ""))
        date_modified = self._parse_date(row.get("Modified", ""))
        date_rated = self._parse_date(row.get("Date Rated", ""))
        dates = [date_created, date_modified, date_rated]
        most_recent_date = max(date for date in dates if date)

        return WatchEntry(
            media_type=media_type,
            display_title=title,
            tmdb_id=str(tmdb_data["media_id"]),
            image_url=tmdb_data.get("image", ""),
            watched_at=most_recent_date,
            status=status,
            score=rating,
        )

    def _add_duplicate_warnings(self, media_id_counts, media_id_titles):
        """Add warnings for duplicate entries."""
        for media_id, count in media_id_counts.items():
            if count > 1:
                titles = media_id_titles[media_id]
                title_list = join_with_commas_and(titles)
                self.warnings.append(
                    f"{title_list}: They were matched to the same TMDB ID {media_id} "
                    "- none imported",
                )

    def _extract_imdb_id(self, row):
        """Extract and clean IMDB ID from row."""
        imdb_id: str = row.get("Const", "").strip()

        if not imdb_id:
            return None

        if imdb_id.startswith("tt"):
            return imdb_id

        # Add 'tt' prefix if not present, TMDB find expects it
        if imdb_id.isdigit():
            return f"tt{imdb_id}"

        return None

    def _is_supported_type(self, title_type):
        """Check if the title type is supported."""
        return title_type in IMDB_TYPE_MAPPING

    def _lookup_in_tmdb(self, imdb_id, title_type):
        """Look up media in TMDB using IMDB ID."""
        try:
            response = app.providers.tmdb.find(imdb_id, "imdb_id")
        except ProviderAPIError as e:
            logger.warning("Error looking up IMDB ID %s in TMDB: %s", imdb_id, e)
            return None

        media_type = IMDB_TYPE_MAPPING.get(title_type, "")

        if media_type == MediaTypes.MOVIE.value and response.get("movie_results"):
            movie = response["movie_results"][0]
            return {
                "media_id": movie["id"],
                "title": movie["title"],
                "image": app.providers.tmdb.get_image_url(movie["poster_path"]),
                "media_type": MediaTypes.MOVIE.value,
            }

        if media_type == MediaTypes.TV.value and response.get("tv_results"):
            tv_show = response["tv_results"][0]
            return {
                "media_id": tv_show["id"],
                "title": tv_show["name"],
                "image": app.providers.tmdb.get_image_url(tv_show["poster_path"]),
                "media_type": MediaTypes.TV.value,
            }

        return None

    def _parse_rating(self, rating_str):
        """Parse user rating from string to decimal."""
        if not rating_str or rating_str.strip() == "":
            return None

        try:
            rating = float(rating_str.strip())
            # IMDB ratings are 1-10, ensure it's in valid range
            min_rating = 1
            max_rating = 10
            if min_rating <= rating <= max_rating:
                return rating
        except (ValueError, TypeError):
            pass

        return None

    def _parse_date(self, date_str):
        """
        Parse date string into datetime object.

        IMDB exports dates in YYYY-MM-DD format.
        """
        date_str = date_str.strip()

        if not date_str:
            return None

        date = parse_datetime(date_str)

        if not date:
            logger.warning("Could not parse date: %s", date_str)
            return None

        return date.replace(
            hour=0,
            minute=0,
            second=0,
            tzinfo=timezone.get_current_timezone(),
        )
