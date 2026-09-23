#!/usr/bin/env python3
"""Render the request flow implemented by the current EKS deployment.

Redis/semantic response caching, serving-time dense retrieval and the optional
known-answer detector are intentionally absent because they are not wired into serving.
"""

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.database import Aurora
from diagrams.aws.general import User
from diagrams.aws.storage import S3
from diagrams.generic.compute import Rack
from diagrams.k8s.compute import Pod
from diagrams.k8s.network import Ingress
from diagrams.onprem.monitoring import Prometheus


OUT = Path(__file__).with_name("request_flow")

GRAPH_ATTR = {
    "bgcolor": "white",
    "compound": "true",
    "fontsize": "18",
    "labelloc": "t",
    "nodesep": "0.65",
    "pad": "0.45",
    "ranksep": "0.9",
    "splines": "spline",
}
NODE_ATTR = {"fontsize": "10"}


def flow(label: str = "") -> Edge:
    """The synchronous request/response path."""
    return Edge(color="#2563eb", fontcolor="#1d4ed8", label=label, penwidth="2.2")


def data(label: str = "") -> Edge:
    """State, artifact or telemetry traffic outside the critical path."""
    return Edge(
        color="#94a3b8",
        constraint="false",
        fontcolor="#64748b",
        label=label,
        style="dotted",
    )


def main() -> None:
    # Never leave a stale artifact when a render is requested.
    OUT.with_suffix(".png").unlink(missing_ok=True)

    with Diagram(
        "Luồng request hiện tại — LiteLLM → Guardrail → vLLM\n"
        "Không Redis/response cache · streaming chỉ phát sau output checks",
        filename=str(OUT),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=GRAPH_ATTR,
        node_attr=NODE_ATTR,
    ):
        caller = User("Client / agent\n(gửi request)")
        receiver = User("Client / agent\n(nhận response)")

        with Cluster("AWS VPC"):
            aurora = Aurora("Aurora PostgreSQL\nprivate subnets\nkey · quota · spend")

            with Cluster("Amazon EKS"):
                # Draw request and response ingress separately. They are the same
                # controller in deployment, but duplicating the icon keeps the return
                # edge from crossing the whole left-to-right flow.
                with Cluster("ingress-nginx · request"):
                    ingress_in = Ingress(
                        "Public lab ingress\nHTTP NodePort :30080\nsource-IP whitelist"
                    )

                with Cluster("namespace llm-serving · request"):
                    litellm_in = Pod(
                        "LiteLLM gateway · 1 replica\n"
                        "Bearer / virtual key\nquota · model permissions · routing"
                    )
                    prepare = Pod(
                        "Guardrail · pipeline.prepare\n"
                        "1  direct injection\n"
                        "2  redact inbound PII\n"
                        "3  canonicalise\n"
                        "4  BM25 retrieval\n"
                        "5  metadata policy\n"
                        "6  document injection\n"
                        "7  prompt + datamarking + cache_salt"
                    )

                with Cluster("namespace inference"):
                    model_a = Pod("vLLM A :8000\nQwen2.5-7B-Instruct")
                    model_b = Pod("vLLM B :8000\nQwen2.5-1.5B-Instruct")

                with Cluster("namespace llm-serving · response"):
                    finalise = Pod(
                        "Guardrail · pipeline.finalise\n"
                        "8  grounding + citations\n"
                        "9  output PII check\n"
                        "10 restore placeholders\n"
                        "fail → safe rejection"
                    )
                    litellm_out = Pod(
                        "LiteLLM response\nusage + spend accounting\n"
                        "release buffered stream"
                    )

                with Cluster("ingress-nginx · response"):
                    ingress_out = Ingress("Response qua ingress\nJSON hoặc SSE")

                corpus = Rack("Corpus index\n798 chunks + metadata\nBM25 loaded at startup")
                metrics = Prometheus("Prometheus\nscrape LiteLLM\nGuardrail · vLLM")
                policy = Rack(
                    "Serving policy\naccess_level = internal-demo\n"
                    "dense + known-answer: off\nsemantic response cache: absent"
                )

        weights = S3("Amazon S3\nmodel weights")

        caller >> flow("POST /v1/chat/completions") >> ingress_in
        ingress_in >> flow() >> litellm_in
        litellm_in >> flow("OpenAI-compatible") >> prepare
        prepare >> flow("model route") >> model_a
        prepare >> flow("model route") >> model_b
        model_a >> flow("full answer") >> finalise
        model_b >> flow("full answer") >> finalise
        finalise >> flow("checked answer") >> litellm_out
        litellm_out >> flow() >> ingress_out
        ingress_out >> flow("JSON / buffered SSE") >> receiver

        litellm_in >> data("key + quota") >> aurora
        litellm_out >> data("spend") >> aurora
        prepare >> data("BM25 search") >> corpus
        model_a >> data("load weights") >> weights
        model_b >> data("load weights") >> weights
        litellm_in >> data("metrics") >> metrics
        prepare >> data("metrics") >> metrics
        model_a >> data("metrics") >> metrics
        policy >> data("deployment config") >> prepare


if __name__ == "__main__":
    main()
    print(f"generated {OUT}.png")
