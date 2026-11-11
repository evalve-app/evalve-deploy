import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

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

    def test_public_sessions_are_rejected(self):
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(b'{"database":"ok"}'), io.BytesIO(b"[]")]
        with patch.object(readiness, "build_opener", return_value=opener):
            with self.assertRaisesRegex(EvalveError, "without credentials"):
                readiness.wait_for_kitaru("https://evalve.example.com")


URL = "https://evalve.example.com"
EVALVE_HTML = b"<!doctype html><html><head><title>Evalve</title></head></html>"
BUNDLED_HTML = b'<!doctype html><html data-app="kitaru"><head><title>Kitaru</title></head></html>'
INFO_JSON = b'{"version":"0.27.6","auth_scheme":"local"}'


def response(body, content_type):
    reply = io.BytesIO(body)
    reply.status = 200
    reply.headers = {"Content-Type": content_type}
    return reply


def html(body):
    return response(body, "text/html; charset=utf-8")


def json_reply(body):
    return response(body, "application/json")


class DashboardCheckTest(unittest.TestCase):
    def check(self, replies, dashboard="bundled"):
        opener = Mock()
        opener.open.side_effect = replies
        with (
            patch.object(readiness, "build_opener", return_value=opener),
            patch.object(readiness.time, "sleep"),
        ):
            readiness.check_dashboard(URL, dashboard, timeout=0)
        return [call.args[0] for call in opener.open.call_args_list]

    def test_each_step_checks_its_dashboard_path_and_the_api(self):
        for dashboard, path in (("bundled", "/app/"), ("evalve", "/")):
            with self.subTest(dashboard=dashboard):
                opened = self.check([html(EVALVE_HTML), json_reply(INFO_JSON)], dashboard)
                self.assertEqual(opened, [f"{URL}{path}", f"{URL}/api/v1/info"])

    def assert_fails(self, replies, message, dashboard="bundled"):
        with self.assertRaises(EvalveError) as caught:
            self.check(replies, dashboard)
        self.assertIn(message, str(caught.exception))
        self.assertIn("retry up", str(caught.exception))

    def test_bundled_dashboard_or_kitaru_on_the_dashboard_path_fails(self):
        for reply in (html(BUNDLED_HTML), json_reply(b'{"detail":"Not Found"}')):
            with self.subTest(reply=reply.getvalue()):
                self.assert_fails(
                    [reply], f"{URL}/app/ returned the Bundled dashboard or Kitaru, not the Evalve dashboard"
                )

    def test_other_html_on_the_dashboard_path_fails(self):
        self.assert_fails(
            [html(b"<html><title>Welcome to nginx</title></html>")],
            f"{URL}/ did not return the Evalve dashboard. Pass the --ui-image build for /",
            dashboard="evalve",
        )

    def test_error_status_on_the_dashboard_path_fails_at_once(self):
        for code in (404, 308):
            with self.subTest(code=code):
                self.assert_fails(
                    [HTTPError(URL, code, "", {}, None)],
                    f"{URL}/app/ returned HTTP {code}, not the Evalve dashboard",
                )

    def test_dashboard_path_without_an_answer_fails(self):
        self.assert_fails(
            [HTTPError(URL, 502, "Bad Gateway", {}, None)],
            f"{URL}/app/ did not answer. Read the evalve-ui service log",
        )
        self.assert_fails([URLError("timed out")], f"{URL}/app/ did not answer")

    def test_dashboard_that_starts_late_passes(self):
        opener = Mock()
        opener.open.side_effect = [
            HTTPError(URL, 502, "Bad Gateway", {}, None),
            html(EVALVE_HTML),
            json_reply(INFO_JSON),
        ]
        with (
            patch.object(readiness, "build_opener", return_value=opener),
            patch.object(readiness.time, "sleep") as sleep,
        ):
            readiness.check_dashboard(URL, "bundled")
        self.assertEqual(sleep.call_count, 1)

    def test_api_info_without_kitaru_json_fails(self):
        for reply in (html(EVALVE_HTML), json_reply(b'{"detail":"Not Found"}')):
            with self.subTest(reply=reply.getvalue()):
                self.assert_fails(
                    [html(EVALVE_HTML), reply],
                    f"{URL}/api/v1/info did not return Kitaru JSON",
                )

    def test_api_info_error_status_fails(self):
        self.assert_fails(
            [html(EVALVE_HTML), HTTPError(URL, 404, "", {}, None)],
            f"{URL}/api/v1/info returned HTTP 404, not Kitaru JSON",
        )
