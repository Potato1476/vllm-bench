#!/usr/bin/env python3
"""What keys exist, what they may call, what they have spent -- and whether we can hand
them out.

    make agent-keys-list

TWO SOURCES THAT CAN DISAGREE, WHICH IS THE WHOLE REASON THIS EXISTS

    LiteLLM / Aurora   the keys the gateway will ACCEPT. Authoritative, but it stores
                       only a hash: the plaintext cannot be read back.
    Secret agent-keys  the plaintext we can still GIVE to a project team.

Aurora is destroyed with the cluster every evening and `make agent-keys` mints new keys
the next morning, so these two drift apart on an ordinary day:

    in Aurora, not in the Secret   the gateway accepts a key nobody can produce any more.
                                   `make agent-key` prints nothing useful.
    in the Secret, not in Aurora   we would hand a team a credential that gets a 401.
                                   This is the dangerous one: it looks like a working
                                   handover and fails on their first call, in their
                                   codebase, where the cause is least visible.

Nothing else compares the two. A listing that showed only one side would be reassuring
and wrong, so this prints the verdict per key and exits non-zero when any key is
unusable.

Never prints a key. `make agent-key AGENT=<id>` is the one place that does.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.scripts.provision_keys import _call  # noqa: E402

NAMESPACE = "llm-serving"
SECRET = "agent-keys"


def secret_aliases(namespace: str, secret: str) -> set[str] | None:
    """Aliases with a plaintext key on the cluster. None when the Secret is absent."""
    try:
        out = subprocess.run(
            ["kubectl", "-n", namespace, "get", "secret", secret, "-o", "jsonpath={.data}"],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    # A present-but-empty value is not a usable key; treat it as missing rather than let
    # it read as "we can hand this out".
    return {k for k, v in data.items() if _decodes_to_something(v)}


def _decodes_to_something(value: str) -> bool:
    try:
        return bool(base64.b64decode(value).decode().strip())
    except Exception:  # noqa: BLE001 - any decode failure means unusable
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--master-key", required=True)
    ap.add_argument("--namespace", default=NAMESPACE)
    ap.add_argument("--secret", default=SECRET)
    a = ap.parse_args()

    try:
        listing = _call(a.base_url, a.master_key,
                        "/key/list?return_full_object=true&size=100", method="GET")
    except urllib.error.HTTPError as exc:
        print(f"  khong doc duoc danh sach key tu LiteLLM ({exc.code})", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"  khong ket noi duoc LiteLLM: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    rows = [r for r in listing.get("keys", listing if isinstance(listing, list) else [])
            if isinstance(r, dict) and r.get("key_alias")]
    live = {r["key_alias"]: r for r in rows}
    held = secret_aliases(a.namespace, a.secret)

    if held is None:
        print(f"  CANH BAO: khong doc duoc Secret {a.namespace}/{a.secret}.")
        print("  Chi liet ke duoc phia LiteLLM; khong biet key nao con dua ra duoc.\n")
        held = set()
        secret_known = False
    else:
        secret_known = True

    print(f"\n  {len(live)} key trong LiteLLM" + (f", {len(held)} co ban ro trong Secret"
                                                  if secret_known else ""))
    print(f"\n  {'alias':14} {'models':46} {'budget':>7} {'da tieu':>8}  trang thai")
    print("  " + "-" * 94)
    broken = 0
    for alias in sorted(live):
        row = live[alias]
        models = ",".join(row.get("models") or []) or "(tat ca)"
        budget = row.get("max_budget")
        spend = round(row.get("spend") or 0.0, 4)
        if not secret_known:
            state = "?"
        elif alias in held:
            state = "dua ra duoc"
        else:
            state = "KHONG co ban ro -- phai --rotate"
            broken += 1
        print(f"  {alias:14} {models[:46]:46} {str(budget):>7} {str(spend):>8}  {state}")

    if secret_known:
        stale = sorted(held - set(live))
        if stale:
            broken += len(stale)
            print()
            for alias in stale:
                print(f"  {alias:14} CO ban ro nhung LiteLLM KHONG biet -- dua ra se bi 401")

    # A column that is always zero reads as "nothing spent", not as "spend is not being
    # measured" -- and the second is the truth here. LiteLLM derives spend from its
    # model-pricing map, and charts/litellm configures no input/output cost per token for
    # the self-hosted models, so every budget in agents.json is decorative: no key can
    # ever trip one. Say so rather than let the zeros be read as information.
    if rows and all(not (r.get("spend") or 0) for r in rows):
        print("  LUU Y: cot 'da tieu' bang 0 cho TAT CA key. Chua cau hinh")
        print("  input_cost_per_token/output_cost_per_token cho model self-hosted, nen")
        print("  LiteLLM tinh spend = 0 va khong han muc nao co the chan duoc.")
        print()

    if not secret_known:
        return 0
    if broken:
        print(f"  {broken} key khong dung duoc. Chay 'make agent-keys' (them --rotate de")
        print("  cap lai toan bo ban ro) roi kiem lai.")
        return 1
    print("  Moi key deu co ban ro va deu duoc LiteLLM chap nhan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
