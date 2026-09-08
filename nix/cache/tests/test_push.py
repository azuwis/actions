"""Unit tests for nix/cache/post/push.py.

Loaded via importlib from the file path (the module lives in `post/`, not in
`tests/`, and must not be on sys.path as a package).  Every test that would
touch subprocesses/network either injects a fake or is skipped when nix is
not on PATH (the CI unit-tests step runs before `./nix` installs anything).
"""
import http.server
import importlib.util
import io
import json
import lzma
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PUSH_PY = Path(__file__).resolve().parents[1] / "post" / "push.py"
_spec = importlib.util.spec_from_file_location("push", PUSH_PY)
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)

H32 = "a" * 32
STORE = "/nix/store/" + H32 + "-pkg-1.0"
SRI_ZERO = "sha256-" + "A" * 43 + "="   # sha256 of 32 zero bytes
NIX32_ZERO = "0" * 52


class SelectPathsTest(unittest.TestCase):
    def infos(self, *sigs_by_path):
        return {path: {"signatures": list(sigs)}
                for path, sigs in sigs_by_path}

    def test_in_index_skipped(self):
        keep = push.select_paths(
            self.infos((STORE, ["any-sig"])), {"entries": {H32: {}}},
            "own-key")
        self.assertEqual(keep, [])

    def test_no_key_any_signature_skipped(self):
        signed = "/nix/store/" + "b" * 32 + "-signed"
        unsigned = "/nix/store/" + "c" * 32 + "-unsigned"
        infos = self.infos((signed, ["elsewhere:abc"]), (unsigned, []))
        keep = push.select_paths(infos, {}, "")
        self.assertEqual(keep, [unsigned])

    def test_with_key_external_signature_skipped(self):
        ext = "/nix/store/" + "b" * 32 + "-ext"
        mixed = "/nix/store/" + "c" * 32 + "-mixed"
        infos = self.infos(
            (ext, ["other-cache:sig"]),
            (mixed, ["own-key:sig", "other-cache:sig"]),
        )
        keep = push.select_paths(infos, {}, "own-key")
        self.assertEqual(keep, [])

    def test_with_key_own_signature_kept(self):
        keep = push.select_paths(
            self.infos((STORE, ["own-key:sig1"])), {}, "own-key")
        self.assertEqual(keep, [STORE])

    def test_missing_own_signature_fails(self):
        with self.assertRaises(push.PushError) as cm:
            push.select_paths(self.infos((STORE, [])), {}, "own-key")
        self.assertIn(STORE, str(cm.exception))


class MakeNarinfoTest(unittest.TestCase):
    def setUp(self):
        self.info = {
            "narHash": SRI_ZERO,
            "narSize": 1000,
            "references": ["/nix/store/" + "b" * 32 + "-dep"],
            "deriver": "/nix/store/" + "c" * 32 + "-pkg-1.0.drv",
            "signatures": ["own-key:sig1"],
        }
        self.convert = lambda h: NIX32_ZERO

    def test_sri_converted_with_single_sha256_prefix(self):
        text = push.make_narinfo(STORE, H32, 123, "f" * 52, self.info,
                                 convert=self.convert)
        self.assertIn("NarHash: sha256:" + NIX32_ZERO, text)
        self.assertEqual(text.count("NarHash: sha256:"), 1)
        self.assertIn("StorePath: " + STORE, text)
        self.assertIn("URL: nar/" + H32 + ".nar." + push.COMPRESSION_EXT, text)
        self.assertIn("Compression: " + push.COMPRESSION, text)
        self.assertIn("FileHash: sha256:" + "f" * 52, text)
        self.assertIn("FileSize: 123", text)
        self.assertIn("NarSize: 1000", text)
        self.assertIn("References: " + "b" * 32 + "-dep", text)
        self.assertIn("Deriver: " + "c" * 32 + "-pkg-1.0.drv", text)
        self.assertIn("Sig: own-key:sig1", text)

    def test_prefix_not_doubled_for_prefixed_narhash_but_convert_skipped(self):
        called = []
        info = dict(self.info)
        info["narHash"] = "sha256:" + NIX32_ZERO  # already prefixed form

        def convert(h):
            called.append(h)
            return NIX32_ZERO

        push.make_narinfo(STORE, H32, 1, "f" * 52, info, convert=convert)
        self.assertEqual(called, [])  # non-SRI input is passed through untouched

    def test_nar_size_zero_skips(self):
        info = dict(self.info)
        info["narSize"] = 0
        with self.assertRaises(push.SkipPath) as cm:
            push.make_narinfo(STORE, H32, 1, "f" * 52, info,
                              convert=self.convert)
        self.assertEqual(str(cm.exception), f"narSize <= 0 for {STORE}")

    def test_empty_file_hash_skips(self):
        with self.assertRaises(push.SkipPath) as cm:
            push.make_narinfo(STORE, H32, 1, "", self.info,
                              convert=self.convert)
        self.assertEqual(str(cm.exception),
                         f"empty FileHash/NarHash for {STORE}")

    def test_empty_nar_hash_skips(self):
        info = dict(self.info)
        info["narHash"] = ""
        with self.assertRaises(push.SkipPath) as cm:
            push.make_narinfo(STORE, H32, 1, "f" * 52, info,
                              convert=self.convert)
        self.assertEqual(str(cm.exception),
                         f"empty FileHash/NarHash for {STORE}")


