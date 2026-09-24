"""Administrative commands for the pod-local semantic response cache."""

from __future__ import annotations

import argparse
import os

from .semantic_cache import SemanticResponseCache


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    invalidate = sub.add_parser("invalidate")
    invalidate.add_argument("--document-id", required=True)
    sub.add_parser("clear")
    args = parser.parse_args()

    import redis

    client = redis.Redis(
        unix_socket_path=os.getenv("REDIS_UNIX_SOCKET", "/run/redis/redis.sock"),
        socket_timeout=2,
    )
    cache = SemanticResponseCache(client)
    if args.command == "invalidate":
        removed = cache.invalidate_document(args.document_id)
        print(f"invalidated {removed} cached answer(s) for {args.document_id}")
    else:
        removed = cache.clear()
        print(f"removed {removed} Redis key(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
