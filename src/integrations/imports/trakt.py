import json
import logging
import re
import zipfile

import requests
from django.conf import settings
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django_celery_beat.models import PeriodicTask

import app
from app import helpers as app_helpers
from app.models import TV, MediaTypes, Season, Sources, Status
from app.providers import services
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from integrations.imports.tmdb_watch_history import (
    TMDBWatchHistoryImportMixin,
    WatchEntry,
)

logger = logging.getLogger(__name__)

TRAKT_API_BASE_URL = "https://api.trakt.tv"
BULK_PAGE_SIZE = 1000

# The archive is expanded in the worker, so cap how much it may hold uncompressed.
MAX_EXPORT_UNCOMPRESSED_BYTES = 100 * 1024 * 1024

# Username used when the archive has no readable user-profile.json.
EXPORT_FALLBACK_USERNAME = "Trakt User"

# Convert Trakt API endpoints to the corresponding export file(s) that contain the data.
ENDPOINTS_TO_FILES = {
    "/history": ("watched-history",),
    "/watchlist": ("lists-watchlist",),
    "/ratings": (
        "ratings-movies",
        "ratings-shows",
        "ratings-seasons",
        "ratings-episodes",
    ),
    "/comments": (
        "comments-movies",
        "comments-shows",
        "comments-seasons",
        "comments-episodes",
    ),
}


def handle_oauth_callback(request, redirect_uri=None):
    """View for getting the Trakt OAuth2 token."""
    code = request.GET["code"]

    url = "https://api.trakt.tv/oauth/token"
    redirect_uri = redirect_uri or app_helpers.build_absolute_app_url(
        request,
        reverse("import_trakt_private"),
    )

    params = {
        "client_id": settings.TRAKT_API,
        "client_secret": settings.TRAKT_API_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }

    try:
        token_response = app.providers.services.api_request(
            "TRAKT",
            "POST",
            url,
            params=params,
        )
    except services.ProviderAPIError as error:
        if error.status_code == requests.codes.unauthorized:
            msg = "Invalid Trakt secret key."
            raise MediaImportError(msg) from error
        raise

    return {
        "refresh_token": token_response["refresh_token"],
        "username": get_username_from_oauth(token_response["access_token"]),
    }


def get_username_from_oauth(access_token):
    """View for getting the Trakt OAuth2 username."""
    url = "https://api.trakt.tv/users/me"

    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": settings.TRAKT_API,
        "Authorization": f"Bearer {access_token}",
    }

    try:
        request = app.providers.services.api_request(
            "TRAKT",
            "GET",
            url,
            headers=headers,
        )
    except services.ProviderAPIError as error:
        if error.status_code == requests.codes.unauthorized:
            msg = "Invalid Trakt secret key."
            raise MediaImportError(msg) from error
        raise

    return request["username"]


def get_access_token(encrypted_refresh_token, redirect_uri=None):
    """Get access token from encrypted refresh token."""
    url = "https://api.trakt.tv/oauth/token"

    decrypted_token = helpers.decrypt(encrypted_refresh_token)
    redirect_uri = redirect_uri or app_helpers.build_absolute_app_url(
        None,
        reverse("import_trakt_private"),
    )

    params = {
        "client_id": settings.TRAKT_API,
        "client_secret": settings.TRAKT_API_SECRET,
        "refresh_token": decrypted_token,
        "grant_type": "refresh_token",
    }
    if redirect_uri:
        params["redirect_uri"] = redirect_uri

    try:
        request = app.providers.services.api_request(
            "TRAKT",
            "POST",
            url,
            params=params,
        )
    except services.ProviderAPIError as error:
        if error.status_code == requests.codes.unauthorized:
            msg = "Invalid Trakt secret key."
            raise MediaImportError(msg) from error
        raise

    # refresh tokens are one time use only
    update_refresh_token(encrypted_refresh_token, request["refresh_token"])
    return request["access_token"]


def update_refresh_token(old_token, new_token):
    """Update the refresh token in periodic tasks."""
    periodic_task = PeriodicTask.objects.filter(
        task="Import from Trakt",
        kwargs__contains=f'"token": "{old_token}"',
    ).first()

    if periodic_task:
        task_kwargs = json.loads(periodic_task.kwargs)
        task_kwargs["token"] = helpers.encrypt(new_token)
        periodic_task.kwargs = json.dumps(task_kwargs)
        periodic_task.save()