def _fake_dump(content=b"nar"):
    """dump_nar stand-in that really creates the NAR file and succeeds."""
    def dump(path, nar_file):
        Path(nar_file).write_bytes(content)
    return dump


class ExportPathsTest(unittest.TestCase):
    """export_paths: a SkipPath from any stage costs exactly one skip, still
    closes the log group and removes the NAR; other paths still upload.

    warn and stderr are captured, so the tests assert the emitted warning text
    and the exact ::group::/::endgroup:: sequence instead of printing them into
    the CI log as annotations and log groups."""

    def test_dump_failure_skips_and_the_rest_upload(self):
        good = "/nix/store/" + "a" * 32 + "-good"
        bad = "/nix/store/" + "b" * 32 + "-bad"
        # narHash must be a form to_base32 passes through untouched:
        # make_narinfo binds convert=nix_hash_convert as a DEFAULT ARGUMENT, so
        # patching the module global does not intercept it, and an SRI hash
        # would shell out to a `nix` that is not on PATH in the CI unit-tests
        # step (it runs before ./nix installs anything).
        infos = {p: {"narHash": NIX32_ZERO, "narSize": 1000}
                 for p in (good, bad)}
        registry = mock.Mock()

        def fake_dump(path, nar_file):
            if path != good:
                raise push.SkipPath(f"failed to dump {path}")
            Path(nar_file).write_bytes(b"nar")

        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(push, "dump_nar", side_effect=fake_dump), \
                mock.patch.object(push, "nix_hash_convert",
                                  return_value="f" * 52), \
                mock.patch.object(push, "warn") as warn, \
                mock.patch.object(sys, "stderr", err):
            skipped, entries = push.export_paths(
                [good, bad], infos, registry, d, "t")
            self.assertEqual(list(Path(d, "nar").iterdir()), [])  # cleaned up
        self.assertEqual((len(entries), skipped), (1, 1))
        self.assertEqual(list(entries), ["a" * 32])
        self.assertEqual(entries["a" * 32]["name"], "good")
        self.assertEqual(entries["a" * 32]["nar_size"], 3)   # file, not narSize
        self.assertIn("NarSize: 1000", entries["a" * 32]["narinfo"])
        self.assertIn("NarHash: sha256:" + NIX32_ZERO,
                      entries["a" * 32]["narinfo"])
        registry.push_blob.assert_called_once()
        self.assertEqual(warn.call_args_list,
                         [mock.call(f"failed to dump {bad}; skipping")])
        # one group per path, opened before the work and closed after; the
        # uploaded line belongs to the path's own group
        self.assertEqual(err.getvalue().splitlines(), [
            f"::group::nix/cache export {'a' * 32}",
            f"uploaded {'a' * 32} (3 bytes)",
            "::endgroup::",
            f"::group::nix/cache export {'b' * 32}",
            "::endgroup::",
        ])

    def test_oversized_nar_skips_before_uploading(self):
        err = io.StringIO()
        registry = mock.Mock()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(push, "dump_nar", _fake_dump()), \
                mock.patch.object(push, "MAX_NAR_SIZE", 0), \
                mock.patch.object(push, "warn") as warn, \
                mock.patch.object(sys, "stderr", err):
            result = push.export_paths(
                [STORE], {STORE: {"narHash": NIX32_ZERO, "narSize": 1000}},
                registry, d, "t")
            self.assertEqual(list(Path(d, "nar").iterdir()), [])
        self.assertEqual(result, (1, {}))
        self.assertEqual(warn.call_args_list,
                         [mock.call(f"{STORE} nar exceeds 10GiB GHCR blob "
                                    "limit; skipping")])
        registry.push_blob.assert_not_called()
        self.assertEqual(err.getvalue().splitlines(),
                         [f"::group::nix/cache export {H32}", "::endgroup::"])

    def test_unexpected_narinfo_error_is_not_hidden(self):
        registry = mock.Mock()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(push, "dump_nar", _fake_dump()), \
                mock.patch.object(push, "nix_hash_convert", return_value="x"), \
                mock.patch.object(push, "make_narinfo",
                                  side_effect=RuntimeError("bug")), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "bug"):
                push.export_paths(
                    [STORE],
                    {STORE: {"narHash": NIX32_ZERO, "narSize": 1000}},
                    registry, d, "t")
            self.assertEqual(list(Path(d, "nar").iterdir()), [])

    def test_nix_hash_failure_only_skips_that_path(self):
        registry = mock.Mock()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(push, "dump_nar", _fake_dump()), \
                mock.patch.object(
                    push, "nix_hash_convert",
                    side_effect=push.PushError("hash conversion failed")), \
                mock.patch.object(push, "warn") as warn, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            result = push.export_paths(
                [STORE], {STORE: {"narHash": NIX32_ZERO, "narSize": 1000}},
                registry, d, "t")

        self.assertEqual(result, (1, {}))
        warn.assert_called_once_with(
            f"narinfo generation failed for {STORE}: hash conversion failed; "
            "skipping")
        registry.push_blob.assert_not_called()


