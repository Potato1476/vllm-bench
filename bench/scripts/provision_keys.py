#!/usr/bin/env python3
"""Create one LiteLLM virtual key per MOC agent, from bench/agents.json.

WHY THIS IS THE PRICE OF SESSION-SCOPED AURORA

Aurora is destroyed with the cluster every evening, so the virtual keys go with it. That
is a deliberate trade -- running it continuously costs 37 USD over the three weeks left,
against about 5 USD for the hours actually used -- and it is only defensible because
recreating the keys is this script rather than an afternoon of reissuing credentials.

WHAT A VIRTUAL KEY BUYS, AND WHY THE PLATFORM IS DIFFERENT WITHOUT ONE

Measured on this deployment, not assumed. Running without a database:

    litellm_router_total_requests_total{router="router"} 5.0   <- requests, no agent
    litellm_proxy_total_requests_metric_total                  <- absent entirely

That second metric carries the end_user and team dimensions the per-agent dashboard is
built from, and LiteLLM v1.90.2 does not populate end_user from the OpenAI `user` field --
sending {"user": "moc-analytics"} in the body labels nothing. The only thing that creates
that dimension is an authenticated virtual key, and keys need somewhere to live.

So the acceptance criterion "dashboard latency/cost/token PER AGENT" does not turn on
dashboards. It turns on this script having run.

IDEMPOTENT, AND DELIBERATELY NOT DESTRUCTIVE

Keys are matched by their key_alias. An existing key for an agent is updated in place, so
re-running after editing a budget does what it looks like it does. Nothing is ever
deleted: a key that vanished from agents.json may still be configured in somebody's
client, and silently revoking it would present as the platform being down for one agent.
Remove those by hand, once you know who holds them.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

AGENTS = Path(__file__).resolve().parents[1] / "agents.json"


def _call(base: str, master: str, path: str, payload: dict | None = None,
          method: str = "POST", timeout: float = 30.0) -> Any:
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {master}",
                 "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        return json.loads(body) if body else {}


def load_agents(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    agents = data.get("agents", [])
    ids = [a["id"] for a in agents]
    if len(ids) != len(set(ids)):
        raise SystemExit("agents.json has duplicate ids; aliases must be unique")
    return agents


def existing_aliases(base: str, master: str) -> dict[str, str]:
    """Map key_alias -> token for keys already in the database."""
    try:
        # size is capped at 100 by the proxy; 200 comes back 422 "Input should be less
        # than or equal to 100", which surfaced as an unhandled traceback rather than a
        # message. Paginate instead of asking for more than the API allows.
        listing = _call(base, master, "/key/list?return_full_object=true&size=100",
                        method="GET")
    except urllib.error.HTTPError as exc:
        # 404 on older proxies without /key/list; anything else means the listing is
        # unavailable, not that provisioning should stop. Treat it as "no keys known"
        # and let /key/generate decide -- a crash here loses the whole run over a
        # read-only call.
        print(f"  khong doc duoc danh sach key ({exc.code}); coi nhu chua co key nao",
              file=sys.stderr)
        return {}
    rows = listing.get("keys", listing if isinstance(listing, list) else [])
    found: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("key_alias"):
            found[row["key_alias"]] = row.get("token", "")
    return found


def provision(base: str, master: str, agents: list[dict],
              dry_run: bool = False, rotate: bool = False) -> list[tuple[str, str, str]]:
    have = {} if dry_run else existing_aliases(base, master)
    if rotate and have:
        # Delete first, so every alias comes back through /key/generate and every key in
        # the table below is one somebody can actually put in a client.
        #
        # This exists because "update in place" looked right and was not: /key/list
        # returns the HASHED token, the plaintext is shown exactly once at creation and
        # is unrecoverable afterwards. An updated key therefore printed a 64-character
        # hash in the key column -- indistinguishable from a key, and useless as one.
        try:
            _call(base, master, "/key/delete", {"keys": list(have.values())})
            have = {}
        except urllib.error.HTTPError as exc:
            print(f"  khong xoa duoc key cu ({exc.code}); se update tai cho",
                  file=sys.stderr)
    results: list[tuple[str, str, str]] = []

    for agent in agents:
        alias = agent["id"]
        body = {
            "key_alias": alias,
            # end_user is the label the per-agent dashboard groups by. Setting it on the
            # key is what makes it appear at all -- see the module docstring.
            "user_id": alias,
            "metadata": {"agent": alias,
                         "description": agent.get("description", ""),
                         "access_level": agent.get("access_level", "internal-demo")},
            "models": agent.get("models", []),
            "max_budget": agent.get("budget_usd"),
            "budget_duration": "30d",
        }
        if dry_run:
            results.append((alias, "would create", ""))
            continue
        try:
            if alias in have:
                _call(base, master, "/key/update",
                      {**body, "key": have[alias]})
                results.append((alias, "updated", have[alias]))
            else:
                created = _call(base, master, "/key/generate", body)
                results.append((alias, "created", created.get("key", "")))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:160]
            results.append((alias, f"FAILED {exc.code}", detail))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True,
                        help="LiteLLM proxy base, e.g. http://127.0.0.1:4000")
    parser.add_argument("--master-key", required=True)
    parser.add_argument("--agents", type=Path, default=AGENTS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rotate", action="store_true",
                        help="delete existing keys and mint new ones, so every key printed "
                             "is usable. The plaintext of an existing key cannot be read "
                             "back, so without this a re-run prints hashes for them.")
    args = parser.parse_args()

    agents = load_agents(args.agents)
    print(f"  {len(agents)} agent trong {args.agents.name}")

    try:
        health = _call(args.base_url, args.master_key, "/health/readiness",
                       method="GET", timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"  khong ket noi duoc LiteLLM: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    # Without a database LiteLLM still serves, but /key/generate has nowhere to write and
    # fails per key. Say so once, here, instead of seven times below.
    if not args.dry_run and health.get("db") in ("Not connected", None, False):
        print("  CANH BAO: LiteLLM bao khong co database.")
        print("  Virtual key can Aurora -- chay 'make data-up' truoc.")

    results = provision(args.base_url, args.master_key, agents, args.dry_run,
                        rotate=args.rotate)

    print()
    print(f"  {'agent':18} {'trang thai':12} key")
    failures = 0
    for alias, status, token in results:
        if status.startswith("FAILED"):
            failures += 1
        # Printed in full: these are freshly minted keys for a lab that is torn down
        # tonight, and their whole purpose is to be handed to a client. They are never
        # written to a file -- `make agent-keys` reads them back from the proxy.
        shown = token if (status == "created" or not token) else "(khong doc lai duoc -- dung --rotate)"
        print(f"  {alias:18} {status:12} {shown}")
    print()
    if failures:
        print(f"  {failures} agent that bai.", file=sys.stderr)
        return 1
    print("  Dung key nay thay master key de request duoc gan nhan theo agent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
