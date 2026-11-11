import hashlib
from http.client import IncompleteRead
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from evalve import EvalveError, cli, registry


def blob(document):
    body = json.dumps(document).encode()
    return body, "sha256:" + hashlib.sha256(body).hexdigest()


CONFIG, CONFIG_DIGEST = blob(
    {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Labels": {cli.KITARU_VERSION_LABEL: "0.27.6"}},
    }
)
AMD64, AMD64_DIGEST = blob(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": CONFIG_DIGEST},
        "layers": [],
    }
)
INDEX, INDEX_DIGEST = blob(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"digest": "sha256:" + "1" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": AMD64_DIGEST, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:" + "2" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
        ],
    }
)
UI = f"ghcr.io/evalve-app/evalve-ui@{INDEX_DIGEST}"
CHALLENGE = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:evalve-app/evalve-ui:pull"'


def unauthorized(url, challenge=CHALLENGE):
    return HTTPError(url, 401, "Unauthorized", {"WWW-Authenticate": challenge}, io.BytesIO())


def reply(body):
    return io.BytesIO(body)


class RegistryTest(unittest.TestCase):
    def run_with(self, replies, function, *args):
        opener = Mock()
        opener.open.side_effect = replies
        with patch.object(registry, "build_opener", return_value=opener):
            result = function(*args)
        return result, [call.args[0] for call in opener.open.call_args_list]

    def test_reads_amd64_labels_of_an_index_with_an_anonymous_token(self):
        labels, requests = self.run_with(
            [
                unauthorized("https://ghcr.io/v2/evalve-app/evalve-ui/manifests/x"),
                reply(b'{"token":"anonymous-token"}'),
                reply(INDEX),
                reply(AMD64),
                reply(CONFIG),
            ],
            registry.image_labels,
            UI,
        )
        self.assertEqual(labels, {cli.KITARU_VERSION_LABEL: "0.27.6"})
        self.assertEqual(
            [request.full_url for request in requests],
            [
                f"https://ghcr.io/v2/evalve-app/evalve-ui/manifests/{INDEX_DIGEST}",
                "https://ghcr.io/token?service=ghcr.io&scope=repository%3Aevalve-app%2Fevalve-ui%3Apull",
                f"https://ghcr.io/v2/evalve-app/evalve-ui/manifests/{INDEX_DIGEST}",
                f"https://ghcr.io/v2/evalve-app/evalve-ui/manifests/{AMD64_DIGEST}",
                f"https://ghcr.io/v2/evalve-app/evalve-ui/blobs/{CONFIG_DIGEST}",
            ],
        )
        self.assertIn("application/vnd.oci.image.index.v1+json", requests[0].get_header("Accept"))
        for request in requests[2:]:
            # A blob redirects to another host, so the token must not follow it.
            self.assertEqual(
                request.unredirected_hdrs.get("Authorization"), "Bearer anonymous-token"
            )
            self.assertNotIn("Authorization", request.headers)

    def test_single_manifest_image_needs_no_index(self):
        reference = f"ghcr.io/evalve-app/evalve-ui@{AMD64_DIGEST}"
        labels, _ = self.run_with(
            [reply(AMD64), reply(CONFIG)], registry.image_labels, reference
        )
        self.assertEqual(labels[cli.KITARU_VERSION_LABEL], "0.27.6")

    def test_image_without_labels_has_no_labels(self):
        config, config_digest = blob(
            {"os": "linux", "architecture": "amd64", "config": {"Labels": None}}
        )
        manifest, digest = blob({"config": {"digest": config_digest}})
        labels, _ = self.run_with(
            [reply(manifest), reply(config)],
            registry.image_labels,
            f"ghcr.io/evalve-app/evalve-ui@{digest}",
        )
        self.assertEqual(labels, {})

    def test_single_manifest_image_for_another_platform_fails(self):
        config, config_digest = blob({"os": "linux", "architecture": "arm64", "config": {}})
        manifest, digest = blob({"config": {"digest": config_digest}})
        with self.assertRaisesRegex(EvalveError, "linux/amd64"):
            self.run_with(
                [reply(manifest), reply(config)],
                registry.image_labels,
                f"ghcr.io/evalve-app/evalve-ui@{digest}",
            )

    def test_oversized_reply_fails_with_its_cause(self):
        with patch.object(registry, "LIMIT", 10), self.assertRaisesRegex(EvalveError, "larger than"):
            self.run_with([reply(INDEX)], registry.tag_digest, "zenmldocker/kitaru-server", "0.27.6")

    def test_manifest_that_does_not_match_its_digest_fails(self):
        with self.assertRaisesRegex(EvalveError, "could not read the labels of ghcr.io/evalve-app/evalve-ui@sha256:"):
            self.run_with([reply(AMD64)], registry.image_labels, UI)

    def test_index_without_linux_amd64_fails(self):
        index, digest = blob({"manifests": [{"digest": AMD64_DIGEST, "platform": {"os": "linux", "architecture": "arm64"}}]})
        with self.assertRaisesRegex(EvalveError, "linux/amd64"):
            self.run_with([reply(index)], registry.image_labels, f"ghcr.io/evalve-app/evalve-ui@{digest}")

    def test_registry_errors_name_the_image(self):
        for failure in (
            HTTPError("https://ghcr.io", 404, "Not Found", {}, io.BytesIO()),
            URLError("no route to host"),
            IncompleteRead(b"partial"),
            reply(b"not json"),
        ):
            with self.subTest(failure=failure), self.assertRaisesRegex(EvalveError, f"could not read the labels of {UI}"):
                self.run_with([failure], registry.image_labels, UI)

    def test_token_realm_must_use_https(self):
        with self.assertRaisesRegex(EvalveError, "could not read the labels"):
            self.run_with(
                [unauthorized("https://ghcr.io", 'Bearer realm="http://ghcr.io/token",service="ghcr.io"')],
                registry.image_labels,
                UI,
            )

    def test_tag_digest_hashes_the_docker_hub_manifest_of_the_tag(self):
        digest, requests = self.run_with(
            [
                unauthorized("https://registry-1.docker.io", 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:zenmldocker/kitaru-server:pull"'),
                reply(b'{"access_token":"hub-token"}'),
                reply(INDEX),
            ],
            registry.tag_digest,
            "zenmldocker/kitaru-server",
            "0.27.6",
        )
        self.assertEqual(digest, INDEX_DIGEST)
        self.assertEqual(
            requests[-1].full_url,
            "https://registry-1.docker.io/v2/zenmldocker/kitaru-server/manifests/0.27.6",
        )
        self.assertIn("application/vnd.docker.distribution.manifest.list.v2+json", requests[-1].get_header("Accept"))

    def test_missing_tag_names_the_tag(self):
        with self.assertRaisesRegex(EvalveError, "zenmldocker/kitaru-server:9.9.9"):
            self.run_with(
                [HTTPError("https://registry-1.docker.io", 404, "Not Found", {}, io.BytesIO())],
                registry.tag_digest,
                "zenmldocker/kitaru-server",
                "9.9.9",
            )


if __name__ == "__main__":
    unittest.main()
