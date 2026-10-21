#!/usr/bin/env bash
# media-hub deploy / upgrade for one Linux host (Ubuntu 22.04 / 24.04, Docker + compose v2).
#
#   deploy/deploy.sh up                   fetch pinned upstreams, patch, build, start both stacks
#   deploy/deploy.sh status               container status of both stacks
#   deploy/deploy.sh logs [service...]    follow media-hub logs (gateway, worker, sau, ...)
#   deploy/deploy.sh tenant NAME [CIDR]   create the tenant if needed and issue an API key
#   deploy/deploy.sh guest-identity       add a guest Douyin identity to DTK's pool (hot board, details, downloads)
#   deploy/deploy.sh down                 stop both stacks; volumes and data are kept
#
# Overrides (environment):
#   GITHUB_CODELOAD   tarball host, default https://codeload.github.com
#   DTK_REGISTRY      prefix for the DTK images, e.g. "registry.example.com/" for a mirror
#   HEALTH_TIMEOUT_SECONDS   how long to wait for a service to turn healthy (default 300)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$ROOT/deploy"
BUILD="$ROOT/.build"
CACHE="$BUILD/cache"
HUB_ENV="$DEPLOY/.env"
DTK_ENV="$DEPLOY/dtk.env"
DTK_ADMIN_FILE="$DEPLOY/dtk-admin.txt"
DTK_ADMIN_USER="admin"
DTK_GATEWAY_KEY_NAME="media-hub-gateway"
# identity:manage lets the gateway import QR-bound Douyin accounts into DTK's identity pool.
DTK_GATEWAY_SCOPES='["douyin:read","media:read","media:write","identity:manage"]'
DTK_GATEWAY_RATE_LIMIT=6000
DTK_INTERNAL_URL="http://dtk-api:8000"
# Distinct names so the stacks never collide with other compose projects on the host.
HUB_PROJECT="${HUB_PROJECT:-mediahub}"
DTK_PROJECT="${DTK_PROJECT:-mediahub-dtk}"
export HUB_SHARED_NETWORK="${HUB_SHARED_NETWORK:-mediahub-shared}"
GENERATE_MARKER="__GENERATE__"
DTK_APP_IMAGE="media-hub/dtk-app"
GITHUB_CODELOAD="${GITHUB_CODELOAD:-https://codeload.github.com}"
DTK_REGISTRY="${DTK_REGISTRY:-}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-300}"
DOWNLOAD_ATTEMPTS=5
DOWNLOAD_RETRY_DELAY_SECONDS=3

# shellcheck source=SCRIPTDIR/versions.env
source "$DEPLOY/versions.env"

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*" >&2; }
die() {
    printf '\033[1;31m[deploy] %s\033[0m\n' "$*" >&2
    exit 1
}

hub_compose() { docker compose -p "$HUB_PROJECT" -f "$DEPLOY/compose.yml" "$@"; }
dtk_compose() {
    docker compose -p "$DTK_PROJECT" -f "$BUILD/dtk/docker/compose.yml" -f "$DEPLOY/dtk.override.yml" \
        --profile downloader "$@"
}

env_value() {
    [[ -f "$HUB_ENV" ]] || return 0
    sed -n "s/^$1=//p" "$HUB_ENV" | tail -n 1
}

set_env_value() {
    local key="$1" value="$2"
    if grep -q "^$key=" "$HUB_ENV"; then
        sed -i "s|^$key=.*|$key=$value|" "$HUB_ENV"
    else
        printf '%s=%s\n' "$key" "$value" >>"$HUB_ENV"
    fi
}

# Print data.<field> of a DTK response envelope read from stdin.
json_field() {
    python3 -c 'import json, sys; print(json.load(sys.stdin)["data"][sys.argv[1]])' "$1"
}

dtk_console() { printf 'http://127.0.0.1:%s' "$DTK_BIND_PORT"; }