def importer(token, user, mode, username=None, redirect_uri=None, file=None):
    """Import the user's data from Trakt.

    Can import using OAuth (token provided), public username, or an export archive
    downloaded from the Trakt website.

    Args:
        token (str, optional): Encrypted OAuth2 refresh token if using OAuth else None
        user: Django user object to import data for
        mode (str): Import mode ("new" or "overwrite")
        username (str, optional): Trakt username if using API import else None
        redirect_uri (str, optional): OAuth2 redirect URI if using OAuth else None
        file (File, optional): Uploaded Trakt export archive else None
    """
    if file:
        trakt_importer = TraktExportImporter(file, user, mode)
    else:
        trakt_importer = TraktImporter(
            username,
            user,
            mode,
            refresh_token=token,
            redirect_uri=redirect_uri,
        )

    return trakt_importer.import_data()


class TraktImporter(TMDBWatchHistoryImportMixin):
    """Class to handle importing user data from Trakt."""

    def __init__(self, username, user, mode, refresh_token=None, redirect_uri=None):
        """Initialize the importer with user details and mode.

        Args:
            username (str): Trakt username to import from
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
            refresh_token (str, optional): Encrypted OAuth2 refresh token if
                using OAuth, None for public import
        """
        self.username = username
        self.user = user
        self.mode = mode
        self.refresh_token = refresh_token
        self.redirect_uri = redirect_uri
        self.user_base_url = f"{TRAKT_API_BASE_URL}/users/{username}"
        self.warnings = []

        self._init_watch_history_state()

        logger.info(
            "Initialized Trakt importer for user %s with mode %s",
            username,
            mode,
        )

    def import_data(self):
        """Import all user data from Trakt."""
        self.process_history()
        self.process_watchlist()
        self.process_ratings()
        self.process_comments()

        return self.finalize_watch_history_import()

    def _make_api_request(self, url):
        """Make a request to the Trakt API with proper headers."""
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": settings.TRAKT_API,
        }
        if self.refresh_token:
            try:
                # already made api_request before, so access_token is set
                headers["Authorization"] = f"Bearer {self.access_token}"
            except AttributeError:
                self.access_token = get_access_token(
                    self.refresh_token,
                    redirect_uri=self.redirect_uri,
                )
                headers["Authorization"] = f"Bearer {self.access_token}"
        return services.api_request(
            "TRAKT",
            "GET",
            url,
            headers=headers,
        )

    def _get_paginated_data(self, endpoint, item_type="items"):
        """Get paginated data from Trakt API."""
        page = 1
        all_data = []

        while True:
            url = f"{endpoint}?page={page}&limit={BULK_PAGE_SIZE}"

            try:
                page_data = self._make_api_request(url)
            except requests.exceptions.HTTPError as error:
                if error.response.status_code == requests.codes.not_found:
                    msg = (
                        f"User slug {self.username} not found. "
                        "User slug can be found in your Trakt profile URL."
                    )
                    raise MediaImportError(msg) from error

                if error.response.status_code == requests.codes.unauthorized:
                    msg = "This account is set to private, use OAuth import instead."
                    raise MediaImportError(msg) from error
                raise

            if not page_data:
                # We've reached the end of the data
                break

            all_data.extend(page_data)
            page += 1
            logger.info(
                "Retrieved page %s of %s for user %s (%s items)",
                page - 1,
                item_type,
                self.username,
                len(page_data),
            )

        logger.info(
            "Retrieved %s total %s for user %s",
            len(all_data),
            item_type,
            self.username,
        )
        return all_data

    def process_history(self):
        """Process watch history from Trakt."""
        logger.info("Importing watch history for user %s", self.username)
        history_endpoint = f"{self.user_base_url}/history"
        full_history = self._get_paginated_data(history_endpoint, "history entries")

        movie_raw_entries = []
        episode_raw_entries = []

        # Process in chronological order (oldest first)
        for entry in reversed(full_history):
            try:
                if entry["type"] == "movie":
                    watch_entry = self._build_movie_watch_entry(entry)
                    if watch_entry:
                        movie_raw_entries.append(watch_entry)
                elif entry["type"] == "episode":
                    watch_entry = self._build_episode_watch_entry(entry)
                    if watch_entry:
                        episode_raw_entries.append(watch_entry)
            except Exception as e:
                msg = f"Error processing history entry: {entry}"
                raise MediaImportUnexpectedError(msg) from e

        movies = self.resolve_movie_entries(movie_raw_entries)
        for movie_entry in movies.values():
            self.process_movie_entry(movie_entry)

        tv_grouped = self.resolve_tv_entries(episode_raw_entries)
        for tmdb_id, seasons in tv_grouped.items():
            self.process_tv_entries(tmdb_id, seasons)

    def _get_date(self, date_str):
        """Parse a Trakt watched_at timestamp and strip seconds/microseconds."""
        return parse_datetime(date_str).replace(second=0, microsecond=0)

    def _get_tmdb_id(self, entry_data):
        """Extract TMDB ID from entry data."""
        if (
            "ids" in entry_data
            and "tmdb" in entry_data["ids"]
            and entry_data["ids"]["tmdb"]
        ):
            return str(entry_data["ids"]["tmdb"])

        self.warnings.append(
            f"{entry_data['title']}: No {Sources.TMDB.label} ID found.",
        )
        return None

    def _build_movie_watch_entry(self, entry):
        """Build an event-based WatchEntry for one movie play from history."""
        movie = entry["movie"]
        tmdb_id = self._get_tmdb_id(movie)
        if not tmdb_id:
            return None
        return WatchEntry(
            media_type=MediaTypes.MOVIE.value,
            display_title=movie["title"],
            tmdb_id=tmdb_id,
            watched_at=self._get_date(entry["watched_at"]),
            event_based=True,
        )

    def _build_episode_watch_entry(self, entry):
        """Build an event-based WatchEntry for one episode play from history."""
        show = entry["show"]
        tmdb_id = self._get_tmdb_id(show)
        if not tmdb_id:
            return None
        return WatchEntry(
            media_type=MediaTypes.EPISODE.value,
            display_title=show["title"],
            tmdb_id=tmdb_id,
            season_number=entry["episode"]["season"],
            episode_number=entry["episode"]["number"],
            watched_at=self._get_date(entry["watched_at"]),
            event_based=True,
        )

    def process_watchlist(self):
        """Process watchlist from Trakt."""
        logger.info("Importing watchlist for user %s", self.username)
        watchlist_endpoint = f"{self.user_base_url}/watchlist"
        watchlist_data = self._make_api_request(watchlist_endpoint)

        for entry in watchlist_data:
            try:
                self._process_generic_entry(entry, status=Status.PLANNING.value)
            except Exception as e:
                msg = f"Error processing watchlist entry: {entry}"
                raise MediaImportUnexpectedError(msg) from e

    def process_ratings(self):
        """Process ratings from Trakt."""
        logger.info("Importing ratings for user %s", self.username)
        ratings_endpoint = f"{self.user_base_url}/ratings"
        ratings_data = self._make_api_request(ratings_endpoint)

        for entry in ratings_data:
            try:
                # Episode has no score field, so episode ratings are skipped.
                if entry["type"] == "episode":
                    continue
                # a rated movie is assumed watched/completed
                status = Status.COMPLETED.value if entry["type"] == "movie" else None
                self._process_generic_entry(entry, status=status, score=entry["rating"])
            except Exception as e:
                msg = f"Error processing rating entry: {entry}"
                raise MediaImportUnexpectedError(msg) from e

    def process_comments(self):
        """Process comments from Trakt."""
        logger.info("Importing comments for user %s", self.username)
        comments_endpoint = f"{self.user_base_url}/comments"
        full_comments = self._get_paginated_data(comments_endpoint, "comments")

        for entry in full_comments:
            try:
                # Episode has no notes field, so episode comments are skipped.
                if entry["type"] == "episode":
                    continue
                self._process_generic_entry(entry, notes=entry["comment"]["comment"])
            except Exception as e:
                msg = f"Error processing comment entry: {entry}"
                raise MediaImportUnexpectedError(msg) from e

    def _process_generic_entry(self, entry, *, status=None, score=None, notes=None):
        """Apply a watchlist/rating/comment entry onto a Movie, TV show or Season."""
        if entry["type"] == "movie":
            media = entry["movie"]
            tmdb_id = self._get_tmdb_id(media)
            if not tmdb_id:
                return
            self.process_movie_entry(
                WatchEntry(
                    media_type=MediaTypes.MOVIE.value,
                    display_title=media["title"],
                    tmdb_id=tmdb_id,
                    status=status,
                    score=score,
                    notes=notes,
                ),
            )

        elif entry["type"] == "show":
            media = entry["show"]
            tmdb_id = self._get_tmdb_id(media)
            if not tmdb_id:
                return
            self.process_single_media_entry(
                WatchEntry(
                    media_type=MediaTypes.TV.value,
                    display_title=media["title"],
                    tmdb_id=tmdb_id,
                    status=status,
                    score=score,
                    notes=notes,
                ),
                TV,
            )

        elif entry["type"] == "season":
            media = entry["show"]
            tmdb_id = self._get_tmdb_id(media)
            if not tmdb_id:
                return
            season_number = entry["season"]["number"]

            tv_instance = self.process_single_media_entry(
                WatchEntry(
                    media_type=MediaTypes.TV.value,
                    display_title=media["title"],
                    tmdb_id=tmdb_id,
                ),
                TV,
                create_status=Status.IN_PROGRESS.value,
            )
            if tv_instance is None:
                return

            self.process_single_media_entry(
                WatchEntry(
                    media_type=MediaTypes.SEASON.value,
                    display_title=f"{media['title']} S{season_number}",
                    tmdb_id=tmdb_id,
                    season_number=season_number,
                    status=status,
                    score=score,
                    notes=notes,
                ),
                Season,
                related_tv=tv_instance,
            )


