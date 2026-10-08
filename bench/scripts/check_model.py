#!/usr/bin/env python3
"""The gates a model version passes before any GPU time is spent on it.

    check_model.py --source s3://bucket/models/moc-7b/2026-11-20-r1/   # the CD gate
    check_model.py --source ./build/moc-7b --expect-name moc-7b \
                   --expect-version 2026-11-20-r1 --verify-hashes      # before publishing

Two gates, both cheap, both before a pod is ever scheduled:

    VALID        the bytes are what a signed manifest says they are, written by someone
                 we accept weights from.
    COMPATIBLE   this engine build can actually serve them, and they fit on the card.

WHY THIS RUNS WITHOUT A HUMAN IN THE LOOP

Rollout is automatic: a version that passes every gate reaches production with nobody
approving it. So these gates are not advisory. Anything that would be caught by a person
glancing at the model before promoting it has to be caught here instead, and anything
this file waves through gets served.

ORDER MATTERS, AND IT IS NOT THE OBVIOUS ONE

The manifest is UNTRUSTED INPUT until its signature verifies. It names files, sizes and
hashes; acting on any of that before checking the signature means trusting whatever an
attacker with write access to the bucket chose to write. So the signature is checked
first, against a public key committed to this repo, and only then is the manifest read as
fact. Every later check is an assertion about signed data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
SIGNERS_DIR = REPO_ROOT / "deploy" / "signers"
CHART_VALUES = REPO_ROOT / "charts" / "vllm" / "values.yaml"

# Cosign v3 emits a bundle: the signature plus its verification metadata, in one JSON
# file. Named for what it is rather than kept as manifest.json.sig, which would describe
# a bare-signature format this no longer is.
SIGNATURE_FILE = "manifest.bundle"

GATE_VALID = "valid"
GATE_COMPATIBLE = "compatible"

# Weight formats that are a deserialisation vulnerability rather than a file format.
# .bin, .pt, .pth and .ckpt are torch pickles, and unpickling runs whatever __reduce__ the
# file asks for -- loading a model is executing it. safetensors is a length-prefixed
# tensor container with no code path for that, which is the entire reason it exists.
#
# This is a hard rule and not a warning. Weights reaching this bucket may be built by a
# pipeline nobody on this project reviews, from base models nobody here chose; "we trust
# the team that wrote it" is exactly the assumption that makes a supply-chain problem a
# production one.
PICKLE_SUFFIXES = {".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle", ".h5", ".msgpack"}

WEIGHT_SUFFIXES = {".safetensors"}

# Present or vLLM cannot start. Tokenizer files are checked as a group below, because a
# checkpoint may legitimately ship tokenizer.json alone or the sentencepiece pair.
REQUIRED_FILES = {"config.json"}
TOKENIZER_CANDIDATES = {"tokenizer.json", "tokenizer.model", "vocab.json"}

# Architectures this platform has actually served, not everything vLLM claims to support.
# A name missing here is not a verdict on the model -- it means nobody has run it through
# the evaluation gate on this engine build, so the automatic path declines and a person
# decides. Widen it deliberately, in a PR, after running one.
KNOWN_ARCHITECTURES = {
    "Qwen2ForCausalLM",
    "Qwen3ForCausalLM",
    "LlamaForCausalLM",
    "MistralForCausalLM",
}

GIB = 1024 ** 3

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CheckError(RuntimeError):
    """Something made the check impossible to perform -- not a failed check."""


# --------------------------------------------------------------------------- findings


@dataclass(frozen=True)
class Finding:
    gate: str
    message: str

    def __str__(self) -> str:
        return f"[{self.gate}] {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    facts: dict[str, object] = field(default_factory=dict)

    def fail(self, gate: str, message: str) -> None:
        self.findings.append(Finding(gate, message))

    @property
    def ok(self) -> bool:
        return not self.findings


# ----------------------------------------------------------------------------- sources


class Source:
    """Where a candidate's files live. Local directory or S3 prefix.

    Both exist because the same checks run in two places and must not drift: the CD gate
    reads S3, and `publish_model.py` runs them against a local directory BEFORE uploading,
    so a malformed version is rejected on the machine that built it rather than fifteen
    minutes later in a pipeline whose output nobody is watching.
    """

    def describe(self) -> str:
        raise NotImplementedError

    def sizes(self) -> dict[str, int]:
        """Relative path -> size in bytes, for every file under the prefix."""
        raise NotImplementedError

    def read_bytes(self, name: str) -> bytes:
        raise NotImplementedError

    def sha256(self, name: str) -> str:
        raise NotImplementedError


class LocalSource(Source):
    def __init__(self, root: Path) -> None:
        self.root = root
        if not root.is_dir():
            raise CheckError(f"khong phai thu muc: {root}")

    def describe(self) -> str:
        return str(self.root)

    def sizes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for path in self.root.rglob("*"):
            if path.is_file():
                out[path.relative_to(self.root).as_posix()] = path.stat().st_size
        return out

    def read_bytes(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def sha256(self, name: str) -> str:
        digest = hashlib.sha256()
        with (self.root / name).open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class S3Source(Source):
    def __init__(self, uri: str) -> None:
        if not uri.startswith("s3://"):
            raise CheckError(f"khong phai s3 uri: {uri}")
        rest = uri[len("s3://"):]
        self.bucket, _, prefix = rest.partition("/")
        self.prefix = prefix.strip("/")
        if not self.bucket or not self.prefix:
            raise CheckError(f"s3 uri thieu bucket hoac prefix: {uri}")

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}/"

    def sizes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        token: str | None = None
        while True:
            cmd = ["aws", "s3api", "list-objects-v2", "--bucket", self.bucket,
                   "--prefix", f"{self.prefix}/", "--max-items", "1000"]
            if token:
                cmd += ["--starting-token", token]
            page = json.loads(_run(cmd) or "{}")
            for row in page.get("Contents", []):
                key = row["Key"]
                if key.endswith("/"):
                    continue
                out[key[len(self.prefix) + 1:]] = int(row["Size"])
            token = page.get("NextToken")
            if not token:
                return out

    def read_bytes(self, name: str) -> bytes:
        key = f"{self.prefix}/{name}"
        proc = subprocess.run(
            ["aws", "s3", "cp", f"s3://{self.bucket}/{key}", "-"],
            capture_output=True, check=False, timeout=300)
        if proc.returncode != 0:
            raise CheckError(f"khong doc duoc {key}: {proc.stderr.decode()[:200]}")
        return proc.stdout

    def sha256(self, name: str) -> str:
        # Deliberately streamed rather than downloaded to a file. Hashing on S3 is only
        # used by --verify-hashes, which the CD gate does NOT enable: reading 15 GiB back
        # out of S3 on every candidate costs minutes and proves only that the bytes were
        # right at that moment. The check that counts happens in the init container, on
        # the bytes the GPU is about to load, every time a pod starts. See the plan, 4.3.
        key = f"{self.prefix}/{name}"
        digest = hashlib.sha256()
        with subprocess.Popen(
            ["aws", "s3", "cp", f"s3://{self.bucket}/{key}", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as proc:
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
            if proc.wait() != 0:
                raise CheckError(f"khong doc duoc {key}")
        return digest.hexdigest()


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
    if proc.returncode != 0:
        raise CheckError(f"{cmd[0]} that bai: {proc.stderr.strip()[:300]}")
    return proc.stdout


# --------------------------------------------------------------------------- signature


def verify_signature(source: Source, signers_dir: Path, allowed: Iterable[str] | None) -> str:
    """Return the name of the signer whose key verifies the manifest.

    Raises if no accepted key verifies it. This runs before the manifest is parsed, so
    everything downstream is reasoning about signed bytes.
    """
    if shutil.which("cosign") is None:
        raise CheckError("khong co cosign tren PATH -- khong the xac minh chu ky")
    if not signers_dir.is_dir():
        raise CheckError(f"khong co thu muc khoa cong khai: {signers_dir}")

    manifest = source.read_bytes("manifest.json")
    signature = source.read_bytes(SIGNATURE_FILE)

    candidates = sorted(p for p in signers_dir.glob("*.pub"))
    if allowed is not None:
        wanted = set(allowed)
        candidates = [p for p in candidates if p.stem in wanted]
        missing = wanted - {p.stem for p in signers_dir.glob("*.pub")}
        if missing:
            raise CheckError(
                f"catalog cho phep {sorted(missing)} ky, nhung khong co khoa cong khai "
                f"tuong ung trong {signers_dir}")
    if not candidates:
        raise CheckError("khong co khoa cong khai nao duoc phep ky")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        blob = Path(tmp) / "manifest.json"
        sig = Path(tmp) / SIGNATURE_FILE
        blob.write_bytes(manifest)
        sig.write_bytes(signature)
        for key in candidates:
            # --bundle, not --signature: cosign v3 writes signature and verification
            # metadata together and v2's bare-signature flags are gone. tlog verification
            # is skipped because signing with a key never wrote to it -- see publish's
            # sign(). Cosign prints a warning about that on every call; the return code is
            # what decides, not the noise on stderr.
            proc = subprocess.run(
                ["cosign", "verify-blob", "--key", str(key),
                 "--bundle", str(sig), "--insecure-ignore-tlog=true", str(blob)],
                capture_output=True, check=False, timeout=120)
            if proc.returncode == 0:
                return key.stem
    raise CheckError(
        "khong khoa cong khai nao duoc phep xac minh duoc manifest.bundle "
        f"(da thu: {', '.join(p.stem for p in candidates)})")


# ------------------------------------------------------------------------ chart limits


@dataclass(frozen=True)
class CardLimits:
    gpu_memory_gib: float
    min_kv_cache_gib: float
    gpu_memory_fraction: float

    @property
    def usable_gib(self) -> float:
        return self.gpu_memory_gib * self.gpu_memory_fraction


def read_card_limits(values_path: Path = CHART_VALUES, *, model_key: str = "a") -> CardLimits:
    """Read the fit constants from the chart rather than restating them here.

    The chart already refuses to render a configuration whose weights cannot fit, and that
    check carries the scar of having been missing: `mode: shared` sat in values.yaml as the
    documented normal state and had never once run, because 14.2 GiB of FP16 weights do not
    fit in a 0.65 share of a 22.5 GiB card and nothing did the multiplication.

    Restating 22.5 and 1.5 in this file would create a second copy of that rule, free to
    drift from the one the cluster actually enforces -- and the failure when they disagree
    is a candidate that passes CI and then will not start. One source, parsed.
    """
    text = values_path.read_text(encoding="utf-8")

    def scalar(key: str) -> float:
        match = re.search(rf"^{key}:\s*([0-9.]+)\s*$", text, re.M)
        if not match:
            raise CheckError(f"khong doc duoc {key} tu {values_path}")
        return float(match.group(1))

    solo = re.search(r"^gpuMemory:\s*$.*?^\s{2}solo:\s*$(.*?)^\s{2}\S", text, re.M | re.S)
    fraction = None
    if solo:
        found = re.search(rf"^\s+{model_key}:\s*([0-9.]+)\s*$", solo.group(1), re.M)
        if found:
            fraction = float(found.group(1))
    if fraction is None:
        # Solo mode gives a model the whole card; 0.90 is vLLM's own default headroom for
        # the CUDA context. Only used when the chart stops declaring it.
        fraction = 0.90
    return CardLimits(scalar("gpuMemoryGiB"), scalar("minKvCacheGiB"), fraction)


# ------------------------------------------------------------------------------ checks


def check(
    source: Source,
    *,
    expect_name: str | None = None,
    expect_version: str | None = None,
    allowed_signers: Iterable[str] | None = None,
    signers_dir: Path = SIGNERS_DIR,
    limits: CardLimits | None = None,
    verify_hashes: bool = False,
    require_signature: bool = True,
    published: bool = True,
) -> Report:
    """`published=False` checks a directory being BUILT rather than one already published.

    A staging directory legitimately has no _READY and no signature yet -- both are
    written last, on purpose. Without this distinction the publisher's own pre-flight
    returns at the first precondition and checks nothing, while still reporting success,
    because the caller filters out exactly the two findings it expected to see. That is
    not a theoretical failure: it is what this function did until it was caught by the
    absence of any `facts` in the output.
    """
    report = Report()
    listing = source.sizes()

    # --- the version is not finished being written -------------------------------------
    #
    # A 15 GiB upload takes minutes, and S3 has no atomic directory. Without this, a
    # candidate gets picked up mid-upload, fails on a shard that is half there, and the
    # issue blames the model for a race in the publisher.
    if published:
        if "_READY" not in listing:
            report.fail(GATE_VALID, "khong co _READY -- version dang duoc ghi dang do")
            return report
        for name in ("manifest.json", SIGNATURE_FILE):
            if name not in listing:
                report.fail(GATE_VALID, f"thieu {name}")
        if not report.ok:
            return report
    elif "manifest.json" not in listing:
        report.fail(GATE_VALID, "thieu manifest.json")
        return report

    # --- nothing below this line reasons about unsigned bytes --------------------------
    signer = None
    if require_signature:
        try:
            signer = verify_signature(source, signers_dir, allowed_signers)
        except CheckError as exc:
            report.fail(GATE_VALID, str(exc))
            return report
        report.facts["signer"] = signer

    try:
        manifest = json.loads(source.read_bytes("manifest.json"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.fail(GATE_VALID, f"manifest.json khong phai JSON hop le: {exc}")
        return report
    if not isinstance(manifest, dict):
        report.fail(GATE_VALID, "manifest.json phai la mot doi tuong JSON")
        return report

    _check_identity(report, manifest, expect_name, expect_version)
    files = _check_files(report, manifest, listing, source, verify_hashes)
    _check_compatibility(report, source, listing, files, limits or read_card_limits())
    report.facts.setdefault("name", manifest.get("name"))
    report.facts.setdefault("version", manifest.get("version"))
    report.facts.setdefault("served_name", manifest.get("served_name"))
    return report


def _check_identity(
    report: Report, manifest: dict, expect_name: str | None, expect_version: str | None
) -> None:
    """The manifest must claim to be exactly the thing we went looking for.

    Signature alone does not establish this. A signed manifest is a statement about ONE
    version; copied under a different path it is still perfectly signed. Without this,
    anyone able to write to the bucket -- not to sign, only to write -- can copy last
    month's signed weights into today's version directory, and CD treats a rollback as an
    upgrade. Everything downstream is fine: the hashes match, the signature verifies, the
    model loads and serves. It is simply the wrong model, forever, silently.
    """
    name, version = manifest.get("name"), manifest.get("version")
    for field_name, value in (("name", name), ("version", version)):
        if not isinstance(value, str) or not value:
            report.fail(GATE_VALID, f"manifest thieu truong {field_name}")
        elif not _VERSION_RE.match(value):
            report.fail(GATE_VALID, f"manifest.{field_name} khong hop le: {value!r}")
    if expect_name is not None and name != expect_name:
        report.fail(GATE_VALID,
                    f"manifest.name = {name!r} nhung duong dan noi {expect_name!r}")
    if expect_version is not None and version != expect_version:
        report.fail(GATE_VALID,
                    f"manifest.version = {version!r} nhung duong dan noi {expect_version!r}")

    kind = manifest.get("kind", "full")
    if kind not in ("full", "lora"):
        report.fail(GATE_VALID, f"manifest.kind khong ho tro: {kind!r}")
    if not manifest.get("served_name"):
        report.fail(GATE_VALID, "manifest thieu served_name -- khong biet phuc vu duoi ten nao")


def _check_files(
    report: Report, manifest: dict, listing: dict[str, int], source: Source,
    verify_hashes: bool,
) -> list[dict]:
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        report.fail(GATE_VALID, "manifest.files rong hoac khong phai danh sach")
        return []

    declared: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            report.fail(GATE_VALID, f"muc files khong hop le: {entry!r}")
            continue
        path = entry["path"]
        if path.startswith("/") or ".." in Path(path).parts:
            # A manifest is data from outside; a path escaping the prefix would have the
            # init container write wherever it points.
            report.fail(GATE_VALID, f"duong dan file khong an toan: {path!r}")
            continue
        declared[path] = entry

    for path, entry in declared.items():
        if path not in listing:
            report.fail(GATE_VALID, f"manifest khai {path} nhung khong co tren nguon")
            continue
        size = entry.get("size")
        if isinstance(size, int) and listing[path] != size:
            report.fail(GATE_VALID,
                        f"{path}: manifest noi {size} byte, thuc te {listing[path]}")
        if not isinstance(entry.get("sha256"), str) or len(entry["sha256"]) != 64:
            report.fail(GATE_VALID, f"{path}: thieu sha256 hop le trong manifest")

    # Files present but NOT in the manifest. The signature covers the manifest, so an
    # unlisted file is unsigned content sitting in the same directory the engine loads
    # from -- which is how an extra config, adapter or tokenizer override gets served
    # without ever having been signed for.
    metadata = {"manifest.json", SIGNATURE_FILE, "_READY"}
    for path in sorted(set(listing) - set(declared) - metadata):
        report.fail(GATE_VALID, f"{path} co tren nguon nhung khong co trong manifest")

    for path in sorted(declared):
        suffix = Path(path).suffix.lower()
        if suffix in PICKLE_SUFFIXES:
            report.fail(
                GATE_VALID,
                f"{path}: dinh dang pickle khong duoc nhan -- nap file nay la chay code "
                "trong no. Chi nhan .safetensors")

    if verify_hashes:
        for path in sorted(declared):
            if path not in listing:
                continue
            expected = declared[path].get("sha256")
            if not isinstance(expected, str):
                continue
            actual = source.sha256(path)
            if actual != expected:
                report.fail(GATE_VALID, f"{path}: sha256 lech (manifest {expected[:12]}…, "
                                        f"thuc te {actual[:12]}…)")

    return list(declared.values())


def _check_compatibility(
    report: Report, source: Source, listing: dict[str, int], files: list[dict],
    limits: CardLimits,
) -> None:
    for required in sorted(REQUIRED_FILES):
        if required not in listing:
            report.fail(GATE_COMPATIBLE, f"thieu {required}")
    if not (TOKENIZER_CANDIDATES & set(listing)):
        report.fail(GATE_COMPATIBLE,
                    "khong co tokenizer (can mot trong: "
                    + ", ".join(sorted(TOKENIZER_CANDIDATES)) + ")")

    weight_files = [f for f in files
                    if Path(str(f.get("path", ""))).suffix.lower() in WEIGHT_SUFFIXES]
    if not weight_files:
        report.fail(GATE_COMPATIBLE, "khong co file .safetensors nao")

    if "config.json" in listing:
        try:
            config = json.loads(source.read_bytes("config.json"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            report.fail(GATE_COMPATIBLE, f"config.json khong doc duoc: {exc}")
            config = None
        if isinstance(config, dict):
            architectures = config.get("architectures") or []
            unknown = [a for a in architectures if a not in KNOWN_ARCHITECTURES]
            if not architectures:
                report.fail(GATE_COMPATIBLE, "config.json khong khai architectures")
            elif unknown:
                report.fail(
                    GATE_COMPATIBLE,
                    f"kien truc chua tung chay tren nen tang nay: {', '.join(unknown)}. "
                    "Khong phai danh gia ve model -- chua ai cho no qua cong danh gia tren "
                    "ban vLLM dang ghim. Mo rong KNOWN_ARCHITECTURES bang mot PR sau khi da chay thu.")
            report.facts["architectures"] = architectures

    # --- does it fit on the card -------------------------------------------------------
    #
    # Weight size is COMPUTED from the files, not declared. charts/vllm/values.yaml has a
    # weightsGiB per model today, written by hand -- which works exactly as long as a human
    # is there to write it. Under automatic rollout nobody is, and a stale hand-written
    # number is worse than none: the fit check would pass against a figure describing a
    # different checkpoint, and the failure arrives as an OOM or a near-zero KV cache
    # twenty minutes into the evaluation window.
    weights_bytes = sum(int(f.get("size") or 0) for f in weight_files)
    weights_gib = weights_bytes / GIB
    kv_gib = limits.usable_gib - weights_gib
    # Four places, not two. These go out as JSON and get read back by the rollout
    # workflow; rounding for display inside a value other code consumes is how a figure
    # becomes 0.0 and nobody notices which step dropped it.
    report.facts["weights_gib"] = round(weights_gib, 4)
    report.facts["kv_cache_gib"] = round(kv_gib, 4)
    if weight_files and kv_gib < limits.min_kv_cache_gib:
        report.fail(
            GATE_COMPATIBLE,
            f"khong vua card: trong so {weights_gib:.1f} GiB, phan duoc dung "
            f"{limits.usable_gib:.1f} GiB ({limits.gpu_memory_fraction:g} cua "
            f"{limits.gpu_memory_gib:.1f} GiB), con {kv_gib:.1f} GiB cho KV cache "
            f"(toi thieu {limits.min_kv_cache_gib:.1f}). Dung ban luong tu hoa, hoac giam "
            "max_model_len.")


# -------------------------------------------------------------------------------- main


def parse_prefix(uri: str) -> tuple[str | None, str | None]:
    """s3://bucket/models/<name>/<version>/ -> (name, version), else (None, None).

    The scheme and the bucket are stripped explicitly. Splitting the raw URI on "/" reads
    `s3://b` as the pair ("s3:", "b"), and a derived name of "s3:" would then be compared
    against the manifest -- turning a malformed argument into an identity mismatch that
    blames the model.
    """
    rest = uri[len("s3://"):] if uri.startswith("s3://") else uri
    parts = [p for p in rest.strip("/").split("/") if p]
    if uri.startswith("s3://"):
        parts = parts[1:]          # drop the bucket
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return None, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="s3://bucket/models/<ten>/<version>/ hoac thu muc")
    ap.add_argument("--expect-name", help="mac dinh: suy ra tu duong dan")
    ap.add_argument("--expect-version", help="mac dinh: suy ra tu duong dan")
    ap.add_argument("--allowed-signers", help="danh sach cach nhau bang dau phay")
    ap.add_argument("--signers-dir", type=Path, default=SIGNERS_DIR)
    ap.add_argument("--verify-hashes", action="store_true",
                    help="doc lai moi file va bam. Cham; cong CD khong bat.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    try:
        source: Source = (S3Source(a.source) if a.source.startswith("s3://")
                          else LocalSource(Path(a.source)))
        name, version = (a.expect_name, a.expect_version)
        if name is None and version is None and a.source.startswith("s3://"):
            name, version = parse_prefix(a.source)
        allowed = ([s.strip() for s in a.allowed_signers.split(",") if s.strip()]
                   if a.allowed_signers else None)
        report = check(source, expect_name=name, expect_version=version,
                       allowed_signers=allowed, signers_dir=a.signers_dir,
                       verify_hashes=a.verify_hashes)
    except CheckError as exc:
        if a.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"  KHONG KIEM DUOC: {exc}", file=sys.stderr)
        return 2

    if a.json:
        print(json.dumps({
            "ok": report.ok,
            "source": source.describe(),
            "facts": report.facts,
            "findings": [{"gate": f.gate, "message": f.message} for f in report.findings],
        }, ensure_ascii=False, indent=1))
    else:
        print(f"\n  {source.describe()}")
        for key, value in report.facts.items():
            print(f"    {key}: {value}")
        print()
        if report.ok:
            print("  DAT: qua ca hai cong.")
        else:
            for finding in report.findings:
                print(f"    {finding}")
            print(f"\n  TRUOT: {len(report.findings)} van de.")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
