"""Tests for the gates a model passes before reaching production.

These matter more than most tests in this repo. Rollout is automatic: whatever
check_model.py waves through gets served, with nobody looking at it. So the tests assert
two different things, and the second is the easy one to forget:

    that a bad version is REJECTED, and
    that it is rejected for the RIGHT REASON, at the RIGHT POINT.

The ordering case is the one worth spelling out. If the manifest were parsed before its
signature verified, the checker would be reasoning about attacker-controlled data while
believing it was reasoning about signed data -- and every later check would still pass,
because they would all be checking the attacker's own numbers against each other.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.scripts import check_model as cm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _safetensors(payload_bytes: int = 2048) -> bytes:
    """A real, minimally valid safetensors file: 8-byte header length, JSON header, data.

    Built rather than mocked because the fit check sums real file sizes, and a stub of a
    different length would make the arithmetic tests measure the stub.
    """
    header = json.dumps({"__metadata__": {"format": "pt"}}).encode()
    return struct.pack("<Q", len(header)) + header + b"\0" * payload_bytes


def build_model(
    root: Path,
    *,
    name: str = "moc-7b",
    version: str = "2026-11-20-r1",
    served_name: str = "qwen2.5-7b",
    architectures: list[str] | None = None,
    weight_bytes: int = 2048,
    shards: int = 1,
    ready: bool = True,
    extra_files: dict[str, bytes] | None = None,
    manifest_patch: dict | None = None,
    drop_from_manifest: set[str] | None = None,
) -> Path:
    """A model directory that passes every gate, which tests then break one way at a time."""
    root.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {
        "config.json": json.dumps({
            "architectures": architectures if architectures is not None else ["Qwen2ForCausalLM"],
            "max_position_embeddings": 8192,
        }).encode(),
        "tokenizer.json": b'{"version": "1.0"}',
    }
    for i in range(shards):
        files[f"model-{i + 1:05d}-of-{shards:05d}.safetensors"] = _safetensors(weight_bytes)
    files.update(extra_files or {})

    for rel, data in files.items():
        (root / rel).write_bytes(data)

    manifest = {
        "name": name,
        "version": version,
        "kind": "full",
        "served_name": served_name,
        "producer": "test",
        "files": [
            {"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for rel, data in sorted(files.items())
            if not (drop_from_manifest and rel in drop_from_manifest)
        ],
    }
    manifest.update(manifest_patch or {})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "manifest.json.sig").write_bytes(b"signature-not-checked-in-these-tests")
    if ready:
        (root / "_READY").write_bytes(b"")
    return root


def run(root: Path, **kwargs) -> cm.Report:
    kwargs.setdefault("require_signature", False)
    kwargs.setdefault("limits", cm.CardLimits(22.5, 1.5, 0.90))
    return cm.check(cm.LocalSource(root), **kwargs)


def messages(report: cm.Report) -> str:
    return " | ".join(f.message for f in report.findings)


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name) / "candidate"
        self.addCleanup(self._dir.cleanup)


class HappyPathTest(_Tmp):
    def test_a_well_formed_version_passes_both_gates(self) -> None:
        build_model(self.root)
        report = run(self.root, expect_name="moc-7b", expect_version="2026-11-20-r1")
        self.assertTrue(report.ok, messages(report))
        self.assertEqual(report.facts["served_name"], "qwen2.5-7b")

    def test_a_sharded_checkpoint_passes(self) -> None:
        build_model(self.root, shards=4)
        self.assertTrue(run(self.root).ok)


class IncompleteUploadTest(_Tmp):
    def test_a_version_still_uploading_is_skipped_not_failed(self) -> None:
        """S3 has no atomic directory and a 15 GiB upload takes minutes. Without _READY
        gating, a candidate is picked up mid-upload and the issue blames the model for a
        race in the publisher."""
        build_model(self.root, ready=False)
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("_READY", messages(report))

    def test_an_incomplete_upload_stops_immediately(self) -> None:
        """One finding, not a cascade. A half-written directory would otherwise report
        every missing shard and bury the one fact that explains all of them."""
        build_model(self.root, ready=False)
        self.assertEqual(len(run(self.root).findings), 1)


class SignatureOrderingTest(_Tmp):
    def test_the_manifest_is_not_parsed_before_the_signature_verifies(self) -> None:
        """The ordering guarantee, asserted directly.

        A manifest that fails verification must never be read as fact. If it were, every
        downstream check would pass -- the attacker's sizes would match the attacker's
        files, and their hashes would match their own manifest.
        """
        build_model(self.root)
        # Make the manifest describe a file that does not exist. If anything parses it,
        # that becomes a second finding.
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["files"].append({"path": "ghost.safetensors", "size": 1, "sha256": "0" * 64})
        (self.root / "manifest.json").write_text(json.dumps(manifest))

        with mock.patch.object(cm, "verify_signature",
                               side_effect=cm.CheckError("chu ky khong hop le")):
            report = cm.check(cm.LocalSource(self.root), require_signature=True)

        self.assertFalse(report.ok)
        self.assertEqual(len(report.findings), 1, messages(report))
        self.assertIn("chu ky", messages(report))
        self.assertNotIn("ghost", messages(report))

    def test_the_verifying_signer_is_recorded(self) -> None:
        build_model(self.root)
        with mock.patch.object(cm, "verify_signature", return_value="training-pipeline"):
            report = cm.check(cm.LocalSource(self.root), require_signature=True,
                              limits=cm.CardLimits(22.5, 1.5, 0.90))
        self.assertTrue(report.ok, messages(report))
        self.assertEqual(report.facts["signer"], "training-pipeline")

    def test_a_signer_without_a_public_key_is_an_error_not_a_pass(self) -> None:
        """`allowed_signers` naming a key the repo does not hold must fail loudly. Falling
        back to "try every key" would let a signer the catalog never authorised through."""
        build_model(self.root)
        with tempfile.TemporaryDirectory() as empty:
            with mock.patch.object(cm.shutil, "which", return_value="/usr/bin/cosign"):
                with self.assertRaises(cm.CheckError) as caught:
                    cm.verify_signature(cm.LocalSource(self.root), Path(empty),
                                        ["training-pipeline"])
        self.assertIn("training-pipeline", str(caught.exception))


class IdentityTest(_Tmp):
    def test_a_signed_manifest_from_another_path_is_rejected(self) -> None:
        """The replay case, and the reason signature alone is not enough.

        A signed manifest is a statement about ONE version. Copied into a different
        version directory it stays perfectly signed, every hash matches, and the model
        loads and serves -- it is simply last month's model, promoted as this month's.
        Only comparing the claim against the path catches it.
        """
        build_model(self.root, version="2026-10-01-r1")
        report = run(self.root, expect_name="moc-7b", expect_version="2026-11-20-r1")
        self.assertFalse(report.ok)
        self.assertIn("2026-10-01-r1", messages(report))

    def test_a_name_mismatch_is_rejected(self) -> None:
        build_model(self.root, name="other-model")
        report = run(self.root, expect_name="moc-7b")
        self.assertFalse(report.ok)
        self.assertIn("other-model", messages(report))

    def test_a_version_cannot_be_a_path_fragment(self) -> None:
        build_model(self.root, version="../../etc")
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("version", messages(report))

    def test_a_missing_served_name_is_rejected(self) -> None:
        """Without it CD does not know which model name the candidate answers to, and a
        pod would come up serving under a name nothing routes to."""
        build_model(self.root, served_name="")
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("served_name", messages(report))


class PickleTest(_Tmp):
    def test_a_torch_pickle_is_refused(self) -> None:
        """Loading a pickle executes it. This is the one check whose absence turns a
        supply-chain problem into a production one."""
        build_model(self.root, extra_files={"pytorch_model.bin": b"\x80\x04garbage"})
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("pytorch_model.bin", messages(report))
        self.assertIn("pickle", messages(report))

    def test_every_pickle_extension_is_refused(self) -> None:
        for suffix in (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".h5"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "c"
                build_model(root, extra_files={f"weights{suffix}": b"x" * 16})
                self.assertFalse(run(root).ok, f"{suffix} khong bi chan")


class FileIntegrityTest(_Tmp):
    def test_a_file_present_but_unsigned_is_rejected(self) -> None:
        """The signature covers the manifest, so a file the manifest does not list is
        unsigned content in the directory the engine loads from."""
        build_model(self.root, extra_files={"adapter_config.json": b"{}"},
                    drop_from_manifest={"adapter_config.json"})
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("adapter_config.json", messages(report))

    def test_a_file_the_manifest_declares_but_is_absent_is_rejected(self) -> None:
        build_model(self.root)
        (self.root / "tokenizer.json").unlink()
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("tokenizer.json", messages(report))

    def test_a_size_that_disagrees_with_the_manifest_is_rejected(self) -> None:
        build_model(self.root)
        manifest = json.loads((self.root / "manifest.json").read_text())
        for entry in manifest["files"]:
            if entry["path"].endswith(".safetensors"):
                entry["size"] += 1
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("byte", messages(report))

    def test_a_path_escaping_the_prefix_is_rejected(self) -> None:
        """The manifest is data from outside. A path like ../../ would have the init
        container write outside the weights directory."""
        build_model(self.root, manifest_patch={"files": [
            {"path": "../../etc/passwd", "size": 1, "sha256": "0" * 64}]})
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("khong an toan", messages(report))

    def test_hash_verification_catches_altered_content_at_the_same_size(self) -> None:
        """Size alone does not detect a swap. fetch_model.sh checked only sizes, which is
        why this exists as a separate, opt-in pass."""
        build_model(self.root)
        shard = next(self.root.glob("*.safetensors"))
        data = bytearray(shard.read_bytes())
        data[-1] ^= 0xFF
        shard.write_bytes(bytes(data))
        self.assertTrue(run(self.root).ok, "kich thuoc khong doi nen cong co ban phai qua")
        report = run(self.root, verify_hashes=True)
        self.assertFalse(report.ok)
        self.assertIn("sha256", messages(report))


class CompatibilityTest(_Tmp):
    def test_an_untried_architecture_is_declined_with_the_reason(self) -> None:
        build_model(self.root, architectures=["SomeBrandNewForCausalLM"])
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("SomeBrandNewForCausalLM", messages(report))
        self.assertIn("KNOWN_ARCHITECTURES", messages(report))

    def test_a_missing_tokenizer_is_caught_before_a_gpu_is_scheduled(self) -> None:
        build_model(self.root)
        (self.root / "tokenizer.json").unlink()
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["files"] = [f for f in manifest["files"] if f["path"] != "tokenizer.json"]
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        report = run(self.root)
        self.assertFalse(report.ok)
        self.assertIn("tokenizer", messages(report))

    def test_weights_too_large_for_the_card_are_rejected(self) -> None:
        """The arithmetic the chart learned to do the hard way: `mode: shared` sat in
        values.yaml as the documented normal state and had never run, because nothing
        multiplied the fraction by the card."""
        tiny = cm.CardLimits(gpu_memory_gib=0.01, min_kv_cache_gib=1.5,
                             gpu_memory_fraction=0.90)
        build_model(self.root, weight_bytes=4096)
        report = run(self.root, limits=tiny)
        self.assertFalse(report.ok)
        self.assertIn("khong vua card", messages(report))

    def test_the_weight_size_is_computed_not_read_from_the_chart(self) -> None:
        """charts/vllm/values.yaml declares weightsGiB by hand, which works only while a
        human is there to write it. Under automatic rollout a stale figure would let the
        fit check pass against a different checkpoint entirely."""
        build_model(self.root, shards=3, weight_bytes=1024 * 1024)
        report = run(self.root)
        self.assertTrue(report.ok, messages(report))
        self.assertAlmostEqual(report.facts["weights_gib"], 3 * 1024 * 1024 / cm.GIB, places=4)


class ChartLimitsTest(unittest.TestCase):
    def test_the_limits_come_from_the_real_chart(self) -> None:
        """Restating 22.5 and 1.5 here would make a second copy of the rule the cluster
        enforces, free to drift. When they disagree, a candidate passes CI and then will
        not start."""
        limits = cm.read_card_limits()
        self.assertGreater(limits.gpu_memory_gib, 1.0)
        self.assertGreater(limits.min_kv_cache_gib, 0.0)
        self.assertTrue(0.0 < limits.gpu_memory_fraction <= 1.0)

    def test_a_chart_without_the_constants_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            values = Path(tmp) / "values.yaml"
            values.write_text("mode: solo-a\n", encoding="utf-8")
            with self.assertRaises(cm.CheckError):
                cm.read_card_limits(values)


class PrefixTest(unittest.TestCase):
    def test_name_and_version_come_from_the_prefix(self) -> None:
        self.assertEqual(
            cm.parse_prefix("s3://b/models/moc-7b/2026-11-20-r1/"),
            ("moc-7b", "2026-11-20-r1"))

    def test_a_prefix_too_short_yields_nothing_rather_than_a_guess(self) -> None:
        self.assertEqual(cm.parse_prefix("s3://b"), (None, None))


if __name__ == "__main__":
    unittest.main()
