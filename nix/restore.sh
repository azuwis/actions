#!/usr/bin/env bash
set -eo pipefail

cache_paths=(/nix/store /nix/var/nix/db /nix/var/nix/gcroots /nix/var/nix/profiles ~/.cache/nix ~/.local/state/nix ~/.nix-channels ~/.nix-defexpr)
parent_paths=(/nix /nix/var/nix)

init_nix() {
  echo "Rename cache paths back"
  for path in "${cache_paths[@]}"; do
    if [ -e "$path.bak" ]; then
      sudo mv -v "$path.bak" "$path"
    fi
  done

  echo "Mark cache need update"
  echo "CACHE_NEED_UPDATE=yes" >>"$GITHUB_ENV"
}

pre() {
  INSTALL_NIX_CLI_PATH=$(readlink -f "$(command -v nix)")
  echo "Install nix cli path: $INSTALL_NIX_CLI_PATH"
  echo "INSTALL_NIX_CLI_PATH=$INSTALL_NIX_CLI_PATH" >>"$GITHUB_ENV"

  if [ -e /nix/var/nix/daemon-socket ]; then
    echo "Multi-user Nix installed"

    echo "Stop nix-daemon"
    case "$RUNNER_OS" in
    Linux) sudo systemctl stop nix-daemon || true ;;
    macOS) sudo launchctl unload /Library/LaunchDaemons/org.nixos.nix-daemon.plist || true ;;
    esac

    echo "Make the parent dir of cache_paths owner to $USER, for actions/cache/restore to have permissions"
    echo "chown $USER ${parent_paths[*]}"
    sudo chown "$USER" "${parent_paths[@]}"
  fi

  echo "Rename cache paths"
  for path in "${cache_paths[@]}"; do
    if [ -e "$path" ]; then
      sudo mv -v "$path" "$path.bak"
    fi
  done

  echo "CACHE_KEY=$CACHE_KEY" >>"$GITHUB_ENV"
  echo "CACHE_TIMESTAMP=$(date +%Y%m%d%H%M%S)" >>"$GITHUB_ENV"
}

post() {
  if [ -e /nix/store ]; then
    echo "Cache hit"
    echo "Install nix cli path: $INSTALL_NIX_CLI_PATH"
    restore_nix_cli_path=$(readlink -f "$(command -v nix)")
    echo "Restore nix cli path: $restore_nix_cli_path"
    if [ "$INSTALL_NIX_CLI_PATH" = "$restore_nix_cli_path" ]; then
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

  if [ -e /nix/var/nix/daemon-socket ]; then
    echo "Multi-user Nix installed"

    echo "Make the parent dir of cache_paths owner back to root, or nix-daemon will complain about permission problems"
    echo "chown root ${parent_paths[*]}"
    sudo chown root "${parent_paths[@]}"

    echo "Start nix-daemon"
    case "$RUNNER_OS" in
    Linux) sudo systemctl start nix-daemon || true ;;
    macOS)
      echo "Enable 'sandbox = relaxed' on macOS, so preinstalled apps will not affect builds"
      echo "sandbox = relaxed" | sudo tee -a /etc/nix/nix.conf
      sudo launchctl load -w /Library/LaunchDaemons/org.nixos.nix-daemon.plist || true
      ;;
    esac
  fi

  if [ -e flake.nix ] && [ "$USE_NIXPKGS_IN_FLAKE" = true ]; then
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
}

"$@"
