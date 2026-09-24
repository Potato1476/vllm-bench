#!/usr/bin/env python3
"""Generate the three reference datasets for the capacity benchmark.

Prompt and output length are part of the experiment design, not incidental detail. On a
24 GB A10G the scarce resource is KV-cache, and KV-cache is consumed per token held in
flight -- so a 4000-token RAG prompt and a 500-token chat prompt produce capacity numbers
that differ by more than an order of magnitude. Lengths are therefore fixed, measured
against the real tokenizer, and published with a checksum, rather than left to whatever
the source text happened to produce.

Output is vLLM's `--dataset-name custom` format: one JSON object per line with a `prompt`
key and an `output_tokens` key. Extra keys are ours and are ignored by the benchmark.

    python3 make_datasets.py --n 2000 --version v1
    python3 make_datasets.py --check v1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Profile:
    prompt_tokens: int
    output_tokens: int
    why: str


# The three shapes the service is expected to see. Each stresses a different part of the
# engine, which is the point of measuring all three rather than averaging them.
PROFILES: dict[str, Profile] = {
    "chat": Profile(500, 300, "short prompt, medium answer -- decode-dominated"),
    "rag": Profile(4000, 250, "long prompt, short answer -- prefill and KV-cache dominated"),
    "code": Profile(1500, 800, "medium prompt, long answer -- the longest time in flight"),
    # The three profiles above are engine-shape probes, chosen to stress different parts
    # of vLLM. None of them is what this platform actually serves, and that difference
    # turned out to matter more than expected.
    #
    # Measured on the 144 reference answers in the retrieval gold set -- the answers Minh
    # wrote for this corpus, which is the closest thing to ground truth available: 32
    # output tokens at the median, 44 at p90, 54 at the longest. The `rag` profile above
    # assumes 250, so every sizing number derived from it describes a workload between
    # five and eight times heavier than the real one.
    #
    # That is the difference between meeting the 3s objective and missing it. At 0.24s
    # TTFT, a 3s budget buys ~37 output tokens on FP16 and ~79 on AWQ. At 250 tokens the
    # SLO is unreachable on any hardware this project can afford; at 44 it is comfortable
    # on AWQ and marginal on FP16.
    #
    # Added rather than substituted: replacing `rag` would silently change what every
    # earlier measurement means, including the week-1 ramp in s3://<artifacts>/runs/.
    # Prompt length stays at 4000 because that part was never in doubt -- five retrieved
    # chunks of ~400 tokens plus the stable prefix really is what gets prefilled.
    "moc": Profile(4000, 48, "the real MOC shape -- long RAG prompt, answer measured "
                             "from the 144 gold answers (p50 32, p90 44)"),
}

# Sentence pool. Order is shuffled per sample so that no two prompts share a long prefix;
# see build_prompt for why that matters more than the wording does.
SENTENCES = [
    "Kubernetes schedules containers onto nodes according to requests and taints.",
    "Prometheus stores samples as a time series keyed by metric name and labels.",
    "vLLM uses PagedAttention to manage the key-value cache in fixed-size blocks.",
    "Continuous batching lets new requests join a running decode step.",
    "A GPU's memory bandwidth, not its arithmetic throughput, often limits decoding.",
    "Time to first token is dominated by the prefill pass over the prompt.",
    "Time between tokens reflects how many sequences share each decode step.",
    "Preemption happens when the scheduler must evict a sequence to free cache blocks.",
    "Tail latency matters more than the mean when a service has a latency objective.",
    "An open-loop load generator keeps offering work regardless of how slow replies are.",
    "A closed-loop generator throttles itself, which hides saturation behind queueing.",
    "Throughput measured without a latency bound is not a capacity number.",
    "Quantisation trades a small accuracy loss for a large memory saving.",
    "Speculative decoding drafts several tokens and verifies them in one pass.",
    "The service level objective defines which requests count as successful.",
]

QUESTIONS = [
    "Summarise the main points of the passage above.",
    "Which constraint does the passage identify as the binding one, and why?",
    "Explain the trade-off described above to a colleague in two sentences.",
    "List the mechanisms mentioned above and what each one is for.",
]


def n_tokens(tok, text: str) -> int:
    return len(tok.encode(text, add_special_tokens=False))


def build_prompt(tok, target_tokens: int, seed: int) -> str:
    """Build a prompt that encodes to close to `target_tokens`.

    The first line carries a per-sample random id, and the sentence order is shuffled.
    Both exist for one reason: vLLM caches KV blocks by prompt prefix. If every prompt
    began with the same boilerplate, most requests would be served from that cache, TTFT
    would collapse, and the benchmark would be measuring the cache rather than the
    engine. Diverging inside the first few tokens prevents that.
    """
    rnd = random.Random(seed)
    header = f"Document {rnd.getrandbits(48):012x}, revision {rnd.randint(1, 999)}.\n"

    body_parts: list[str] = []
    while True:
        pool = SENTENCES[:]
        rnd.shuffle(pool)
        body_parts.extend(pool)
        if n_tokens(tok, header + " ".join(body_parts)) >= target_tokens + 40:
            break

    question = rnd.choice(QUESTIONS)
    tail = f"\n\nQuestion: {question}"
    budget = target_tokens - n_tokens(tok, header) - n_tokens(tok, tail)

    # Encode-slice-decode lands near the target; decode/encode is not always idempotent,
    # so the result is corrected by a few tokens rather than trusted.
    body = " ".join(body_parts)
    ids = tok.encode(body, add_special_tokens=False)[:budget]
    body = tok.decode(ids)

    prompt = header + body + tail
    for _ in range(8):
        diff = target_tokens - n_tokens(tok, prompt)
        if diff == 0:
            break
        if diff < 0:
            ids = tok.encode(body, add_special_tokens=False)[:diff]
            body = tok.decode(ids)
        else:
            body = body + " " + " ".join(rnd.choice(SENTENCES) for _ in range(1 + diff // 12))
            ids = tok.encode(body, add_special_tokens=False)[: budget + diff]
            body = tok.decode(ids)
        prompt = header + body + tail
    return prompt


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def generate(tok, name: str, prof: Profile, n: int, version: str, out_dir: Path) -> dict:
    path = out_dir / f"{name}-{version}.jsonl"
    lengths: list[int] = []
    with path.open("w", encoding="utf-8") as fh:
        for i in range(n):
            # Seed from the profile name as well as the index, so `chat` sample 7 and
            # `rag` sample 7 are different documents rather than the same text at two
            # lengths. Regenerating with the same version reproduces byte for byte.
            seed = int(hashlib.sha256(f"{name}:{version}:{i}".encode()).hexdigest()[:12], 16)
            prompt = build_prompt(tok, prof.prompt_tokens, seed)
            lengths.append(n_tokens(tok, prompt))
            fh.write(json.dumps({
                # keys vLLM's custom dataset reads
                "prompt": prompt,
                "output_tokens": prof.output_tokens,
                # our bookkeeping; ignored by the benchmark
                "id": f"{name}-{i:05d}",
                "profile": name,
                "prompt_tokens": lengths[-1],
            }, ensure_ascii=False) + "\n")

    mean = statistics.mean(lengths)
    drift = abs(mean - prof.prompt_tokens) / prof.prompt_tokens
    return {
        "file": path.name,
        "profile": name,
        "n": n,
        "target_prompt_tokens": prof.prompt_tokens,
        "actual_prompt_tokens_mean": round(mean, 1),
        "actual_prompt_tokens_min": min(lengths),
        "actual_prompt_tokens_max": max(lengths),
        "drift_from_target": round(drift, 4),
        "output_tokens": prof.output_tokens,
        "sha256": sha256_of(path),
        "why": prof.why,
    }


def cmd_generate(args) -> int:
    from transformers import AutoTokenizer

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(args.model)

    entries = []
    for name, prof in PROFILES.items():
        print(f"generating {name} ({prof.prompt_tokens} in / {prof.output_tokens} out) ...",
              file=sys.stderr)
        entries.append(generate(tok, name, prof, args.n, args.version, out_dir))

    manifest = {
        "version": args.version,
        "tokenizer": args.model,
        "n_per_profile": args.n,
        "datasets": entries,
        # Without this, a result file six weeks old cannot be traced to the input that
        # produced it, and cross-run comparisons stop being defensible.
        "note": "regenerating with the same --version and --model reproduces these checksums",
    }
    mpath = out_dir / f"manifest-{args.version}.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    print()
    print(f"{'file':22} {'n':>6} {'target':>7} {'actual':>8} {'drift':>7}  sha256")
    bad = 0
    for e in entries:
        flag = "" if e["drift_from_target"] <= 0.02 else "  <-- OUT OF TOLERANCE"
        if flag:
            bad += 1
        print(f"{e['file']:22} {e['n']:>6} {e['target_prompt_tokens']:>7} "
              f"{e['actual_prompt_tokens_mean']:>8} {e['drift_from_target']:>6.2%}  "
              f"{e['sha256'][:16]}{flag}")
    print(f"\nmanifest: {mpath}")
    if bad:
        print(f"\n{bad} dataset(s) drifted more than 2% from the target length.",
              file=sys.stderr)
        return 1
    return 0


def cmd_check(args) -> int:
    """Re-verify checksums. Run this in CI and before any measurement session."""
    out_dir = Path(args.out_dir)
    mpath = out_dir / f"manifest-{args.check}.json"
    if not mpath.exists():
        print(f"no manifest at {mpath}", file=sys.stderr)
        return 1
    manifest = json.loads(mpath.read_text())
    bad = 0
    for e in manifest["datasets"]:
        path = out_dir / e["file"]
        if not path.exists():
            print(f"  MISSING  {e['file']}")
            bad += 1
            continue
        actual = sha256_of(path)
        if actual == e["sha256"]:
            print(f"  ok       {e['file']}")
        else:
            print(f"  CHANGED  {e['file']}\n           manifest {e['sha256'][:16]} "
                  f"!= file {actual[:16]}")
            bad += 1
    if bad:
        print(f"\n{bad} file(s) do not match the manifest. Any run using them is not "
              f"comparable to earlier runs.", file=sys.stderr)
        return 1
    print(f"\nall {len(manifest['datasets'])} datasets match manifest {manifest['version']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct",
                    help="tokenizer to measure prompt length with; must match the served model")
    ap.add_argument("--n", type=int, default=2000, help="samples per profile")
    ap.add_argument("--version", default="v1", help="dataset version, part of the filename")
    ap.add_argument("--out-dir", default=str(HERE))
    ap.add_argument("--check", metavar="VERSION",
                    help="verify checksums of an existing version instead of generating")
    args = ap.parse_args()
    return cmd_check(args) if args.check else cmd_generate(args)


if __name__ == "__main__":
    raise SystemExit(main())
