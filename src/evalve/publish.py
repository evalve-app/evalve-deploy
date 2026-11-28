"""Push a checked agent image and register its Kitaru agent version.

The builder holds one credential, a Kitaru API key. The Evalve backend gives a
short-lived password for the one agent image repository, and Kitaru keeps the
secrets of the agent. Nothing here prints the key, the password or a secret
value, or puts one on the command line of a child process.
"""

from __future__ import annotations

import getpass
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from evalve import EvalveError
from evalve.readiness import NoRedirect
from evalve.supervisor.supervisor import CONTRACT_VERSION

HTTP_SECONDS = 15
REPLY_BYTES = 65_536
CREDENTIAL_ATTEMPTS = 3
RETRY_SECONDS = 5
LOGIN_SECONDS = 60
# A copy of the secret name rule of Kitaru 0.27.6. Kitaru checks a name only
# when it makes the secret, and the command makes a secret after the push.
SECRET_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,253}[A-Za-z0-9])?")
IDENTIFIER = re.compile(r"[A-Za-z0-9-]{1,64}")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
# The engine gets these values as arguments, so none can look like a flag.
REGISTRY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?")
REPOSITORY = re.compile(r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?")
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*")


def check_api_url(url: str) -> None:
    if url.startswith(("'", '"')) or url.endswith(("'", '"')):
        raise EvalveError(
            "KITARU_API_URL contains literal quotes. Use KITARU_API_URL=https://… without quotes in --env-file."
        )
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise EvalveError("KITARU_API_URL is not a valid HTTP(S) URL.") from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise EvalveError(
            "KITARU_API_URL must be an HTTP(S) URL without credentials, query or fragment."
        )
    if parts.scheme == "http" and parts.hostname != "localhost":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise EvalveError(
                "KITARU_API_URL must use HTTPS. HTTP is allowed only for localhost or a literal loopback IP."
            )


class Api:
    """Kitaru at `/api/v1` and the Evalve backend at `/evalve`, with one key."""

    def __init__(self, url: str, key: str):
        check_api_url(url)
        # The HTTP client names a header value that it refuses, so a key that
        # a header cannot hold must stop here, with no value in the message.
        if not key.isascii() or any(c.isspace() or not c.isprintable() for c in key):
            raise EvalveError(
                "KITARU_API_KEY has a character that an HTTP header cannot hold. Check the value for spaces and line breaks."
            )
        self.base = url.rstrip("/")
        self.key = key
        self.private = [key]
        self.opener = build_opener(NoRedirect())

    def request(self, method: str, path: str, body=None) -> tuple[int, object]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.key}"}
        data = b"" if method == "POST" else None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            try:
                with self.opener.open(request, timeout=HTTP_SECONDS) as response:
                    status, raw = response.status, response.read(REPLY_BYTES + 1)
            except HTTPError as error:
                with error:
                    status, raw = error.code, error.read(REPLY_BYTES + 1)
        except (URLError, OSError, HTTPException) as exc:
            raise EvalveError(
                "Could not reach Kitaru or the Evalve backend at KITARU_API_URL. Check the URL and the network, then retry."
            ) from exc
        try:
            return status, json.loads(raw) if len(raw) <= REPLY_BYTES else None
        except ValueError:
            return status, None

    def text(self, reply) -> str:
        """The text of an error reply when it is safe to print, or nothing."""
        detail = reply.get("detail") if isinstance(reply, dict) else None
        # A validation error of Kitaru is a list that can repeat the request,
        # and a request can hold a secret value. Only plain text is printed.
        if not isinstance(detail, str) or any(p in detail for p in self.private):
            return ""
        return "".join(c if c.isprintable() else " " for c in detail).strip()[:300]

    def find(self, collection: str, name: str) -> str | None:
        what = "the agent" if collection == "agents" else f"the secret {name}"
        condition = {"field": "name", "op": "eq", "value": name}
        query = urlencode({"filter": json.dumps(condition, separators=(",", ":"))})
        status, reply = self.request("GET", f"/api/v1/{collection}?{query}")
        if status in (401, 403):
            raise EvalveError("Kitaru refused the Kitaru API key.")
        items = reply.get("items") if isinstance(reply, dict) else None
        unexpected = EvalveError(
            f"Kitaru gave an unexpected reply to the lookup of {what} (HTTP {status})."
        )
        if status != 200 or not isinstance(items, list):
            raise unexpected
        found = [
            item.get("id")
            for item in items
            if isinstance(item, dict) and item.get("name") == name
        ]
        if not found:
            return None
        # The ID becomes a part of a URL path and of the run spec.
        if len(found) > 1 or not valid(IDENTIFIER, found[0]):
            raise unexpected
        return found[0]

    def push_credential(self) -> dict:
        for attempt in range(CREDENTIAL_ATTEMPTS):
            status, reply = self.request(
                "POST", "/evalve/v1/agent-images/push-credential"
            )
            if status != 503 or attempt == CREDENTIAL_ATTEMPTS - 1:
                break
            time.sleep(RETRY_SECONDS)
        if status == 401:
            raise EvalveError("The Evalve backend refused the Kitaru API key.")
        if status in (404, 503):
            raise EvalveError(
                self.text(reply)
                or f"The Evalve backend gave no push credential (HTTP {status})."
            )
        if status != 200:
            raise EvalveError(
                f"The Evalve backend gave an unexpected reply to the push credential request (HTTP {status})."
            )
        password = reply.get("password") if isinstance(reply, dict) else None
        if (
            not isinstance(password, str)
            or not password
            or "\n" in password
            or not valid(REGISTRY, reply.get("registry"))
            or not valid(REPOSITORY, reply.get("repository"))
            or not valid(USERNAME, reply.get("username"))
        ):
            raise EvalveError("The Evalve backend gave an invalid push credential.")
        self.private.append(password)
        return reply

    def make_secret(self, name: str, value: str) -> str | None:
        """The ID of the new secret, or nothing when Kitaru has the name."""
        status, reply = self.request(
            "POST", "/api/v1/secrets", {"name": name, "values": {name: value}}
        )
        if status == 409:
            return None
        made = reply.get("id") if isinstance(reply, dict) else None
        if not 200 <= status < 300 or not valid(IDENTIFIER, made):
            raise self.refusal(f"make the secret {name}", status, reply)
        return made

    def register(self, agent_id: str, body: dict) -> tuple[str, int]:
        status, reply = self.request(
            "POST", f"/api/v1/agents/{agent_id}/versions", body
        )
        made = reply.get("id") if isinstance(reply, dict) else None
        number = reply.get("version") if isinstance(reply, dict) else None
        if (
            not 200 <= status < 300
            or not valid(IDENTIFIER, made)
            or not isinstance(number, int)
        ):
            raise self.refusal("register the agent version", status, reply)
        return made, number

    def refusal(self, action: str, status: int, reply) -> EvalveError:
        text = self.text(reply)
        return EvalveError(
            f"Kitaru did not {action} (HTTP {status})" + (f": {text}" if text else ".")
        )