class PathInfosTest(unittest.TestCase):
    """`nix path-info --json --json-format 1` is always a map keyed by store
    path; malformed output is a PushError."""

    def test_map_form(self):
        data = {STORE: {"narSize": 1}}
        with mock.patch.object(push, "nix_json", return_value=data) as nix_json:
            self.assertEqual(push.path_infos([STORE]), data)
        nix_json.assert_called_once_with(
            "path-info", "--json", "--json-format", "1", "--", STORE)

    def test_bad_input_raises(self):
        for bad in ([{"path": STORE, "narSize": 1}], {STORE: "not a dict"},
                    "nope"):
            with mock.patch.object(push, "nix_json", return_value=bad), \
                    self.assertRaises(push.PushError):
                push.path_infos([STORE])

    def test_all_paths_form(self):
        data = {STORE: {"narSize": 1}}
        with mock.patch.object(push, "nix_json", return_value=data) as nix_json:
            self.assertEqual(push.path_infos(), data)
        nix_json.assert_called_once_with(
            "path-info", "--all", "--json", "--json-format", "1")


class MergeIndexTest(unittest.TestCase):
    def test_new_entries_override_existing(self):
        h = "d" * 32
        existing = {"version": 1, "entries": {h: {"name": "old", "narinfo": "old"}}}
        new = {h: {"name": "new", "narinfo": "new", "nar_digest": "sha256:x",
                   "nar_size": 1, "added": "t"}}
        index = push.merge_index(existing, new, "own-key", "owner/repo",
                                 "2026-09-02T00:00:00Z")
        self.assertEqual(index["entries"][h]["name"], "new")
        self.assertEqual(len(index["entries"]), 1)
        self.assertEqual(index["version"], 1)
        self.assertEqual(index["gc_roots"], [])
        self.assertEqual(index["image"], "ghcr.io/owner/repo/nix-cache")
        self.assertEqual(index["repo"], "owner/repo")
        self.assertEqual(index["registry"], "ghcr.io")

    def test_public_key_kept_when_no_key(self):
        existing = {"public_key": "old-key", "entries": {}}
        index = push.merge_index(existing, {}, "", "r", "t")
        self.assertEqual(index["public_key"], "old-key")

    def test_public_key_overridden_by_own_key(self):
        existing = {"public_key": "old-key", "entries": {}}
        index = push.merge_index(existing, {}, "new-key", "r", "t")
        self.assertEqual(index["public_key"], "new-key")

    def test_dirty_existing_json_rejected(self):
        """A corrupt existing index fails instead of becoming an empty one."""
        for blob in (b"not json", b"null", b"[1, 2]", b"",
                     b'{"entries": null}', b'{"entries": []}',
                     b'{"entries": [1, 2]}', b'{"entries": "x"}'):
            with self.assertRaises(push.PushError) as cm:
                push.parse_index(blob)
            self.assertEqual(str(cm.exception),
                             "failed to parse existing cache index")

    def test_absent_or_empty_entries_accepted(self):
        for blob in (b'{}', b'{"entries": {}}'):
            self.assertIsInstance(push.parse_index(blob), dict)

    def test_existing_index_loaded(self):
        self.assertEqual(push.parse_index(b'{"public_key": "k"}'),
                         {"public_key": "k"})


