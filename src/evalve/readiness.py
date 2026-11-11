"""Verify the public endpoint before giving its URL to the operator."""

import json
import re
import time
from urllib.error import HTTPError, URLError
from collections.abc import Callable
from urllib.request import HTTPRedirectHandler, OpenerDirector, build_opener

from evalve import EvalveError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def wait_for_kitaru(url: str, timeout: int = 600) -> None:
    if not url.startswith("https://"):
        raise EvalveError("Kitaru readiness requires an HTTPS URL.")
    opener = build_opener(NoRedirect())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(f"{url}/health", timeout=8) as response:
                healthy = json.loads(response.read(65_536)).get("database") == "ok"
            if healthy:
                try:
                    with opener.open(f"{url}/api/v1/sessions", timeout=8):
                        raise EvalveError(
                            "Kitaru accepted an API request without credentials."
                        )
                except HTTPError as exc:
                    exc.close()
                    if exc.code == 401:
                        return
        except HTTPError as exc:
            exc.close()
        except (URLError, OSError, ValueError, AttributeError):
            pass
        time.sleep(5)
    raise EvalveError(
        f"Kitaru did not become ready at {url}. Check DNS and the EC2 bootstrap logs, then retry up."
    )


DASHBOARD_PATHS = {"bundled": "/app/", "evalve": "/"}
# Contract with evalve-ui (ADR 0001, "Container contract"): its index.html title.
EVALVE_TITLE = re.compile(rb"<title>\s*Evalve\s*</title>", re.IGNORECASE)
BUNDLED_MARKER = re.compile(rb'data-app="kitaru"|<title>\s*Kitaru\s*</title>', re.IGNORECASE)
API_ROUTE_STEP = "Check the API routes in /etc/evalve/Caddyfile on EC2, then retry up."


def check_dashboard(url: str, dashboard: str, timeout: int = 180) -> None:
    """Check on the public URL that Caddy sends the dashboard path and the API to the right container."""
    path = DASHBOARD_PATHS[dashboard]
    opener = build_opener(NoRedirect())
    poll(
        opener,
        f"{url}{path}",
        lambda body, content_type: require_evalve_dashboard(f"{url}{path}", path, body, content_type),
        no_answer=(
            f"{url}{path} did not answer. Read the evalve-ui service log on EC2 "
            "with Session Manager (journalctl -u evalve-ui), then retry up."
        ),
        refused=lambda code: (
            f"{url}{path} returned HTTP {code}, not the Evalve dashboard. "
            f"Pass the --ui-image build for {path} and retry up."
        ),
        timeout=timeout,
    )
    poll(
        opener,
        f"{url}/api/v1/info",
        lambda body, content_type: require_kitaru_info(url, body),
        no_answer=(
            f"{url}/api/v1/info did not answer. Read the evalve-kitaru service log on EC2 "
            "with Session Manager (journalctl -u evalve-kitaru), then retry up."
        ),
        refused=lambda code: (
            f"{url}/api/v1/info returned HTTP {code}, not Kitaru JSON. {API_ROUTE_STEP}"
        ),
        timeout=timeout,
    )


def poll(
    opener: OpenerDirector,
    address: str,
    accept: Callable[[bytes, str], None],
    *,
    no_answer: str,
    refused: Callable[[int], str],
    timeout: int,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with opener.open(address, timeout=8) as response:
                accept(response.read(65_536), response.headers.get("Content-Type", ""))
                return
        except HTTPError as exc:
            exc.close()
            if exc.code < 500:
                raise EvalveError(refused(exc.code)) from None
        except (URLError, OSError):
            pass
        if time.monotonic() >= deadline:
            raise EvalveError(no_answer)
        time.sleep(5)


def require_evalve_dashboard(
    address: str, path: str, body: bytes, content_type: str
) -> None:
    if content_type.startswith("text/html") and EVALVE_TITLE.search(body):
        return
    if BUNDLED_MARKER.search(body) or content_type.startswith("application/json"):
        raise EvalveError(
            f"{address} returned the Bundled dashboard or Kitaru, not the Evalve dashboard. "
            "Check the dashboard routes in /etc/evalve/Caddyfile on EC2, then retry up."
        )
    raise EvalveError(
        f"{address} did not return the Evalve dashboard. "
        f"Pass the --ui-image build for {path} and retry up."
    )


def require_kitaru_info(url: str, body: bytes) -> None:
    try:
        info = json.loads(body)
    except ValueError:
        info = None
    if not (
        isinstance(info, dict)
        and isinstance(info.get("version"), str)
        and isinstance(info.get("auth_scheme"), str)
    ):
        raise EvalveError(
            f"{url}/api/v1/info did not return Kitaru JSON. {API_ROUTE_STEP}"
        )
