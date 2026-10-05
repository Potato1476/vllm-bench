#!/usr/bin/env python3
"""Draw the TC2 curve: savings against an external API, over sustained load.

    make finops-plot
    make finops-plot CALLS=3 OUT=reports/images/tc2.png

IT IMPORTS THE COST MODEL FROM finops_curve.py RATHER THAN RESTATING IT. A chart and a
table that each carry their own copy of the arithmetic drift apart on the first edit, and
the drift is invisible: both look authoritative, and nobody re-derives a picture.

WHY THE LINE IS A SAWTOOTH AND MUST BE DRAWN AS ONE

Self-hosted cost is a STEP function. It jumps when load crosses what the current GPUs can
serve, then stays flat while the new GPU fills. API cost is a straight line through the
origin. So savings climb as a GPU fills, fall at every GPU added, and climb again.

The tempting chart is a smooth rising curve to 89%. It would be a lie of exactly the kind
this repo keeps catching: it hides that adding a GPU at 10, 20 and 25 req/s makes the
platform momentarily WORSE value, which is the one thing a capacity decision turns on.

WHY THE X AXIS SAYS "SUSTAINED"

The denominator is a 24/7 average, not a peak. A platform serving 10 req/s through eight
business hours averages 3.3 req/s across the week, and lands a third of the way along
this axis from where its dashboard suggests. Reading a peak against this curve overstates
savings by roughly 3x, so the axis label says so and the annotation repeats it.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from bench.scripts.finops_curve import (  # noqa: E402
    OUTPUT_TOKENS, PROMPT_TOKENS, REQ_PER_GPU, api_cost_per_hour, cluster_cost,
)

# Measured ceiling. Past this the curve is arithmetic, not evidence, and the chart shades
# it so nobody quotes a number from a region the platform was never run in.
MEASURED_MAX_RPS = 50.0


def _series(max_rps: float, calls: float, price_in: float, price_out: float):
    """Cost per 1k OUTPUT tokens, self-hosted and API, plus savings, over load.

    Stepped at a fine grain rather than sampled at round numbers: the discontinuity at
    each GPU boundary is the point, and a coarse grid rounds it into a smooth ramp.
    """
    rps, self_cost, api_cost, savings, gpus_at = [], [], [], [], []
    r = 0.25
    while r <= max_rps + 1e-9:
        gpus = max(1, math.ceil(r / REQ_PER_GPU))
        cluster = cluster_cost(gpus)
        api = api_cost_per_hour(r, price_in, price_out, calls)
        out_k = r * 3600 * calls * OUTPUT_TOKENS / 1000
        rps.append(r)
        self_cost.append(cluster / out_k)
        api_cost.append(api / out_k)
        savings.append(100.0 * (1 - cluster / api) if api > 0 else float("nan"))
        gpus_at.append(gpus)
        r += 0.25
    return rps, self_cost, api_cost, savings, gpus_at


def _first_crossing(rps, savings, threshold: float) -> float | None:
    for x, s in zip(rps, savings):
        if s >= threshold:
            return x
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api-in", type=float, default=0.15)
    ap.add_argument("--api-out", type=float, default=0.60)
    ap.add_argument("--api-name", default="gpt-4o-mini")
    ap.add_argument("--calls", type=float, default=1.0175)
    ap.add_argument("--max-rps", type=float, default=50.0)
    ap.add_argument("--out", default="reports/images/tc2-savings.png")
    a = ap.parse_args()

    rps, self_cost, api_cost, savings, gpus = _series(
        a.max_rps, a.calls, a.api_in, a.api_out)
    breakeven = _first_crossing(rps, savings, 0.0)
    tc2 = _first_crossing(rps, savings, 30.0)

    fig, (ax_cost, ax_save) = plt.subplots(
        2, 1, figsize=(10, 8.5), sharex=True,
        gridspec_kw={"height_ratios": [1, 1.15], "hspace": 0.12})

    teal, deep, gold, grey = "#209799", "#0e5b5c", "#c98a00", "#9aa5a5"

    # --- top: why the savings line has the shape it has --------------------------
    ax_cost.step(rps, self_cost, where="post", color=teal, lw=2,
                 label="Tự vận hành (bậc thang: mỗi GPU thêm vào là một bậc)")
    ax_cost.plot(rps, api_cost, color=gold, lw=2, ls="--",
                 label=f"API ngoài ({a.api_name}, giá niêm yết)")
    ax_cost.set_ylabel("USD / 1k token đầu ra")
    ax_cost.set_yscale("log")
    ax_cost.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.4f}"))
    ax_cost.grid(alpha=0.25, which="both")
    ax_cost.legend(loc="upper right", fontsize=9)
    ax_cost.set_title(
        "TC2 — chi phí trên 1k token so với API ngoài\n"
        f"{PROMPT_TOKENS} token vào / {OUTPUT_TOKENS} ra mỗi lời gọi · "
        f"{a.calls:g} lời gọi mỗi prompt · {REQ_PER_GPU:g} req/s mỗi GPU L4 (đo được)",
        fontsize=12, loc="left")

    # --- bottom: the criterion itself --------------------------------------------
    ax_save.plot(rps, savings, color=deep, lw=2)
    ax_save.axhline(30, color="#c0392b", lw=1.4, ls="--")
    ax_save.axhline(0, color=grey, lw=1)
    ax_save.fill_between(rps, 30, savings, where=[s >= 30 for s in savings],
                         color=teal, alpha=0.13, interpolate=True)
    ax_save.text(a.max_rps * 0.99, 31.5, "ngưỡng TC2: giảm 30%",
                 color="#c0392b", fontsize=9, ha="right", va="bottom")

    # Staggered. Break-even and the TC2 crossing sit 0.75 req/s apart on a 50 req/s
    # axis, so two labels at the same height overprint into an unreadable smear -- which
    # is how the first draft of this chart came out.
    for x, label, colour, y in ((breakeven, "hoà vốn", "#6b7777", -45),
                                (tc2, "đạt TC2", "#c0392b", -80)):
        if x is None:
            continue
        ax_save.axvline(x, color=colour, lw=1.2, ls=":")
        ax_save.annotate(
            f"{label}: {x:.2f} req/s", xy=(x, y),
            xytext=(a.max_rps * 0.10, y), color=colour, fontsize=9.5, va="center",
            arrowprops=dict(arrowstyle="->", color=colour, lw=1, shrinkA=0, shrinkB=2))

    # Each GPU boundary, which is where the line falls.
    for n in range(1, int(math.ceil(a.max_rps / REQ_PER_GPU))):
        ax_save.axvline(n * REQ_PER_GPU, color=grey, lw=0.7, alpha=0.5)
        ax_save.text(n * REQ_PER_GPU, -8, f"+GPU {n + 1}", rotation=90,
                     fontsize=7.5, color=grey, ha="right", va="top")

    if a.max_rps > MEASURED_MAX_RPS:
        ax_save.axvspan(MEASURED_MAX_RPS, a.max_rps, color=grey, alpha=0.12)
        ax_save.text((MEASURED_MAX_RPS + a.max_rps) / 2, -100,
                     "ngoài vùng đã đo — ngoại suy", fontsize=8.5,
                     color=grey, ha="center")

    ax_save.set_ylabel("Tiết kiệm so với API ngoài (%)")
    ax_save.set_xlabel("Tải DUY TRÌ, trung bình 24/7 (req/s) — không phải tải đỉnh")
    # Clipped at -120, not at the true minimum. Below about 0.8 req/s the line dives
    # past -1000% and, drawn to scale, squashes the entire 0-89% band -- the part the
    # criterion is about -- into the top sliver of the panel. The note says what was cut.
    ax_save.set_ylim(-120, 100)
    ax_save.text(a.max_rps * 0.99, -114,
                 "dưới ~0,8 req/s đường còn tụt sâu hơn nhiều (cắt bớt để nhìn được vùng 0–89%)",
                 fontsize=8, color=grey, ha="right", va="bottom")
    ax_save.set_xlim(0, a.max_rps)
    ax_save.grid(alpha=0.25)

    fig.text(0.011, 0.012,
             "Trục hoành là trung bình 24/7. Một hệ chạy 10 req/s trong 8 giờ hành chính "
             "chỉ đạt ~3,3 req/s duy trì — đọc tải đỉnh vào biểu đồ này sẽ phóng đại mức "
             "tiết kiệm khoảng 3 lần.\nGiá API là giá niêm yết tại thời điểm đo và CẦN "
             "XÁC NHẬN LẠI. Chi phí tự vận hành gồm EKS, GPU, 2 node CPU và Aurora.",
             fontsize=8, color="#555", va="bottom")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")

    print(f"  da ve {out}")
    print(f"  hoa von      ~{breakeven:.2f} req/s duy tri" if breakeven else "  khong hoa von")
    print(f"  dat TC2      ~{tc2:.2f} req/s duy tri" if tc2 else "  khong dat TC2")
    print(f"  tiet kiem toi da trong vung do duoc: {max(savings):.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
