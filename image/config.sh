# KIWI runs this inside the image root at build time.
. /.kconfig
. /.profile
set -xe

# No login anywhere: lock root, no getty on any console, no rescue shells.
passwd -l root
systemctl mask serial-getty@ttyS0.service getty@tty1.service console-getty.service \
    emergency.service emergency.target rescue.service rescue.target debug-shell.service

# Time is needed for signed AWS calls. Use the link-local Amazon time service
# only, and switch off chrony's command port.
grep -q '^confdir /etc/chrony.d' /etc/chrony.conf || echo 'confdir /etc/chrony.d' >> /etc/chrony.conf

# NVIDIA. The pipeline put two things into the image tree:
#  - userspace packages from Amazon's NVIDIA repository (signatures checked on
#    the builder). Installed without their dependency on the driver build
#    system, so no compiler ends up in the sealed image.
#  - kernel modules compiled on the builder for this kernel and signed with
#    our Secure Boot key. The kernel refuses any other module under Secure Boot.
if [ -d /var/tmp/smh-rpms ]; then
    rpm --import /var/tmp/smh-rpms/keys/*
    if rpm -K /var/tmp/smh-rpms/*.rpm | grep -v "digests signatures OK"; then
        echo "CHECK-IMAGE FAIL: an NVIDIA package has no valid signature"; exit 1
    fi
    rpm -ivh --nodeps /var/tmp/smh-rpms/*.rpm
    rm -rf /var/tmp/smh-rpms
    kver=$(ls /lib/modules | head -1)
    install -d /lib/modules/$kver/extra/nvidia
    install -m 0644 /usr/local/lib/smh/nvidia-modules/*.ko /lib/modules/$kver/extra/nvidia/
    rm -rf /usr/local/lib/smh/nvidia-modules
    depmod -a "$kver"
    systemctl disable nvidia-persistenced.service 2>/dev/null || true
fi

# The only things this image does at boot.
chmod 0700 /usr/local/sbin/smh-boot /usr/local/sbin/smh-report /usr/local/sbin/smh-disk
chmod -R go-rwx /usr/local/lib/smh /etc/smh
mkdir -p /var/lib/smh /var/lib/smh-scratch /var/lib/docker
systemctl enable smh-disk.service docker.service smh-loader.service
# Optional services are on only when the loader settings have their section.
for pair in jobs:smh-jobs.service logs:smh-logship.service lease:smh-lease.service gate:smh-gate.service gate:smh-gatekeys.service gate:nginx.service; do
    section=${pair%%:*}; unit=${pair##*:}
    if python3 -c "import json,sys; sys.exit(0 if '$section' in json.load(open('/etc/smh/loader.json')) else 1)"; then
        touch /etc/smh/$section-enabled
        systemctl enable "$unit"
    fi
done
# The front gate must be exactly our file: four fixed paths, TLS, and one
# included file, the customer's key list.
if [ -f /etc/smh/gate-enabled ]; then
    [ "$(grep -c proxy_pass /etc/nginx/nginx.conf)" = 4 ] \
        && [ "$(grep -c 'include ' /etc/nginx/nginx.conf)" = 1 ] && grep -q 'include /run/smh-gate/keys.map;' /etc/nginx/nginx.conf \
        && grep -q 'listen 443 ssl;' /etc/nginx/nginx.conf && ! grep -q 'listen 80' /etc/nginx/nginx.conf \
        || { echo "CHECK-IMAGE FAIL: unexpected front gate configuration"; exit 1; }
    rm -rf /etc/nginx/conf.d /etc/nginx/default.d
    chmod 0700 /usr/local/sbin/smh-gate
fi

# Refuse to build an image that has any way in.
bash /usr/local/lib/smh/check-image.sh /
