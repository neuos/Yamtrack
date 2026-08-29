import logging
from collections import defaultdict

import requests
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

import app
from app import helpers as app_helpers
from app.models import TV, Episode, MediaTypes, Movie, Season, Sources, Status
from app.providers import services
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from integrations.imports.tmdb_watch_history import (
    TMDBWatchHistoryImportMixin,
    WatchEntry,
)

logger = logging.getLogger(__name__)


def get_token(request):
    """View for getting the SIMKL OAuth2 token."""
    code = request.GET["code"]
    url = "https://api.simkl.com/oauth/token"

    headers = {
        "Content-Type": "application/json",
    }

    params = {
        "client_id": settings.SIMKL_ID,
        "client_secret": settings.SIMKL_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": app_helpers.build_absolute_app_url(
            request,
            reverse("import_simkl_private"),
        ),
    }

    try:
        token_response = app.providers.services.api_request(
            "SIMKL",
            "POST",
            url,
            headers=headers,
            params=params,
        )
    except services.ProviderAPIError as error:
        if error.status_code == requests.codes.unauthorized:
            msg = "Invalid SIMKL secret key."
            raise MediaImportError(msg) from error
        raise

    return {
        "access_token": token_response["access_token"],
        "username": get_username(token_response["access_token"]),
    }


def get_username(token):
    """Get the username from SIMKL using the provided token."""
    try:
        user_info = app.providers.services.api_request(
            "SIMKL",
            "POST",
            "https://api.simkl.com/users/settings",
            headers={
                "Authorization": f"Bearer {token}",
                "simkl-api-key": settings.SIMKL_ID,
                "Content-Type": "application/json",
            },
        )
    except services.ProviderAPIError as error:
        if error.status_code == requests.codes.unauthorized:
            msg = "Invalid SIMKL secret key."
            raise MediaImportError(msg) from error
        raise

    return user_info["user"]["name"]


def importer(token, user, mode):
    """Import tv shows, movies and anime from SIMKL."""
    simkl_importer = SimklImporter(token, user, mode)
    return simkl_importer.import_data()


