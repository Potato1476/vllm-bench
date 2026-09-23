#!/usr/bin/env python3
"""TARGET request flow: what the platform is being built towards.

    make diagrams

THIS IS NOT WHAT IS DEPLOYED. `request_flow.py` renders the flow actually running on EKS
today, and it deliberately omits the Redis response cache, serving-time dense retrieval
and the known-answer detector because none of those are wired into serving yet. Keeping
the two files apart is the point: one picture that shows what exists, one that shows what
it is for. A single diagram doing both duties ends up being wrong about one of them, and
the reader cannot tell which.

Read this one when deciding what to build next; read `request_flow.py` when debugging
what just happened.

THE POD SPLIT IS NOT COSMETIC. The target architecture runs LiteLLM and the guardrail as
two pods:

    LiteLLM Gateway Pod  (API key, quota, router)
        -> Guardrail Pod (PII scan, injection check)
            -> vLLM Pod

An earlier version of this diagram merged them, and the merge hid a real ordering bug.
The infrastructure diagram wires Redis to the GATEWAY. But a cache key is only correct
when built from the question after normalisation and after PII redaction, and both of
those happen in the GUARDRAIL pod. A gateway that looks up the cache before calling the
guardrail therefore:

    1. keys on raw text, so "Cho tôi biết: X" and "X" are two entries
    2. puts unredacted PII into the cache key, into Redis and into the request log
    3. can serve a cached answer for a prompt the guardrail would have blocked

So Redis moves to the guardrail pod. The flow stays linear -- gateway, guardrail, vLLM,
exactly the arrows in the infrastructure diagram -- and the ordering becomes correct. The
gateway stays a thin router: auth, quota, per-agent labelling, retry.

THE ALTERNATIVE, for the record: keep Redis on the gateway and use LiteLLM's built-in
cache, which then forces normalisation and PII redaction into the gateway too. That works,
and it splits the safety logic across two services. Two homes for one security control is
the arrangement where the copy that gets fixed is never the copy in use, so this project
keeps all of it in one pod.

RETRIEVAL LIVES IN THE GUARDRAIL POD, which the infrastructure diagram does not show at
all. The reason is stage 8: the document-level injection scan reads the retrieved chunks.
Splitting retrieval from the scanner that inspects its output means shipping five chunks
of ~900 characters across the network twice per request, to separate two steps that
always run together.

FOUR ORDERINGS THAT ARE BUGS IF REVERSED:

1. Normalisation before the cache key. Measured: canonicalising first took the proportion
   of paraphrase pairs retrieving an identical chunk set from 18.5% to 100%.

2. The cache key includes access_level. A cached answer was computed from documents the
   first caller could read; serving it to someone with fewer rights leaks them through
   the summary. Same argument as vLLM's cache_salt, one layer up.

3. PII redacted before the cache is touched, not before the model is called -- and a
   request that contained PII is never cached at all. Redaction makes two different
   questions collide: "chuyến của 0912345678" and "chuyến của 0987654321" both become
   "chuyến của [PHONE_1]". B would be served A's answer with B's number pasted back in.

4. A cache hit must not bypass the output guardrail. Solved by construction: only answers
   that already passed stages 11 and 12 are stored, so a hit is safe because of what was
   allowed in.

DRAWING NOTES. Every edge is its own statement: chaining `a >> Edge(label=x) >> b >> c`
applies the label to BOTH hops. The write-back edge carries constraint="false" so it does
not drag Redis to the far right and shrink the HIT branch to a stub. The caller is drawn
twice, as sender and receiver, because one node pulls every return edge back across the
graph and turns a left-to-right flow into a knot.
"""

from __future__ import annotations

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import EC2
from diagrams.aws.database import Aurora, ElasticacheForRedis
from diagrams.aws.general import User
from diagrams.aws.network import ElbNetworkLoadBalancer
from diagrams.aws.storage import S3
from diagrams.onprem.monitoring import Prometheus

OUT = Path(__file__).with_name("request_flow_target")

GRAPH_ATTR = {
    "fontsize": "14",
    "labelloc": "t",
    "splines": "ortho",
    "nodesep": "0.55",
    "ranksep": "1.0",
    "pad": "0.5",
}
NODE_ATTR = {"fontsize": "11"}


def hot(label: str = "") -> Edge:
    """On the critical path of a cache miss."""
    return Edge(color="#1f77b4", penwidth="2.2", label=label, fontcolor="#1f77b4")


def skip(label: str = "") -> Edge:
    """The shortcut a cache hit takes."""
    return Edge(color="#2ca02c", penwidth="2.2", style="dashed", label=label,
                fontcolor="#2ca02c")


def stop(label: str = "") -> Edge:
    """A refusal."""
    return Edge(color="#d62728", penwidth="2.0", style="bold", label=label,
                fontcolor="#d62728")


