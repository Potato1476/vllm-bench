"""Tests for the publisher -- the half of the contract that writes.

The behaviour worth testing is not "it uploads files". It is the three refusals:

    it refuses to publish a version that would fail the CD gate,
    it refuses to overwrite a version that already exists,
    it refuses to write _READY when what landed does not match what was sent.

Each of those, skipped, produces a version that looks published and is not usable --
and the symptom surfaces somewhere else entirely: a GitHub issue fifteen minutes later,
a rollback that returns different weights, or a pod that will not start.
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.scripts import check_model as cm  # noqa: E402
from bench.scripts import publish_model as pm  # noqa: E402


def write_raw(root: Path, *, extra: dict[str, bytes] | None = None,
              architectures: list[str] | None = None) -> Path:
    """A plausible model directory, as a training pipeline would leave it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({
        "architectures": architectures or ["Qwen2ForCausalLM"],
        "max_position_embeddings": 8192,
    }))
    (root / "tokenizer.json").write_text('{"version": "1.0"}')
    header = json.dumps({"__metadata__": {"format": "pt"}}).encode()
    (root / "model-00001-of-00001.safetensors").write_bytes(
        struct.pack("<Q", len(header)) + header + b"\0" * 4096)
    for name, data in (extra or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name) / "raw"
        self.addCleanup(self._dir.cleanup)


class StagingGateTest(_Tmp):
    def test_an_unpublished_directory_is_still_fully_checked(self) -> None:
        """The regression this exists for.

        A staging directory has no _READY and no signature yet -- both are written last on
        purpose. check() used to return at that first precondition, and the publisher
        filtered those two findings out, so its pre-flight reported success having checked
        nothing at all. Every later gate still ran in CI, so the only visible symptom was
        an empty facts block that nobody would look twice at.
        """
        write_raw(self.root, extra={"pytorch_model.bin": b"\x80\x04junk"})
        files = pm.discover(self.root)
        manifest = pm.build_manifest(self.root, files, name="m", version="v",
                                     kind="full", served_name="s", producer="t")
        (self.root / "manifest.json").write_text(json.dumps(manifest))

        report = cm.check(cm.LocalSource(self.root), require_signature=False,
                          published=False, limits=cm.CardLimits(22.5, 1.5, 0.90))
        self.assertFalse(report.ok)
        self.assertIn("pickle", " | ".join(f.message for f in report.findings))

    def test_an_unpublished_directory_reports_facts(self) -> None:
        """Facts present is the cheap signal that the gate did real work; their absence is
        what exposed the bug above."""
        write_raw(self.root)
        files = pm.discover(self.root)
        manifest = pm.build_manifest(self.root, files, name="m", version="v",
                                     kind="full", served_name="s", producer="t")
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        report = cm.check(cm.LocalSource(self.root), require_signature=False,
                          published=False, limits=cm.CardLimits(22.5, 1.5, 0.90))
        self.assertTrue(report.ok, [str(f) for f in report.findings])
        self.assertIn("weights_gib", report.facts)

    def test_a_published_directory_without_ready_is_still_skipped(self) -> None:
        """The CD gate's behaviour must NOT change: on S3, a missing _READY means the
        upload is still in progress."""
        write_raw(self.root)
        (self.root / "manifest.json").write_text("{}")
        (self.root / cm.SIGNATURE_FILE).write_bytes(b"x")
        report = cm.check(cm.LocalSource(self.root), require_signature=False)
        self.assertFalse(report.ok)
        self.assertIn("_READY", " | ".join(f.message for f in report.findings))


class DiscoverTest(_Tmp):
    def test_build_leftovers_are_not_published(self) -> None:
        """Not a safety issue, a cost one: everything here is synced onto the NVMe of
        every pod on every start, and a Hub download's .cache/ is hundreds of megabytes of
        lock and resume metadata."""
        write_raw(self.root, extra={".cache/lock": b"x" * 100, ".DS_Store": b"y"})
        found = {p.as_posix() for p in pm.discover(self.root)}
        self.assertNotIn(".DS_Store", found)
        self.assertFalse(any(p.startswith(".cache/") for p in found))
        self.assertIn("config.json", found)

    def test_contract_metadata_is_never_taken_from_the_source(self) -> None:
        """manifest.json and _READY left over from an earlier publish must not be copied
        forward -- a stale manifest would be hashed into the new version as an ordinary
        file, and a stale _READY would make CD pick the version up before it is signed."""
        write_raw(self.root, extra={"manifest.json": b"{}", "_READY": b""})
        found = {p.as_posix() for p in pm.discover(self.root)}
        self.assertNotIn("manifest.json", found)
        self.assertNotIn("_READY", found)


class ManifestTest(_Tmp):
    def test_every_file_gets_a_size_and_a_hash(self) -> None:
        write_raw(self.root)
        files = pm.discover(self.root)
        manifest = pm.build_manifest(self.root, files, name="m", version="v")
        self.assertEqual(len(manifest["files"]), len(files))
        for entry in manifest["files"]:
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertGreater(entry["size"], 0)

    def test_the_hash_is_of_the_content(self) -> None:
        write_raw(self.root)
        first = pm.build_manifest(self.root, pm.discover(self.root), name="m")
        (self.root / "config.json").write_text('{"architectures": ["LlamaForCausalLM"]}')
        second = pm.build_manifest(self.root, pm.discover(self.root), name="m")
        by_path = {e["path"]: e["sha256"] for e in first["files"]}
        changed = {e["path"]: e["sha256"] for e in second["files"]}
        self.assertNotEqual(by_path["config.json"], changed["config.json"])


