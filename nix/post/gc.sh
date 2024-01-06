#!/usr/bin/env bash
set -eo pipefail

if [ -f flake.nix ]
then
  # create gcroots for flake inputs to prevent gc
  flake_inputs=$(nix flake archive --json --no-write-lock-file)
  while [[ $flake_inputs =~ /nix/store/[^\"]+ ]]
  do
    store_path="${BASH_REMATCH[0]}"
    nix build --out-link "/tmp/${store_path##*/}" "$store_path"
    flake_inputs="${flake_inputs/${store_path}/}"
  done
fi

nix-store --gc
nix-store --optimise
nix-store --gc --print-roots
