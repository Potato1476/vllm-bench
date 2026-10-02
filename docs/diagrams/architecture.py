#!/usr/bin/env python3
"""Render the compact three-replica HA architecture to docs/architecture.png.

Uses the same icon library as request_flow.py. An explicit SVG grid prevents
Graphviz from stretching the supporting services into a tall, sparse picture.
"""

import base64
from html import escape
from pathlib import Path
import shutil
import subprocess
import tempfile

import diagrams
from diagrams.aws.compute import ECR
from diagrams.aws.database import Aurora
from diagrams.aws.general import Users
from diagrams.aws.network import Endpoint
from diagrams.aws.security import SecretsManager
from diagrams.aws.storage import S3
from diagrams.k8s.compute import Job, Pod
from diagrams.k8s.network import Ingress
from diagrams.onprem.inmemory import Redis
from diagrams.onprem.monitoring import Grafana, Prometheus


OUT = Path(__file__).resolve().parents[1] / "architecture.png"
ICON_ROOT = Path(diagrams.__file__).resolve().parent.parent
W, H = 2560, 1530
BLUE, GREEN, SLATE, ORANGE = "#2563eb", "#16a34a", "#64748b", "#d97706"


def box(x, y, w, h, fill, stroke="#cbd5e1", radius=20):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'


def label(x, y, value, size=22, color="#243247", bold=False, center=False):
    return (f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
            f'font-weight="{700 if bold else 400}" text-anchor="{"middle" if center else "start"}" '
            f'font-family="Arial, Helvetica, sans-serif">{escape(value)}</text>')


def icon(kind, x, y, size=82):
    raw = (ICON_ROOT / kind._icon_dir / kind._icon).read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f'<image x="{x}" y="{y}" width="{size}" height="{size}" href="data:image/png;base64,{encoded}"/>'


def path(d, color=BLUE, dashed=False, arrow=True, width=4):
    marker_id = {BLUE: "arrow-blue", GREEN: "arrow-green", SLATE: "arrow-slate", ORANGE: "arrow-orange"}[color]
    marker = f' marker-end="url(#{marker_id})"' if arrow else ""
    dash = ' stroke-dasharray="9 8"' if dashed else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"{dash}{marker}/>'


def section(x, y, w, h, title, fill="#f3f0fa"):
    return box(x, y, w, h, fill, "#c7c3d4") + label(x + 23, y + 36, title, 22, SLATE, True)


def card(x, y, w, h, kind, title, lines=(), badge=""):
    size = 62 if h < 175 else 80
    parts = [box(x, y, w, h, "#ffffff", "#bac8d8"), icon(kind, x + (w-size)/2, y + 14, size)]
    title_y = y + (104 if h < 175 else 126)
    parts.append(label(x+w/2, title_y, title, 24, "#17243a", True, True))
    for n, item in enumerate(lines):
        parts.append(label(x+w/2, title_y + 28 + n*26, item, 18, SLATE, center=True))
    if badge:
        parts += [box(x+w-72, y+12, 53, 34, "#dbeafe", BLUE, 15),
                  label(x+w-45, y+36, badge, 20, BLUE, True, True)]
    return "".join(parts)


