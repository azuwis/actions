#!/usr/bin/env bash
set -eo pipefail

warn() {
  echo "::warning::$1"
}

# GHCR requires lowercase
NIXCACHE_REPO="$(printf '%s' "$NIXCACHE_REPO" | tr '[:upper:]' '[:lower:]')"

{
  echo "NIXCACHE_REPO=$NIXCACHE_REPO"
  echo "NIXCACHE_PORT=$NIXCACHE_PORT"
} >>"$GITHUB_ENV"

INDEX_DIR="$RUNNER_TEMP/nixcache-proxy"
mkdir -p "$INDEX_DIR"
chmod 700 "$INDEX_DIR"

# NIXCACHE_UPSTREAM="" => No upstream fallback (Nix queries cache.nixos.org itself in parallel)
NIXCACHE_INDEX_DIR="$INDEX_DIR" NIXCACHE_UPSTREAM="" \
  python3 "$GITHUB_ACTION_PATH/nixcache-proxy.py" >"$INDEX_DIR/proxy.log" 2>&1 &
PROXY_PID=$!

if ! kill -0 "$PROXY_PID" 2>/dev/null; then
  warn "Proxy failed to start, skipping substituter configuration"
  exit 0
fi

# _status blocks until the index prefetch completes
if ! curl -fs --max-time 60 --retry 15 --retry-delay 1 --retry-connrefused \
  -o /dev/null "http://127.0.0.1:$NIXCACHE_PORT/_status"; then
  warn "Proxy status check failed, skipping substituter configuration"
  exit 0
fi

BLOCK="extra-substituters = http://127.0.0.1:$NIXCACHE_PORT"
if [ -n "$NIXCACHE_PUBLIC_KEY" ]; then
  BLOCK="$BLOCK
extra-trusted-public-keys = $NIXCACHE_PUBLIC_KEY"
else
  warn "no public_key input: adding require-sigs = false (disables signature verification for ALL substituters)"
  BLOCK="$BLOCK
require-sigs = false"
fi

apply_config() { # $1 = file, $2 = use sudo (1/0)
  local file="$1"
  local -a sudo_cmd=()
  if [ "$2" = 1 ]; then
    sudo_cmd=(sudo)
  else
    mkdir -p "$(dirname "$file")"
  fi
  if ! printf '\n%s\n' "$BLOCK" | "${sudo_cmd[@]}" tee -a "$file" >/dev/null; then
    warn "failed to write $file (nix.conf block)"
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

echo "::group::nix/cache"
echo "OCI substituter configured: http://127.0.0.1:$NIXCACHE_PORT (repo=$NIXCACHE_REPO)"
[ -n "$NIXCACHE_PUBLIC_KEY" ] || echo "unsigned mode: require-sigs = false"
echo "::endgroup::"
