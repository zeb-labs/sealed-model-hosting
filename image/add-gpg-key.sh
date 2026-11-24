# KIWI calls this with the build repo file: pin the Amazon Linux 2023 signing key.
echo "gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-amazon-linux-2023" >> "$1"
