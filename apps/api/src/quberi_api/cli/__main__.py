"""Operational CLI entry point.

Runnable via::

    python -m quberi_api.cli <subcommand> [options]

or inside the API container::

    docker compose exec api python -m quberi_api.cli <subcommand> [options]

Subcommands are intentionally thin wrappers around service functions so that
whatever ships via the future HTTP admin API also ships here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Optional

from quberi_api.core.config import get_settings
from quberi_api.db.session import create_engine, create_sessionmaker, dispose_engine
from quberi_api.services.errors import ServiceError
from quberi_api.services.onboarding import BootstrapResult, bootstrap_organization

# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


async def _cmd_bootstrap_organization(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_engine(settings.postgres_dsn.get_secret_value())
    session_factory = create_sessionmaker(engine)
    try:
        async with session_factory() as session:
            result: BootstrapResult = await bootstrap_organization(
                session,
                name=args.name,
                slug=args.slug,
                owner_email=args.owner_email,
                owner_display_name=args.owner_name,
                default_locale=args.locale,
                timezone=args.timezone,
                data_region=args.data_region,
            )
    finally:
        await dispose_engine(engine)

    payload = {
        "organization_id": str(result.organization_id),
        "identity_id": str(result.identity_id),
        "user_id": str(result.user_id),
        "personal_space_id": str(result.personal_space_id),
        "role_binding_id": str(result.role_binding_id),
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quberi",
        description="Quberi operational CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    boot = subparsers.add_parser(
        "bootstrap-organization",
        help="Create a new organisation with an owner + personal space.",
    )
    boot.add_argument("--name", required=True, help="Human-readable organisation name.")
    boot.add_argument(
        "--slug",
        required=True,
        help="Lowercase URL-safe identifier, 1-80 chars (a-z, 0-9, -).",
    )
    boot.add_argument(
        "--owner-email", required=True, help="Email for the first owner user."
    )
    boot.add_argument(
        "--owner-name",
        required=True,
        help="Display name for the first owner user.",
    )
    boot.add_argument(
        "--locale", default="en-US", help="Default locale (default: en-US)."
    )
    boot.add_argument(
        "--timezone", default="UTC", help="Default timezone (default: UTC)."
    )
    boot.add_argument(
        "--data-region",
        default="local",
        help="Data region tag for tenancy routing (default: local).",
    )
    boot.set_defaults(func=_cmd_bootstrap_organization)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return asyncio.run(args.func(args))
    except ServiceError as exc:
        # Expected, actionable errors — print cleanly, exit non-zero.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
