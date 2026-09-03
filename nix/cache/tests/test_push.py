"""Unit tests for nix/cache/post/push.py.

Loaded via importlib from the file path (the module lives in `post/`, not in
`tests/`, and must not be on sys.path as a package).  Every test that would
touch subprocesses/network either injects a fake or is skipped when nix is
not on PATH (the CI unit-tests step runs before `./nix` installs anything).
"""
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
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


class FilterPathsTest(unittest.TestCase):
    def rows(self, *sigs_by_path):
        return [(p, list(sigs)) for p, sigs in sigs_by_path]

    def test_in_index_skipped(self):
        keep, missing = push.filter_paths([(STORE, ["any-sig"])], {H32}, "own-key")
        self.assertEqual(keep, [])
        self.assertEqual(missing, [])

    def test_no_key_any_signature_skipped(self):
        signed = "/nix/store/" + "b" * 32 + "-signed"
        unsigned = "/nix/store/" + "c" * 32 + "-unsigned"
        rows = [(signed, ["elsewhere:abc"]), (unsigned, [])]
        keep, missing = push.filter_paths(rows, set(), "")
        self.assertEqual(keep, [unsigned])
        self.assertEqual(missing, [])

    def test_with_key_external_signature_skipped(self):
        ext = "/nix/store/" + "b" * 32 + "-ext"
        mixed = "/nix/store/" + "c" * 32 + "-mixed"
        rows = [(ext, ["other-cache:sig"]), (mixed, ["own-key:sig", "other-cache:sig"])]
        keep, missing = push.filter_paths(rows, set(), "own-key")
        self.assertEqual(keep, [])
        self.assertEqual(missing, [])

    def test_with_key_own_signature_kept(self):
        keep, missing = push.filter_paths([(STORE, ["own-key:sig1"])], set(), "own-key")
        self.assertEqual(keep, [STORE])
        self.assertEqual(missing, [])

    def test_missing_own_signature_reported(self):
        keep, missing = push.filter_paths([(STORE, [])], set(), "own-key")
        self.assertEqual(keep, [])
        self.assertEqual(missing, [STORE])


class MakeNarinfoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name) / "cache"
        self.cache_dir.mkdir()
        self.path_info = json.dumps({
            STORE: {
                "narHash": SRI_ZERO,
                "narSize": 1000,
                "references": ["/nix/store/" + "b" * 32 + "-dep"],
                "deriver": "/nix/store/" + "c" * 32 + "-pkg-1.0.drv",
                "signatures": ["own-key:sig1"],
            }
        })
        self.convert = lambda h: NIX32_ZERO

    def tearDown(self):
        self.tmp.cleanup()

    def narinfo_file(self):
        return self.cache_dir / (H32 + ".narinfo")

    def test_sri_converted_with_single_sha256_prefix(self):
        push.make_narinfo(STORE, H32, 123, "f" * 52, self.path_info,
                          str(self.cache_dir), convert=self.convert)
        text = self.narinfo_file().read_text()
        self.assertIn("NarHash: sha256:" + NIX32_ZERO, text)
        self.assertEqual(text.count("NarHash: sha256:"), 1)
        self.assertIn("StorePath: " + STORE, text)
        self.assertIn("URL: nar/" + H32 + ".nar.xz", text)
        self.assertIn("Compression: xz", text)
        self.assertIn("FileHash: sha256:" + "f" * 52, text)
        self.assertIn("FileSize: 123", text)
        self.assertIn("NarSize: 1000", text)
        self.assertIn("References: " + "b" * 32 + "-dep", text)
        self.assertIn("Deriver: " + "c" * 32 + "-pkg-1.0.drv", text)
        self.assertIn("Sig: own-key:sig1", text)

    def test_prefix_not_doubled_for_prefixed_narhash_but_convert_skipped(self):
        called = []
        info = json.loads(self.path_info)
        info[STORE]["narHash"] = "sha256:" + NIX32_ZERO  # already prefixed form

        def convert(h):
            called.append(h)
            return NIX32_ZERO

        push.make_narinfo(STORE, H32, 1, "f" * 52, json.dumps(info),
                          str(self.cache_dir), convert=convert)
        self.assertEqual(called, [])  # non-SRI input is passed through untouched

    def test_nar_size_zero_skips(self):
        info = json.loads(self.path_info)
        info[STORE]["narSize"] = 0
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(push.NarinfoSkip) as cm:
                push.make_narinfo(STORE, H32, 1, "f" * 52, json.dumps(info),
                                  str(self.cache_dir), convert=self.convert)
        self.assertEqual(cm.exception.code, 3)
        self.assertIn(f"narSize <= 0 for {STORE}; skipping", err.getvalue())
        self.assertFalse(self.narinfo_file().exists())

    def test_empty_file_hash_skips(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(push.NarinfoSkip) as cm:
                push.make_narinfo(STORE, H32, 1, "", self.path_info,
                                  str(self.cache_dir), convert=self.convert)
        self.assertEqual(cm.exception.code, 4)
        self.assertIn(f"empty FileHash/NarHash for {STORE}; skipping", err.getvalue())
        self.assertFalse(self.narinfo_file().exists())

    def test_empty_nar_hash_skips(self):
        info = json.loads(self.path_info)
        info[STORE]["narHash"] = ""
        with self.assertRaises(push.NarinfoSkip) as cm:
            push.make_narinfo(STORE, H32, 1, "f" * 52, json.dumps(info),
                              str(self.cache_dir), convert=self.convert)
        self.assertEqual(cm.exception.code, 4)

    def test_array_form_path_info(self):
        # old nix --json (no format 1) emits the array form; make_narinfo
        # must tolerate it exactly like the bash inline python.
        array_info = json.dumps([{"narHash": SRI_ZERO, "narSize": 7,
                                  "references": [], "deriver": "", "signatures": []}])
        push.make_narinfo(STORE, H32, 1, "f" * 52, array_info,
                          str(self.cache_dir), convert=self.convert)
        self.assertIn("NarSize: 7", self.narinfo_file().read_text())


class MergeIndexTest(unittest.TestCase):
    def test_new_entries_override_existing(self):
        h = "d" * 32
        existing = {"version": 1, "entries": {h: {"name": "old", "narinfo": "old"}}}
        new = {h: {"name": "new", "narinfo": "new", "nar_digest": "sha256:x",
                   "nar_size": 1, "added": "t"}}
        index = push.merge_index(existing, new, "own-key", "owner/repo",
                                 "ghcr.io", "2026-09-02T00:00:00Z")
        self.assertEqual(index["entries"][h]["name"], "new")
        self.assertEqual(len(index["entries"]), 1)
        self.assertEqual(index["version"], 1)
        self.assertEqual(index["gc_roots"], [])
        self.assertEqual(index["image"], "ghcr.io/owner/repo/nix-cache")
        self.assertEqual(index["repo"], "owner/repo")
        self.assertEqual(index["registry"], "ghcr.io")

    def test_public_key_kept_when_no_key(self):
        existing = {"public_key": "old-key", "entries": {}}
        index = push.merge_index(existing, {}, "", "r", "ghcr.io", "t")
        self.assertEqual(index["public_key"], "old-key")

    def test_public_key_overridden_by_own_key(self):
        existing = {"public_key": "old-key", "entries": {}}
        index = push.merge_index(existing, {}, "new-key", "r", "ghcr.io", "t")
        self.assertEqual(index["public_key"], "new-key")

    def test_dirty_existing_json_tolerated(self):
        self.assertEqual(push.parse_existing("not json"), {})
        self.assertEqual(push.parse_existing("null"), {})
        self.assertEqual(push.parse_existing("[1, 2]"), {})
        new = {"a": "b"}
        index = push.merge_index(push.parse_existing("{corrupt"), new, "", "r",
                                 "ghcr.io", "t")
        self.assertEqual(index["entries"], {"a": "b"})
        self.assertEqual(index["public_key"], "")


class FailOrSkipTest(unittest.TestCase):
    """401/403 -> warning + exit 0; anything else -> error + exit 1."""

    def test_401_403_warn_and_exit_zero(self):
        for code in (401, 403):
            err = io.StringIO()
            with redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    push.fail_or_skip(code, "blob upload failed for sha256:x")
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("::warning::blob upload failed for sha256:x "
                          "(HTTP %d: insufficient permission; fork PRs and "
                          "missing packages:* permissions are skipped)" % code,
                          err.getvalue())

    def test_other_code_error_and_exit_one(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                push.fail_or_skip(500, "OCI manifest push failed (cache-index)")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("::error::OCI manifest push failed (cache-index) "
                      "(HTTP 500)", err.getvalue())


class UrlTest(unittest.TestCase):
    def test_token_url_scope_and_service(self):
        self.assertEqual(
            push.token_url("MyOrg/MyRepo", "ghcr.io"),
            "https://ghcr.io/token?scope=repository:MyOrg/MyRepo/nix-cache:"
            "pull,push&service=ghcr.io")

    def test_location_relative_without_query(self):
        self.assertEqual(
            push.build_put_url("/v2/o/r/nix-cache/blobs/uploads/u-1", "ghcr.io",
                               "sha256:abc"),
            "https://ghcr.io/v2/o/r/nix-cache/blobs/uploads/u-1?digest=sha256:abc")

    def test_location_relative_with_query_uses_ampersand(self):
        self.assertEqual(
            push.build_put_url("/v2/o/r/blobs/uploads/u-2?_state=1", "ghcr.io",
                               "sha256:abc"),
            "https://ghcr.io/v2/o/r/blobs/uploads/u-2?_state=1&digest=sha256:abc")

    def test_location_absolute_left_untouched(self):
        self.assertEqual(
            push.build_put_url("https://storage.example.com/u-3?x=1", "ghcr.io",
                               "sha256:abc"),
            "https://storage.example.com/u-3?x=1&digest=sha256:abc")


class _FakeResponse:
    def __init__(self, status, body=b"", headers=()):
        self.status = status
        self._body = body
        self._headers = list(headers)

    def read(self):
        return self._body

    def getheaders(self):
        return self._headers

    def close(self):
        pass


class _FakeConn:
    def __init__(self, host, port, timeout=None, scenario=()):
        self.host, self.port, self.timeout = host, port, timeout
        self.scenario = scenario
        self.requested = []

    def request(self, method, path, body=None, headers=None):
        # consume a file body the way http.client does, then record the bytes
        data = body.read() if hasattr(body, "read") else body
        self.requested.append((method, path, data, headers))

    def getresponse(self):
        item = self.scenario.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


class HttpRequestTest(unittest.TestCase):
    """http_request retry semantics: 5xx and transport errors retried with
    curl --retry 3 --retry-all-errors semantics; retries=0 call sites (HEAD,
    manifest GETs) never retry."""

    def run_request(self, scenario, retries, method="GET", body=None, retry_delay=0.0):
        conns = []

        def factory(host, port, timeout=None):
            conn = _FakeConn(host, port, timeout, scenario)
            conns.append(conn)
            return conn

        url = "https://ghcr.io/v2/o/r/nix-cache/manifests/cache-index"
        with mock.patch("http.client.HTTPSConnection", side_effect=factory):
            result = push.http_request(method, url, body=body, retries=retries,
                                       retry_delay=retry_delay)
        return result, conns

    def test_5xx_retried_then_success(self):
        (status, _, body), conns = self.run_request(
            [_FakeResponse(500), _FakeResponse(200, b"ok")], retries=3)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(len(conns), 2)  # initial + 1 retry

    def test_5xx_retry_budget_is_three(self):
        (status, _, _), conns = self.run_request(
            [_FakeResponse(500)] * 4, retries=3)
        self.assertEqual(status, 500)
        self.assertEqual(len(conns), 4)  # curl --retry 3 -> 4 attempts

    def test_408_and_429_retried_then_success(self):
        # curl --retry-all-errors retries 408/429 as well (like 5xx)
        (status, _, body), conns = self.run_request(
            [_FakeResponse(429), _FakeResponse(408), _FakeResponse(200, b"ok")],
            retries=3)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(len(conns), 3)

    def test_429_exhausts_budget_like_5xx(self):
        (status, _, _), conns = self.run_request(
            [_FakeResponse(429)] * 4, retries=3)
        self.assertEqual(status, 429)
        self.assertEqual(len(conns), 4)

    def test_4xx_not_retried(self):
        (status, _, _), conns = self.run_request([_FakeResponse(403)], retries=3)
        self.assertEqual(status, 403)
        self.assertEqual(len(conns), 1)

    def test_retries_zero_returns_immediately(self):
        # HEAD and fetch/readback GETs pass retries=0: a 500 surfaces as-is
        (status, _, _), conns = self.run_request([_FakeResponse(500)], retries=0)
        self.assertEqual(status, 500)
        self.assertEqual(len(conns), 1)

    def test_transport_error_retried_then_success(self):
        (status, _, body), conns = self.run_request(
            [ConnectionError("boom"), _FakeResponse(200, b"ok")], retries=3)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(len(conns), 2)

    def test_transport_error_exhausted_returns_zero(self):
        (status, _, _), conns = self.run_request(
            [ConnectionError("boom")] * 3, retries=2)
        self.assertEqual(status, 0)
        self.assertEqual(len(conns), 3)

    def test_retried_put_replays_file_body_from_zero(self):
        payload = b"payload" * 100
        f = io.BytesIO(payload)
        (status, _, _), conns = self.run_request(
            [_FakeResponse(503), _FakeResponse(201)], retries=3, method="PUT",
            body=f)
        self.assertEqual(status, 201)
        # each attempt must have sent the full payload (seek(0) before request)
        for conn in conns:
            self.assertEqual(conn.requested[0][2], payload)


@unittest.skipUnless(shutil.which("nix"), "nix not on PATH")
class RealNixTest(unittest.TestCase):
    def test_sri_converts_to_bare_nix32(self):
        # nix 2.34 path-info --json emits SRI narHashes; convert yields a
        # bare nix-base32 (52 chars for sha256) with no prefix.
        b32 = push.nix_hash_convert(SRI_ZERO)
        self.assertEqual(len(b32), 52)
        self.assertNotIn("-", b32)

    def test_make_narinfo_end_to_end_with_real_convert(self):
        with tempfile.TemporaryDirectory() as tmp:
            push.make_narinfo(STORE, H32, 1, "f" * 52,
                              json.dumps({STORE: {"narHash": SRI_ZERO,
                                                  "narSize": 10}}),
                              tmp)
            text = (Path(tmp) / (H32 + ".narinfo")).read_text()
            self.assertIn("NarHash: sha256:" + NIX32_ZERO, text)


if __name__ == "__main__":
    unittest.main()