class TraktExportImporter(TraktImporter):
    """Run the standard Trakt import against an export archive instead of the API."""

    def __init__(self, file, user, mode):
        """Initialize from a Trakt export archive."""
        # The archive appends to this list while files are read during the import,
        # so it is shared with the importer instead of being merged afterwards.
        warnings = []
        self.export_archive = TraktArchiveManager(file, warnings)

        user_profile = self.export_archive.parse_json("user-profile")
        username = (
            user_profile.get("username")
            if isinstance(user_profile, dict)
            else EXPORT_FALLBACK_USERNAME
        )

        super().__init__(username or EXPORT_FALLBACK_USERNAME, user, mode)
        self.warnings = warnings

    def _make_api_request(self, url):
        """Read from the export archive instead of making API calls."""
        return self._get_paginated_data(url)

    def _get_paginated_data(self, endpoint, item_type="items"):
        """Read the export files matching the endpoint."""
        relative_filepath = endpoint.removeprefix(self.user_base_url)
        prefixes = ENDPOINTS_TO_FILES.get(relative_filepath)

        if prefixes is None:
            logger.warning("No Trakt export file mapped for endpoint %s", endpoint)
            return []

        # Read the JSON files from the export archive and concatenate their contents.
        entries = self.export_archive.load(*prefixes)
        logger.info(
            "Retrieved %s total %s for user %s from export archive",
            len(entries),
            item_type,
            self.username,
        )
        return entries


