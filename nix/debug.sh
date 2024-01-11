#!/usr/bin/env bash
set -eo pipefail

mkdir -p ~/.ssh
chmod 700 ~/.ssh
curl --no-progress-meter --fail --location --output ~/.ssh/authorized_keys "https://github.com/$GITHUB_ACTOR.keys"
# Save env vars for sshd
printenv > ~/.ssh/environment
chmod 600 ~/.ssh/environment

ssh-keygen -q -t ecdsa -f ~/.ssh/ssh_host_ecdsa_key -N ''
cat >~/.ssh/sshd_config <<EOF
AcceptEnv LANG LC_*
AllowUsers $USER
HostKey $HOME/.ssh/ssh_host_ecdsa_key
KbdInteractiveAuthentication no
PasswordAuthentication no
PermitRootLogin no
PermitUserEnvironment yes
Port 3456
PrintMotd no
EOF
if [ -f /etc/ssh/sshd_config ]
then
  grep '^Subsystem' /etc/ssh/sshd_config >> ~/.ssh/sshd_config
fi
/usr/sbin/sshd -f ~/.ssh/sshd_config

nix-env -f '<nixpkgs>' -iA cloudflared
cloudflared tunnel --no-autoupdate --url tcp://127.0.0.1:3456 >& /tmp/cloudflared.log &

url=$(until grep -o -m1 '[a-z-]*\.trycloudflare\.com' /tmp/cloudflared.log; do sleep 2; done)
cat /tmp/cloudflared.log
echo
echo "$url"

if [ "$1" = "nopause" ]
then
  echo "Nopause, exit 0"
  exit
fi

until [ -f ~/continue ] || [ -f ~/skip ]
do
  sleep 10
  echo "$url"
done

if [ -f ~/skip ]
then
  echo "Skip, exit 1"
  exit 1
fi