class SigningSetupTest(unittest.TestCase):
    def test_signed_index_without_key_skips_push(self):
        with tempfile.TemporaryDirectory() as work_dir, \
                self.assertRaises(push.SkipPush):
            push.signing_setup("", {"public_key": "k:abc"}, work_dir)

    def test_falsy_public_key_is_unsigned(self):
        with tempfile.TemporaryDirectory() as work_dir:
            for index in ({}, {"public_key": None}, {"public_key": ""}):
                self.assertEqual(
                    push.signing_setup("", index, work_dir),
                    push.Signing())

    def test_index_key_mismatch_fails(self):
        converted = subprocess.CompletedProcess(
            ["nix"], 0, stdout="new-key:public\n", stderr="")
        with tempfile.TemporaryDirectory() as work_dir, \
                mock.patch.object(push, "nix", return_value=converted), \
                self.assertRaises(push.PushError) as cm:
            push.signing_setup(
                "new-key:secret", {"public_key": "old-key:public"},
                work_dir)
        self.assertIn("key rotation is not supported", str(cm.exception))


class CollectCandidatesTest(unittest.TestCase):
    def test_closure_failure_is_not_silently_ignored(self):
        with mock.patch.object(push.os.path, "exists", return_value=True), \
                mock.patch.object(
                    push, "path_infos",
                    side_effect=push.PushError("closure failed")):
            with self.assertRaisesRegex(push.PushError, "closure failed"):
                push.collect_candidates(STORE)


class RunTest(unittest.TestCase):
    def test_success_returns_summary_and_verifies_merged_index(self):
        old_hash = "b" * 32
        existing = {"entries": {old_hash: {"name": "old"}}}
        new_entry = {"name": "pkg", "nar_size": 3}
        registry = mock.Mock(spec=push.Registry)
        registry.fetch_index.return_value = existing
        registry.publish_index.return_value = "sha256:index"
        config = push.Config("owner/repo", "token", "", STORE, "")

        with mock.patch.object(push.Registry, "login",
                               return_value=registry), \
                mock.patch.object(push, "collect_candidates",
                                  return_value=[STORE]), \
                mock.patch.object(push, "load_path_infos", return_value={
                    STORE: {"signatures": []},
                }), \
                mock.patch.object(push, "export_paths",
                                  return_value=(2, {H32: new_entry})), \
                mock.patch.object(push.time, "strftime", return_value="t"), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            summary = push.run(config, "/tmp/work")

        self.assertEqual(summary, push.Summary(uploaded=1, skipped=2, total=2))
        index = registry.publish_index.call_args.args[0]
        self.assertEqual(index["entries"], {old_hash: {"name": "old"},
                                            H32: new_entry})
        registry.verify.assert_called_once_with("sha256:index")


class MainTest(unittest.TestCase):
    def test_success_returns_zero_and_prints_summary(self):
        err = io.StringIO()
        with mock.patch.object(
                push, "run", return_value=push.Summary(2, 1, 3)), \
                mock.patch.object(sys, "stderr", err):
            code = push.main({"NIXCACHE_REPO": "OWNER/REPO"})

        self.assertEqual(code, 0)
        self.assertIn(
            "::notice::nix/cache: uploaded 2, skipped 1, index entries 3",
            err.getvalue())

    def test_skip_is_warning_with_zero_exit(self):
        err = io.StringIO()
        with mock.patch.object(push, "run",
                               side_effect=push.SkipPush("no permission")), \
                mock.patch.object(sys, "stderr", err):
            code = push.main({})

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "::warning::no permission\n")

    def test_push_error_is_error_with_nonzero_exit(self):
        err = io.StringIO()
        with mock.patch.object(push, "run",
                               side_effect=push.PushError("broken")), \
                mock.patch.object(sys, "stderr", err):
            code = push.main({})

        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "::error::broken\n")

    def test_os_error_is_error_with_nonzero_exit(self):
        err = io.StringIO()
        with mock.patch.object(push, "run",
                               side_effect=OSError("disk full")), \
                mock.patch.object(sys, "stderr", err):
            code = push.main({})

        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "::error::disk full\n")