def valid(pattern: re.Pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def check_secret_name(name: str) -> None:
    if not SECRET_NAME.fullmatch(name):
        raise EvalveError(
            f"--secret name {name} is not a name that Kitaru accepts: letters, digits, _ and -, "
            "with a letter or a digit first and last, and 255 characters at most."
        )


@dataclass
class Publication:
    """What the push and the registration need, and how far they got."""

    api: Api
    agent: str
    agent_id: str
    # The ID of each secret, in the order of the flags. A secret that Kitaru
    # does not have yet has no ID, and its value waits in `values`.
    secrets: dict[str, str | None]
    values: dict[str, str]
    unused: list[str]
    made: list[str] = field(default_factory=list)
    registry: str | None = None
    target: str | None = None
    tag: str | None = None
    pushed: bool = False
    digest: str | None = None
    version: tuple[str, int] | None = None


def prepare(args, url: str, key: str) -> Publication:
    """Each refusal that needs no image, so that the builder does not wait for it."""
    api = Api(url, key)
    agent_id = api.find("agents", args.agent)
    if agent_id is None:
        raise EvalveError(
            f"Kitaru has no agent `{args.agent}`. Register it with Onboarding or with `kitaru agent register`."
        )
    secrets, values, unused = {}, {}, []
    for name in args.secret:
        secrets[name] = api.find("secrets", name)
        value = os.environ.get(name, "")
        if secrets[name] is not None:
            if value:
                unused.append(name)
            continue
        if not value and sys.stdin is not None and sys.stdin.isatty():
            value = getpass.getpass(f"Value of the new Kitaru secret {name}: ")
        if not value:
            raise EvalveError(
                f"Kitaru has no secret {name}, and the command has no value for it. "
                f"Set the environment variable {name}, or run the command in a terminal and type the value."
            )
        values[name] = value
        api.private.append(value)
    return Publication(api, args.agent, agent_id, secrets, values, unused)


def publish(publication: Publication, engine, args, image: str, tag: str, commit: str):
    api = publication.api
    credential = api.push_credential()
    registry, repository = credential["registry"], credential["repository"]
    target = f"{registry}/{repository}:{tag}"
    # From here the engine can hold the password, so the logout must run.
    publication.registry = registry
    try:
        login = engine.call(
            "login",
            registry,
            "--username",
            credential["username"],
            "--password-stdin",
            input=credential["password"],
            timeout=LOGIN_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise EvalveError(
            f"{engine.name} login to the agent image repository timed out after {LOGIN_SECONDS} seconds."
        ) from exc
    if login.returncode:
        raise EvalveError(
            f"{engine.name} login to the agent image repository failed (exit {login.returncode}). Check that the engine is running, then retry."
        )
    engine.images.append(target)  # Also cleaned if the engine fails after tagging.
    if engine.call("tag", image, target).returncode:
        raise EvalveError("Tagging the agent image for the repository failed.")
    print(f"Pushing {target}…", flush=True)
    pushed = engine.call("push", target, capture_output=False)
    if pushed.returncode:
        raise EvalveError(
            f"{engine.name} push failed (exit {pushed.returncode}). The output above has the reason."
        )
    publication.target, publication.tag, publication.pushed = target, tag, True
    publication.digest = pushed_digest(engine, target, f"{registry}/{repository}")
    for name, value in publication.values.items():
        made = api.make_secret(name, value)
        if made is not None:
            publication.made.append(name)
        else:
            # A different builder made it in the meantime.
            made = api.find("secrets", name)
        if made is None:
            raise EvalveError(
                f"Kitaru has a secret with the name {name} that its lookup does not show, so the command cannot attach it. Use a different secret name."
            )
        publication.secrets[name] = made
    run_spec = {
        "command": args.agent_command,
        "working_dir": args.working_dir,
        "env": {
            **dict(item.split("=", 1) for item in args.env),
            "EVALVE_AGENT_IMAGE_DIGEST": publication.digest,
            "EVALVE_COMMIT": commit,
            "EVALVE_SUPERVISOR_CONTRACT_VERSION": str(CONTRACT_VERSION),
        },
        "secret_ids": list(publication.secrets.values()),
    }
    if args.timeout_seconds is not None:
        run_spec["timeout_seconds"] = args.timeout_seconds
    publication.version = api.register(
        publication.agent_id,
        {
            "display_version": tag,
            "description": f"Agent image {tag}, built by evalve build.",
            "run_spec": run_spec,
            "capabilities": {"tools": args.tool},
        },
    )


def pushed_digest(engine, target: str, repository: str) -> str:
    """The digest that the engine reports for the name in the repository.

    Some engines list a digest for a local name before a push, so only the
    exit code of the push shows that the repository has the image.
    """
    inspected = engine.call("image", "inspect", target)
    try:
        record = json.loads(inspected.stdout)[0] if not inspected.returncode else {}
        listed = record.get("RepoDigests") or []
        digests = {
            entry.removeprefix(f"{repository}@")
            for entry in listed
            if isinstance(entry, str) and entry.startswith(f"{repository}@")
        }
    except (ValueError, LookupError, AttributeError, TypeError):
        digests = set()
    if len(digests) != 1 or not valid(DIGEST, next(iter(digests))):
        raise EvalveError(
            f"{engine.name} reported no single digest for the pushed agent image."
        )
    return digests.pop()


def logout(publication: Publication | None, engine) -> str | None:
    """What stays behind when the engine could not forget the password."""
    if publication is None or publication.registry is None:
        return None
    registry = publication.registry
    try:
        if engine.call("logout", registry, timeout=LOGIN_SECONDS).returncode == 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return f"the login to {registry} (run {engine.name} logout {registry})"


def unregistered(publication: Publication | None) -> str:
    """The text for a stop after the push, or nothing."""
    if publication is None or not publication.pushed or publication.version:
        return ""
    return (
        f"The image `{publication.tag}` is in the repository, but no agent version was registered. "
        "Run the command again. It makes a new image."
    )


def made_secrets(publication: Publication | None) -> str:
    if publication is None or not publication.made:
        return ""
    return (
        f"Kitaru secrets that this run made: {', '.join(publication.made)}. "
        "A second run attaches them as they are."
    )


def report(publication: Publication) -> None:
    identifier, number = publication.version
    print(f"Agent: {publication.agent}")
    print(f"Agent version: {identifier} (number {number})")
    print(f"Image tag: {publication.tag}")
    print(f"Image digest: {publication.digest}")
    for name in publication.secrets:
        print(f"Secret {name}: {'made' if name in publication.made else 'found'}")
    for name in publication.unused:
        print(
            f"Secret `{name}` exists in Kitaru and was attached as it is. "
            "The value in your environment was not used."
        )
