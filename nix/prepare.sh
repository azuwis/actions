#!/usr/bin/env bash

case "$RUNNER_OS" in
  Linux)
    if [ "$(findmnt -bno size /mnt)" -gt 20000000000 ]
    then
      df -h -x tmpfs
      echo "/mnt is large, bind mount /mnt/nix, skip disk clean"
      sudo install -d -o "$RUNNER_USER" /mnt/nix /nix
      sudo mount --bind /mnt/nix /nix
    elif [ "$CLEAN" = true ]
    then
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
    ;;
  macOS)
    # Disable MDS service on macOS
    sudo launchctl unload -w /System/Library/LaunchDaemons/com.apple.metadata.mds.plist || true
    ;;
esac
