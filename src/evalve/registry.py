"""Read public image metadata from an OCI registry without Docker."""

from __future__ import annotations

import hashlib
import json
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from evalve import EvalveError

DOCKER_HUB = "registry-1.docker.io"
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
ACCEPT = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
LIMIT = 4 * 1024 * 1024  # Bytes read from one registry reply.
# KeyError, TypeError and AttributeError come from a reply with an unexpected shape.
UNREADABLE = (
    HTTPError,
    URLError,
    HTTPException,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)


class Registry:
    """Anonymous pull access to one repository through the registry API."""

    def __init__(self, host: str, repository: str):
        self.base = f"https://{host}/v2/{repository}"
        self.opener = build_opener()
        self.token: str | None = None

    def get(self, path: str, accept: str | None = None) -> bytes:
        for attempt in range(2):
            request = Request(f"{self.base}/{path}")
            if accept:
                request.add_header("Accept", accept)
            if self.token:
                # Blobs redirect to another host. The token must not go there.
                request.add_unredirected_header("Authorization", f"Bearer {self.token}")
            try:
                with self.opener.open(request, timeout=15) as response:
                    return read_limited(response)
            except HTTPError as exc:
                exc.close()
                if exc.code != 401 or attempt:
                    raise
                self.token = self.anonymous_token(exc.headers.get("WWW-Authenticate", ""))
        raise AssertionError("unreachable")

    def anonymous_token(self, challenge: str) -> str:
        if not challenge.lower().startswith("bearer "):
            raise ValueError("the registry did not offer an anonymous token")
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = fields.pop("realm", "")
        if not realm.startswith("https://"):
            raise ValueError("the registry token address does not use HTTPS")
        with self.opener.open(Request(f"{realm}?{urlencode(fields)}"), timeout=15) as reply:
            answer = json.loads(read_limited(reply))
        token = answer.get("token") or answer.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("the registry returned no token")
        return token


def read_limited(response) -> bytes:
    body = response.read(LIMIT + 1)
    if len(body) > LIMIT:
        raise ValueError(f"the registry reply is larger than {LIMIT} bytes")
    return body


def digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def parse_verified(body: bytes, digest: str) -> dict:
    if digest_of(body) != digest:
        raise ValueError(f"the content does not match {digest}")
    return json.loads(body)


def image_labels(reference: str) -> dict[str, str]:
    """Return the config labels of the linux/amd64 image of a public digest reference."""
    try:
        name, digest = reference.split("@", 1)
        host, repository = name.split("/", 1)
        client = Registry(host, repository)
        manifest = parse_verified(client.get(f"manifests/{digest}", ACCEPT), digest)
        if "manifests" in manifest:
            digest = next(
                (
                    item["digest"]
                    for item in manifest["manifests"]
                    if item.get("platform", {}).get("os") == "linux"
                    and item.get("platform", {}).get("architecture") == "amd64"
                ),
                None,
            )
            if digest is None:
                raise ValueError("the image has no linux/amd64 manifest")
            manifest = parse_verified(client.get(f"manifests/{digest}", ACCEPT), digest)
        config_digest = manifest["config"]["digest"]
        config = parse_verified(client.get(f"blobs/{config_digest}"), config_digest)
        if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
            raise ValueError("the image is not a linux/amd64 image")
        return dict(config.get("config", {}).get("Labels") or {})
    except UNREADABLE as exc:
        raise EvalveError(
            f"Evalve could not read the labels of {reference}: {exc}. "
            "Check that the image is public and the network is available, then retry up."
        ) from None


def tag_digest(repository: str, tag: str) -> str:
    """Return the digest that a Docker Hub tag points to now."""
    try:
        body = Registry(DOCKER_HUB, repository).get(f"manifests/{tag}", ACCEPT)
    except UNREADABLE as exc:
        raise EvalveError(
            f"Evalve could not read Docker Hub tag {repository}:{tag}: {exc}. "
            "Check the --kitaru-version and the network, then retry up."
        ) from None
    return digest_of(body)
