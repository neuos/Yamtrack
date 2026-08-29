from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django_celery_beat.models import PeriodicTask

from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError


class TraktOAuthViewTests(TestCase):
    """Test Trakt OAuth redirect URL handling."""

    def setUp(self):
        """Create user for the tests."""
        credentials = {"username": "testuser", "password": "testpass123"}
        self.user = get_user_model().objects.create_user(**credentials)
        self.client.login(**credentials)

    @override_settings(URLS=["https://yamtrack.example.com:8924"], TRAKT_API="client")
    def test_trakt_oauth_uses_configured_public_url(self):
        """Test Trakt authorization uses the configured public URL."""
        response = self.client.post(
            reverse("trakt_oauth"),
            {"mode": "new", "frequency": "once", "time": "14:30"},
        )

        self.assertEqual(response.status_code, 302)
        redirect = urlparse(response["Location"])
        query = parse_qs(redirect.query)
        self.assertEqual(redirect.scheme, "https")
        self.assertEqual(redirect.netloc, "trakt.tv")
        self.assertEqual(query["client_id"], ["client"])
        self.assertEqual(
            query["redirect_uri"],
            ["https://yamtrack.example.com:8924/import/trakt/private"],
        )

        state = self.client.session[query["state"][0]]
        self.assertEqual(
            state["redirect_uri"],
            "https://yamtrack.example.com:8924/import/trakt/private",
        )

    @override_settings(URLS=["https://yamtrack.example.com:8924"])
    @patch("integrations.views.tasks.import_trakt.delay")
    @patch("integrations.views.trakt.handle_oauth_callback")
    def test_trakt_callback_reuses_stored_redirect_uri(
        self,
        mock_oauth_callback,
        mock_import_trakt,
    ):
        """Test the token exchange and import task reuse the original redirect URI."""
        redirect_uri = "https://yamtrack.example.com:8924/import/trakt/private"
        session = self.client.session
        session["state-token"] = {
            "mode": "new",
            "frequency": "once",
            "time": "14:30",
            "redirect_uri": redirect_uri,
        }
        session.save()
        mock_oauth_callback.return_value = {
            "refresh_token": "refresh-token",
            "username": "trakt-user",
        }

        response = self.client.get(
            reverse("import_trakt_private"),
            {"code": "code", "state": "state-token"},
        )

        self.assertRedirects(response, reverse("import_data"))
        mock_oauth_callback.assert_called_once()
        self.assertEqual(
            mock_oauth_callback.call_args.kwargs["redirect_uri"],
            redirect_uri,
        )
        mock_import_trakt.assert_called_once()
        self.assertEqual(
            mock_import_trakt.call_args.kwargs["redirect_uri"],
            redirect_uri,
        )

    @patch("integrations.views.trakt.handle_oauth_callback")
    def test_trakt_callback_rejects_missing_state(self, mock_oauth_callback):
        """Test missing OAuth state is handled without a server error."""
        response = self.client.get(
            reverse("import_trakt_private"),
            {"code": "code"},
        )

        self.assertRedirects(response, reverse("import_data"))
        mock_oauth_callback.assert_not_called()


class TraktExportUploadViewTests(TestCase):
    """Test the Trakt export archive upload view."""

    def setUp(self):
        """Create user for the tests."""
        credentials = {"username": "testuser", "password": "testpass123"}
        self.user = get_user_model().objects.create_user(**credentials)
        self.client.login(**credentials)

    @patch("integrations.views.tasks.import_trakt.delay")
    def test_upload_queues_task_with_archive(self, mock_import_trakt):
        """The uploaded archive is forwarded to the import task."""
        upload = SimpleUploadedFile(
            "trakt-export.zip",
            b"archive-contents",
            content_type="application/zip",
        )

        # The upload is closed once the response finishes, so read it while queueing.
        queued = {}
        mock_import_trakt.side_effect = lambda **kwargs: queued.update(
            kwargs,
            contents=kwargs["file"].read(),
        )

        response = self.client.post(
            reverse("import_trakt_export"),
            {"mode": "new", "trakt_export_zip": upload},
        )

        self.assertEqual(response.status_code, 302)
        mock_import_trakt.assert_called_once()
        self.assertEqual(queued["user_id"], self.user.id)
        self.assertEqual(queued["mode"], "new")
        self.assertEqual(queued["contents"], b"archive-contents")

    @patch("integrations.views.tasks.import_trakt.delay")
    def test_missing_file_is_rejected(self, mock_import_trakt):
        """Posting without a file shows an error instead of queueing a task."""
        response = self.client.post(reverse("import_trakt_export"), {"mode": "new"})

        self.assertEqual(response.status_code, 302)
        mock_import_trakt.assert_not_called()