def side(label: str = "") -> Edge:
    """Off the critical path: lookups, storage, telemetry."""
    return Edge(color="#8c8c8c", style="dotted", label=label, fontcolor="#8c8c8c")


def store(label: str = "") -> Edge:
    """Write-back into the cache.

    constraint=false is load-bearing. Without it this edge pulls Redis to the far right,
    next to the last stage that writes to it, the lookup becomes a wire spanning the whole
    picture, and the HIT branch shrinks to a stub in the corner -- the most important
    decision in the flow drawn as the least visible thing on the page.
    """
    return Edge(color="#8c8c8c", style="dotted", label=label, fontcolor="#8c8c8c",
                constraint="false")


def main() -> None:
    with Diagram(
        "Luồng một request — KIẾN TRÚC MỤC TIÊU (chưa triển khai hết)\n"
        "xanh đậm = CACHE MISS · xanh lá đứt = CACHE HIT · đỏ = từ chối · "
        "xám chấm = ngoài đường tới hạn",
        filename=str(OUT), outformat="png", show=False,
        graph_attr=GRAPH_ATTR, node_attr=NODE_ATTR, direction="LR",
    ):
        caller = User("7 agent MOC\n(gửi)")
        back = User("7 agent MOC\n(nhận)")
        nlb = ElbNetworkLoadBalancer("NLB nội bộ\nTLS 1.3 : 443")

        with Cluster("POD 1 — LiteLLM Gateway (CPU)\nđịnh tuyến, không chạm nội dung"):
            auth = EC2("1 · Auth + quota\ngắn nhãn agent cho metric")

        aurora = Aurora("Aurora PG\nkey · quota · log")

        with Cluster("POD 2 — Guardrail (CPU)\nmọi bước đọc nội dung đều ở đây"):
            with Cluster("Vào"):
                norm = EC2("2 · Chuẩn hoá câu hỏi\nNFC · cắt cụm dẫn nhập")
                gin = EC2("3 · Chặn injection\nnguồn = người dùng")
                pii = EC2("4 · Che PII\n0912345678 → [PHONE_1]")
                key = EC2("5 · Cache key\nhash(chuẩn hoá + agent\n+ access_level)\n"
                          "có PII → KHÔNG cache")

            with Cluster("Chỉ khi CACHE MISS"):
                ret = EC2("6 · Truy hồi\nBM25 + dense, RRF k=60")
                pol = EC2("7 · Policy metadata\nquyền: bỏ · hiệu lực: giữ + báo")
                dscan = EC2("8 · Chặn injection\nnguồn = tài liệu")
                build = EC2("9 · Dựng prompt\ndatamark · cache_salt")

        redis = ElasticacheForRedis("Redis — SEMANTIC CACHE\n"
                                    "nối vào POD 2, không phải gateway\n"
                                    "chỉ lưu câu ĐÃ QUA kiểm")
        vstore = Aurora("pgvector\nvector corpus dựng sẵn")

        with Cluster("POD 3 — vLLM (GPU)"):
            vllm = EC2("10 · Sinh câu trả lời\nprefix cache KV bên trong")

        # Declared after POD 3 on purpose: graphviz ranks clusters in declaration order,
        # and declaring the return stages alongside the inbound ones put 11-13 ABOVE 2-5,
        # so the picture read backwards.
        with Cluster("POD 2 — Guardrail, đường về"):
            ground = EC2("11 · Kiểm căn cứ\ntrích dẫn bịa → chặn")
            pout = EC2("12 · Quét PII ra")
            restore = EC2("13 · Khôi phục placeholder")

        s3 = S3("S3\ntrọng số · dataset")
        prom = Prometheus("Prometheus\nTTFT · TPOT · VRAM")

        # --- vào ------------------------------------------------------------------
        caller >> hot() >> nlb
        nlb >> hot() >> auth
        auth >> side() >> aurora
        auth >> hot("sang POD 2") >> norm
        norm >> hot() >> gin
        gin >> hot() >> pii
        pii >> hot() >> key
        gin >> stop("BLOCK\ndừng hẳn") >> back

        # --- cache: điểm rẽ nhánh -------------------------------------------------
        key >> hot("tra cứu") >> redis
        redis >> skip("CACHE HIT\nbỏ qua 6-12\nkhông chạm GPU") >> restore
        key >> hot("CACHE MISS\nchạy 6-12") >> ret

        # --- miss -----------------------------------------------------------------
        ret >> side() >> vstore
        ret >> hot() >> pol
        pol >> hot() >> dscan
        dscan >> hot() >> build
        build >> hot("sang POD 3") >> vllm
        vllm >> side() >> s3

        # --- về -------------------------------------------------------------------
        vllm >> hot("về POD 2") >> ground
        ground >> hot() >> pout
        pout >> store("lưu lại\nchỉ câu đã qua kiểm") >> redis
        pout >> hot() >> restore
        restore >> hot() >> back

        vllm >> side() >> prom


if __name__ == "__main__":
    main()
    print(f"  {OUT}.png")
