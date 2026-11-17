import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from evalve import EvalveError, readiness


class ReadinessTest(unittest.TestCase):
    def test_https_health_and_unauthenticated_401_are_required(self):
        url = "https://evalve.example.com"
        opener = Mock()
        opener.open.side_effect = [
            io.BytesIO(b'{"database":"ok"}'),
            HTTPError(url, 401, "Unauthorized", {}, None),
        ]
        with patch.object(readiness, "build_opener", return_value=opener):
            readiness.wait_for_kitaru(url)
        self.assertEqual(
            [call.args[0] for call in opener.open.call_args_list],
            [f"{url}/health", f"{url}/api/v1/sessions"],
        )

    BACKEND = "ghcr.io/evalve-app/evalve-backend@sha256:" + "a" * 64
    URL = "https://evalve.example.com"

    def backend_answer(self, code, image=None, database="ok"):
        body = (
            '{"status":"x","database":"%s","version":"0.1.0","image":"%s"}'
            % (database, image or self.BACKEND)
        ).encode()
        if code == 200:
            return io.BytesIO(body)
        return HTTPError(self.URL, code, "x", {}, io.BytesIO(body))

    def test_backend_waits_through_503_then_needs_the_image_and_a_401(self):
        opener = Mock()
        opener.open.side_effect = [
            self.backend_answer(503, database="unavailable"),
            self.backend_answer(200),
            HTTPError(self.URL, 401, "Unauthorized", {}, None),
        ]
        with (
            patch.object(readiness, "build_opener", return_value=opener),
            patch.object(readiness.time, "sleep") as sleep,
        ):
            readiness.wait_for_backend(self.URL, self.BACKEND)
        self.assertEqual(
            [call.args[0] for call in opener.open.call_args_list],
            [
                f"{self.URL}/evalve/health",
                f"{self.URL}/evalve/health",
                f"{self.URL}/evalve/v1/me",
            ],
        )
        sleep.assert_called_once()

    def test_backend_with_another_image_fails_without_waiting(self):
        other = "ghcr.io/evalve-app/evalve-backend@sha256:" + "b" * 64
        for code in (200, 503):
            with self.subTest(code=code):
                opener = Mock()
                opener.open.side_effect = [self.backend_answer(code, image=other)]
                with (
                    patch.object(readiness, "build_opener", return_value=opener),
                    patch.object(readiness.time, "sleep") as sleep,
                ):
                    with self.assertRaisesRegex(EvalveError, f"runs {other}, not"):
                        readiness.wait_for_backend(self.URL, self.BACKEND)
                sleep.assert_not_called()

    def test_backend_that_accepts_no_credentials_is_rejected(self):
        opener = Mock()
        opener.open.side_effect = [self.backend_answer(200), io.BytesIO(b"{}")]
        with patch.object(readiness, "build_opener", return_value=opener):
            with self.assertRaisesRegex(EvalveError, "without credentials"):
                readiness.wait_for_backend(self.URL, self.BACKEND)

    def test_backend_that_never_answers_times_out(self):
        opener = Mock()
        opener.open.side_effect = OSError("connection refused")
        with (
            patch.object(readiness, "build_opener", return_value=opener),
            patch.object(readiness.time, "sleep"),
            patch.object(readiness.time, "monotonic", side_effect=[0, 1, 2, 700]),
        ):
            with self.assertRaisesRegex(EvalveError, "did not become ready"):
                readiness.wait_for_backend(self.URL, self.BACKEND)

    def test_backend_check_requires_https(self):
        with self.assertRaisesRegex(EvalveError, "HTTPS"):
            readiness.wait_for_backend("http://evalve.example.com", self.BACKEND)

    def test_public_sessions_are_rejected(self):
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(b'{"database":"ok"}'), io.BytesIO(b"[]")]
        with patch.object(readiness, "build_opener", return_value=opener):
            with self.assertRaisesRegex(EvalveError, "without credentials"):
                readiness.wait_for_kitaru("https://evalve.example.com")
