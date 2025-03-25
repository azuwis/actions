#!/usr/bin/env bash
set -eo pipefail

chown_paths=(/nix/var/nix/db/reserved /nix/var/nix/db/big-lock)

pre() {
  if [ "$CACHE_NEED_UPDATE" = no ]; then
    echo "Skip clean-up, CACHE_NEED_UPDATE is no"
    exit
  fi

  # Create gcroot for flake input nixpkgs to prevent gc,
  # ignore other inputs because flake inputs are lazy.
  if [ -f flake.lock ]; then
    if store_path=$(nix flake archive --json --dry-run | jq -r '.inputs.nixpkgs.path'); then
      rev=$(jq -r '.nodes.nixpkgs.locked.rev' flake.lock)
      nix build --out-link "/tmp/nixpkgs-$rev" "$store_path"
    fi
  fi

  echo "::group::Nix collect garbage"
  if [ -e /nix/var/nix/daemon-socket ]; then
    # nix-collect-garbage not in sudo secure_path
    ncg=$(command -v nix-collect-garbage)
    sudo sh -c "$ncg -d"
  fi
  nix-collect-garbage -d
  nix-store --optimise
  echo "::endgroup::"

  echo "List gcroots:"
  gcroots=$(nix-store --gc --print-roots | grep -v -F -e '/proc/' -e '{lsof}' -e '/profiles/channels-' -e 'flake-registry.json')
  echo "$gcroots"

  echo "Save gcroots:"
  mkdir -p ~/.cache/nix
  mv -v ~/.cache/nix/gcroots ~/.cache/nix/gcroots-old || true
  echo "$gcroots" | awk '{print $3}' | sort -t - -k 2 >~/.cache/nix/gcroots

  if [ -f ~/.cache/nix/gcroots-old ]; then
    echo "Compare gcroots:"
    if diff -u ~/.cache/nix/gcroots-old ~/.cache/nix/gcroots; then
      echo "Gcroots are the same"
    else
      echo "Gcroots are different, mark cache need update"
      echo "CACHE_NEED_UPDATE=yes" >>"$GITHUB_ENV"
    fi
  else
    echo "Gcroots file does not exist, mark cache need update"
    echo "CACHE_NEED_UPDATE=yes" >>"$GITHUB_ENV"
  fi

  if [ -e /nix/var/nix/daemon-socket ]; then
    echo "Multi-user Nix installed, change some files' owner to $USER, for actions/cache/save to have permissions"
    echo "chown $USER ${chown_paths[*]}"
    sudo chown "$USER" "${chown_paths[@]}"
  fi
}

"$@"