class LayerDigestTest(unittest.TestCase):
    def test_first_layer_digest(self):
        self.assertEqual(
            push.layer_digest(b'{"layers": [{"digest": "sha256:abc"}]}'),
            "sha256:abc")

    def test_malformed_shapes_fail(self):
        for body in (b"", b"null", b"[]", b"{}", b'{"layers": []}',
                     b'{"layers": ["x"]}', b'{"layers": null}',
                     b'{"layers": [null]}'):
            with self.assertRaises(push.PushError):
                push.layer_digest(body)


class HttpErrorTest(unittest.TestCase):
    """401/403 skip the push; anything else fails it."""

    def test_401_403_skip_push(self):
        for code in (401, 403):
            with self.assertRaises(push.SkipPush) as cm:
                push.raise_http_error(
                    code, "blob upload failed for sha256:x")
            self.assertIn(
                "blob upload failed for sha256:x "
                "(HTTP %d: insufficient permission; fork PRs and "
                "missing packages:* permissions are skipped)" % code,
                str(cm.exception))

    def test_other_code_fails(self):
        with self.assertRaises(push.PushError) as cm:
            push.raise_http_error(
                500, "OCI manifest push failed (cache-index)")
        self.assertEqual(
            str(cm.exception),
            "OCI manifest push failed (cache-index) (HTTP 500)")


class RegistryLoginTest(unittest.TestCase):
    """GITHUB_TOKEN is not a default environment variable, so an empty token is
    a real failure mode: it must be named instead of surfacing as an ambiguous
    403, and the status code must be reported."""

    def test_empty_token_fails_before_any_request(self):
        with mock.patch.object(push, "http_request") as http_request:
            with self.assertRaises(push.PushError) as cm:
                push.Registry.login("o/r", "")
        self.assertIn("GITHUB_TOKEN is unset", str(cm.exception))
        http_request.assert_not_called()

    def test_status_code_is_reported(self):
        with mock.patch.object(push, "http_request",
                               return_value=(403, [], b'{"errors":[]}')):
            with self.assertRaises(push.SkipPush) as cm:
                push.Registry.login("o/r", "ghs_fake")
        self.assertIn("HTTP 403", str(cm.exception))

    def test_200_without_token_field_is_invalid(self):
        with mock.patch.object(push, "http_request",
                               return_value=(200, [], b'{"token": ""}')):
            with self.assertRaises(push.PushError) as cm:
                push.Registry.login("o/r", "ghs_fake")
        self.assertEqual(str(cm.exception), "GHCR token response is invalid")

    def test_success_returns_the_registry_token(self):
        with mock.patch.object(push, "http_request",
                               return_value=(200, [], b'{"token": "oci-abc"}')):
            registry = push.Registry.login("o/r", "ghs_fake")
        self.assertEqual(registry, push.Registry("o/r", "oci-abc"))