def build_svg():
    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<defs>'
        '<marker id="arrow-blue" markerUnits="userSpaceOnUse" markerWidth="20" markerHeight="20" refX="18" refY="10" orient="auto"><path d="M2 2 L18 10 L2 18 Z" fill="#2563eb"/></marker>'
        '<marker id="arrow-green" markerUnits="userSpaceOnUse" markerWidth="20" markerHeight="20" refX="18" refY="10" orient="auto"><path d="M2 2 L18 10 L2 18 Z" fill="#16a34a"/></marker>'
        '<marker id="arrow-slate" markerUnits="userSpaceOnUse" markerWidth="18" markerHeight="18" refX="16" refY="9" orient="auto"><path d="M2 2 L16 9 L2 16 Z" fill="#64748b"/></marker>'
        '<marker id="arrow-orange" markerUnits="userSpaceOnUse" markerWidth="18" markerHeight="18" refX="16" refY="9" orient="auto"><path d="M2 2 L16 9 L2 16 Z" fill="#d97706"/></marker>'
        '</defs>',
        box(0, 0, W, H, "#ffffff", "#ffffff", 0),
        label(1280, 68, "DA#51 · Kiến trúc LLM Serving HA", 40, "#17243a", True, True),
        label(1280, 109, "LiteLLM ×3  →  Guardrail ×3  →  vLLM  |  Redis dùng chung · Aurora PostgreSQL", 24, SLATE, center=True),
        box(240, 148, 2080, 1078, "#e7f5fc", "#b4cedb", 24),
        label(270, 190, "AWS VPC · 2 Availability Zones", 27, "#33566d", True),
        section(310, 225, 1940, 205, "DỮ LIỆU VÀ KẾT NỐI DÙNG CHUNG", "#edf3eb"),
        section(310, 475, 1940, 705, "AMAZON EKS · PUBLIC SUBNETS", "#eef3e7"),
        section(337, 535, 273, 350, "ingress-nginx"),
        section(640, 535, 740, 350, "llm-serving · ≥3 tooling nodes / 2 AZ"),
        section(1410, 535, 807, 350, "inference · GPU nodes"),
        section(337, 925, 1880, 216, "monitoring / benchmark"),
        section(310, 1270, 1940, 210, "AWS MANAGED SERVICES · tồn tại qua các phiên lab", "#e7f5fc"),
        card(500, 267, 305, 145, Aurora, "Aurora PostgreSQL", ("writer + failover reader",)),
        card(875, 267, 305, 145, Redis, "Redis dùng chung", ("multi-AZ · cache + rate limit",)),
        card(1555, 267, 350, 145, Endpoint, "S3 Gateway Endpoint", ("model weights trong VPC",)),
        path("M675 720 H625 V450 H652 V422", SLATE, True, width=3),
        path("M829 622 V597 H1010", SLATE, True, False, 3),
        path("M1195 622 V597 H1010 V422", SLATE, True, width=3),
        path("M1760 622 H1800", SLATE, True, False, 3),
        path("M1850 622 H1800 V450 H1730 V422", SLATE, True, width=3),
        label(738, 470, "key · quota · spend", 18, SLATE, center=True),
        label(1117, 470, "router · response cache", 18, SLATE, center=True),
        label(1798, 470, "S3 sync", 18, SLATE, center=True),
        card(365, 622, 218, 205, Ingress, "NGINX ingress", ("NodePort :30080", "IP allowlist")),
        card(675, 622, 308, 205, Pod, "LiteLLM", ("virtual key · quota", "router · ClusterIP :4000"), "×3"),
        card(1041, 622, 308, 205, Pod, "Guardrail", ("PII · injection · BM25", "output checks · :8080"), "×3"),
        card(1450, 622, 310, 205, Pod, "vLLM A", ("Qwen2.5-7B · AWQ", "OpenAI API :8000")),
        card(1850, 622, 310, 205, Pod, "vLLM B", ("Qwen2.5-1.5B · AWQ", "OpenAI API :8000")),
        icon(Users, 66, 661, 100),
        label(116, 791, "7 agent / client", 23, "#243247", True, True),
        label(116, 820, "Bearer key · X-Agent-Id", 18, SLATE, center=True),
        path("M188 720 H355"), path("M583 720 H665"), path("M983 720 H1031"),
        path("M1349 720 H1440", GREEN),
        path("M1349 720 H1395 V870 H2005 V815", GREEN),
        label(275, 703, "API", 19, BLUE, True, True),
        label(1385, 697, "route A", 19, GREEN, True),
        label(1700, 864, "route B", 19, GREEN, True),
        label(1795, 908, "Shared: 2 model / L4 với GPU time-slicing  ·  Solo: 1 model / GPU", 20, SLATE, center=True),
        label(1010, 859, "PDB minAvailable: 2 cho LiteLLM và guardrail", 19, SLATE, center=True),
        card(385, 962, 385, 156, Prometheus, "Prometheus", ("LiteLLM · guardrail · vLLM",)),
        card(835, 962, 385, 156, Grafana, "Grafana", ("latency · token · cost",)),
        card(1285, 962, 385, 156, Pod, "Tempo", ("OTEL request traces",)),
        card(1735, 962, 385, 156, Job, "k6 Job", ("load test · tooling node",)),
        path("M1170 886 V921 H578 V952", ORANGE, True, width=3),
        path("M1195 886 V921 H1477 V952", ORANGE, True, width=3),
        path("M770 1040 H825", ORANGE, True, width=3),
        label(830, 915, "metrics", 19, ORANGE, center=True),
        label(1355, 915, "OTEL", 19, ORANGE, center=True),
        card(415, 1312, 430, 144, SecretsManager, "Secrets Manager", ("Aurora master credential",)),
        card(950, 1312, 430, 144, ECR, "Amazon ECR", ("guardrail + runner images",)),
        card(1485, 1312, 430, 144, S3, "S3 artifacts", ("weights · datasets · runs",)),
        path("M415 1384 H270 V340 H490", SLATE, True, width=3),
        path("M1165 1312 V1238 H1250 V900 H1365 V750 H1359", SLATE, True, width=3),
        path("M1915 1384 H2280 V340 H1915", SLATE, True, width=3),
        label(1280, 1514, "Xanh: request  ·  Lục: chọn model  ·  Xám: phụ thuộc  ·  Cam: quan sát", 21, SLATE, center=True),
        "</svg>",
    ]
    return "\n".join(p)


def main():
    with tempfile.TemporaryDirectory(prefix="architecture-") as folder:
        source = Path(folder) / "architecture.svg"
        source.write_text(build_svg(), encoding="utf-8")
        if converter := shutil.which("rsvg-convert"):
            subprocess.run([converter, "--output", str(OUT), str(source)], check=True)
        elif converter := shutil.which("magick"):
            subprocess.run([converter, str(source), str(OUT)], check=True)
        else:
            raise RuntimeError("Install rsvg-convert or ImageMagick to render docs/architecture.png")
    print(f"generated {OUT}")


if __name__ == "__main__":
    main()
