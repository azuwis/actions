#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXCACHE_REPO="$(printf '%s' "$NIXCACHE_REPO" | tr '[:upper:]' '[:lower:]')"
PROXY_PID=""
READY=0

echo "::add-mask::${GITHUB_TOKEN}"

# vendored proxy uses PEP 604 unions => Python >= 3.10
if ! python3 -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null; then
  echo "::error::nix/cache requires Python >= 3.10 (found: $(python3 --version 2>&1 || echo unknown))"
  exit 1
fi

if ! [[ "$NIXCACHE_PORT" =~ ^[1-9][0-9]{0,4}$ ]] || (( NIXCACHE_PORT > 65535 )); then
  echo "::error::nix/cache: invalid port: '$NIXCACHE_PORT' (expected 1-65535)"
  exit 1
fi

INDEX_DIR="$RUNNER_TEMP/nixcache-proxy"
mkdir -p "$INDEX_DIR"
chmod 700 "$INDEX_DIR"
# empty => no upstream fallback (Nix queries cache.nixos.org itself in parallel)
NIXCACHE_UPSTREAM="" \
NIXCACHE_INDEX_DIR="$INDEX_DIR" \
  python3 "$SCRIPT_DIR/nixcache-proxy.py" >"$INDEX_DIR/proxy.log" 2>&1 &
PROXY_PID=$!

if kill -0 "$PROXY_PID" 2>/dev/null \
  && curl -fs --max-time 2 --retry 15 --retry-delay 1 --retry-connrefused \
      -o /dev/null "http://127.0.0.1:$NIXCACHE_PORT/nix-cache-info" 2>/dev/null; then
  READY=1
fi
if [ "$READY" = 1 ]; then
  # 60s cap: cold start blocks on the index prefetch lock
  STATUS_REPO="$(curl -fs --max-time 60 "http://127.0.0.1:$NIXCACHE_PORT/_status" 2>/dev/null | jq -r '.repo // empty' 2>/dev/null || true)"
  if [ -n "$STATUS_REPO" ] && [ "$STATUS_REPO" != "$NIXCACHE_REPO" ]; then
    echo "::warning::proxy identity mismatch (serves repo=$STATUS_REPO, expected=$NIXCACHE_REPO); not configuring substituter"
    READY=0
  elif [ -z "$STATUS_REPO" ]; then
    echo "::warning::proxy status check failed (port $NIXCACHE_PORT)"
    READY=0
  fi
fi
if [ "$READY" != 1 ]; then
  echo "::warning::nix cache proxy not ready; skipping substituter configuration (proxy.log: $INDEX_DIR/proxy.log)"
  if [ -n "$PROXY_PID" ]; then
    kill "$PROXY_PID" 2>/dev/null || true
    PROXY_PID=""
  fi
fi

warn() {
  echo "::warning::$1"
}

if [ "$READY" = 1 ]; then
  BLOCK="extra-substituters = http://127.0.0.1:$NIXCACHE_PORT
extra-trusted-substituters = http://127.0.0.1:$NIXCACHE_PORT"
  if [ -n "$NIXCACHE_PUBLIC_KEY" ]; then
    IDX_KEY="$(curl -fs --max-time 10 "http://127.0.0.1:$NIXCACHE_PORT/public-key" 2>/dev/null | tr -d '\n' || true)"
    if [ -n "$IDX_KEY" ] && [ "$IDX_KEY" != "$NIXCACHE_PUBLIC_KEY" ]; then
      echo "::warning::index advertises a different public key than the public_key input; trusting the local input"
    fi
    BLOCK="$BLOCK
extra-trusted-public-keys = $NIXCACHE_PUBLIC_KEY"
  else
    warn "no public_key input: adding require-sigs = false (disables signature verification for ALL substituters)"
    BLOCK="$BLOCK
require-sigs = false"
  fi

  # portable marker replace: GNU/BSD sed -i.bak, then drop the backup
  apply_config() { # $1 = file, $2 = use sudo (1/0)
    local file="$1"
    local -a sudo_cmd=()
    if [ "$2" = 1 ]; then
      sudo_cmd=(sudo)
    else
      mkdir -p "$(dirname "$file")"
    fi
    "${sudo_cmd[@]}" sed -i.bak '/^# nix-cache begin$/,/^# nix-cache end$/d' "$file" 2>/dev/null || true
    "${sudo_cmd[@]}" rm -f "$file.bak" 2>/dev/null || true
    if ! printf '\n# nix-cache begin\n%s\n# nix-cache end\n' "$BLOCK" | "${sudo_cmd[@]}" tee -a "$file" >/dev/null; then
      warn "failed to write $file (nix.conf marker block)"
    fi
  }

  if [ -e /nix/var/nix/daemon-socket ]; then
    sudo mkdir -p /etc/nix
    [ -e /etc/nix/nix.conf ] || sudo touch /etc/nix/nix.conf
    apply_config /etc/nix/nix.conf 1
  else
    warn "no nix daemon socket found; configuring user-level nix.conf only"
  fi
  apply_config "${HOME}/.config/nix/nix.conf" 0

  if [ -e /nix/var/nix/daemon-socket ]; then
    case "$RUNNER_OS" in
    macOS)
      sudo launchctl unload /Library/LaunchDaemons/org.nixos.nix-daemon.plist 2>/dev/null || true
      sudo launchctl load -w /Library/LaunchDaemons/org.nixos.nix-daemon.plist 2>/dev/null || true
      ;;
    *)
      if ! sudo systemctl restart nix-daemon 2>/dev/null; then
        warn "failed to restart nix-daemon; substituter config may not be effective"
      fi
      ;;
    esac
  fi

  if nix show-config 2>/dev/null | grep -q "127.0.0.1:$NIXCACHE_PORT"; then
    echo "::group::nix/cache"
    echo "OCI substituter configured: http://127.0.0.1:$NIXCACHE_PORT (repo=$NIXCACHE_REPO)"
    [ -n "$NIXCACHE_PUBLIC_KEY" ] || echo "unsigned mode: require-sigs = false"
    echo "::endgroup::"
  else
    warn "nix does not report the cache substituter (port $NIXCACHE_PORT); check nix.conf and daemon restart"
  fi
fi

# state for nix/cache/post
{
  echo "NIXCACHE_REPO=$NIXCACHE_REPO"
  echo "NIXCACHE_PORT=$NIXCACHE_PORT"
  echo "NIXCACHE_PROXY_PID=$PROXY_PID"
} >>"$GITHUB_ENV"