# dtk_admin_login JAR: a console session for the administrator in deploy/dtk-admin.txt.
dtk_admin_login() {
    local password
    password=$(sed -n 's/^password=//p' "$DTK_ADMIN_FILE")
    curl -fsS -c "$1" -X POST "$(dtk_console)/api/v1/auth/login" -H 'Content-Type: application/json' \
        -d "{\"username\":\"$DTK_ADMIN_USER\",\"password\":\"$password\"}" >/dev/null
}

# issue_gateway_key JAR: print a new gateway key (shown once by DTK).
issue_gateway_key() {
    curl -fsS -b "$1" -X POST "$(dtk_console)/api/v1/admin/api-keys" -H 'Content-Type: application/json' \
        -d "{\"name\":\"$DTK_GATEWAY_KEY_NAME\",\"scopes\":$DTK_GATEWAY_SCOPES,\"rate_limit\":$DTK_GATEWAY_RATE_LIMIT}" |
        json_field key
}

# Values DTK's compose file interpolates; they are not read from its env_file.
export_dtk_vars() {
    local maxmemory console_port
    maxmemory=$(env_value DTK_REDIS_MAXMEMORY)
    console_port=$(env_value DTK_CONSOLE_PORT)
    export DTK_IMAGE="$DTK_APP_IMAGE"
    export DTK_IMAGE_TAG="$DTK_VERSION"
    export DTK_DOWNLOADER_IMAGE="${DTK_REGISTRY}${DTK_DOWNLOADER_BASE_IMAGE}"
    export DTK_REDIS_MAXMEMORY="${maxmemory:-160mb}"
    # The console listens on loopback only; reach it through an SSH tunnel.
    export DTK_BIND_HOST=127.0.0.1
    export DTK_BIND_PORT="${console_port:-8000}"
}

require_tools() {
    local tool
    for tool in docker curl openssl tar sed; do
        command -v "$tool" >/dev/null || die "$tool is required"
    done
    docker compose version >/dev/null 2>&1 || die "the docker compose v2 plugin is missing"
    docker info >/dev/null 2>&1 || die "cannot reach the docker daemon (run as root or join the docker group)"
}

warn_resources() {
    local mem_kb swap_kb
    mem_kb=$(awk '/^MemTotal/ {print $2}' /proc/meminfo)
    swap_kb=$(awk '/^SwapTotal/ {print $2}' /proc/meminfo)
    if ((mem_kb < 7000000 && swap_kb == 0)); then
        log "WARNING: $((mem_kb / 1024)) MiB RAM and no swap; add a 4G swap file before publishing (README)"
    fi
}

# Retried here rather than with curl's --retry-all-errors, which older distributions lack.
# A stalled transfer is aborted (under 1 KB/s for 60 s) instead of waited on forever.
download() {
    local url="$1" target="$2" attempt
    for ((attempt = 1; attempt <= DOWNLOAD_ATTEMPTS; attempt++)); do
        if curl -fsSL --connect-timeout 20 --speed-limit 1024 --speed-time 60 "$url" -o "$target.part"; then
            mv "$target.part" "$target"
            return 0
        fi
        log "download attempt $attempt/$DOWNLOAD_ATTEMPTS failed: $url"
        sleep "$DOWNLOAD_RETRY_DELAY_SECONDS"
    done
    rm -f "$target.part"
    die "could not download $url"
}

# Download NAME from REPO at COMMIT once, then unpack a fresh tree on every run.
fetch_source() {
    local name="$1" repo="$2" commit="$3"
    local archive="$CACHE/$name-$commit.tar.gz" dest="$BUILD/$name"
    mkdir -p "$CACHE"
    if [[ ! -s "$archive" ]]; then
        log "downloading $repo@${commit:0:12}"
        download "$GITHUB_CODELOAD/$repo/tar.gz/$commit" "$archive"
    fi
    rm -rf "$dest"
    mkdir -p "$dest"
    tar -xzf "$archive" --strip-components=1 -C "$dest"
}

