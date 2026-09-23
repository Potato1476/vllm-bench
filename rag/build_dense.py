#!/usr/bin/env python3
"""Encode the corpus once and write the vectors to disk.

Runs under a python that has torch, which is deliberately NOT the python that serves
requests:

    .venv-embed/bin/python -m rag.build_dense

The output is two files -- a float32 .npy and an ids/model json -- that the request path
loads with numpy alone. Splitting it this way means the gateway never imports torch, and
the encode can move to a GPU box or a CI job without touching the serving code.

The model id is recorded in the json and checked on load. Vectors from a different model
are not comparable, and the failure mode is silent: search still returns k results, all
of them meaningless.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .corpus import load_chunks
from .dense import MODEL_ID, SentenceTransformerBackend, build_index

DEFAULT_OUT = Path("data/index/dense-vi")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--model", default=MODEL_ID)
    ap.add_argument("--device", default=None, help="mps | cuda | cpu (auto by default)")
    ap.add_argument("--batch-size", type=int, default=16)
    a = ap.parse_args()

    chunks = load_chunks()
    print(f"  {len(chunks)} chunk, model {a.model}")

    t0 = time.perf_counter()
    backend = SentenceTransformerBackend(a.model, device=a.device,
                                         batch_size=a.batch_size)
    print(f"  nap model: {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    index = build_index([c.chunk_id for c in chunks], [c.text for c in chunks], backend)
    dt = time.perf_counter() - t0
    print(f"  ma hoa: {dt:.1f}s  ({len(chunks)/dt:.1f} chunk/s)")

    index.save(a.out)
    npy = Path(a.out).with_suffix(".npy")
    print(f"  ghi {npy}  {npy.stat().st_size/1e6:.1f} MB  shape={index.vectors.shape}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
