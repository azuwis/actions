#!/usr/bin/env bash
set -eo pipefail

random=$(openssl rand -base64 18)
echo "$random" | age -a -R ~/.ttyd/"$GITHUB_ACTOR.keys"
while true
do
  read -r response
  if [ "$response" = "$random" ]
  then
    exec -l bash
  fi
done
