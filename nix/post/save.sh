#!/usr/bin/env bash
set -eo pipefail

pre() {
  # Create gcroot for flake input nixpkgs to prevent gc,
  # ignore other inputs because flake inputs are lazy.
  if [ -f flake.nix ]
  then
    if store_path=$(nix flake archive --json --dry-run | jq -r '.inputs.nixpkgs.path')
    then
      nix build --out-link "/tmp/${store_path##*/}" "$store_path"
    fi
  fi

  nix-collect-garbage -d
  nix-store --optimise
  nix-store --gc --print-roots | grep -v -F -e '/proc/' -e '{lsof}'

  mv -v /nix/var/gcroots /nix/var/gcroots-old || true
  nix-store --gc --print-roots | grep -v -F -e '/proc/' -e '{lsof}' | awk '{print $3}' | sort > /nix/var/gcroots

  if [ -f /nix/var/gcroots-old ]
  then
    if diff -u /nix/var/gcroots-old /nix/var/gcroots
    then
      echo "Gcroots are the same"
    else
      echo "Gcroots are different, mark cache need update"
      echo "CACHE_NEED_UPDATE=yes" >> "$GITHUB_ENV"
    fi
  else
    echo "Gcroots file does not exist, mark cache need update"
    echo "CACHE_NEED_UPDATE=yes" >> "$GITHUB_ENV"
  fi
}

"$@"
