#!/usr/bin/env python3
"""Request flow for the MOC copilot platform: what happens, in what order, and why.

    .venv-embed/bin/python docs/diagrams/request_flow.py

This complements the infrastructure diagram rather than replacing it. The infra diagram
answers "what runs where"; this one answers "in what order", which is where the
correctness lives. Four orderings that are bugs if you get them wrong:

1. NORMALISATION COMES BEFORE THE CACHE KEY.
   "Cho tôi biết: X" and "X" are the same question. Keyed on the raw text they are two
   entries and the hit rate collapses. Measured on this project's evaluation set:
   canonicalising first took the proportion of paraphrase pairs retrieving an identical
   chunk set from 18.5% to 100%.

2. THE CACHE KEY MUST INCLUDE access_level.
   A cached answer was computed from documents the first caller was allowed to read.
   Serving it to someone with fewer rights leaks those documents through their summary.
   Same argument as vLLM's cache_salt, one layer up, and Redis will not make it for you.

3. PII IS REDACTED BEFORE THE CACHE IS TOUCHED, not before the model is called.
   Redacting only on the way to the model still leaves the raw value in the cache key,
   the request log and the trace exporter -- three copies outside the model that nobody
   thinks of as an LLM problem.

3b. AND A REQUEST THAT CONTAINED PII IS NEVER CACHED.
   Redaction creates a collision that reads as a feature until you trace it:

       A asks  "tra cứu chuyến của 0912345678"  ->  "tra cứu chuyến của [PHONE_1]"
       B asks  "tra cứu chuyến của 0987654321"  ->  "tra cứu chuyến của [PHONE_1]"

   Same key. B is served the answer computed from A's data, and step 12 then rewrites
   [PHONE_1] with B's number, so the leak arrives wearing B's own details and nothing
   looks wrong. Including the redacted VALUES in the key would fix the collision and
   would also put the identity data back in the key, which is what redaction was for.
   A question about a specific person has an answer about that person; it is not a
   shared result, so it is not cached.

4. A CACHE HIT MUST NOT BYPASS THE OUTPUT GUARDRAIL.
   Solved by construction rather than by a second check: only responses that already
   passed grounding and PII egress are stored, so a hit is safe because of what was
   allowed in. Re-running the checks on every hit throws away most of the latency the
   cache was bought for.

TWO CACHES, NOT THE SAME THING. Redis stores finished ANSWERS and a hit skips retrieval
and the GPU entirely. vLLM's prefix cache stores KV BLOCKS inside the engine and only
helps requests that reach it. They compose, and the prompt layout that maximises the
second has nothing to do with the key that maximises the first.

DRAWING NOTE. Every edge is its own statement. Chaining `a >> Edge(label=...) >> b >> c`
in `diagrams` applies that label to BOTH hops, which silently scatters "MISS" across the
whole graph -- the first render of this file had it on six unrelated edges.
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

OUT = Path(__file__).with_name("request_flow")

GRAPH_ATTR = {
    "fontsize": "14",
    "labelloc": "t",
    "splines": "ortho",
    "nodesep": "0.6",
    "ranksep": "1.1",
    "pad": "0.5",
    "compound": "true",
}
NODE_ATTR = {"fontsize": "11"}


# Fresh Edge per call: reusing one instance across statements lets attributes leak.
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
    """Writing the finished answer back into the cache.

    constraint=false is load-bearing. Without it this edge pulls Redis to the far right
    of the graph, next to the last stage that writes to it, and the cache lookup at
    stage 4 becomes a wire running the entire width of the picture. The HIT branch then
    reduces to a stub in the corner -- the single most important decision in the flow,
    drawn as the least visible thing on the page. constraint=false says "draw this edge
    but do not let it decide where the nodes go".
    """
    return Edge(color="#8c8c8c", style="dotted", label=label, fontcolor="#8c8c8c",
                constraint="false")


def main() -> None:
    with Diagram(
        "Luồng một request — MOC copilot\n"
        "xanh đậm = đường khi CACHE MISS · xanh lá đứt = đường tắt khi CACHE HIT · "
        "đỏ = từ chối · xám chấm = ngoài đường tới hạn",
        filename=str(OUT), outformat="png", show=False,
        graph_attr=GRAPH_ATTR, node_attr=NODE_ATTR, direction="LR",
    ):
        # The caller is drawn twice, as sender and receiver. One node would pull every
        # return edge back across the graph and turn a left-to-right flow into a knot.
        caller = User("7 agent MOC\n(gửi)")
        back = User("7 agent MOC\n(nhận)")
        nlb = ElbNetworkLoadBalancer("NLB nội bộ\nTLS 1.3 : 443")

        with Cluster("Gateway — LiteLLM (pod CPU)"):
            auth = EC2("1 · Auth + quota\ngắn nhãn agent")
            norm = EC2("2 · Chuẩn hoá câu hỏi\nNFC · cắt cụm dẫn nhập")
            gin = EC2("3 · Guardrail vào\ninjection · che PII")
            key = EC2("4 · Cache key\nhash(câu chuẩn hoá\n+ agent + access_level)\n"
                      "CÓ PII → KHÔNG cache")

        redis = ElasticacheForRedis("Redis — SEMANTIC CACHE\n"
                                    "lưu CÂU TRẢ LỜI đã qua kiểm\n"
                                    "key = câu chuẩn hoá + agent + access_level")
        aurora = Aurora("Aurora PG\nkey · quota · log")

        with Cluster("Chỉ chạy khi CACHE MISS"):
            ret = EC2("5 · Truy hồi\nBM25 + dense, RRF k=60")
            pol = EC2("6 · Policy metadata\nquyền: bỏ · hiệu lực: giữ + báo")
            dscan = EC2("7 · Quét injection\ntrong tài liệu")
            build = EC2("8 · Dựng prompt\ndatamark · cache_salt")
            vllm = EC2("9 · vLLM sinh\n(prefix cache KV bên trong)")

        vstore = Aurora("pgvector\nvector corpus dựng sẵn")

        with Cluster("Guardrail ra — đường về"):
            ground = EC2("10 · Kiểm căn cứ\ntrích dẫn bịa → chặn")
            pout = EC2("11 · Quét PII ra")

        restore = EC2("12 · Khôi phục placeholder")
        s3 = S3("S3\ntrọng số · dataset")
        prom = Prometheus("Prometheus\nTTFT · TPOT · VRAM")

        # --- vào ------------------------------------------------------------------
        caller >> hot() >> nlb
        nlb >> hot() >> auth
        auth >> hot() >> norm
        norm >> hot() >> gin
        gin >> hot() >> key
        auth >> side() >> aurora
        gin >> stop("BLOCK\ndừng hẳn") >> back

        # --- cache: điểm rẽ nhánh của cả luồng ------------------------------------
        key >> hot("tra cứu\n(bỏ qua nếu câu hỏi có PII)") >> redis

        # HIT: nhảy thẳng tới bước 12. Không truy hồi, không GPU, không guardrail ra --
        # an toàn vì chỉ câu trả lời ĐÃ QUA bước 10-11 mới được lưu vào đây.
        redis >> skip("CACHE HIT\n→ bỏ qua 5-11\nkhông chạm GPU") >> restore

        # MISS: đi hết đường dài.
        key >> hot("CACHE MISS\n→ chạy 5-11") >> ret
        ret >> side() >> vstore
        ret >> hot() >> pol
        pol >> hot() >> dscan
        dscan >> hot() >> build
        build >> hot() >> vllm
        vllm >> side() >> s3

        # --- về -------------------------------------------------------------------
        vllm >> hot() >> ground
        ground >> hot() >> pout
        pout >> store("lưu lại\n(chỉ câu đã qua kiểm)") >> redis
        pout >> hot() >> restore
        restore >> hot() >> back

        vllm >> side() >> prom


if __name__ == "__main__":
    main()
    print(f"  {OUT}.png")
