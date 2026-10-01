"""Operator CLI: ``media-hub init-db | create-tenant | create-key | bootstrap | export-openapi``."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Sequence

from hub.constants import Scope
from hub.db import create_engine, create_schema, create_session_factory
from hub.errors import ErrorCode, HubError
from hub.main import create_app
from hub.services.tenants import create_tenant, find_tenant_by_name, issue_key, key_view, tenant_view
from hub.settings import Settings, get_settings

DEFAULT_KEY_NAME = "default"
EXPORT_OPENAPI = "export-openapi"
DEFAULT_OPENAPI_PATH = "docs/openapi.json"


def write_openapi(output: Path) -> None:
    """The OpenAPI document does not depend on runtime configuration, so none is read."""
    spec = create_app(Settings.model_construct(), configure_logging=False).openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _scopes(raw: str | None) -> list[Scope]:
    if not raw:
        return list(Scope)
    return [Scope(value.strip()) for value in raw.split(",") if value.strip()]


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    engine = create_engine(get_settings().database_url)
    try:
        await create_schema(engine)
        if args.command == "init-db":
            return {"initialized": True}
        async with create_session_factory(engine)() as session:
            if args.command == "create-tenant":
                tenant = await create_tenant(session, name=args.name, rate_limit_per_min=args.rate_limit)
                return {**tenant_view(tenant), "webhook_secret": tenant.webhook_secret}
            tenant = await find_tenant_by_name(session, args.tenant)
            created = False
            if tenant is None:
                if args.command != "bootstrap":
                    raise HubError(ErrorCode.NOT_FOUND, "no such tenant", details={"name": args.tenant})
                tenant = await create_tenant(session, name=args.tenant)
                created = True
            key, plaintext = await issue_key(
                session,
                tenant,
                name=args.key_name,
                scopes=_scopes(args.scopes),
                ip_allowlist=args.ip or (),
                rate_limit_per_min=args.rate_limit,
            )
            result: dict[str, Any] = {
                "tenant": tenant_view(tenant),
                "key": {**key_view(key), "key": plaintext},
            }
            if created:
                result["webhook_secret"] = tenant.webhook_secret
            return result
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="media-hub")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="create the database tables")
    tenant = commands.add_parser("create-tenant", help="create a tenant")
    tenant.add_argument("--name", required=True)
    tenant.add_argument("--rate-limit", type=int, default=None)
    for name, help_text in (
        ("create-key", "issue an API key for an existing tenant"),
        ("bootstrap", "create the tenant if needed and issue an API key"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--tenant", required=True)
        command.add_argument("--key-name", default=DEFAULT_KEY_NAME)
        command.add_argument("--scopes", help="comma separated; default: all scopes")
        command.add_argument("--ip", action="append", help="allowed CIDR, repeatable")
        command.add_argument("--rate-limit", type=int, default=None)
    export = commands.add_parser(EXPORT_OPENAPI, help="write the OpenAPI document")
    export.add_argument("--output", default=DEFAULT_OPENAPI_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == EXPORT_OPENAPI:
        write_openapi(Path(args.output))
        print(json.dumps({"written": args.output}))
        return
    try:
        result = asyncio.run(_run(args))
    except HubError as error:
        raise SystemExit(json.dumps(error.to_dict(), ensure_ascii=False)) from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