apply_patches() {
    local patch rc
    for patch in "$ROOT"/patches/dtk/*.patch; do
        [[ -e "$patch" ]] || continue
        log "applying $(basename "$patch")"
        if command -v patch >/dev/null; then
            # --forward returns non-zero when hunks are already applied; that is
            # harmless (the source is already in the desired state).
            rc=0
            patch -d "$BUILD/dtk" -p1 --forward --quiet <"$patch" || rc=$?
            if ((rc != 0)); then
                # Verify the patch is truly already applied (dry-run reverse succeeds).
                if patch -d "$BUILD/dtk" -p1 --reverse --dry-run --quiet <"$patch" >/dev/null 2>&1; then
                    log "patch already applied, skipping"
                else
                    die "patch failed: $(basename "$patch")"
                fi
            fi
        else
            GIT_CEILING_DIRECTORIES="$BUILD" git -C "$BUILD/dtk" apply "$patch"
        fi
    done
}

fill_generated_secrets() {
    local file="$1"
    while grep -q "$GENERATE_MARKER" "$file"; do
        sed -i "0,/$GENERATE_MARKER/s//$(openssl rand -hex 24)/" "$file"
    done
}

write_dtk_env() {
    local pg redis
    pg=$(openssl rand -hex 24)
    redis=$(openssl rand -hex 24)
    cat >"$DTK_ENV" <<EOF
DTK_SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n')
POSTGRES_PASSWORD=$pg
REDIS_PASSWORD=$redis
DTK_DATABASE_URL=postgresql+asyncpg://dtk:$pg@postgres:5432/dtk
DTK_REDIS_URL=redis://:$redis@redis:6379/0
DTK_LOG_LEVEL=info
DTK_LOG_JSON=true
DTK_BROWSER_RPC_URL=
DTK_DOWNLOADER_URL=http://downloader:9100
DTK_DOWNLOADER_TOKEN=$(openssl rand -hex 24)
DTK_DOWNLOADER_WORKERS=2
DTK_DOWNLOADER_ITEM_WORKERS=2
EOF
}

ensure_env_files() {
    if [[ ! -f "$HUB_ENV" ]]; then
        log "writing deploy/.env from env.example"
        (umask 077 && cp "$DEPLOY/env.example" "$HUB_ENV")
    fi
    # Also covers a deploy/.env prepared by hand from env.example.
    fill_generated_secrets "$HUB_ENV"
    if [[ ! -f "$DTK_ENV" ]]; then
        log "writing deploy/dtk.env with fresh secrets (back it up: it decrypts DTK's cookie store)"
        (umask 077 && write_dtk_env)
    fi
    chmod 600 "$HUB_ENV" "$DTK_ENV"
    ln -sfn "$DTK_ENV" "$BUILD/dtk/.env"
}

ensure_network() {
    docker network inspect "$HUB_SHARED_NETWORK" >/dev/null 2>&1 ||
        docker network create "$HUB_SHARED_NETWORK" >/dev/null
}

build_dtk_image() {
    log "building $DTK_APP_IMAGE:$DTK_VERSION (DTK $DTK_VERSION + patches/dtk)"
    docker build --pull \
        --build-arg "DTK_BASE_IMAGE=${DTK_REGISTRY}${DTK_BASE_IMAGE}:${DTK_VERSION}" \
        -t "$DTK_APP_IMAGE:$DTK_VERSION" \
        -f "$DEPLOY/dtk/Dockerfile" "$BUILD/dtk"
}

# First run only: create DTK's console administrator and issue the gateway's DTK key.
# Every secret stays on this host: the admin login in deploy/dtk-admin.txt, the key in deploy/.env.
bootstrap_dtk() {
    [[ -z "$(env_value HUB_DTK_API_KEY)" ]] || return 0
    command -v python3 >/dev/null || die "python3 is required to bootstrap DTK"
    local console initialized token password jar key
    console=$(dtk_console)
    initialized=$(curl -fsS "$console/api/setup/status" | json_field initialized)
    if [[ "$initialized" == "False" ]]; then
        log "creating the DTK console administrator (login saved to deploy/dtk-admin.txt)"
        # shellcheck disable=SC2016  # expanded by the shell inside the redis container
        token=$(dtk_compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning GET setup:token')
        [[ -n "$token" ]] || die "DTK's setup token expired; run: docker compose -p $DTK_PROJECT restart api, then retry"
        password=$(openssl rand -hex 16)
        (umask 077 && printf 'url=%s\nusername=%s\npassword=%s\n' "$console" "$DTK_ADMIN_USER" "$password" >"$DTK_ADMIN_FILE")
        curl -fsS -X POST "$console/api/setup/init" -H 'Content-Type: application/json' \
            -d "{\"token\":\"$token\",\"username\":\"$DTK_ADMIN_USER\",\"password\":\"$password\"}" >/dev/null
    else
        [[ -f "$DTK_ADMIN_FILE" ]] || die "DTK is already initialized; set HUB_DTK_API_KEY in deploy/.env by hand"
    fi
    jar=$(mktemp)
    dtk_admin_login "$jar"
    key=$(issue_gateway_key "$jar")
    rm -f "$jar"
    set_env_value HUB_DTK_BASE_URL "$DTK_INTERNAL_URL"
    set_env_value HUB_DTK_API_KEY "$key"
    log "issued the gateway's DTK API key into deploy/.env"
}

# Keys never gain scopes, so a gateway key issued before identity:manage was needed is
# replaced: the new key goes into deploy/.env here, and the old one is revoked by
# revoke_replaced_gateway_key once the gateway runs on the new one.
REPLACED_KEY_PREFIX=""
ensure_gateway_key_scopes() {
    local key status jar
    key=$(env_value HUB_DTK_API_KEY)
    [[ -n "$key" ]] || return 0
    # A dry-run import stores nothing: 200 means the key may import, 403 means it lacks the scope.
    status=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$(dtk_console)/api/v1/admin/identities/import" \
        -H "X-API-Key: $key" -H 'Content-Type: application/json' \
        -d '{"platform":"douyin","cookies":"ttwid=scope-probe","dry_run":true}')
    [[ "$status" == 403 ]] || return 0
    [[ -f "$DTK_ADMIN_FILE" ]] ||
        die "the gateway's DTK key lacks identity:manage; issue one with $DTK_GATEWAY_SCOPES and put it in deploy/.env"
    log "replacing the gateway's DTK key with one that may import identities"
    jar=$(mktemp)
    dtk_admin_login "$jar"
    set_env_value HUB_DTK_API_KEY "$(issue_gateway_key "$jar")"
    rm -f "$jar"
    REPLACED_KEY_PREFIX=$(sed -n 's/^dtk_\([0-9a-f]*\)_.*/\1/p' <<<"$key")
}

