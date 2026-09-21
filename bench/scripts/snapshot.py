#!/usr/bin/env python3
"""Export this session's time series to S3 before the cluster that holds them is destroyed.

`lab-down` used to print "confirm the time series are exported" and then take a yes --
with nothing in the repo that could actually do the exporting. The numbers that explain a
bottleneck live only in Prometheus's PVC, and `cleanup-volumes` deletes that PVC on the
way out. Whatever is not here is gone, and costs GPU hours to reproduce.

This pulls the series the analysis actually uses rather than snapshotting the whole TSDB.
A TSDB snapshot needs --web.enable-admin-api, writes into the same doomed PVC, and lands
as a directory nothing can read without another Prometheus. A JSON range export is smaller,
diffable, and readable from a notebook six weeks later, which is where the report gets
written.
"""
import argparse
import datetime as dt
import gzip
import json
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# The three layers of the dashboard, plus the lineage needed to interpret them. Anything
# not listed is reconstructable from the cluster config; anything here is not.
SERIES = [
    # service level -- what a caller experienced
    "vllm:ttft_seconds:p95", "vllm:ttft_seconds:p99", "vllm:tpot_seconds:p99",
    "vllm:e2e_seconds:p95", "vllm:output_tokens:rate1m", "vllm:request_rate:rate1m",
    # engine state -- what the scheduler did about it
    "vllm:num_requests_running", "vllm:num_requests_waiting",
    "vllm:kv_cache_usage_perc", "vllm:request_success_total",
    "rate(vllm:num_preemptions_total[2m])",
    # hardware -- which resource actually ran out
    "DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_PROF_DRAM_ACTIVE",
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "DCGM_FI_PROF_GR_ENGINE_ACTIVE",
    "DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_FB_FREE", "DCGM_FI_DEV_SM_CLOCK",
    "DCGM_FI_DEV_POWER_USAGE", "DCGM_FI_DEV_GPU_TEMP",
    # the Kubernetes view of the GPU, which DCGM cannot give
    'sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})',
    'sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})',
    # the pods' own CPU, to prove a latency number was not a throttling artefact
    'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="inference"}[2m]))',
]


class Unreachable(Exception):
    """No Prometheus to export from -- which is not the same as a failed export.

    Exits 2 rather than 1 so `lab-down` can tell the two apart. A failed export with a
    live Prometheus means measurements are about to be destroyed and the teardown must
    stop. No Prometheus at all usually means the cluster is already gone, or a previous
    teardown died partway -- and there, refusing to continue would strand billable
    resources to protect data that does not exist.
    """


