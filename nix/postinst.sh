#!/usr/bin/env bash
set -eo pipefail

case "$RUNNER_OS" in
macOS)
  # Disable fseventsd on /nix volume
  sudo mkdir -p /nix/.fseventsd
  sudo touch /nix/.fseventsd/no_log
  sudo pkill -9 fseventsd || true
  # Hide /nix from Finder and Spotlight
  sudo touch /nix/.metadata_never_index
  sudo chflags hidden /nix
  ;;
esac

if [ -e flake.lock ] && [ "$USE_NIXPKGS_IN_FLAKE" = true ]; then
  nixpkgs=$(jq -r '.nodes.nixpkgs.locked | "\(.type):\(.owner)/\(.repo)/\(.rev)"' flake.lock)
  if [ "$nixpkgs" != "null:null/null/null" ]; then
    echo "Setup nixpkgs in flake.nix to nix registry and NIX_PATH: $nixpkgs"
    outpath=$(nix flake archive --json "$nixpkgs" | jq -r '.path')
    nix registry add nixpkgs "$outpath"
    echo "NIX_PATH=nixpkgs=flake:nixpkgs" >>"$GITHUB_ENV"
  else
    echo "Failed to get nixpkgs in flake.nix"
  fi
elif [ -n "$NIXPKGS_URL" ]; then
  if [ "$(nix-channel --list | awk '/^nixpkgs / {print $2}')" = "$NIXPKGS_URL" ]; then
    echo "Skip setup nix-channel, use cached nixpkgs $NIXPKGS_URL"
  else
    echo "Setup nix-channel nixpkgs to $NIXPKGS_URL"
    nix-channel --add "$NIXPKGS_URL" nixpkgs
    nix-channel --update
  fi
fi

echo "Setup ~/.nix-defexpr for nix-env"
mkdir -p ~/.nix-defexpr
echo 'import <nixpkgs>' >~/.nix-defexpr/default.nix