revoke_replaced_gateway_key() {
    [[ -n "$REPLACED_KEY_PREFIX" ]] || return 0
    local jar id
    jar=$(mktemp)
    dtk_admin_login "$jar"
    id=$(curl -fsS -b "$jar" "$(dtk_console)/api/v1/admin/api-keys" | python3 -c '
import json, sys
keys = json.load(sys.stdin)["data"]
print(next((k["id"] for k in keys if k["prefix"] == sys.argv[1] and k["status"] == "active"), ""))
' "$REPLACED_KEY_PREFIX")
    if [[ -n "$id" ]]; then
        curl -fsS -b "$jar" -X DELETE "$(dtk_console)/api/v1/admin/api-keys/$id" >/dev/null
        log "revoked the replaced gateway DTK key ($REPLACED_KEY_PREFIX...)"
    fi
    rm -f "$jar"
}

# wait_healthy COMPOSE_FUNCTION SERVICE
wait_healthy() {
    local compose="$1" service="$2" id status
    local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
    log "waiting for $service"
    while ((SECONDS < deadline)); do
        id=$("$compose" ps -q "$service" 2>/dev/null || true)
        if [[ -n "$id" ]]; then
            status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id")
            [[ "$status" == healthy ]] && return 0
        fi
        sleep 3
    done
    die "$service is not healthy after ${HEALTH_TIMEOUT_SECONDS}s; inspect with: deploy/deploy.sh logs $service"
}

next_steps() {
    local port
    port=$(env_value HUB_BIND_PORT)
    cat >&2 <<EOF

media-hub is up. Gateway: http://<server-ip>:${port:-8080}  (readiness: /readyz)

Remaining one-time steps:
 1. Open the DTK console through an SSH tunnel (it only listens on loopback) and log in
    with the account in deploy/dtk-admin.txt:
      ssh -L ${DTK_BIND_PORT}:127.0.0.1:${DTK_BIND_PORT} <user>@<server>   then browse http://127.0.0.1:${DTK_BIND_PORT}
 2. Douyin data endpoints answer UPSTREAM_UNAVAILABLE until DTK's identity pool has an
    identity. Binding a Douyin account by QR code (step 4) imports its login automatically;
    a cookie can also be imported by hand in the console.
 3. Issue the key your product backend will use (the key is printed once):
      deploy/deploy.sh tenant <tenant-name> <backend-egress-ip>/32
 4. Bind publishing accounts: POST /api/v1/accounts/login-sessions (QR code, polled), see docs/API.md.
EOF
}

cmd_up() {
    require_tools
    warn_resources
    fetch_source dtk "$DTK_REPO" "$DTK_COMMIT"
    apply_patches
    fetch_source sau "$SAU_REPO" "$SAU_COMMIT"
    ensure_env_files
    ensure_network
    export_dtk_vars
    build_dtk_image

    log "starting DTK: postgres, redis, api, worker, downloader"
    dtk_compose pull postgres redis downloader
    dtk_compose up -d --no-build --remove-orphans
    wait_healthy dtk_compose api
    bootstrap_dtk
    ensure_gateway_key_scopes

    export SAU_COMMIT
    local profiles=()
    if [[ -n "$(env_value HUB_DOMAIN)" ]]; then
        profiles=(--profile tls)
    fi
    log "building and starting media-hub: gateway, worker, sau, postgres, redis"
    hub_compose "${profiles[@]}" up -d --build --remove-orphans
    wait_healthy hub_compose gateway
    wait_healthy hub_compose sau
    revoke_replaced_gateway_key
    next_steps
}

dtk_deployed() { [[ -f "$BUILD/dtk/docker/compose.yml" ]]; }

cmd_status() {
    export_dtk_vars
    hub_compose ps
    if dtk_deployed; then
        dtk_compose ps
    fi
}

# The key is shown once, so it is written to a file only root can read instead of the terminal.
cmd_tenant() {
    (($# >= 1)) || die "usage: deploy/deploy.sh tenant NAME [CIDR...]"
    local name="$1" cidr
    shift
    local args=(media-hub bootstrap --tenant "$name")
    for cidr in "$@"; do
        args+=(--ip "$cidr")
    done
    local output="$DEPLOY/tenant-${name//[^A-Za-z0-9._-]/_}.json"
    # stdin closed: `compose run` would otherwise swallow whatever is piped into this script.
    (umask 077 && hub_compose run --rm --no-deps -T migrate "${args[@]}" <"/dev/null" >"$output")
    log "API key issued; it is in ${output#"$ROOT"/} (view with: cat ${output#"$ROOT"/})"
}

# A guest jar from SAU's browser, imported by the gateway's CLI: the hot board, details and
# downloads work before anyone binds an account. Search needs a logged-in identity, which
# every QR-bound Douyin account adds by itself (HUB_DATA_IDENTITY_FROM_LOGINS).
cmd_guest_identity() {
    log "opening www.douyin.com in SAU's browser for a guest identity"
    hub_compose exec -T sau python /usr/local/share/sau-patches/guest-jar.py |
        hub_compose exec -T gateway media-hub import-jar -
}

cmd_down() {
    export_dtk_vars
    hub_compose --profile tls down
    if dtk_deployed; then
        dtk_compose down
    fi
}

main() {
    local command="${1:-up}"
    if (($# > 0)); then
        shift
    fi
    case "$command" in
        up) cmd_up ;;
        status) cmd_status ;;
        logs) hub_compose logs -f --tail=200 "$@" ;;
        tenant) cmd_tenant "$@" ;;
        guest-identity) cmd_guest_identity ;;
        down) cmd_down ;;
        *)
            sed -n '2,14p' "${BASH_SOURCE[0]}" >&2
            exit 64
            ;;
    esac
}

main "$@"