def sh(*cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout.strip()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Prom:
    """Talks to Prometheus, starting its own port-forward if nothing is listening.

    Run from `lab-down` there may be no port-forward at all, and asking the operator to
    set one up first is exactly the kind of step that gets skipped at the end of a session.
    """

    def __init__(self, url=None):
        self.proc = None
        if url:
            # Checked even when given explicitly. Skipping this once produced the worst
            # possible outcome: every query failed, an empty file was written, and the
            # script exited 0 -- telling lab-down the measurements were safe.
            if not self._alive(url):
                raise Unreachable(f"{url} khong phan hoi")
            self.base = url
            return
        if self._alive("http://localhost:9090"):
            self.base = "http://localhost:9090"
            return
        port = free_port()
        self.proc = subprocess.Popen(
            ["kubectl", "-n", "monitoring", "port-forward",
             "svc/kps-kube-prometheus-stack-prometheus", f"{port}:9090"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://localhost:{port}"
        for _ in range(30):
            if self._alive(self.base):
                return
            time.sleep(1)
        raise Unreachable("Prometheus khong phan hoi qua port-forward")

    @staticmethod
    def _alive(base):
        try:
            urllib.request.urlopen(base + "/-/ready", timeout=3).read()
            return True
        except Exception:
            return False

    def range(self, expr, start, end, step):
        u = self.base + "/api/v1/query_range?" + urllib.parse.urlencode(
            {"query": expr, "start": start, "end": end, "step": step})
        with urllib.request.urlopen(u, timeout=180) as r:
            return json.load(r)["data"]["result"]

    def close(self):
        if self.proc:
            self.proc.terminate()


def lineage():
    """Everything needed to answer 'what produced these numbers' without the cluster."""
    out = {"captured_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        out["git_sha"] = sh("git", "rev-parse", "--short", "HEAD")
        out["git_dirty"] = bool(sh("git", "status", "--porcelain"))
    except Exception:
        pass
    try:
        nodes = json.loads(sh("kubectl", "get", "nodes", "-o", "json"))
        out["nodes"] = [{
            "name": n["metadata"]["name"],
            "instance_type": n["metadata"]["labels"].get("node.kubernetes.io/instance-type"),
            "workload": n["metadata"]["labels"].get("workload"),
            "gpu_allocatable": n["status"]["allocatable"].get("nvidia.com/gpu"),
        } for n in nodes["items"]]
    except Exception:
        pass
    try:
        pods = json.loads(sh("kubectl", "-n", "inference", "get", "pod",
                             "-l", "app=vllm-server", "-o", "json"))
        out["vllm"] = [{
            "pod": p["metadata"]["name"],
            "image": p["spec"]["containers"][0]["image"],
            "args": p["spec"]["containers"][0].get("args", []),
            "started": p["status"].get("startTime"),
        } for p in pods["items"]]
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12,
                    help="how far back to export (default 12)")
    ap.add_argument("--step", default="15s")
    ap.add_argument("--bucket", default=None,
                    help="artifacts bucket; read from terraform if omitted")
    ap.add_argument("--url", default=None, help="Prometheus base URL")
    ap.add_argument("--label", default="", help="short name for this session")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()

    end = time.time()
    start = end - a.hours * 3600

    try:
        p = Prom(a.url)
    except Unreachable as e:
        print(f"  {e}")
        print("  khong co gi de xuat (cum da bi xoa?) -- ma thoat 2")
        return 2
    try:
        data, empty = {}, []
        for expr in SERIES:
            try:
                r = p.range(expr, start, end, a.step)
            except Exception as e:
                print(f"  ! {expr}: {e}")
                continue
            if not r:
                empty.append(expr)
                continue
            data[expr] = r
            pts = sum(len(s["values"]) for s in r)
            print(f"  {expr[:58]:58} {len(r):2} series, {pts:6} diem")
    finally:
        p.close()

    if empty:
        print(f"\n  khong co du lieu ({len(empty)}): " + ", ".join(e[:40] for e in empty))

    # An export of nothing is not an export. Writing the file anyway and exiting 0 would
    # let lab-down destroy the cluster believing the session was saved.
    if not data:
        print("\n  KHONG series nao tra ve du lieu -- khong ghi file gi ca")
        return 2

    doc = {
        "window": {"start": start, "end": end, "step": a.step,
                   "start_iso": dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(),
                   "end_iso": dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat()},
        "lineage": lineage(),
        "series": data,
    }

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}{'-' + a.label if a.label else ''}"
    local = f"/tmp/snapshot-{name}.json.gz"
    with gzip.open(local, "wt") as f:
        json.dump(doc, f)
    size = __import__("os").path.getsize(local)
    print(f"\n  ghi {local}  ({size/1e6:.2f} MB nen)")

    if a.no_upload:
        return 0

    bucket = a.bucket
    if not bucket:
        import re
        raw = subprocess.run(["terraform", "-chdir=terraform/core", "output", "-raw",
                              "artifacts_bucket_name"], capture_output=True, text=True).stdout
        m = re.search(r"\b[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\b", raw)
        bucket = m.group(0) if m else None
    if not bucket:
        print("  KHONG xac dinh duoc bucket -- file van con o", local)
        return 1

    key = f"s3://{bucket}/runs/{name}/metrics.json.gz"
    sh("aws", "s3", "cp", local, key)
    print(f"  tai len {key}")
    print("\n  doc lai bang:")
    print(f"    aws s3 cp {key} - | gunzip | python3 -m json.tool | less")
    return 0


if __name__ == "__main__":
    sys.exit(main())