class SimklImporter(TMDBWatchHistoryImportMixin):
    """Class to handle importing user data from Simkl."""

    SIMKL_API_BASE_URL = "https://api.simkl.com"

    def __init__(self, token, user, mode):
        """Initialize the importer with token, user, and mode.

        Args:
            token (str): Simkl OAuth token
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
        """
        self.token = helpers.decrypt(token)
        self.user = user
        self.mode = mode
        self.warnings = []

        self._init_watch_history_state()

        # Anime isn't TMDB-backed, so it still uses the older shared helper
        # pattern rather than the TMDB watch-history mixin.
        self.existing_media = helpers.get_existing_media(user)
        self.to_delete = defaultdict(lambda: defaultdict(set))
        self.bulk_media = defaultdict(list)

        logger.info(
            "Initialized Simkl importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import all user data from Simkl."""
        data = self._get_user_list()

        if not data:
            return {}, ""

        self._process_media_lists(data)

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)

        imported_counts, deduplicated_messages = self.finalize_watch_history_import()
        for media_type, media_list in self.bulk_media.items():
            if media_list:
                imported_counts[media_type] = len(media_list)

        return imported_counts, deduplicated_messages

    def _get_user_list(self):
        """Get the user's list from Simkl."""
        url = f"{self.SIMKL_API_BASE_URL}/sync/all-items/"
        headers = {
            "Authorization": f"Bearer: {self.token}",
            "simkl-api-key": settings.SIMKL_ID,
        }
        params = {
            "extended": "full",
            "episode_watched_at": "yes",
            "memos": "yes",
        }

        return app.providers.services.api_request(
            "SIMKL",
            "GET",
            url,
            headers=headers,
            params=params,
        )

    def _process_media_lists(self, data):
        """Process all media types from Simkl."""
        if "shows" in data:
            self._process_tv_list(data["shows"])
        if "movies" in data:
            self._process_movie_list(data["movies"])
        if "anime" in data:
            self._process_anime_list(data["anime"])

    def _process_tv_list(self, tv_list):
        """Process TV list from Simkl."""
        logger.info("Processing tv shows")
        existing_tv_ids = set()

        for tv in tv_list:
            try:
                title = tv["show"]["title"]
                logger.debug("Processing %s", title)

                try:
                    tmdb_id = tv["show"]["ids"]["tmdb"]
                except KeyError:
                    self.warnings.append(f"{title}: No TMDB ID found")
                    continue

                if tmdb_id in existing_tv_ids:
                    self.warnings.append(
                        f"{title} ({tmdb_id}) already present in the import list",
                    )
                    continue

                tv_status = self._get_status(tv["status"])

                try:
                    season_numbers = [season["number"] for season in tv["seasons"]]
                except KeyError:
                    season_numbers = []

                try:
                    metadata = app.providers.tmdb.tv_with_seasons(
                        tmdb_id,
                        season_numbers,
                    )
                except services.ProviderAPIError as error:
                    if error.status_code == requests.codes.not_found:
                        self.warnings.append(
                            f"{title}: not found in {Sources.TMDB.label} "
                            f"with ID {tmdb_id}.",
                        )
                        continue
                    raise

                tv_id = str(tmdb_id)
                tv_item = self._get_or_create_item(MediaTypes.TV.value, tv_id, metadata)

                tv_instance, _created, skip = self._resolve_or_create(
                    TV,
                    self.tv_creates,
                    self.tv_updates,
                    MediaTypes.TV.value,
                    tv_id,
                    tv_item,
                )
                existing_tv_ids.add(tmdb_id)
                if skip:
                    continue

                self._apply_show_status(
                    tv_instance,
                    WatchEntry(
                        media_type=MediaTypes.TV.value,
                        display_title=title,
                        status=tv_status,
                        score=tv["user_rating"],
                        notes=tv["memo"]["text"] if tv["memo"] != {} else "",
                        watched_at=self._get_history_date(tv),
                    ),
                )

                if season_numbers:
                    self._process_seasons_and_episodes(
                        tv,
                        tv_instance,
                        metadata,
                        tv_status,
                    )

            except Exception as error:
                msg = f"Error processing entry: {tv}"
                raise MediaImportUnexpectedError(msg) from error

        logger.info("Processed %d tv shows", len(tv_list))

    def _process_seasons_and_episodes(self, tv, tv_instance, metadata, tv_status):
        """Process seasons and episodes for a TV show."""
        tmdb_id = str(tv["show"]["ids"]["tmdb"])

        for season in tv["seasons"]:
            season_number = season["number"]
            episodes = season["episodes"]
            season_metadata = metadata[f"season/{season_number}"]

            season_item = self._get_or_create_item(
                MediaTypes.SEASON.value,
                tmdb_id,
                {"title": metadata["title"], "image": season_metadata["image"]},
                season_number=season_number,
            )

            if episodes[-1]["number"] == season_metadata["max_progress"]:
                season_status = Status.COMPLETED.value
            else:
                season_status = tv_status

            season_instance, _created, season_skip = self._resolve_or_create(
                Season,
                self.season_creates,
                self.season_updates,
                MediaTypes.SEASON.value,
                tmdb_id,
                season_item,
                season_number=season_number,
                create_kwargs={
                    "related_tv": tv_instance,
                    "status": Status.IN_PROGRESS.value,
                },
            )
            if not season_skip:
                self._apply_show_status(
                    season_instance,
                    WatchEntry(
                        media_type=MediaTypes.SEASON.value,
                        display_title=f"{metadata['title']} S{season_number}",
                        status=season_status,
                        watched_at=self._get_history_date(tv),
                    ),
                )

            # Process episodes
            for episode in episodes:
                ep_img = self._get_episode_image(episode, season_number, metadata)
                episode_item = self._get_or_create_item(
                    MediaTypes.EPISODE.value,
                    tmdb_id,
                    {"title": metadata["title"], "image": ep_img},
                    season_number=season_number,
                    episode_number=episode["number"],
                )

                episode_instance, _created, episode_skip = self._resolve_or_create(
                    Episode,
                    self.episode_creates,
                    self.episode_updates,
                    MediaTypes.EPISODE.value,
                    tmdb_id,
                    episode_item,
                    season_number=season_number,
                    episode_number=episode["number"],
                    create_kwargs={"related_season": season_instance},
                )
                if episode_skip:
                    continue

                episode_instance.end_date = self._get_date(episode.get("watched_at"))
                episode_instance._history_date = (
                    episode_instance.end_date or timezone.now()
                )

    def _get_episode_image(self, episode, season_number, metadata):
        """Get the image for the episode."""
        for episode_metadata in metadata[f"season/{season_number}"]["episodes"]:
            if episode_metadata["episode_number"] == episode["number"]:
                return (
                    f"https://image.tmdb.org/t/p/w500{episode_metadata['still_path']}"
                )
        return settings.IMG_NONE

    def _process_movie_list(self, movie_list):
        """Process movie list from Simkl."""
        logger.info("Processing movies")
        existing_movie_ids = set()

        for movie in movie_list:
            try:
                title = movie["movie"]["title"]
                logger.debug("Processing %s", title)

                try:
                    tmdb_id = movie["movie"]["ids"]["tmdb"]
                except KeyError:
                    self.warnings.append(f"{title}: No TMDB ID found")
                    continue

                if tmdb_id in existing_movie_ids:
                    self.warnings.append(
                        f"{title} ({tmdb_id}) already present in the import list",
                    )
                    continue

                movie_status = self._get_status(movie["status"])

                try:
                    metadata = app.providers.tmdb.movie(tmdb_id)
                except services.ProviderAPIError as error:
                    if error.status_code == requests.codes.not_found:
                        self.warnings.append(
                            f"{title}: not found in {Sources.TMDB.label} "
                            f"with ID {tmdb_id}.",
                        )
                        continue
                    raise

                movie_id = str(tmdb_id)
                movie_item = self._get_or_create_item(
                    MediaTypes.MOVIE.value,
                    movie_id,
                    metadata,
                )

                movie_instance, _created, skip = self._resolve_or_create(
                    Movie,
                    self.movie_creates,
                    self.movie_updates,
                    MediaTypes.MOVIE.value,
                    movie_id,
                    movie_item,
                )
                existing_movie_ids.add(tmdb_id)
                if skip:
                    continue

                self._apply_movie_fields(
                    movie_instance,
                    WatchEntry(
                        media_type=MediaTypes.MOVIE.value,
                        display_title=title,
                        status=movie_status,
                        score=movie["user_rating"],
                        notes=movie["memo"]["text"] if movie["memo"] != {} else "",
                        watched_at=self._get_date(movie.get("last_watched_at")),
                    ),
                )
                movie_instance._history_date = self._get_history_date(movie)

            except Exception as error:
                msg = f"Error processing entry: {movie}"
                raise MediaImportUnexpectedError(msg) from error

        logger.info("Processed %d movies", len(movie_list))

    def _process_anime_list(self, anime_list):
        """Process anime list from Simkl."""
        logger.info("Processing anime")
        existing_anime_ids = set()

        for anime in anime_list:
            try:
                title = anime["show"]["title"]
                logger.debug("Processing %s", title)

                try:
                    mal_id = anime["show"]["ids"]["mal"]
                except KeyError:
                    self.warnings.append(f"{title}: No MyAnimeList ID found")
                    continue

                if mal_id in existing_anime_ids:
                    self.warnings.append(
                        f"{title} ({mal_id}) already present in the import list",
                    )
                    continue

                # Check if we should process this entry based on mode
                if not helpers.should_process_media(
                    self.existing_media,
                    self.to_delete,
                    MediaTypes.ANIME.value,
                    Sources.MAL.value,
                    str(mal_id),
                    self.mode,
                ):
                    continue

                anime_status = self._get_status(anime["status"])

                try:
                    metadata = app.providers.mal.anime(mal_id)
                except services.ProviderAPIError as error:
                    if error.status_code == requests.codes.not_found:
                        self.warnings.append(
                            f"{title}: not found in {Sources.MAL.label} "
                            f"with ID {mal_id}.",
                        )
                        continue
                    raise

                anime_item, _ = app.models.Item.objects.get_or_create(
                    media_id=mal_id,
                    source=Sources.MAL.value,
                    media_type=MediaTypes.ANIME.value,
                    defaults={
                        "title": metadata["title"],
                        "image": metadata["image"],
                    },
                )

                anime_instance = app.models.Anime(
                    item=anime_item,
                    user=self.user,
                    status=anime_status,
                    score=anime["user_rating"],
                    progress=anime["watched_episodes_count"],
                    start_date=self._get_start_date(anime),
                    end_date=self._get_end_date(
                        anime_status,
                        anime.get("last_watched_at"),
                    ),
                    notes=anime["memo"]["text"] if anime["memo"] != {} else "",
                )
                anime_instance._history_date = self._get_history_date(anime)

                self.bulk_media[MediaTypes.ANIME.value].append(anime_instance)
                existing_anime_ids.add(mal_id)

            except Exception as error:
                msg = f"Error processing entry: {anime}"
                raise MediaImportUnexpectedError(msg) from error

        logger.info("Processed %d anime", len(anime_list))

    def _get_status(self, status):
        """Map Simkl status to internal status."""
        status_mapping = {
            "completed": Status.COMPLETED.value,
            "watching": Status.IN_PROGRESS.value,
            "plantowatch": Status.PLANNING.value,
            "hold": Status.PAUSED.value,
            "dropped": Status.DROPPED.value,
        }

        return status_mapping.get(status, Status.IN_PROGRESS.value)

    def _get_date(self, date_str):
        """Convert the date from Simkl to a date object, stripping seconds."""
        if date_str:
            return parse_datetime(date_str).replace(second=0, microsecond=0)
        return None

    def _get_start_date(self, anime):
        """Get the start date based on earliest watched episode."""
        if "seasons" in anime:
            episodes = anime["seasons"][0]["episodes"]
            current_min_date = None

            for episode in episodes:
                date = self._get_date(episode.get("watched_at"))
                if date is not None and (
                    current_min_date is None or date < current_min_date
                ):
                    current_min_date = date

            return current_min_date

        return None

    def _get_end_date(self, anime_status, last_watched_at):
        """Get the end date based on the anime status."""
        if anime_status == Status.COMPLETED.value:
            return self._get_date(last_watched_at)
        return None

    def _get_history_date(self, entry):
        """Get the history date from the entry."""
        if entry.get("last_watched_at"):
            return parse_datetime(entry.get("last_watched_at"))

        if entry.get("added_to_watchlist_at"):
            return parse_datetime(entry.get("added_to_watchlist_at"))

        return timezone.now()
