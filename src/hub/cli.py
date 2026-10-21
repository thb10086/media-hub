"""Operator CLI: ``media-hub init-db | create-tenant | create-key | bootstrap | sync-identities |
import-jar | export-openapi``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from hub.constants import Scope
from hub.db import create_engine, create_schema, create_session_factory
from hub.errors import ErrorCode, HubError
from hub.main import create_app
from hub.providers.dtk import DtkClient
from hub.providers.sau import SauClient
from hub.services.data_identities import browser_profile, build_data_identities, import_jar
from hub.services.tenants import create_tenant, find_tenant_by_name, issue_key, key_view, tenant_view
from hub.settings import Settings, get_settings

DEFAULT_KEY_NAME = "default"
EXPORT_OPENAPI = "export-openapi"
SYNC_IDENTITIES = "sync-identities"
IMPORT_JAR = "import-jar"
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


async def _sync_identities(settings: Settings, engine: Any, limit: int) -> dict[str, Any]:
    """One pass of the worker's identity sync, for a backfill or a check after a fix."""
    if not settings.dtk_base_url or not settings.sau_base_url:
        raise HubError(ErrorCode.NOT_CONFIGURED, "HUB_DTK_BASE_URL and HUB_SAU_BASE_URL are both required")
    dtk = DtkClient(
        settings.dtk_base_url,
        settings.dtk_api_key.get_secret_value() if settings.dtk_api_key else "",
        wait_seconds=settings.dtk_wait_seconds,
        timeout=settings.dtk_timeout_seconds,
    )
    sau = SauClient(
        settings.sau_base_url,
        request_timeout=settings.sau_request_timeout_seconds,
        publish_timeout=settings.sau_publish_timeout_seconds,
        login_timeout=settings.sau_login_timeout_seconds,
    )
    try:
        identities = build_data_identities(settings, dtk, sau)
        assert identities is not None
        report = await identities.sync(create_session_factory(engine), limit=limit)
        return {**report.as_dict(), "importing": identities.imports_logins}
    finally:
        await dtk.aclose()
        await sau.aclose()


def _read_jar(source: str) -> dict[str, Any]:
    raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    try:
        jar = json.loads(raw)
    except ValueError as exc:
        raise HubError(ErrorCode.INVALID_PARAM, "the jar is not JSON (a Playwright storage state)") from exc
    if not isinstance(jar, dict):
        raise HubError(ErrorCode.INVALID_PARAM, "the jar is not a Playwright storage state")
    return jar


async def _import_jar(jar: dict[str, Any]) -> dict[str, Any]:
    """Import a Playwright storage state into DTK's identity pool.

    deploy.sh guest-identity pipes a guest jar from SAU's browser in here, so the
    pool has an identity for the hot board, details and downloads before anyone
    has bound an account. Cookie values are never printed.
    """
    settings = get_settings()
    if not settings.dtk_base_url:
        raise HubError(ErrorCode.NOT_CONFIGURED, "HUB_DTK_BASE_URL is required")
    dtk = DtkClient(
        settings.dtk_base_url,
        settings.dtk_api_key.get_secret_value() if settings.dtk_api_key else "",
        wait_seconds=settings.dtk_wait_seconds,
        timeout=settings.dtk_timeout_seconds,
    )
    try:
        imported = await import_jar(dtk, browser_profile(settings), jar)
    finally:
        await dtk.aclose()
    report = imported.get("report") if isinstance(imported.get("report"), dict) else {}
    return {
        "identity_id": imported["identity_id"],
        "authenticated": report.get("authenticated"),
        "usable": report.get("usable"),
        "cookie_names": report.get("cookie_names"),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        await create_schema(engine)
        if args.command == "init-db":
            return {"initialized": True}
        if args.command == SYNC_IDENTITIES:
            return await _sync_identities(settings, engine, args.limit)
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
    sync = commands.add_parser(
        SYNC_IDENTITIES, help="import bound Douyin accounts still missing from DTK's identity pool"
    )
    sync.add_argument("--limit", type=int, default=200)
    jar = commands.add_parser(IMPORT_JAR, help="import a Playwright storage state into DTK's identity pool")
    jar.add_argument("file", help="path to the storage state JSON, or - for stdin")
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
        if args.command == IMPORT_JAR:
            result = asyncio.run(_import_jar(_read_jar(args.file)))
        else:
            result = asyncio.run(_run(args))
    except HubError as error:
        raise SystemExit(json.dumps(error.to_dict(), ensure_ascii=False)) from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
