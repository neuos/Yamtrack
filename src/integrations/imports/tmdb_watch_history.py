"""Shared TMDB lookup + status/dedup/store workflow for watch-history importers.

Every watch-history importer (Amazon, Netflix, IMDB, Simkl, Trakt, ...) does the
same four steps: (1) get raw data from a file or API, (2) transform it into a
common model, (3) look it up against TMDB, and (4) store it into the app DB
considering status, duplicates, and "new"/"overwrite" mode. Steps 1 and 2 are
source-specific and stay in each importer's own module. Steps 3 and 4 are
implemented once here, driven by the `WatchEntry` common model.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from simple_history.utils import bulk_create_with_history, bulk_update_with_history

from app.models import TV, Episode, Item, MediaTypes, Movie, Season, Sources, Status
from app.providers import services
from integrations.imports.episode_title_matching import match_episode_number

logger = logging.getLogger(__name__)


@dataclass
class WatchEntry:
    """Common model a source's step 2 produces for the shared step 3/4 workflow.

    `media_type` is MediaTypes.MOVIE.value or MediaTypes.EPISODE.value: the
    identity granularity this entry is deduplicated and stored at (a TV show
    is always represented by grouping its episodes, never as a standalone
    entry). Either `tmdb_id` is already known (Trakt/Simkl/IMDB get one from
    their own API/bridge), or `search_title`/`search_media_type` are set so
    the shared workflow can resolve it via a TMDB title search (Amazon/
    Netflix). `episode_number` may be left unset with `episode_title` set
    instead, in which case the shared workflow fuzzy-matches the title
    against TMDB's episode list for the season (Netflix).

    `status`/`score`/`notes` are explicit overrides for sources that already
    know them (IMDB ratings, Simkl list status, Trakt ratings/watchlist).
    When `status` is left as None, the shared workflow derives
    COMPLETED/IN_PROGRESS from watched progress instead.

    `event_based` marks this entry as one play among possibly several for the
    same Movie/Episode identity (Amazon/Netflix/Trakt history rows), rather
    than the current state of that identity (IMDB, Simkl, Trakt ratings/
    watchlist/comments). Movie and Episode have no database uniqueness
    constraint on identity - the app's own statistics deliberately support a
    rewatched item having multiple rows - so event-based entries are
    deduplicated on (identity, watched_at) instead of identity alone: a new
    watched_at creates a new row (preserving rewatch history), but
    re-importing the same play again is a no-op. TV and Season are never
    event-based: both have a real DB uniqueness constraint on identity, so
    they're always a single row updated in place regardless of this flag.
    """

    media_type: str
    display_title: str

    tmdb_id: str | None = None
    search_title: str | None = None
    search_media_type: str | None = None

    season_number: int | None = None
    episode_number: int | None = None
    episode_title: str | None = None

    image_url: str = ""
    watched_at: datetime | None = None
    status: str | None = None
    score: float | None = None
    notes: str | None = None
    event_based: bool = False


class TMDBWatchHistoryImportMixin:
    """Shared step 3 (TMDB lookup) and step 4 (store) workflow."""

    def _init_watch_history_state(self):
        """Initialize caches and bulk create/update lists. Call from __init__."""
        self._tmdb_search_cache = {}
        self._tmdb_metadata_cache = {}
        self._pending_records = {}
        self._identity_instances = {}

        self.movie_creates = []
        self.movie_updates = []
        self.tv_creates = []
        self.tv_updates = []
        self.season_creates = []
        self.season_updates = []
        self.episode_creates = []
        self.episode_updates = []

    # ---- step 3: TMDB lookup ----

    def _search_tmdb(self, media_type, query):
        """Search TMDB by title, memoized for the life of the import."""
        cache_key = (media_type, query.casefold())
        if cache_key in self._tmdb_search_cache:
            return self._tmdb_search_cache[cache_key]

        try:
            results = services.search(media_type, query, 1, Sources.TMDB.value).get(
                "results",
                [],
            )
        except services.ProviderAPIError:
            results = []

        match = results[0] if results else None
        self._tmdb_search_cache[cache_key] = match
        return match

    def _search_tmdb_with_fallback(self, primary_type, secondary_type, query):
        """Search `primary_type`, falling back to `secondary_type` if no match.

        Returns (media_type, result) for whichever type matched, or (None, None).
        """
        match = self._search_tmdb(primary_type, query)
        if match:
            return primary_type, match

        match = self._search_tmdb(secondary_type, query)
        if match:
            return secondary_type, match

        return None, None

    def _get_watch_history_metadata(
        self,
        media_type,
        tmdb_id,
        title,
        *,
        season_number=None,
        episode_number=None,
    ):
        """Fetch TMDB metadata, memoized, converting 404s into warnings."""
        cache_key = (media_type, tmdb_id, season_number, episode_number)
        if cache_key in self._tmdb_metadata_cache:
            return self._tmdb_metadata_cache[cache_key]

        kwargs = {}
        if season_number is not None:
            kwargs["season_numbers"] = [season_number]
        if episode_number is not None:
            kwargs["episode_number"] = episode_number

        try:
            metadata = services.get_media_metadata(
                media_type,
                tmdb_id,
                Sources.TMDB.value,
                **kwargs,
            )
        except services.ProviderAPIError as error:
            if error.status_code == requests.codes.not_found:
                label = title
                if media_type == MediaTypes.SEASON.value and season_number is not None:
                    label = f"{title} S{season_number}"
                self.warnings.append(
                    f"{label}: not found in {Sources.TMDB.label} with ID {tmdb_id}.",
                )
                self._tmdb_metadata_cache[cache_key] = None
                return None
            raise

        self._tmdb_metadata_cache[cache_key] = metadata
        return metadata

    def _get_or_create_item(
        self,
        media_type,
        tmdb_id,
        metadata,
        *,
        season_number=None,
        episode_number=None,
    ):
        """Upsert the Item row for a TMDB-backed identity."""
        item_kwargs = {
            "media_id": tmdb_id,
            "source": Sources.TMDB.value,
            "media_type": media_type,
        }
        if season_number is not None:
            item_kwargs["season_number"] = season_number
        if episode_number is not None:
            item_kwargs["episode_number"] = episode_number

        defaults = {
            "title": metadata["title"],
            "image": metadata.get("image") or settings.IMG_NONE,
        }

        item, _ = Item.objects.update_or_create(**item_kwargs, defaults=defaults)
        return item

    def _with_image_fallback(self, metadata, image_url):
        """Prefer TMDB metadata but fall back to a source-provided image."""
        enriched = dict(metadata or {})
        if not enriched.get("image") and image_url:
            enriched["image"] = image_url
        if not enriched.get("image"):
            enriched["image"] = settings.IMG_NONE
        return enriched

    # ---- resolving raw entries against TMDB (amazon/netflix-style title search) ----

    def resolve_movie_entries(self, raw_entries):
        """Search unresolved movie entries against TMDB, deduplicating by identity.

        Event-based entries (see `WatchEntry.event_based`) are deduplicated on
        identity+`watched_at` instead, so distinct plays of the same movie are
        all kept rather than collapsed to the latest.
        """
        resolved = {}
        for entry in raw_entries:
            if entry.tmdb_id is None:
                match = self._search_tmdb(
                    entry.search_media_type or MediaTypes.MOVIE.value,
                    entry.search_title,
                )
                if not match:
                    self.warnings.append(
                        f"{entry.display_title}: Couldn't find a match "
                        f"in {Sources.TMDB.label}",
                    )
                    continue
                entry.tmdb_id = str(match["media_id"])

            key = (
                (entry.tmdb_id, entry.watched_at)
                if entry.event_based
                else entry.tmdb_id
            )
            existing = resolved.get(key)
            if not existing or self._is_more_recent(entry, existing):
                resolved[key] = entry

        return resolved

    def resolve_tv_entries(self, raw_entries):
        """Search/identify unresolved TV episode entries, grouped for storage.

        Returns {tmdb_id: {season_number: [WatchEntry, ...]}}. Entries that
        already carry a concrete episode_number pass through unchanged
        (Amazon). Entries with only an episode_title are fuzzy-matched
        against TMDB's episode list, scanning all seasons if season_number
        isn't known either (Netflix).
        """
        grouped = {}
        deduped = {}

        for entry in raw_entries:
            if entry.tmdb_id is None:
                match = self._search_tmdb(
                    entry.search_media_type or MediaTypes.TV.value,
                    entry.search_title,
                )
                if not match:
                    self.warnings.append(
                        f"{entry.display_title}: Couldn't find a match "
                        f"in {Sources.TMDB.label}",
                    )
                    continue
                entry.tmdb_id = str(match["media_id"])

            if entry.episode_number is None:
                self._identify_episode(entry)
                if entry.episode_number is None:
                    continue

            identity_key = (entry.tmdb_id, entry.season_number, entry.episode_number)
            key = (
                (*identity_key, entry.watched_at) if entry.event_based else identity_key
            )
            existing = deduped.get(key)
            if not existing or self._is_more_recent(entry, existing):
                deduped[key] = entry

        for key, entry in deduped.items():
            tmdb_id, season_number = key[0], key[1]
            grouped.setdefault(tmdb_id, {}).setdefault(season_number, []).append(entry)

        return grouped

    def _is_more_recent(self, entry, existing):
        if entry.watched_at is None:
            return False
        if existing.watched_at is None:
            return True
        return entry.watched_at > existing.watched_at

    def _identify_episode(self, entry):
        """Resolve entry.episode_number (and season_number) via fuzzy title match."""
        if entry.season_number is not None:
            season_metadata = self._get_watch_history_metadata(
                MediaTypes.SEASON.value,
                entry.tmdb_id,
                entry.display_title,
                season_number=entry.season_number,
            )
            if season_metadata:
                entry.episode_number = match_episode_number(
                    entry.episode_title,
                    season_metadata.get("episodes", []),
                )
            if entry.episode_number is None:
                self.warnings.append(
                    f"{entry.display_title}: Couldn't match episode "
                    f"'{entry.episode_title}' in Season {entry.season_number} "
                    f"on {Sources.TMDB.label}",
                )
            return

        tv_metadata = self._get_watch_history_metadata(
            MediaTypes.TV.value,
            entry.tmdb_id,
            entry.display_title,
        )
        if not tv_metadata:
            return

        related_seasons = tv_metadata.get("related", {}).get("seasons", [])
        ordered_seasons = sorted(
            (
                s.get("season_number")
                for s in related_seasons
                if s.get("season_number") is not None
            ),
            key=lambda number: (number == 0, number),
        )
        for season_number in ordered_seasons:
            season_metadata = self._get_watch_history_metadata(
                MediaTypes.SEASON.value,
                entry.tmdb_id,
                entry.display_title,
                season_number=season_number,
            )
            if not season_metadata:
                continue
            episode_number = match_episode_number(
                entry.episode_title,
                season_metadata.get("episodes", []),
            )
            if episode_number is not None:
                entry.season_number = season_number
                entry.episode_number = episode_number
                return

        self.warnings.append(
            f"{entry.display_title}: Couldn't match episode "
            f"'{entry.episode_title}' in any season on {Sources.TMDB.label}",
        )

    # ---- step 4: store, considering status/duplicates/mode ----

    def _resolve_or_create(  # noqa: PLR0913
        self,
        model,
        creates_list,
        updates_list,
        media_type,
        tmdb_id,
        item,
        *,
        season_number=None,
        episode_number=None,
        create_kwargs=None,
        event_key=None,
    ):
        """Find the existing record for this identity, or queue a new one.

        Returns (instance, created, skip). `skip` is True when the record
        already exists and mode is "new" - the caller should leave it
        untouched. Memoized per identity (or per identity+`event_key` when
        given - see `WatchEntry.event_based`) so multiple entries touching
        the same show/season/episode within one import (e.g. Trakt's
        history, watchlist, ratings and comments passes) share the same
        instance.
        """
        pending_key = (media_type, tmdb_id, season_number, episode_number, event_key)
        if pending_key in self._pending_records:
            return self._pending_records[pending_key]

        filter_kwargs = {"item__media_id": tmdb_id, "item__source": Sources.TMDB.value}
        if season_number is not None:
            filter_kwargs["item__season_number"] = season_number
        if episode_number is not None:
            filter_kwargs["item__episode_number"] = episode_number
        if event_key is not None:
            filter_kwargs["end_date"] = event_key
        if media_type == MediaTypes.EPISODE.value:
            filter_kwargs["related_season__user"] = self.user
        else:
            filter_kwargs["user"] = self.user

        existing = model.objects.filter(**filter_kwargs).first()

        if existing:
            skip = self.mode == "new"
            if not skip:
                updates_list.append(existing)
            result = (existing, False, skip)
        else:
            kwargs = {"item": item, **(create_kwargs or {})}
            if media_type != MediaTypes.EPISODE.value:
                kwargs["user"] = self.user
            instance = model(**kwargs)
            creates_list.append(instance)
            result = (instance, True, False)

        self._pending_records[pending_key] = result
        if not result[2]:
            identity_key = (media_type, tmdb_id, season_number, episode_number)
            self._identity_instances.setdefault(identity_key, []).append(result[0])
        return result

    def _identity_touched_this_run(self, media_type, tmdb_id, *, season_number=None):
        """Return instances already resolved this run for an identity.

        Used for non-event-based touches (Trakt's ratings/watchlist/comments)
        so they can update every already-created-this-run play of a movie or
        episode instead of resolving/creating a separate, untouched row keyed
        by its own (lack of an) event_key.
        """
        identity_key = (media_type, tmdb_id, season_number, None)
        return self._identity_instances.get(identity_key, [])

    def _apply_movie_fields(self, movie, entry):
        """Apply status/progress/dates/score/notes from `entry` onto a Movie.

        Movie.progress/start_date/end_date are plain stored fields. `watched_at`
        always drives the history timestamp, but only sets progress/dates when
        the movie is actually completed (a source can supply `watched_at` as a
        "last touched" date alongside a non-completed explicit status, e.g.
        IMDB's Planning entries, which should not gain a fake end date).
        """
        if entry.status is not None:
            movie.status = entry.status
        elif entry.watched_at is not None:
            movie.status = Status.COMPLETED.value

        if movie.status == Status.COMPLETED.value:
            movie.progress = 1
            if entry.watched_at is not None:
                if movie.start_date is None:
                    movie.start_date = entry.watched_at
                movie.end_date = entry.watched_at

        self._apply_score_and_notes(movie, entry)
        movie._history_date = entry.watched_at or timezone.now()

    def _apply_show_status(self, instance, entry):
        """Apply status/score/notes from `entry` onto a TV or Season.

        TV/Season progress/start_date/end_date are computed properties
        derived live from their episodes, so only status/score/notes (real
        stored fields) are ever written here.
        """
        if entry.status is not None:
            instance.status = entry.status

        self._apply_score_and_notes(instance, entry)
        instance._history_date = entry.watched_at or timezone.now()

    def _apply_score_and_notes(self, instance, entry):
        if entry.score is not None:
            instance.score = entry.score
        if entry.notes is not None:
            instance.notes = entry.notes

    def process_movie_entry(self, entry):
        """Create or update a Movie record from a resolved watch entry.

        A non-event-based entry (a rating/watchlist/comment touch, not a
        play) applies to every play of this movie already resolved earlier
        in the same run, rather than resolving its own separate record keyed
        by the absence of an event - otherwise a movie both watched and
        rated in the same import would end up with two rows.
        """
        tmdb_id = entry.tmdb_id

        if not entry.event_based:
            already_touched = self._identity_touched_this_run(
                MediaTypes.MOVIE.value,
                tmdb_id,
            )
            if already_touched:
                for movie in already_touched:
                    self._apply_score_and_notes(movie, entry)
                    if entry.status is not None:
                        movie.status = entry.status
                return

        metadata = self._get_watch_history_metadata(
            MediaTypes.MOVIE.value,
            tmdb_id,
            entry.display_title,
        )
        if not metadata:
            return

        item = self._get_or_create_item(
            MediaTypes.MOVIE.value,
            tmdb_id,
            self._with_image_fallback(metadata, entry.image_url),
        )

        movie, _created, skip = self._resolve_or_create(
            Movie,
            self.movie_creates,
            self.movie_updates,
            MediaTypes.MOVIE.value,
            tmdb_id,
            item,
            event_key=entry.watched_at if entry.event_based else None,
        )
        if skip:
            return

        self._apply_movie_fields(movie, entry)

    def process_single_media_entry(
        self,
        entry,
        model,
        *,
        related_tv=None,
        create_status=None,
    ):
        """Create or update a standalone TV or Season record (no episodes).

        Used when a source already knows the show/season's status directly
        (IMDB, Simkl, Trakt's watchlist/ratings/comments passes) rather than
        deriving it from watched episode progress. `create_status` is only
        used as the initial status for a brand-new record when `entry.status`
        itself is None (e.g. Trakt scaffolding a TV parent for a season-level
        rating); it's ignored once `entry.status` is set or the record exists.
        """
        media_type = MediaTypes.SEASON.value if related_tv else MediaTypes.TV.value
        tmdb_id = entry.tmdb_id
        metadata = self._get_watch_history_metadata(
            media_type,
            tmdb_id,
            entry.display_title,
            season_number=entry.season_number,
        )
        if not metadata:
            return None

        item = self._get_or_create_item(
            media_type,
            tmdb_id,
            self._with_image_fallback(metadata, entry.image_url),
            season_number=entry.season_number,
        )

        creates_list = self.season_creates if related_tv else self.tv_creates
        updates_list = self.season_updates if related_tv else self.tv_updates
        create_kwargs = {"related_tv": related_tv} if related_tv else {}
        if create_status is not None:
            create_kwargs["status"] = create_status

        instance, _created, skip = self._resolve_or_create(
            model,
            creates_list,
            updates_list,
            media_type,
            tmdb_id,
            item,
            season_number=entry.season_number,
            create_kwargs=create_kwargs,
        )
        if skip:
            return instance

        self._apply_show_status(instance, entry)
        return instance

    def process_tv_entries(self, tmdb_id, seasons):  # noqa: C901, PLR0912
        """Create/update a TV show and its seasons/episodes from watch history.

        `seasons` is {season_number: [WatchEntry, ...]}, as produced by
        `resolve_tv_entries`. Status is derived from accumulated progress
        unless an entry explicitly sets one (Simkl already knows its show's
        list status).
        """
        season_numbers = sorted(seasons.keys())
        if not season_numbers:
            return

        first_entry = seasons[season_numbers[0]][0]
        tv_metadata = self._get_watch_history_metadata(
            MediaTypes.TV.value,
            tmdb_id,
            first_entry.display_title,
        )
        if not tv_metadata:
            return

        tv_item = self._get_or_create_item(
            MediaTypes.TV.value,
            tmdb_id,
            self._with_image_fallback(tv_metadata, first_entry.image_url),
        )

        tv, tv_created, tv_skip = self._resolve_or_create(
            TV,
            self.tv_creates,
            self.tv_updates,
            MediaTypes.TV.value,
            tmdb_id,
            tv_item,
            create_kwargs={"status": Status.IN_PROGRESS.value},
        )
        if tv_skip:
            return

        # TV.progress/start_date/end_date are computed live from episodes, so
        # track existing watched-episode counts ourselves to decide status
        # before those episodes are actually saved to the DB.
        tv_existing_progress = (
            0
            if tv_created
            else Episode.objects.filter(related_season__related_tv=tv)
            .values("item__season_number", "item__episode_number")
            .distinct()
            .count()
        )
        show_episode_numbers = set()
        show_max_progress = tv_metadata.get("max_progress")
        explicit_status = first_entry.status

        for season_number in season_numbers:
            season_entries = sorted(
                seasons[season_number],
                key=lambda entry: entry.watched_at or timezone.now(),
            )
            season_metadata = self._get_watch_history_metadata(
                MediaTypes.SEASON.value,
                tmdb_id,
                season_entries[0].display_title,
                season_number=season_number,
            )
            if not season_metadata:
                continue

            season_item = self._get_or_create_item(
                MediaTypes.SEASON.value,
                tmdb_id,
                self._with_image_fallback(season_metadata, season_entries[0].image_url),
                season_number=season_number,
            )

            season, season_created, season_skip = self._resolve_or_create(
                Season,
                self.season_creates,
                self.season_updates,
                MediaTypes.SEASON.value,
                tmdb_id,
                season_item,
                season_number=season_number,
                create_kwargs={"related_tv": tv, "status": Status.IN_PROGRESS.value},
            )
            if season_skip:
                continue

            # Count distinct episodes, not rows: an event-based episode can have
            # several rows (one per rewatch) for the same episode_number.
            season_existing_progress = (
                0
                if season_created
                else season.episodes.values("item__episode_number").distinct().count()
            )
            season_episode_numbers = set()
            season_max_progress = season_metadata.get("max_progress")

            for episode_entry in season_entries:
                episode_metadata = self._get_watch_history_metadata(
                    MediaTypes.EPISODE.value,
                    tmdb_id,
                    episode_entry.display_title,
                    season_number=season_number,
                    episode_number=episode_entry.episode_number,
                )
                if not episode_metadata:
                    continue

                episode_item = self._get_or_create_item(
                    MediaTypes.EPISODE.value,
                    tmdb_id,
                    self._with_image_fallback(
                        episode_metadata, episode_entry.image_url
                    ),
                    season_number=season_number,
                    episode_number=episode_entry.episode_number,
                )

                episode, _episode_created, episode_skip = self._resolve_or_create(
                    Episode,
                    self.episode_creates,
                    self.episode_updates,
                    MediaTypes.EPISODE.value,
                    tmdb_id,
                    episode_item,
                    season_number=season_number,
                    episode_number=episode_entry.episode_number,
                    create_kwargs={"related_season": season},
                    event_key=(
                        episode_entry.watched_at if episode_entry.event_based else None
                    ),
                )
                if episode_skip:
                    continue

                episode.end_date = episode_entry.watched_at
                episode._history_date = episode_entry.watched_at or timezone.now()

                season_episode_numbers.add(episode_entry.episode_number)
                show_episode_numbers.add((season_number, episode_entry.episode_number))

            if explicit_status is not None:
                season.status = explicit_status
            else:
                season_total_progress = season_existing_progress + len(
                    season_episode_numbers,
                )
                season.status = (
                    Status.COMPLETED.value
                    if season_max_progress
                    and season_total_progress >= season_max_progress
                    else Status.IN_PROGRESS.value
                )
            season._history_date = season_entries[-1].watched_at or timezone.now()

        if not show_episode_numbers and explicit_status is None:
            return

        if explicit_status is not None:
            tv.status = explicit_status
        else:
            show_total_progress = tv_existing_progress + len(show_episode_numbers)
            tv.status = (
                Status.COMPLETED.value
                if show_max_progress and show_total_progress >= show_max_progress
                else Status.IN_PROGRESS.value
            )
        tv._history_date = timezone.now()

        self._apply_score_and_notes(tv, first_entry)

    def finalize_watch_history_import(self):
        """Bulk create/update all queued records in one transaction.

        Returns (imported_counts, deduplicated_warnings_or_none).
        """
        with transaction.atomic():
            if self.movie_creates:
                bulk_create_with_history(
                    self.movie_creates,
                    Movie,
                    batch_size=500,
                    default_user=self.user,
                )
            if self.tv_creates:
                bulk_create_with_history(
                    self.tv_creates,
                    TV,
                    batch_size=500,
                    default_user=self.user,
                )
            if self.season_creates:
                bulk_create_with_history(
                    self.season_creates,
                    Season,
                    batch_size=500,
                    default_user=self.user,
                )
            if self.episode_creates:
                bulk_create_with_history(
                    self.episode_creates,
                    Episode,
                    batch_size=500,
                    default_user=self.user,
                )

            if self.movie_updates:
                bulk_update_with_history(
                    self.movie_updates,
                    Movie,
                    fields=[
                        "status",
                        "progress",
                        "start_date",
                        "end_date",
                        "score",
                        "notes",
                    ],
                    default_user=self.user,
                )
            if self.tv_updates:
                bulk_update_with_history(
                    self.tv_updates,
                    TV,
                    fields=["status", "score", "notes"],
                    default_user=self.user,
                )
            if self.season_updates:
                bulk_update_with_history(
                    self.season_updates,
                    Season,
                    fields=["status", "score", "notes"],
                    default_user=self.user,
                )
            if self.episode_updates:
                bulk_update_with_history(
                    self.episode_updates,
                    Episode,
                    fields=["end_date"],
                    default_user=self.user,
                )

        imported_counts = {
            MediaTypes.MOVIE.value: len(self.movie_creates) + len(self.movie_updates),
            MediaTypes.TV.value: len(self.tv_creates) + len(self.tv_updates),
            MediaTypes.SEASON.value: len(self.season_creates)
            + len(self.season_updates),
            MediaTypes.EPISODE.value: len(self.episode_creates)
            + len(self.episode_updates),
        }

        deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
        return imported_counts, deduplicated_messages if self.warnings else None
