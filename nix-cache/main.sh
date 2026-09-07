#!/usr/bin/env bash
set -eo pipefail

# GHCR requires lowercase
NIXCACHE_REPO="$(printf '%s' "$NIXCACHE_REPO" | tr '[:upper:]' '[:lower:]')"

cat <<EOF >>"$GITHUB_ENV"
NIXCACHE_REPO=$NIXCACHE_REPO
NIXCACHE_PORT=$NIXCACHE_PORT
EOF

index_dir="$RUNNER_TEMP/nix-cache-proxy"
mkdir -p "$index_dir"

# NIXCACHE_UPSTREAM="" => no upstream fallback (Nix queries cache.nixos.org itself in parallel)
NIXCACHE_INDEX_DIR="$index_dir" NIXCACHE_UPSTREAM="" \
  python3 "$GITHUB_ACTION_PATH/proxy.py" >"$index_dir/proxy.log" 2>&1 &
PROXY_PID=$!

# _status blocks until the index prefetch completes
if ! kill -0 "$PROXY_PID" 2>/dev/null ||
  ! curl -fs --max-time 60 --retry 15 --retry-delay 1 --retry-connrefused \
    -o /dev/null "http://127.0.0.1:$NIXCACHE_PORT/_status"; then
  echo "::warning::Proxy failed to start, skipping substituter configuration"
  exit 0
fi

BLOCK="extra-substituters = http://127.0.0.1:$NIXCACHE_PORT"
if [ -n "$NIXCACHE_PUBLIC_KEY" ]; then
  BLOCK="$BLOCK
extra-trusted-public-keys = $NIXCACHE_PUBLIC_KEY"
else
  echo "::warning::No public_key input, adding require-sigs = false (disables signature verification for ALL substituters)"
  BLOCK="$BLOCK
require-sigs = false"
fi

if [ -e /nix/var/nix/daemon-socket ]; then
  echo "Multi-user Nix installed, appending config to /etc/nix/nix.conf"
  echo "$BLOCK" | sudo tee -a /etc/nix/nix.conf >/dev/null
  echo "Restarting nix-daemon"
  case "$RUNNER_OS" in
  Linux) sudo systemctl restart nix-daemon ;;
  macOS)
    sudo launchctl unload /Library/LaunchDaemons/org.nixos.nix-daemon.plist
    sudo launchctl load -w /Library/LaunchDaemons/org.nixos.nix-daemon.plist
    ;;
  esac
  probe_path=$(readlink -f "$(command -v nix)")
  for _ in {1..30}; do
    nix-store --store daemon --query --hash "$probe_path" >/dev/null 2>&1 && break
    echo "Waiting for nix-daemon"
    sleep 1
  done
else
  echo "Single-user Nix installed, trying /etc/nix/nix.conf first"
  echo "$BLOCK" >>/etc/nix/nix.conf || {
    echo "Appending config to ~/.config/nix/nix.conf instead"
    mkdir -p ~/.config/nix
    echo "$BLOCK" >>~/.config/nix/nix.conf
  }
fi

echo "nix-cache substituter configured: http://127.0.0.1:$NIXCACHE_PORT (repo=$NIXCACHE_REPO)"
