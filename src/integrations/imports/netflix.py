import logging
import re
from csv import DictReader
from datetime import datetime

from django.utils import timezone

from app.models import MediaTypes, Sources
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from integrations.imports.tmdb_watch_history import (
    TMDBWatchHistoryImportMixin,
    WatchEntry,
)

logger = logging.getLogger(__name__)

# Netflix uses ": " to separate show name, season info, and episode title, e.g.
# "Stranger Things: Stranger Things 5: Chapter One". A title with only one such
# split is ambiguous (could be "Show: Episode Title" or "Movie: Subtitle"), so
# these indicators in the second segment are what tips it towards a TV show.
_MIN_TV_TITLE_PARTS = 2
_TV_INDICATOR_RE = re.compile(
    r"\bseason\s+\d+\b|\bseries\s+\d+\b|\bstaffel\s+\d+\b|\bpart\s+\d+\b"
    r"|\bvolume\s+\d+\b|\bepisode\s+\d+\b|\bep\.\s*\d+\b|\d+\s*$",
    re.IGNORECASE,
)
_SEASON_NUMBER_RE = re.compile(r"(\d+)\s*$")


def importer(file, user, mode):
    """Import media from a Netflix viewing history CSV export."""
    netflix_importer = NetflixImporter(file, user, mode)
    return netflix_importer.import_data()


class NetflixImporter(TMDBWatchHistoryImportMixin):
    """Class to handle importing user data from Netflix viewing history CSV."""

    def __init__(self, file, user, mode):
        """Initialize the importer with file, user, and mode."""
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []

        self._init_watch_history_state()

        logger.info(
            "Initialized Netflix importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import all user data from the Netflix viewing history CSV file."""
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

        reader = DictReader(decoded_file)
        if not {"Title", "Date"}.issubset(reader.fieldnames or []):
            msg = (
                "Invalid Netflix CSV format. Expected columns: Title, Date. "
                f"Found: {', '.join(reader.fieldnames or [])}"
            )
            raise MediaImportError(msg)

        return list(reader)

    def _resolve_row(self, row):
        """Resolve a single Netflix CSV row into a WatchEntry."""
        title = (row.get("Title") or "").strip()
        if not title:
            return None

        watched_at = self._parse_date((row.get("Date") or "").strip())
        if not watched_at:
            self.warnings.append(f"{title}: couldn't parse the watched date")
            return None

        show_name, is_tv = self._extract_show_name(title)
        if not is_tv:
            return WatchEntry(
                media_type=MediaTypes.MOVIE.value,
                display_title=title,
                search_title=title,
                search_media_type=MediaTypes.MOVIE.value,
                watched_at=watched_at,
                event_based=True,
            )

        return self._resolve_tv_row(title, show_name, watched_at)

    def _resolve_tv_row(self, title, show_name, watched_at):
        """Resolve a TV-shaped row, falling back to a movie search.

        Some movie titles ("Spider-Man: Homecoming") look like a show because
        they contain a colon, so a show search that comes up empty is retried
        as a movie before giving up.
        """
        _, season_segment, episode_title = self._parse_netflix_title(title)
        season_number = self._extract_season_number(season_segment)

        tv_match = self._search_tmdb(MediaTypes.TV.value, show_name)
        if tv_match:
            return WatchEntry(
                media_type=MediaTypes.EPISODE.value,
                display_title=show_name,
                tmdb_id=str(tv_match["media_id"]),
                season_number=season_number,
                episode_title=episode_title,
                watched_at=watched_at,
                event_based=True,
            )

        movie_match = self._search_tmdb(MediaTypes.MOVIE.value, show_name)
        if movie_match:
            return WatchEntry(
                media_type=MediaTypes.MOVIE.value,
                display_title=show_name,
                tmdb_id=str(movie_match["media_id"]),
                watched_at=watched_at,
                event_based=True,
            )

        self.warnings.append(
            f"{show_name}: Couldn't find a match in {Sources.TMDB.label}",
        )
        return None

    def _extract_show_name(self, title):
        """Split a Netflix title into a show/movie name and a TV-vs-movie guess.

        Netflix separates show name, season info, and episode title with
        ": ". Three or more segments is unambiguously a show. Two segments is
        a show only if the second segment looks like a season/episode
        indicator; otherwise it's a movie subtitle.
        """
        parts = title.split(": ")
        if len(parts) < _MIN_TV_TITLE_PARTS:
            return title.strip(), False

        if len(parts) >= 3:  # noqa: PLR2004
            return parts[0].strip(), True

        if _TV_INDICATOR_RE.search(parts[1].strip()):
            return parts[0].strip(), True

        return title.strip(), False

    def _parse_netflix_title(self, title):
        """Split a Netflix title into show name, season segment, and episode title.

        E.g. "Stranger Things: Stranger Things 5: Chapter One" becomes
        ("Stranger Things", "Stranger Things 5", "Chapter One").
        """
        parts = title.split(": ")
        if len(parts) >= 3:  # noqa: PLR2004
            # The episode title itself may contain ": ", so rejoin the rest.
            return parts[0].strip(), parts[1].strip(), ": ".join(parts[2:]).strip()
        if len(parts) == _MIN_TV_TITLE_PARTS:
            return parts[0].strip(), None, parts[1].strip()
        return title.strip(), None, None

    def _extract_season_number(self, season_segment):
        """Extract a trailing season number, e.g. "Stranger Things 5" -> 5."""
        if not season_segment:
            return None

        match = _SEASON_NUMBER_RE.search(season_segment)
        return int(match.group(1)) if match else None

    def _parse_date(self, date_str):
        """Parse a Netflix date, exported as M/D/YY or M/D/YYYY."""
        if not date_str:
            return None

        for date_format in ("%m/%d/%y", "%m/%d/%Y"):
            try:
                parsed = datetime.strptime(date_str, date_format)  # noqa: DTZ007
            except ValueError:
                continue
            return parsed.replace(tzinfo=timezone.get_current_timezone())

        logger.warning("Could not parse Netflix date: %s", date_str)
        return None