class OverwriteTest(_Tmp):
    def test_publishing_over_an_existing_version_is_refused(self) -> None:
        """The quiet one. Rollback has to return the exact bytes that ran; republishing
        over a version leaves deploy/state.yaml naming a version that is no longer what it
        was, so a rollback restores something nobody chose."""
        write_raw(self.root)
        argv = ["--from", str(self.root), "--name", "moc-7b", "--version", "v1",
                "--served-name", "qwen2.5-7b", "--signer", "training-pipeline",
                "--bucket", "some-bucket"]
        with mock.patch.object(pm, "s3_prefix_exists", return_value=True), \
             mock.patch.object(pm, "upload") as uploaded, \
             mock.patch.object(pm, "sign") as signed:
            code = pm.main(argv)
        self.assertEqual(code, 1)
        uploaded.assert_not_called()
        signed.assert_not_called()

    def test_the_overwrite_check_runs_before_hashing(self) -> None:
        """Hashing 15 GiB to then refuse is a quarter of an hour wasted on an answer known
        up front."""
        write_raw(self.root)
        argv = ["--from", str(self.root), "--name", "moc-7b", "--version", "v1",
                "--served-name", "qwen2.5-7b", "--signer", "t", "--bucket", "b"]
        with mock.patch.object(pm, "s3_prefix_exists", return_value=True), \
             mock.patch.object(pm, "build_manifest") as hashed:
            pm.main(argv)
        hashed.assert_not_called()


class RefusalTest(_Tmp):
    def test_a_bad_model_is_refused_before_anything_is_uploaded(self) -> None:
        write_raw(self.root, extra={"pytorch_model.bin": b"\x80\x04junk"})
        argv = ["--from", str(self.root), "--name", "moc-7b", "--version", "v1",
                "--served-name", "qwen2.5-7b", "--signer", "t", "--bucket", "b"]
        with mock.patch.object(pm, "s3_prefix_exists", return_value=False), \
             mock.patch.object(pm, "upload") as uploaded, \
             mock.patch.object(pm, "sign") as signed, \
             mock.patch.object(pm, "put_ready") as ready:
            code = pm.main(argv)
        self.assertEqual(code, 1)
        uploaded.assert_not_called()
        signed.assert_not_called()
        ready.assert_not_called()

    def test_an_untried_architecture_is_refused_locally(self) -> None:
        write_raw(self.root, architectures=["SomethingNewForCausalLM"])
        argv = ["--from", str(self.root), "--name", "m", "--version", "v1",
                "--served-name", "s", "--signer", "t", "--dry-run"]
        self.assertEqual(pm.main(argv), 1)

    def test_a_readback_mismatch_leaves_the_version_invisible(self) -> None:
        """_READY is what makes a version visible to CD. If what landed disagrees with
        what was sent, not writing it is the whole mitigation: the half-uploaded version
        simply never exists as far as rollout is concerned."""
        write_raw(self.root)
        argv = ["--from", str(self.root), "--name", "moc-7b", "--version", "v1",
                "--served-name", "qwen2.5-7b", "--signer", "t", "--bucket", "b"]
        truncated = mock.Mock()
        truncated.sizes.return_value = {"config.json": 1}   # everything else missing
        with mock.patch.object(pm, "s3_prefix_exists", return_value=False), \
             mock.patch.object(pm, "sign"), \
             mock.patch.object(pm, "upload"), \
             mock.patch.object(pm, "S3Source", return_value=truncated), \
             mock.patch.object(pm, "put_ready") as ready:
            code = pm.main(argv)
        self.assertEqual(code, 1)
        ready.assert_not_called()

    def test_a_clean_publish_writes_ready_last(self) -> None:
        write_raw(self.root)
        argv = ["--from", str(self.root), "--name", "moc-7b", "--version", "v1",
                "--served-name", "qwen2.5-7b", "--signer", "t", "--bucket", "b"]
        order: list[str] = []
        sent: dict[str, int] = {}
        landed = mock.Mock()
        landed.sizes.side_effect = lambda: sent

        def fake_upload(staging, bucket, prefix):
            order.append("upload")
            sent.update({p.relative_to(staging).as_posix(): p.stat().st_size
                         for p in staging.rglob("*") if p.is_file()})

        with mock.patch.object(pm, "s3_prefix_exists", return_value=False), \
             mock.patch.object(pm, "sign", side_effect=lambda *a, **k: order.append("sign")), \
             mock.patch.object(pm, "upload", side_effect=fake_upload), \
             mock.patch.object(pm, "S3Source", return_value=landed), \
             mock.patch.object(pm, "put_ready", side_effect=lambda *a: order.append("ready")):
            code = pm.main(argv)
        self.assertEqual(code, 0)
        self.assertEqual(order, ["sign", "upload", "ready"])


if __name__ == "__main__":
    unittest.main()


class SignFlagsTest(unittest.TestCase):
    def test_signing_uses_a_bundle_and_makes_no_network_call(self) -> None:
        """cosign v3 removed --output-signature and --tlog-upload; signing with --key and
        no signing config contacts nothing, which is exactly the property we want."""
        with mock.patch.object(pm.shutil, "which", return_value="/usr/bin/cosign"), \
             mock.patch.object(pm.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stderr="")) as run:
            pm.sign(Path("/tmp/manifest.json"), "awskms:///alias/k", Path("/tmp/out.bundle"))
        argv = list(run.call_args[0][0])
        self.assertEqual(argv[:2], ["cosign", "sign-blob"])
        self.assertIn("--bundle", argv)
        self.assertIn("--yes", argv)
        for gone in ("--output-signature", "--tlog-upload", "--tlog-upload=false"):
            self.assertNotIn(gone, argv, f"{gone} da bi cosign v3 bo")