class RegistryTest(unittest.TestCase):
    def test_request_only_sends_token_to_registry(self):
        registry = push.Registry("o/r", "secret")
        with mock.patch.object(push, "http_request",
                               return_value=(200, [], b"")) as request:
            registry.request("GET", "manifests/tag")
            registry.request("PUT", "https://storage.example/upload")

        first_headers = request.call_args_list[0].kwargs["headers"]
        second_headers = request.call_args_list[1].kwargs["headers"]
        self.assertEqual(first_headers["Authorization"], "Bearer secret")
        self.assertNotIn("Authorization", second_headers)

    def test_push_blob_accepts_in_memory_data(self):
        registry = push.Registry("o/r", "secret")
        responses = [
            (404, [], b""),
            (202, [("Location", "/upload/1")], b""),
            (201, [], b""),
        ]
        with mock.patch.object(push.Registry, "request",
                               side_effect=responses) as request:
            digest = registry.push_blob(b"abc")

        self.assertEqual(
            digest,
            "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9c"
            "b410ff61f20015ad")
        put = request.call_args_list[2]
        self.assertEqual(put.args[:2], ("PUT", push.build_put_url(
            "/upload/1", digest)))
        self.assertEqual(put.kwargs["body"], b"abc")
        self.assertEqual(put.kwargs["headers"]["Content-Length"], "3")

    def test_malformed_existing_manifest_fails_instead_of_resetting_index(self):
        registry = push.Registry("o/r", "secret")
        with mock.patch.object(push.Registry, "fetch_manifest",
                               return_value=(200, b'{"layers": []}')), \
                self.assertRaises(push.PushError):
            registry.fetch_index()

    def test_publish_index_builds_config_and_layer_in_memory(self):
        registry = push.Registry("o/r", "secret")
        digests = ["sha256:config", "sha256:index"]
        with mock.patch.object(push.Registry, "push_blob",
                               side_effect=digests) as push_blob, \
                mock.patch.object(push.Registry,
                                  "put_manifest") as put_manifest:
            digest = registry.publish_index({"entries": {}})

        self.assertEqual(digest, "sha256:index")
        config_body, index_body = [call.args[0]
                                   for call in push_blob.call_args_list]
        self.assertEqual(config_body, b"{}\n")
        self.assertEqual(json.loads(index_body), {"entries": {}})
        manifest = json.loads(put_manifest.call_args.args[1])
        self.assertEqual(manifest["config"]["digest"], "sha256:config")
        self.assertEqual(manifest["layers"][0]["digest"], "sha256:index")


class UrlTest(unittest.TestCase):
    def test_token_url_scope_and_service(self):
        self.assertEqual(
            push.token_url("MyOrg/MyRepo"),
            "https://ghcr.io/token?scope=repository:MyOrg/MyRepo/nix-cache:"
            "pull,push&service=ghcr.io")

    def test_location_relative_without_query(self):
        self.assertEqual(
            push.build_put_url("/v2/o/r/nix-cache/blobs/uploads/u-1",
                               "sha256:abc"),
            "https://ghcr.io/v2/o/r/nix-cache/blobs/uploads/u-1?digest=sha256:abc")

    def test_location_relative_with_query_uses_ampersand(self):
        self.assertEqual(
            push.build_put_url("/v2/o/r/blobs/uploads/u-2?_state=1",
                               "sha256:abc"),
            "https://ghcr.io/v2/o/r/blobs/uploads/u-2?_state=1&digest=sha256:abc")

    def test_location_absolute_left_untouched(self):
        self.assertEqual(
            push.build_put_url("https://storage.example.com/u-3?x=1",
                               "sha256:abc"),
            "https://storage.example.com/u-3?x=1&digest=sha256:abc")


class _ScenarioHandler(http.server.BaseHTTPRequestHandler):
    """Serve a scripted scenario, recording every request as
    (method, path, body, headers-dict).  A scenario item that is an
    exception is raised before any response bytes are written, which the
    client observes as a transport error."""
    scenario = []       # shared script: (status, [(name, value)], body) or Exception
    requests = []       # shared record of requests seen

    def _handle(self):
        body = self._read_body()
        _ScenarioHandler.requests.append(
            (self.command, self.path, body, dict(self.headers.items())))
        item = _ScenarioHandler.scenario.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, headers, data = item
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            body = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if not size:
                    self.rfile.readline()
                    return body
                body += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    do_GET = do_HEAD = do_PUT = do_POST = _handle

    def log_message(self, *args):
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass                     # scripted transport errors are expected