class NetflixConnectViewTests(TestCase):
    """Test validating Netflix cookies and stashing the discovered profiles."""

    def setUp(self):
        """Create and log in a user for the tests."""
        credentials = {"username": "testuser", "password": "testpass123"}
        self.user = get_user_model().objects.create_user(**credentials)
        self.client.login(**credentials)

    @patch("integrations.views.netflix_api.discover_session")
    def test_valid_cookies_stash_profiles_and_redirect(self, mock_discover_session):
        """Valid cookies redirect back to import_data with a netflix_state token."""
        profiles = [{"guid": "guid-1", "name": "Alex", "is_kids": False}]
        mock_discover_session.return_value = {
            "build_id": "vabc123",
            "profiles": profiles,
        }

        response = self.client.post(
            reverse("import_netflix_connect"),
            {
                "netflix_id": "nid",
                "secure_netflix_id": "snid",
                "mode": "new",
                "frequency": "once",
                "time": "14:30",
            },
        )

        self.assertEqual(response.status_code, 302)
        redirect = urlparse(response["Location"])
        self.assertEqual(redirect.path, reverse("import_data"))
        state_token = parse_qs(redirect.query)["netflix_state"][0]

        state = self.client.session[state_token]
        self.assertEqual(state["profiles"], profiles)
        self.assertEqual(helpers.decrypt(state["netflix_id"]), "nid")
        self.assertEqual(helpers.decrypt(state["secure_netflix_id"]), "snid")

    @patch("integrations.views.netflix_api.discover_session")
    def test_invalid_cookies_are_rejected(self, mock_discover_session):
        """An invalid-session error shows a message instead of queueing anything."""
        mock_discover_session.side_effect = MediaImportError("Invalid session.")
        session_keys_before = set(self.client.session.keys())

        response = self.client.post(
            reverse("import_netflix_connect"),
            {
                "netflix_id": "nid",
                "secure_netflix_id": "snid",
                "mode": "new",
                "frequency": "once",
                "time": "14:30",
            },
        )

        self.assertRedirects(response, reverse("import_data"))
        self.assertEqual(set(self.client.session.keys()), session_keys_before)


class NetflixAutoImportViewTests(TestCase):
    """Test finalizing a live Netflix import/schedule for a chosen profile."""

    def setUp(self):
        """Create and log in a user, and stash a connected Netflix session."""
        credentials = {"username": "testuser", "password": "testpass123"}
        self.user = get_user_model().objects.create_user(**credentials)
        self.client.login(**credentials)

        self.profiles = [
            {"guid": "guid-1", "name": "Alex", "is_kids": False},
            {"guid": "guid-2", "name": "Kids", "is_kids": True},
        ]

    def _stash_state(self, **overrides):
        state = {
            "netflix_id": helpers.encrypt("nid"),
            "secure_netflix_id": helpers.encrypt("snid"),
            "profiles": self.profiles,
            "mode": "new",
            "frequency": "once",
            "time": "14:30",
            **overrides,
        }
        session = self.client.session
        session["state-token"] = state
        session.save()

    @patch("integrations.views.tasks.import_netflix.delay")
    def test_once_queues_a_task_and_clears_session(self, mock_import_netflix):
        """A one-time import queues the task with the chosen profile."""
        self._stash_state()

        response = self.client.post(
            reverse("import_netflix_auto"),
            {"state": "state-token", "profile_guid": "guid-1"},
        )

        self.assertRedirects(response, reverse("import_data"))
        mock_import_netflix.assert_called_once()
        call_kwargs = mock_import_netflix.call_args.kwargs
        self.assertEqual(call_kwargs["user_id"], self.user.id)
        self.assertEqual(call_kwargs["mode"], "new")
        self.assertEqual(call_kwargs["profile_guid"], "guid-1")
        self.assertEqual(helpers.decrypt(call_kwargs["netflix_id"]), "nid")
        self.assertEqual(helpers.decrypt(call_kwargs["secure_netflix_id"]), "snid")
        self.assertNotIn("state-token", self.client.session)

    @patch("integrations.views.tasks.import_netflix.delay")
    def test_daily_creates_a_periodic_task(self, mock_import_netflix):
        """A daily frequency schedules a PeriodicTask instead of queueing once."""
        self._stash_state(frequency="daily")

        response = self.client.post(
            reverse("import_netflix_auto"),
            {"state": "state-token", "profile_guid": "guid-2"},
        )

        self.assertRedirects(response, reverse("import_data"))
        mock_import_netflix.assert_not_called()
        periodic_task = PeriodicTask.objects.get(task="Import from Netflix")
        self.assertIn("Kids", periodic_task.name)

    @patch("integrations.views.tasks.import_netflix.delay")
    def test_expired_state_is_rejected(self, mock_import_netflix):
        """A missing/expired state token shows an error instead of importing."""
        response = self.client.post(
            reverse("import_netflix_auto"),
            {"state": "not-a-real-token", "profile_guid": "guid-1"},
        )

        self.assertRedirects(response, reverse("import_data"))
        mock_import_netflix.assert_not_called()
