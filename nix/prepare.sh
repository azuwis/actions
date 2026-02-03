#!/usr/bin/env bash

case "$RUNNER_OS" in
Linux)
  if [ "$CLEAN" = true ]; then
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
  fi
  df -h -x tmpfs
  echo
  disks=()
  disks_free=()
  while read -r free target; do
    disks+=("$target")
    disks_free+=("$free")
  done < <(df --block-size=1 --output=avail,target | sort -rn | awk '$1 ~ /^[0-9]+$/ && $1 > 20*1024*1024*1024 {print $1, $2}')
  if [ "$BTRFS" = true ]; then
    echo "Make /nix BTRFS RAID0 from ${disks[*]}"
    loops=()
    for i in "${!disks[@]}"; do
      sudo touch "${disks[$i]}/btrfs"
      sudo chmod 600 "${disks[$i]}/btrfs"
      sudo fallocate --zero-range --length "$((${disks_free[$i]} - 2 * 1024 * 1024 * 1024))" /btrfs
      sudo losetup "/dev/loop$i" /btrfs
      loops+=("/dev/loop$i")
    done
    sudo mkfs.btrfs --data raid0 "${loops[@]}"
    sudo mkdir /nix
    sudo mount -t btrfs -o compress=zstd /dev/loop0 /nix
    sudo chown "${USER}:" /nix
  elif [ "${disks[0]}" != "/" ]; then
    echo "${disks[0]} is the largest free disk, create ${disks[0]}/nix and bind mount to /nix"
    sudo install -d -o "$USER" "${disks[0]}/nix" /nix
    sudo mount --bind "${disks[0]}/nix" /nix
  fi
  ;;
macOS)
  if [ "$CLEAN" = true ]; then
    echo "Disk clean, before:"
    df -h /
    sudo rm -rf \
      /Applications/Xcode_* \
      /Library/Developer/CoreSimulator \
      /Library/Frameworks \
      /Users/runner/.dotnet \
      /Users/runner/.rustup \
      /Users/runner/Library/Android \
      /Users/runner/Library/Caches \
      /Users/runner/Library/Developer/CoreSimulator \
      /Users/runner/hostedtoolcache
    echo
    echo "After:"
    df -h /
  fi
  # This save about 110G disk space, and take about 0.6s
  sudo rm -rf \
    /Library/Developer/CoreSimulator \
    /Users/runner/Library/Developer/CoreSimulator
  # Disable MDS service on macOS
  sudo mdutil -i off -a || true
  sudo launchctl unload -w /System/Library/LaunchDaemons/com.apple.metadata.mds.plist || true
  ;;
esac
