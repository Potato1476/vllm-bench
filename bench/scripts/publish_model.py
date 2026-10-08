#!/usr/bin/env python3
"""Publish a model version so CD will accept it.

    publish_model.py --from ./build/moc-7b --name moc-7b --version 2026-11-20-r1 \
                     --served-name qwen2.5-7b --signer training-pipeline

Takes a directory of weights and produces the layout in docs/cicd-plan.md §4.1: hashed
manifest, signature, and `_READY` written last. How the directory came to exist is not
this script's business -- trained here, fine-tuned, bought, downloaded. The contract is
the only thing CD knows about.

ORDER, AND WHY IT IS THIS ONE

    1. build the manifest        hash every file that will be uploaded
    2. run the gates LOCALLY     same code as the CD gate, on the machine that built it
    3. sign the manifest         after it is final, never before
    4. upload everything but _READY
    5. verify what landed        sizes read back from S3
    6. write _READY              the only thing that makes the version visible to CD

Step 2 is the point of the whole script. The CD gate runs fifteen minutes later in a
pipeline nobody is watching, and its rejection arrives as a GitHub issue; the same failure
caught here is a message on the terminal of the person who caused it, before anything was
uploaded.

Step 6 is why `_READY` exists at all. S3 has no atomic directory and a 15 GiB upload takes
minutes, so without a marker written strictly last, CD picks up a version mid-upload and
blames the model for a race in this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.scripts.check_model import (  # noqa: E402
    GIB, CheckError, LocalSource, S3Source, check, read_card_limits,
)

# Build leftovers that are not part of the model. Uploading them is not dangerous but it
# is not harmless either: every one is synced onto the NVMe of every pod on every start,
# and `.cache/` from a Hub download is hundreds of megabytes of lock and resume metadata.
EXCLUDE_NAMES = {".DS_Store", "_READY", "manifest.json", "manifest.json.sig"}
EXCLUDE_DIRS = {".cache", ".git", "__pycache__"}


def discover(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if set(rel.parts) & EXCLUDE_DIRS or rel.name in EXCLUDE_NAMES:
            continue
        out.append(rel)
    return out


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: Path, files: list[Path], **fields) -> dict:
    entries = []
    for rel in files:
        full = root / rel
        entries.append({
            "path": rel.as_posix(),
            "size": full.stat().st_size,
            "sha256": sha256_file(full),
        })
    return {**fields, "files": entries}


def sign(manifest_path: Path, key_uri: str, out_path: Path) -> None:
    """Sign with cosign, without publishing to the public transparency log.

    `--tlog-upload=false` is deliberate. Rekor is a public, append-only log; an entry
    there announces that this organisation signed an artefact with a given digest at a
    given time. For weights trained on internal data that is a disclosure with no
    corresponding benefit, since verification here is against a public key committed to
    this repo and needs no third party to be reachable at rollout time.
    """
    if shutil.which("cosign") is None:
        raise CheckError("khong co cosign tren PATH (brew install cosign)")
    proc = subprocess.run(
        ["cosign", "sign-blob", "--key", key_uri, "--tlog-upload=false",
         "--yes", "--output-signature", str(out_path), str(manifest_path)],
        capture_output=True, text=True, check=False, timeout=300)
    if proc.returncode != 0:
        raise CheckError(f"cosign sign-blob that bai: {proc.stderr.strip()[:300]}")


def s3_prefix_exists(bucket: str, prefix: str) -> bool:
    proc = subprocess.run(
        ["aws", "s3api", "list-objects-v2", "--bucket", bucket,
         "--prefix", f"{prefix}/", "--max-items", "1"],
        capture_output=True, text=True, check=False, timeout=120)
    if proc.returncode != 0:
        raise CheckError(f"khong doc duoc S3: {proc.stderr.strip()[:200]}")
    return bool(json.loads(proc.stdout or "{}").get("Contents"))


def upload(staging: Path, bucket: str, prefix: str) -> None:
    proc = subprocess.run(
        ["aws", "s3", "sync", str(staging), f"s3://{bucket}/{prefix}/",
         "--only-show-errors", "--exclude", "_READY"],
        capture_output=True, text=True, check=False, timeout=7200)
    if proc.returncode != 0:
        raise CheckError(f"upload that bai: {proc.stderr.strip()[:300]}")


def put_ready(bucket: str, prefix: str) -> None:
    with tempfile.NamedTemporaryFile() as marker:
        proc = subprocess.run(
            ["aws", "s3", "cp", marker.name, f"s3://{bucket}/{prefix}/_READY",
             "--only-show-errors"],
            capture_output=True, text=True, check=False, timeout=300)
    if proc.returncode != 0:
        raise CheckError(f"khong ghi duoc _READY: {proc.stderr.strip()[:300]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="source", required=True, type=Path)
    ap.add_argument("--name", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--served-name", required=True)
    ap.add_argument("--signer", required=True, help="ten khoa, vd training-pipeline")
    ap.add_argument("--key-uri", help="mac dinh awskms:///alias/model-signer-<signer>")
    ap.add_argument("--bucket", default=os.getenv("ARTIFACTS_BUCKET"))
    ap.add_argument("--kind", default="full", choices=("full", "lora"))
    ap.add_argument("--source-note", default="", help="model nay tu dau ra, de nguoi doc issue biet")
    ap.add_argument("--dry-run", action="store_true", help="dung sau khi kiem, khong upload")
    a = ap.parse_args(argv)

    try:
        root: Path = a.source
        if not root.is_dir():
            raise CheckError(f"khong phai thu muc: {root}")
        prefix = f"models/{a.name}/{a.version}"
        key_uri = a.key_uri or f"awskms:///alias/model-signer-{a.signer}"

        files = discover(root)
        if not files:
            raise CheckError(f"khong co file nao trong {root}")
        total = sum((root / f).stat().st_size for f in files)
        print(f"\n  {len(files)} file, {total / GIB:.2f} GiB  <- {root}")

        # --- refuse to overwrite ------------------------------------------------------
        #
        # Before hashing 15 GiB, not after. A version that already exists is the one case
        # where doing nothing is right: rollback has to return the exact bytes that ran,
        # and republishing over a version silently changes what a rollback means -- the
        # state file still names the version, but the version is no longer what it was.
        if a.bucket and not a.dry_run:
            if s3_prefix_exists(a.bucket, prefix):
                raise CheckError(
                    f"s3://{a.bucket}/{prefix}/ da ton tai. Version khong duoc ghi de: "
                    "rollback phai tra ve dung bo byte da chay. Dung so hieu version moi.")

        # --- staging copy -------------------------------------------------------------
        with tempfile.TemporaryDirectory(prefix="publish-model-") as tmp:
            staging = Path(tmp)
            for rel in files:
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.link(root / rel, dest) if _same_device(root, staging) else shutil.copy2(root / rel, dest)

            print("  bam sha256...")
            manifest = build_manifest(
                staging, files,
                name=a.name, version=a.version, kind=a.kind,
                served_name=a.served_name, producer=a.signer,
                source=a.source_note,
            )
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")

            # --- the gates, locally, before anything is uploaded -----------------------
            print("  chay cong kiem tra tai cho...")
            report = check(
                LocalSource(staging),
                expect_name=a.name, expect_version=a.version,
                require_signature=False,   # not signed yet -- that is step 3
                published=False,           # no _READY yet either -- that is step 6
                limits=read_card_limits(),
            )
            if not report.ok:
                for finding in report.findings:
                    print(f"    {finding}")
                raise CheckError(f"{len(report.findings)} van de -- khong xuat ban")
            for key, value in report.facts.items():
                print(f"    {key}: {value}")

            if a.dry_run:
                print("\n  --dry-run: dung tai day, chua upload gi.")
                return 0
            if not a.bucket:
                raise CheckError("thieu --bucket (hoac ARTIFACTS_BUCKET)")

            print(f"  ky manifest bang {key_uri}")
            sign(manifest_path, key_uri, staging / "manifest.json.sig")

            print(f"  upload -> s3://{a.bucket}/{prefix}/")
            upload(staging, a.bucket, prefix)

            # --- read back before declaring it ready ----------------------------------
            print("  doc lai tu S3 de doi chieu...")
            landed = S3Source(f"s3://{a.bucket}/{prefix}/").sizes()
            for entry in manifest["files"]:
                path, size = entry["path"], entry["size"]
                if landed.get(path) != size:
                    raise CheckError(
                        f"{path} tren S3 la {landed.get(path)} byte, cho doi {size}. "
                        "Khong ghi _READY; version nay CD se khong thay.")

            put_ready(a.bucket, prefix)
            print(f"\n  XONG: s3://{a.bucket}/{prefix}/")
            print("  CD se thay trong vong 15 phut.")
        return 0
    except CheckError as exc:
        print(f"\n  LOI: {exc}", file=sys.stderr)
        return 1


def _same_device(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
