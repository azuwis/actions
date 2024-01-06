#!/usr/bin/env bash
set -eo pipefail

if [ -z "$TTYD_CREDENTIAL" ] && [ "$TTYD_LOGIN_WITH_AGE" != "true" ]
then
  echo "TTYD_CREDENTIAL is empty and TTYD_LOGIN_WITH_AGE not enabled, skip debug"
  exit
fi

ttyd_args=()

if [ -n "$TTYD_CREDENTIAL" ]
then
  ttyd_args=(--credential "$TTYD_CREDENTIAL")
fi

if [ "$TTYD_LOGIN_WITH_AGE" = "true" ]
then
  mkdir -p ~/.ttyd
  curl --no-progress-meter --fail --location --output ~/.ttyd/"$GITHUB_ACTOR.keys" "https://github.com/$GITHUB_ACTOR.keys"
  ttyd_args=("${ttyd_args[@]}" "$GITHUB_ACTION_PATH/login.sh")
else
  ttyd_args=("${ttyd_args[@]}" bash --login)
fi

nix-env -f '<nixpkgs>' -iA age cloudflared ttyd

ttyd --interface 127.0.0.1 --port 3456 --writable --once "${ttyd_args[@]}" &
cloudflared tunnel --url http://127.0.0.1:3456 2>&1 | tee /tmp/cloudflared.log &

until [ -f ~/continue ] || [ -f ~/skip ]
do
  sleep 10
  grep -F -m1 '.trycloudflare.com' /tmp/cloudflared.log || true
done

if [ -f ~/skip ]
then
  echo "Skip, exit 1"
  exit 1
fi
