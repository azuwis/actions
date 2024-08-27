#!/usr/bin/env bash
set -eo pipefail

cache_paths=(/nix/store /nix/var/nix/db/db.sqlite /nix/var/nix/gcroots /nix/var/nix/profiles ~/.cache/nix ~/.local/state/nix)

init_nix() {
  echo "Rename cache paths back"
  for path in "${cache_paths[@]}"; do
    if [ -e "$path.bak" ]; then
      mv -v "$path.bak" "$path"
    fi
  done

  echo "Mark cache need update"
  echo "CACHE_NEED_UPDATE=yes" >>"$GITHUB_ENV"
}

pre() {
  echo "Rename cache paths"
  for path in "${cache_paths[@]}"; do
    if [ -e "$path" ]; then
      mv -v "$path" "$path.bak"
    fi
  done

  echo "CACHE_KEY=$CACHE_KEY" >>"$GITHUB_ENV"
  echo "CACHE_TIMESTAMP=$(date +%Y%m%d%H%M%S)" >>"$GITHUB_ENV"
}

post() {
  if [ -e /nix/store ]; then
    echo "Cache hit"
    if nix --version; then
      echo "Restore succeed"
    else
      echo "Restore failed, discard cache"
      mkdir /tmp/nix-restore-post
      for path in "${cache_paths[@]}"; do
        if [ -e "$path" ]; then
          mv -v "$path" "/tmp/nix-restore-post/${path//\//_}"
        fi
      done
      init_nix
    fi
  else
    echo "Cache miss"
    init_nix
  fi

  if [ -e flake.nix ] && [ "$USE_NIXPKGS_IN_FLAKE" = true ]; then
    nixpkgs=$(jq -r '.nodes.nixpkgs.locked | "\(.type):\(.owner)/\(.repo)/\(.rev)"' flake.lock)
    echo "Use nixpkgs in flake.nix: $nixpkgs"
    outpath=$(nix flake archive --json "$nixpkgs" | jq -r '.path')
    nix registry add nixpkgs "$outpath"
    echo "NIX_PATH=nixpkgs=flake:nixpkgs" >>"$GITHUB_ENV"
  elif [ -n "$NIXPKGS_URL" ]; then
    echo "Setup nix-channel"
    nix-channel --add "$NIXPKGS_URL" nixpkgs
    nix-channel --update
  fi
}

"$@"
