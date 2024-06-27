#!/usr/bin/env bash
set -eo pipefail

echo "Before:"
echo
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

sudo docker image prune --all --force

echo
echo "After:"
echo
df -h -x tmpfs
