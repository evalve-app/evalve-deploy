"""Verify the public endpoint before giving its URL to the operator."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener

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


def wait_for_backend(url: str, image: str, timeout: int = 600) -> None:
    """Wait until the Evalve backend answers through Caddy with the pinned image."""
    if not url.startswith("https://"):
        raise EvalveError("Backend readiness requires an HTTPS URL.")
    opener = build_opener(NoRedirect())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = None
        try:
            with opener.open(f"{url}/evalve/health", timeout=8) as response:
                health = json.loads(response.read(65_536))
        except HTTPError as exc:
            # 503 means the backend runs but cannot reach its database yet.
            if exc.code == 503:
                try:
                    health = json.loads(exc.read(65_536))
                except (OSError, ValueError):
                    pass
            exc.close()
        except (URLError, OSError, ValueError):
            pass
        if isinstance(health, dict):
            running_image = health.get("image")
            # Waiting does not fix a wrong build, so stop at once.
            if running_image is not None and running_image != image:
                raise EvalveError(
                    f"The Evalve backend runs {running_image}, not {image}. "
                    "Check the image and retry."
                )
            if health.get("database") == "ok":
                if running_image is None:
                    raise EvalveError(
                        "The Evalve backend did not report its image. "
                        "Check EVALVE_IMAGE in the backend configuration on EC2 and retry."
                    )
                try:
                    with opener.open(f"{url}/evalve/v1/me", timeout=8):
                        raise EvalveError(
                            "The Evalve backend accepted a request without credentials."
                        )
                except HTTPError as exc:
                    exc.close()
                    if exc.code == 401:
                        return
                except (URLError, OSError):
                    pass
        time.sleep(5)
    raise EvalveError(
        f"The Evalve backend did not become ready at {url}/evalve/health. "
        "Check the evalve-backend unit on EC2, then retry."
    )
