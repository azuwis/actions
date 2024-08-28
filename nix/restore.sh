#!/usr/bin/env bash
set -eo pipefail

cache_paths=(/nix/store /nix/var/nix/db/db.sqlite /nix/var/nix/gcroots /nix/var/nix/profiles ~/.cache/nix ~/.local/state/nix)

init_nix() {
  list "init_nix begin"

  echo "Rename cache paths back"
  for path in "${cache_paths[@]}"; do
    if [ -e "$path.bak" ]; then
      sudo mv -v "$path.bak" "$path"
    fi
  done

  list "init_nix end"

  echo "Mark cache need update"
  echo "CACHE_NEED_UPDATE=yes" >>"$GITHUB_ENV"
}

list() {
  echo "::group::List files $1"
  sudo ls -ld /nix
  sudo ls -l /nix
  sudo ls -lR /nix/var
  echo "::endgroup::"
}

pre() {
  echo "::group::Try stop nix-daemon"
  case "$RUNNER_OS" in
  Linux) sudo systemctl stop nix-daemon || true ;;
  macOS) sudo launchctl unload /Library/LaunchDaemons/org.nixos.nix-daemon.plist || true ;;
  esac
  echo "::endgroup::"

  list "pre begin"

  # Make the parent dir of cache_paths writable to $USER,
  # so actions/cache/restore have permission
  sudo chown "$USER" /nix /nix/var/nix /nix/var/nix/db
  echo "Rename cache paths"
  for path in "${cache_paths[@]}"; do
    if [ -e "$path" ]; then
      sudo mv -v "$path" "$path.bak"
    fi
  done

  list "pre end"

  echo "::endgroup::"
  echo "CACHE_KEY=$CACHE_KEY" >>"$GITHUB_ENV"
  echo "CACHE_TIMESTAMP=$(date +%Y%m%d%H%M%S)" >>"$GITHUB_ENV"
}

post() {
  list "post begin"
  if [ -e /nix/store ]; then
    echo "Cache hit"
    if nix --version; then
      echo "Restore succeed"
    else
      echo "Restore failed, discard cache"
      for path in "${cache_paths[@]}"; do
        if [ -e "$path" ]; then
          sudo mv -v "$path" "$path.failed"
        fi
      done
      init_nix
    fi
  else
    echo "Cache miss"
    init_nix
  fi

  list "post middle"

  echo "::group::Try start nix-daemon"
  case "$RUNNER_OS" in
  Linux) sudo systemctl start nix-daemon || true ;;
  macOS) sudo launchctl load -w /Library/LaunchDaemons/org.nixos.nix-daemon.plist || true ;;
  esac
  echo "::endgroup::"

  list "post end"

  if [ -e flake.nix ] && [ "$USE_NIXPKGS_IN_FLAKE" = true ]; then
    nixpkgs=$(jq -r '.nodes.nixpkgs.locked | "\(.type):\(.owner)/\(.repo)/\(.rev)"' flake.lock)
    echo "Use nixpkgs in flake.nix: $nixpkgs"
    outpath=$(nix flake archive --json "$nixpkgs" | jq -r '.path')
    nix registry add nixpkgs "$outpath"
    echo "NIX_PATH=nixpkgs=flake:nixpkgs" >>"$GITHUB_ENV"
  elif [ -n "$NIXPKGS_URL" ]; then
    echo "Setup nix-channel"
    nix store ping --store daemon
    nix-channel --add "$NIXPKGS_URL" nixpkgs
    nix-channel --update
  fi
}

"$@"
