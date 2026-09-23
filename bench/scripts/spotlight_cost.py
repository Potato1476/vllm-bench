#!/usr/bin/env python3
"""What datamarking costs in tokens, measured with the engine's own tokenizer.

    PYTHONPATH=. .venv-embed/bin/python bench/scripts/spotlight_cost.py

Hines et al. recommend datamarking as the default spotlighting mode and note that the
transformation "does not show a detrimental impact on downstream NLP tasks". They do not
quantify its token cost, and for a project whose bottleneck is memory bandwidth on a
24 GB card, that cost is the deciding number: every extra prompt token is prefill time on
the TTFT budget and KV cache that the batch cannot use.

The character count barely moves -- one whitespace run becomes one marker character -- so
anyone measuring characters concludes datamarking is free. It is not. The marker is a
Private Use Area codepoint, outside any BPE vocabulary trained on natural text, so it
falls back to byte-level pieces where a plain space cost one token.

This compares three markers so the choice is made on evidence:

    U+E000    the paper's recommendation, guaranteed absent from real text
    ^         the paper's illustrative example, in-vocabulary but collides with real text
    |         a common in-vocabulary alternative

Collision risk and token cost pull in opposite directions, and this is the number that
says by how much.
"""

from __future__ import annotations

import sys

from guardrails.spotlight import datamark
from rag.corpus import load_chunks

MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"

CANDIDATES = (
    ("U+E000 (khuyen nghi cua bai bao)", ""),
    ("^ (vi du trong bai bao)", "^"),
    ("| (thay the trong tu dien)", "|"),
)


def main() -> int:
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(MODEL_ID)
    chunks = load_chunks()
    texts = [c.text for c in chunks]

    plain_tokens = sum(len(tok.encode(t)) for t in texts)
    plain_chars = sum(len(t) for t in texts)
    print(f"  {len(texts)} chunk, {plain_chars} ky tu, {plain_tokens} token")
    print(f"  {plain_tokens / plain_chars * 100:.1f} token / 100 ky tu (tieng Viet)\n")

    print(f"  {'marker':34} {'token':>9} {'ty le':>7} {'va cham':>9}")
    for name, marker in CANDIDATES:
        marked = [datamark(t, marker) for t in texts]
        n = sum(len(tok.encode(t)) for t in marked)
        # How often the marker already appears in the corpus. A collision means a real
        # character is indistinguishable from the provenance signal.
        collisions = sum(t.count(marker) for t in texts)
        print(f"  {name:34} {n:9} {n / plain_tokens:6.2f}x {collisions:9}")

    print("\n  === Anh huong len mot request ===")
    per_chunk = plain_tokens / len(texts)
    marked_e000 = sum(len(tok.encode(datamark(t, ""))) for t in texts) / len(texts)
    for k in (3, 5, 8):
        print(f"  {k} chunk: {per_chunk * k:6.0f} -> {marked_e000 * k:6.0f} token "
              f"(+{(marked_e000 - per_chunk) * k:.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