class _LocalServer:
    """A scripted HTTP server on 127.0.0.1; push.py's HTTPSConnection is
    patched to connect here while URL hostnames stay virtual."""

    def __init__(self, scenario):
        _ScenarioHandler.scenario = list(scenario)
        _ScenarioHandler.requests = []
        self.httpd = _QuietServer(("127.0.0.1", 0), _ScenarioHandler)
        self.port = self.httpd.server_address[1]
        # short poll so shutdown() returns quickly instead of ~0.5s per test
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class HttpRequestTest(unittest.TestCase):
    """http_request semantics against a real local HTTP server: 408/429/5xx
    and transport errors retried like curl --retry 3 --retry-all-errors;
    GET/HEAD follow 301/302/303/307/308, PUT/POST only 307/308 replaying the
    body and dropping Authorization when a redirect leaves the host;
    retries=0 call sites never retry."""

    URL = "https://ghcr.io/v2/o/r/nix-cache/manifests/cache-index"

    def run_request(self, scenario, retries, method="GET", body=None,
                    headers=None):
        with _LocalServer(scenario) as server:
            def factory(host, port, timeout=None):
                return http.client.HTTPConnection(
                    "127.0.0.1", server.port, timeout=timeout)

            with mock.patch("http.client.HTTPSConnection",
                            side_effect=factory), \
                    mock.patch.object(push, "RETRY_DELAY", 0):
                result = push.http_request(method, self.URL, headers=headers,
                                           body=body, retries=retries)
            requests = list(_ScenarioHandler.requests)
        return result, requests

    def test_5xx_retried_then_success(self):
        (status, _, body), requests = self.run_request(
            [(500, [], b""), (200, [], b"ok")], retries=3)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(len(requests), 2)       # initial + 1 retry

    def test_5xx_retry_budget_is_three(self):
        (status, _, _), requests = self.run_request(
            [(500, [], b"")] * 4, retries=3)
        self.assertEqual(status, 500)
        self.assertEqual(len(requests), 4)       # curl --retry 3 -> 4 attempts

    def test_408_and_429_retried_then_success(self):
        # curl --retry-all-errors retries 408/429 as well (like 5xx)
        (status, _, body), requests = self.run_request(
            [(429, [], b""), (408, [], b""), (200, [], b"ok")], retries=3)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(len(requests), 3)

    def test_429_exhausts_budget_like_5xx(self):
        (status, _, _), requests = self.run_request(
            [(429, [], b"")] * 4, retries=3)
        self.assertEqual(status, 429)
        self.assertEqual(len(requests), 4)

    def test_4xx_not_retried(self):
        (status, _, _), requests = self.run_request([(403, [], b"")], retries=3)
        self.assertEqual(status, 403)
        self.assertEqual(len(requests), 1)

    def test_get_follows_307_redirect(self):
        (status, _, body), requests = self.run_request(
            [(307, [("Location", "https://cdn.example.com/next")], b""),
             (200, [], b"ok")], retries=3)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(len(requests), 2)       # one attempt, one hop

    def test_cross_host_redirect_strips_authorization(self):
        # a redirect leaving ghcr.io must not carry the bearer token along
        (status, _, _), requests = self.run_request(
            [(302, [("Location", "https://cdn.example.com/next")], b""),
             (200, [], b"ok")], retries=0,
            headers={"Authorization": "Bearer tok"})
        self.assertEqual(status, 200)
        self.assertEqual(requests[0][3].get("Authorization"), "Bearer tok")
        self.assertNotIn("Authorization", requests[1][3])

    def test_relative_location_resolved_absolute(self):
        (status, _, _), requests = self.run_request(
            [(307, [("Location", "/v2/o/r/blobs/sha256:abc")], b""),
             (200, [], b"ok")], retries=0)
        self.assertEqual(status, 200)
        self.assertEqual(requests[1][1], "/v2/o/r/blobs/sha256:abc")

    def test_redirect_chain_limited_to_five_hops(self):
        (status, _, _), requests = self.run_request(
            [(302, [("Location", "/loop")], b"")] * 6, retries=0)
        self.assertEqual(status, 302)            # 6th hop not followed
        self.assertEqual(len(requests), 6)       # 1 + 5 follows

    def test_head_follows_redirect(self):
        (status, _, _), requests = self.run_request(
            [(307, [("Location", "/next")], b""),
             (404, [], b"")], retries=0, method="HEAD")
        self.assertEqual(status, 404)
        self.assertEqual(len(requests), 2)

    def test_put_follows_307_308_with_body_replay(self):
        payload = b"payload" * 100
        (status, _, _), requests = self.run_request(
            [(307, [("Location", "/up/1")], b""),
             (308, [("Location", "/up/2")], b""),
             (201, [], b"")], retries=0, method="PUT",
            body=io.BytesIO(payload))
        self.assertEqual(status, 201)
        self.assertEqual(len(requests), 3)       # 1 + 2 hops
        for request in requests:                 # full replay each hop
            self.assertEqual(request[2], payload)

    def test_put_302_not_followed(self):
        # GET/HEAD follow 301/302/303/307/308; PUT/POST only 307/308
        (status, _, _), requests = self.run_request(
            [(302, [("Location", "/up")], b"")], retries=0, method="PUT")
        self.assertEqual(status, 302)
        self.assertEqual(len(requests), 1)

    def test_retries_zero_returns_immediately(self):
        # HEAD and fetch/readback GETs pass retries=0: a 500 surfaces as-is
        (status, _, _), requests = self.run_request([(500, [], b"")], retries=0)
        self.assertEqual(status, 500)
        self.assertEqual(len(requests), 1)

    def test_transport_error_retried_then_success(self):
        (status, _, body), requests = self.run_request(
            [ConnectionError("boom"), (200, [], b"ok")], retries=3)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(len(requests), 2)

    def test_transport_error_exhausted_returns_zero(self):
        (status, _, _), requests = self.run_request(
            [ConnectionError("boom")] * 3, retries=2)
        self.assertEqual(status, 0)
        self.assertEqual(len(requests), 3)

    def test_retried_put_replays_file_body_from_zero(self):
        payload = b"payload" * 100
        (status, _, _), requests = self.run_request(
            [(503, [], b""), (201, [], b"")], retries=3, method="PUT",
            body=io.BytesIO(payload))
        self.assertEqual(status, 201)
        # each attempt must have sent the full payload (seek(0) before request)
        for request in requests:
            self.assertEqual(request[2], payload)


