#!/usr/bin/env python3
"""Cost against an external API, as a curve over offered load -- never as one number.

WHY A SINGLE "SAVES 30%" FIGURE IS NOT WORTH REPORTING

TC2 asks for cost per 1k tokens at least 30% below an external API. Answered as a scalar
that claim is unfalsifiable: self-hosting bills RENTED TIME and an API bills TOKENS, so
the ratio between them is a function of utilisation and nothing else. The same cluster,
unchanged, is four times more expensive than the API at 1 req/s and six times cheaper at
20. Quoting either end is true and useless.

This is the same reasoning bench/scripts/availability.py already applies to uptime --
"availability is a curve against offered load, not a scalar" -- applied to money.

WHAT THE CURVE SHOWS THAT A NUMBER HIDES

Self-hosted cost is a STEP function: it jumps when load crosses what the current GPUs can
serve and another must be added, then stays flat while that GPU fills. API cost is a
straight line through the origin. So savings climb as a GPU fills, drop at each new GPU,
and climb again -- and the break-even is a point on that line, not a property of the
platform.

Reporting the curve also makes the operating decision visible: for any expected load
there is a cheapest correct answer, and below the break-even the cheapest correct answer
is to call somebody else's API.

WHICH INPUTS ARE MEASURED AND WHICH ARE NOT

    measured    10 req/s per L4 for qwen2.5-7b AWQ at p95 1886ms, 8642 probes
    measured    ~1300 prompt tokens, ~45 output tokens per request
    measured    1.0175 model calls per prompt (one generation, 1.75% grounding retry)
    stated      g6.xlarge $0.8048/hr, g6e.xlarge $1.861/hr -- from terraform/cluster/variables.tf
    ASSUMED     external API list prices, which change; --api-in/--api-out override them
    UNKNOWN     calls per prompt for a real agentic MOC copilot. --calls sweeps it,
                and it moves the break-even by an order of magnitude.

    make finops
    make finops CALLS=3 HOURS=217          # agentic, business hours only
"""

from __future__ import annotations

import argparse
import math

# From terraform/cluster/variables.tf, which records them next to the instance type.
GPU_HOURLY = 0.8048          # g6.xlarge, 1x L4 24GB
CPU_HOURLY = 0.1008          # m7i.large; m7i.xlarge is double
AURORA_HOURLY = 0.073        # db.t4g.medium, from terraform/data/terraform.tfvars
EKS_HOURLY = 0.10            # control plane, standard support

# Measured: bench ramp, 8642 probes, cache off, qwen2.5-7b AWQ on one L4.
REQ_PER_GPU = 10.0
PROMPT_TOKENS = 1300
OUTPUT_TOKENS = 45
CALLS_PER_PROMPT = 1.0175    # one generation + the 1.75% grounding retry


def cluster_cost(gpus: int, cpu_nodes: int = 2) -> float:
    return (EKS_HOURLY + gpus * GPU_HOURLY + cpu_nodes * CPU_HOURLY + AURORA_HOURLY)


def api_cost_per_hour(rps: float, price_in: float, price_out: float,
                      calls: float) -> float:
    reqs = rps * 3600 * calls
    return reqs * (PROMPT_TOKENS * price_in + OUTPUT_TOKENS * price_out) / 1e6


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api-in", type=float, default=0.15,
                    help="USD per 1M input tokens (default: gpt-4o-mini list)")
    ap.add_argument("--api-out", type=float, default=0.60,
                    help="USD per 1M output tokens")
    ap.add_argument("--api-name", default="gpt-4o-mini")
    ap.add_argument("--calls", type=float, default=CALLS_PER_PROMPT,
                    help="model calls per user prompt; 1 = single-shot RAG, 3-10 = agentic")
    ap.add_argument("--max-rps", type=float, default=50.0)
    args = ap.parse_args()

    print(f"  So sanh voi {args.api_name}: "
          f"${args.api_in}/1M vao, ${args.api_out}/1M ra  (gia niem yet, CAN XAC NHAN)")
    print(f"  {args.calls:g} loi goi model moi prompt  |  "
          f"{PROMPT_TOKENS} token vao / {OUTPUT_TOKENS} ra moi loi goi")
    print(f"  Suc phuc vu do duoc: {REQ_PER_GPU:g} req/s moi GPU L4\n")

    print(f"  {'req/s':>6} {'GPU':>4} {'tu serving':>11} {'API':>10} "
          f"{'tiet kiem':>10}  {'USD/1k token ra':>16}")
    print("  " + "-" * 64)

    grid = [0.5, 1, 1.5, 2, 3, 5, 7, 10, 12, 15, 20, 25, 30, 40, 50]
    breakeven = None
    for rps in grid:
        if rps > args.max_rps:
            break
        gpus = max(1, math.ceil(rps / REQ_PER_GPU))
        own = cluster_cost(gpus)
        api = api_cost_per_hour(rps, args.api_in, args.api_out, args.calls)
        save = (1 - own / api) * 100 if api > 0 else float("-inf")
        out_tokens_k = rps * 3600 * args.calls * OUTPUT_TOKENS / 1000
        per_k = own / out_tokens_k if out_tokens_k else 0
        if breakeven is None and save > 0:
            breakeven = rps
        mark = "  <- hoa von" if breakeven == rps else ""
        print(f"  {rps:>6g} {gpus:>4} {own:>10.2f} {api:>10.2f} "
              f"{save:>9.0f}% {per_k:>16.5f}{mark}")

    print()
    # The 30% line is the criterion, so say where it is rather than leaving it to be read
    # off the table.
    target = None
    r = 0.25
    while r <= args.max_rps:
        gpus = max(1, math.ceil(r / REQ_PER_GPU))
        own = cluster_cost(gpus)
        api = api_cost_per_hour(r, args.api_in, args.api_out, args.calls)
        if api > 0 and (1 - own / api) >= 0.30:
            target = r
            break
        r += 0.25
    print(f"  Hoa von (tiet kiem > 0%)      : ~{breakeven:g} req/s duy tri"
          if breakeven else "  Khong hoa von trong dai da xet")
    if target:
        print(f"  Dat TC2 (tiet kiem >= 30%)    : ~{target:g} req/s duy tri")
        print(f"                                  = {target*3600*24:,.0f} request/ngay 24/7")
    else:
        print("  TC2 (>=30%) khong dat duoc o bat ky muc tai nao trong dai da xet.")
    print()
    print("  Luu y: 'req/s duy tri' la trung binh 24/7, khong phai dinh. Mot he thong")
    print("  chay 10 req/s trong 8 gio hanh chinh co trung binh 24/7 chi ~3,3 req/s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
