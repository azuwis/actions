#!/usr/bin/env bash

case "$RUNNER_OS" in
Linux)
  if [ "$CLEAN" = true ]; then
    echo
    echo "Disk clean, before:"
    df -h -x tmpfs
    sudo rm -rf \
      /etc/skel/.cargo \
      /etc/skel/.dotnet \
      /etc/skel/.rustup \
      /home/runner/.cargo \
      /home/runner/.dotnet \
      /home/runner/.rustup \
      /home/runneradmin/.cargo \
      /home/runneradmin/.dotnet \
      /home/runneradmin/.rustup \
      /opt/az \
      /opt/google \
      /opt/hostedtoolcache \
      /opt/microsoft \
      /opt/pipx \
      /root/.sbt \
      /usr/lib/google-cloud-sdk \
      /usr/lib/jvm \
      /usr/local \
      /usr/share/az_* \
      /usr/share/dotnet \
      /usr/share/miniconda \
      /usr/share/swift
    docker image prune --all --force >/dev/null
    echo
    echo "After:"
    df -h -x tmpfs
  fi
  if [ "$BTRFS" = true ]; then
    sudo touch /btrfs /mnt/btrfs
    sudo fallocate --zero-range --length "$(($(df --block-size=1 --output=avail / | sed -n 2p) - 2147483648))" /btrfs
    sudo fallocate --zero-range --length "$(df --block-size=1 --output=avail /mnt | sed -n 2p)" /mnt/btrfs
    sudo losetup /dev/loop6 /mnt/btrfs
    sudo losetup /dev/loop7 /btrfs
    sudo mkfs.btrfs --data raid0 /dev/loop6 /dev/loop7
    sudo mkdir /nix
    sudo mount -t btrfs -o compress=zstd /dev/loop6 /nix
    sudo chown "${RUNNER_USER}:" /nix
  elif [ "$(findmnt -bno size /mnt)" -gt 20000000000 ]; then
    df -h -x tmpfs
    echo "/mnt is large, bind mount /mnt/nix"
    sudo install -d -o "$RUNNER_USER" /mnt/nix /nix
    sudo mount --bind /mnt/nix /nix
  fi
  ;;
macOS)
  # Disable MDS service on macOS
  sudo launchctl unload -w /System/Library/LaunchDaemons/com.apple.metadata.mds.plist || true
  ;;
esac
