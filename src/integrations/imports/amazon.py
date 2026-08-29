import logging
import re
from csv import DictReader
from datetime import UTC, datetime

from app.models import MediaTypes
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from integrations.imports.tmdb_watch_history import (
    TMDBWatchHistoryImportMixin,
    WatchEntry,
)

logger = logging.getLogger(__name__)

MOVIE_TYPE = "Movie"
SERIES_TYPE = "Series"
SEASON_TITLE_RE = re.compile(
    r"^(?P<title>.+?)(?:\s*[-,:]\s*|\s+)(?:Season|Staffel)\s+(?P<season>\d+)(?:\s*\[[^\]]+\])?$",
)
EPISODE_TITLE_RE = re.compile(
    r"^Episode\s+(?P<episode>\d+)\s*[:\-]?(?:\s*(?P<title>.*))?$",
)


def importer(file, user, mode):
    """Import media history from an Amazon Prime CSV export."""
    amazon_importer = AmazonPrimeImporter(file, user, mode)
    return amazon_importer.import_data()


class AmazonPrimeImporter(TMDBWatchHistoryImportMixin):
    """Class to handle importing user watch history from Amazon Prime CSV exports."""

    def __init__(self, file, user, mode):
        """Initialize the importer with file, user, and mode."""
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []

        self._init_watch_history_state()

        logger.info(
            "Initialized Amazon Prime importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import all user data from the Amazon Prime CSV file."""
        rows = self._read_rows()
        raw_entries = [
            entry for entry in (self._resolve_row(row) for row in rows) if entry
        ]

        movie_entries = [
            e for e in raw_entries if e.media_type == MediaTypes.MOVIE.value
        ]
        episode_entries = [
            e for e in raw_entries if e.media_type == MediaTypes.EPISODE.value
        ]

        for entry in self.resolve_movie_entries(movie_entries).values():
            try:
                self.process_movie_entry(entry)
            except Exception as error:
                msg = f"Error processing entry: {entry.display_title}"
                raise MediaImportUnexpectedError(msg) from error

        tv_grouped = self.resolve_tv_entries(episode_entries)
        for tmdb_id, seasons in tv_grouped.items():
            try:
                self.process_tv_entries(tmdb_id, seasons)
            except Exception as error:
                msg = f"Error processing series entries for {tmdb_id}"
                raise MediaImportUnexpectedError(msg) from error

        return self.finalize_watch_history_import()

    def _read_rows(self):
        """Read and decode the uploaded CSV file."""
        try:
            raw_file = self.file.read()
            try:
                decoded_file = raw_file.decode("utf-8-sig").splitlines()
            except UnicodeDecodeError:
                decoded_file = raw_file.decode("latin-1").splitlines()
        except UnicodeDecodeError as error:
            msg = "Invalid file format. Please upload a CSV file."
            raise MediaImportError(msg) from error

        return list(DictReader(decoded_file))

    def _resolve_row(self, row):
        """Resolve a single Amazon Prime CSV row into a WatchEntry."""
        row_type = (row.get("Type") or "").strip()
        title = (row.get("Title") or "").strip()
        watched_at = self._parse_watched_at(row.get("Date Watched"))

        if not title or not watched_at:
            self.warnings.append(
                f"{title or 'Unknown title'}: missing title or watched date",
            )
            return None

        if row_type == MOVIE_TYPE:
            return self._resolve_movie_row(row, title, watched_at)

        if row_type == SERIES_TYPE:
            return self._resolve_series_row(row, title, watched_at)

        self.warnings.append(f"{title}: unsupported type '{row_type}' - skipped")
        return None

    def _resolve_movie_row(self, row, title, watched_at):
        """Resolve a movie row into a WatchEntry to be searched against TMDB."""
        return WatchEntry(
            media_type=MediaTypes.MOVIE.value,
            display_title=title,
            search_title=title,
            search_media_type=MediaTypes.MOVIE.value,
            image_url=(row.get("Image URL") or "").strip(),
            watched_at=watched_at,
            event_based=True,
        )

    def _resolve_series_row(self, row, title, watched_at):
        """Resolve a series row into a WatchEntry to be searched against TMDB."""
        series_title, season_number = self._parse_series_title(title)
        if season_number is None:
            self.warnings.append(f"{title}: couldn't parse a season number")
            return None

        episode_title = (row.get("Episode Title") or "").strip()
        episode_number = self._parse_episode_number(episode_title)
        if episode_number is None:
            self.warnings.append(f"{title}: couldn't parse an episode number")
            return None

        return WatchEntry(
            media_type=MediaTypes.EPISODE.value,
            display_title=series_title,
            search_title=series_title,
            search_media_type=MediaTypes.TV.value,
            season_number=season_number,
            episode_number=episode_number,
            image_url=(row.get("Image URL") or "").strip(),
            watched_at=watched_at,
            event_based=True,
        )

    def _parse_series_title(self, title):
        """Split a series title into the show title and season number."""
        match = SEASON_TITLE_RE.match(title)
        if not match:
            return title, None

        return match.group("title").strip(), int(match.group("season"))

    def _parse_episode_number(self, episode_title):
        """Extract an episode number from the exported episode title."""
        match = EPISODE_TITLE_RE.match(episode_title)
        if not match:
            return None

        return int(match.group("episode"))

    def _parse_watched_at(self, watched_at):
        """Convert the Amazon epoch-millisecond timestamp into a datetime."""
        if watched_at in (None, ""):
            return None

        try:
            watched_at_ms = int(str(watched_at).strip())
        except (TypeError, ValueError):
            return None

        return datetime.fromtimestamp(watched_at_ms / 1000, tz=UTC)
