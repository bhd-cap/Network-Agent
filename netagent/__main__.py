"""Command line entry point: ``python -m netagent`` / ``netagent``."""
from __future__ import annotations

import argparse
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netagent", description="NetAgent multi-agent platform")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the service (default)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    sub.add_parser("token", help="print the admin token used to sign in to the console")
    key = sub.add_parser("create-key", help="create an API key")
    key.add_argument("name")
    key.add_argument("--scope", choices=["tasks", "admin"], default="tasks")
    args = parser.parse_args(argv)

    from .config import get_settings

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if args.command == "token":
        print(settings.admin_token)
        return 0
    if args.command == "create-key":
        from .db import now
        from .runtime import Runtime
        from .security import new_api_key

        rt = Runtime(settings)
        value, prefix, digest = new_api_key()
        rt.db.insert("api_keys", {"name": args.name, "prefix": prefix, "key_hash": digest, "scope": args.scope,
                                  "created_at": now()})
        print(value)
        return 0

    import uvicorn

    from .api import create_app

    host = getattr(args, "host", None) or settings.host
    port = getattr(args, "port", None) or settings.port
    print(f"\n  NetAgent console:  http://{'localhost' if host in ('0.0.0.0', '::') else host}:{port}")
    if os.environ.get("NETAGENT_ADMIN_TOKEN", "").strip():
        print("  Admin token:       set by NETAGENT_ADMIN_TOKEN")
    else:  # generated on first start: show it so the operator can sign in
        print(f"  Admin token:       {settings.admin_token}   (also: netagent token)")
    print(f"  Data directory:    {settings.data_dir}")
    if settings.demo:
        print("  Demo mode:         ON (offline mock models and simulated devices)")
    print()
    uvicorn.run(create_app(), host=host, port=port, log_level=settings.log_level.lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
