#!/usr/bin/env bash
set -eo pipefail

pre() {
  # Create gcroots for flake inputs to prevent gc, note the flake itself is excluded
  if [ -f flake.nix ]
  then
    nix flake archive --json --no-write-lock-file | jq -r '.inputs | .. | .path? // empty' | while read -r store_path
  do
    nix build --out-link "/tmp/${store_path##*/}" "$store_path"
  done
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