class TraktArchiveManager:
    """Class to manage a Trakt export archive."""

    def __init__(self, file, warnings=None):
        """Open the archive and index its JSON files.

        Args:
            file: Uploaded export archive, or any file-like object
            warnings (list, optional): List to append read warnings to
        """
        try:
            self.zipfile = zipfile.ZipFile(file)
        except zipfile.BadZipFile as e:
            msg = "The uploaded file is not a valid Trakt export archive."
            raise MediaImportError(msg) from e

        self.warnings = warnings if warnings is not None else []

        # Map each JSON base name to its full path inside the archive, so archives
        # that keep the export in a subdirectory can still be read.
        self._files = {}

        uncompressed_size = 0

        # Check the uncompressed size of the archive to avoid memory issues.
        # file_size is what the archive declares, so this is a guard against
        # oversized exports rather than against deliberately crafted archives.
        for info in self.zipfile.infolist():
            if info.is_dir():
                continue
            uncompressed_size += info.file_size
            if uncompressed_size > MAX_EXPORT_UNCOMPRESSED_BYTES:
                msg = "The uncompressed Trakt export archive is too large to import."
                raise MediaImportError(msg)

            name = info.filename.rsplit("/", 1)[-1]
            if not name.lower().endswith(".json"):
                continue
            # Store the base name without the .json suffix for easier matching.
            self._files.setdefault(name[: -len(".json")], info.filename)

        if not self._recognized_export():
            msg = (
                "The uploaded archive does not contain any Trakt export data. "
                "Upload the ZIP file downloaded from the Trakt website."
            )
            raise MediaImportError(msg)

    def _recognized_export(self):
        """Check whether the archive holds at least one known export file."""
        if "user-profile" in self._files:
            return True

        return any(
            self._match_names(prefix)
            for prefixes in ENDPOINTS_TO_FILES.values()
            for prefix in prefixes
        )

    def parse_json(self, base_name):
        """Read a JSON file from the archive, returning None when unavailable."""
        filename = self._files.get(base_name)
        if filename is None:
            return None

        try:
            return json.loads(self.zipfile.read(filename))
        except (KeyError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            logger.exception("Trakt export file %s could not be read", filename)
            self.warnings.append(f"{base_name}.json: could not be read, skipped.")
            return None

    def load(self, *prefixes):
        """Concatenate every file into a single list, sorted by page number."""
        entries = []
        for prefix in prefixes:
            for base_name in self._match_names(prefix):
                page = self.parse_json(base_name)
                if page is None:
                    # parse_json already recorded why the file was skipped.
                    continue
                if isinstance(page, list):
                    entries.extend(page)
                else:
                    self.warnings.append(
                        f"{base_name}.json: unexpected contents, skipped."
                    )
        return entries

    def _match_names(self, prefix):
        """Return base names for prefixes by page."""
        names = []
        if prefix in self._files:
            names.append(prefix)

        pages = {}
        page_pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
        for base_name in self._files:
            match = page_pattern.match(base_name)
            if match:
                pages[int(match.group(1))] = base_name

        # Trakt splits large sections into pages numbered contiguously from 1, e.g.
        # watched-history-1.json. Only following that run keeps a custom list such as
        # lists-watchlist-2025 from being read as a page of lists-watchlist.
        page = 1
        while page in pages:
            names.append(pages[page])
            page += 1

        return names
