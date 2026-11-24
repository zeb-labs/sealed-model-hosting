#!/bin/bash
# Build check: fail the image build if any way to log in or run remote commands
# exists. KIWI runs this at the end of config.sh, inside the image root.
# "Left the package out" is not proof (the AWS sample shows why), so this
# checks the finished image instead.
#   check-image.sh [root]     root defaults to /
set -uo pipefail
ROOT="${1:-/}"
problems=0
bad() { echo "CHECK-IMAGE FAIL: $*"; problems=$((problems + 1)); }

# 1. Packages that open a door.
FORBIDDEN="openssh-server openssh-clients amazon-ssm-agent cloud-init cloud-init-cfg-ec2
ec2-instance-connect ec2-instance-connect-selinux telnet telnet-server rsh rsh-server
dropbear cockpit cockpit-ws xinetd vsftpd tigervnc-server mosh tmate socat nmap-ncat"
for p in $FORBIDDEN; do
    rpm --root "$ROOT" -q "$p" >/dev/null 2>&1 && bad "package installed: $p"
done

# 2. Login prompts and rescue shells must be masked (a link to /dev/null).
for u in serial-getty@ttyS0.service getty@tty1.service console-getty.service \
         emergency.service rescue.service debug-shell.service; do
    [ "$(readlink "$ROOT/etc/systemd/system/$u" 2>/dev/null)" = "/dev/null" ] || bad "not masked: $u"
done

# 3. No remote-access service may be enabled.
for u in sshd.service sshd.socket amazon-ssm-agent.service cloud-init.service \
         cloud-init-local.service cloud-config.service cloud-final.service; do
    find "$ROOT/etc/systemd/system" -name "$u" -path "*.wants/*" 2>/dev/null | grep -q . && bad "enabled: $u"
done

# 4. No account can log in with a password, and no SSH keys are present.
while IFS=: read -r user hash _; do
    case "$hash" in ""|"*"|"!"*|"x") ;; *) bad "account has a password: $user" ;; esac
    [ -z "$hash" ] && bad "account has an EMPTY password: $user"
done < "$ROOT/etc/shadow"
find "$ROOT/root" "$ROOT/home" -name authorized_keys 2>/dev/null | grep -q . && bad "authorized_keys file present"

if [ "$problems" -gt 0 ]; then
    echo "CHECK-IMAGE: $problems problem(s). Build refused."
    exit 1
fi
echo "CHECK-IMAGE OK: no login or remote-command path found"
