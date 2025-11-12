#!/usr/bin/env bash
set -eo pipefail

# Setup git user name and email, for commands like `git commit` `git cherry-pick`
git config get user.name || git config --global user.name 'github-actions[bot]'
git config get user.email || git config --global user.email '41898282+github-actions[bot]@users.noreply.github.com'

mkdir -p ~/.ssh
chmod 700 ~/.ssh
curl --no-progress-meter --fail --location --output ~/.ssh/authorized_keys "https://github.com/$GITHUB_ACTOR.keys"
# Save env vars for sshd
printenv >~/.ssh/environment
chmod 600 ~/.ssh/environment

ssh-keygen -q -t ecdsa -f ~/.ssh/ssh_host_ecdsa_key -N ''
cat >~/.ssh/sshd_config <<EOF
AcceptEnv LANG LC_*
AllowUsers $USER
HostKey $HOME/.ssh/ssh_host_ecdsa_key
KbdInteractiveAuthentication no
PasswordAuthentication no
PermitRootLogin no
# PermitUserEnvironment yes
Port 3456
PrintMotd no
EOF
for file in /usr/lib/openssh/sftp-server /usr/libexec/sftp-server; do
  if [ -f "$file" ]; then
    echo "Subsystem sftp $file" >>~/.ssh/sshd_config
    break
  fi
done
/usr/sbin/sshd -f ~/.ssh/sshd_config

nix-env -f '<nixpkgs>' -iA cloudflared tmux

cloudflared tunnel --no-autoupdate --url tcp://127.0.0.1:3456 >&/tmp/cloudflared.log &
url=$(until grep -o -m1 '[a-z-]*\.trycloudflare\.com' /tmp/cloudflared.log; do sleep 2; done)
cat /tmp/cloudflared.log
cat <<'EOF'

Add to ~/.ssh/config:

Host *.trycloudflare.com
    User runner
    ProxyCommand cloudflared access tcp --hostname https://%h
    UserKnownHostsFile /dev/null
    StrictHostKeyChecking accept-new

EOF
echo "$url"

# Add nix-profile to PATH
cat <<'EOF' >>~/.bash_profile

nix_bin_path="$HOME/.nix-profile/bin"
if [ -n "${PATH##*"$nix_bin_path"}" ] && [ -n "${PATH##*"$nix_bin_path":*}" ]
then
  export PATH="${nix_bin_path}:$PATH"
fi

>&2 cat <<EOL

USAGE:
  tmux attach      # Enter the dev environment
  touch ~/continue # Continue the job
  touch ~/skip     # Skip the job
EOL
EOF
export TERMINFO_DIRS="$HOME/.nix-profile/share/terminfo"
tmux new-session -c "$GITHUB_WORKSPACE" -d

if [ "$1" = "nopause" ]; then
  echo "Nopause, exit 0"
  exit
fi

until [ -f ~/continue ] || [ -f ~/skip ]; do
  sleep 10
  echo "$url"
done

if [ -f ~/skip ]; then
  echo "Skip, exit 1"
  exit 1
fi
