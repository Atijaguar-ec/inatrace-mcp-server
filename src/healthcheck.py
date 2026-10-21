"""
Container healthcheck probe.

Targets /health/ready, not /health/live: this service is a read-only regulatory
oracle, so a process that cannot reach PostgreSQL is running but useless. Exits 0
only when the server reports READY with a live connection pool.

Uses the standard library alone so the runtime image needs no curl or wget.
"""
import json
import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("PORT", "8000")
    url = f"http://127.0.0.1:{port}/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            if resp.status != 200:
                print(f"healthcheck: {url} devolvió HTTP {resp.status}", file=sys.stderr)
                return 1
            body = json.load(resp)
    except Exception as exc:
        print(f"healthcheck: {url} inalcanzable: {exc}", file=sys.stderr)
        return 1

    if body.get("db_pool_connected") is not True:
        print(f"healthcheck: estado '{body.get('status')}' sin pool de base de datos", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