@unittest.skipUnless(shutil.which("nix"), "nix not on PATH")
class RealNixTest(unittest.TestCase):
    def test_sri_converts_to_bare_nix32(self):
        # nix 2.34 path-info --json emits SRI narHashes; convert yields a
        # bare nix-base32 (52 chars for sha256) with no prefix.
        b32 = push.nix_hash_convert(SRI_ZERO)
        self.assertEqual(len(b32), 52)
        self.assertNotIn("-", b32)

    def test_make_narinfo_end_to_end_with_real_convert(self):
        text = push.make_narinfo(STORE, H32, 1, "f" * 52,
                                 {"narHash": SRI_ZERO, "narSize": 10})
        self.assertIn("NarHash: sha256:" + NIX32_ZERO, text)


@unittest.skipUnless(shutil.which("nix") and shutil.which("nix-hash"),
                     "nix / nix-hash not on PATH")
class FileHashEquivalenceTest(unittest.TestCase):
    """FileHash is derived from the blob digest, so `nix hash convert` on a
    sha256:<hex> must agree byte-for-byte with `nix-hash --flat` on the file.
    Otherwise every narinfo FileHash silently changes."""

    def test_convert_from_hex_matches_nix_hash_flat(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"hello nix-cache")
            f.flush()
            cli = subprocess.run(
                ["nix-hash", "--flat", "--type", "sha256", "--base32", f.name],
                capture_output=True, text=True, check=True).stdout.strip()
            digest = push.blob_digest(f.name)
        self.assertEqual(push.nix_hash_convert(digest), cli)
        self.assertEqual(len(cli), 52)      # sha256 in nix-base32
        self.assertNotIn("-", cli)


class CompressStreamTest(unittest.TestCase):
    """dump_nar prefers stdlib zstd and falls back to xz."""

    def _data(self):
        return b"hello world\n" * 100000

    def test_format_selection_matches_runtime(self):
        expected = (("zstd", "zst") if push._ZstdCompressor is not None
                    else ("xz", "xz"))
        self.assertEqual((push.COMPRESSION, push.COMPRESSION_EXT), expected)

    def test_active_path_round_trip(self):
        out = io.BytesIO()
        push._compress_stream(io.BytesIO(self._data()), out)
        if push.COMPRESSION == "zstd":
            from compression.zstd import ZstdDecompressor
            data = ZstdDecompressor().decompress(out.getvalue())
        else:
            data = lzma.decompress(out.getvalue())
        self.assertEqual(data, self._data())

    def test_active_path_magic(self):
        out = io.BytesIO()
        push._compress_stream(io.BytesIO(b"abc"), out)
        magic = (b"\x28\xb5\x2f\xfd" if push.COMPRESSION == "zstd"
                 else b"\xfd7zXZ\x00")
        self.assertTrue(out.getvalue().startswith(magic))

    def test_xz_fallback(self):
        out = io.BytesIO()
        with mock.patch.object(push, "_ZstdCompressor", None):
            push._compress_stream(io.BytesIO(self._data()), out)
        self.assertEqual(lzma.decompress(out.getvalue()), self._data())


if __name__ == "__main__":
    unittest.main()
