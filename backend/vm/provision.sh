#!/bin/bash
# Turns smolvm's stock Ubuntu guest into the platform VM's clean image. Runs once, as
# root, with internet access; the disk it leaves behind is what every boot starts from.
# The image is rebuilt whenever this file changes. An optional argument is a CA bundle
# to trust, for networks that inspect TLS.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# Python's own tools read certifi unless told to use the system store, which holds the
# extra CA as well as the standard roots.
export PIP_CERT=/etc/ssl/certs/ca-certificates.crt
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

apt-get update
apt-get install -y --no-install-recommends \
    bubblewrap build-essential ca-certificates curl git python3 python3-dev \
    libnss3-tools python3-pip python3-venv python-is-python3 tar util-linux

if [ -n "${1:-}" ]; then
    install -D -m 0644 "$1" /usr/local/share/ca-certificates/odysseus-extra.crt
    update-ca-certificates
fi

# The sandbox user runs workspace commands; the browser and search users run the
# platform services, so neither can read a workspace.
for spec in sandbox:1100 browser:1101 searxng:1102; do
    name=${spec%%:*}
    uid=${spec##*:}
    id -u "$name" >/dev/null 2>&1 || useradd --create-home --uid "$uid" --shell /bin/bash "$name"
done
install -d -m 0755 /opt/odysseus /work

# Chromium trusts its own NSS store, not the system one.
if [ -n "${1:-}" ]; then
    nssdb=/home/browser/.pki/nssdb
    install -d -m 0700 "$nssdb"
    certutil -d "sql:$nssdb" -N --empty-password
    certutil -d "sql:$nssdb" -A -n odysseus-extra -t "C,," -i "$1"
    chown -R browser:browser /home/browser/.pki
fi

# Headless Chromium for page rendering, from Playwright's builds.
python3 -m venv /opt/odysseus/playwright
/opt/odysseus/playwright/bin/pip install --no-cache-dir playwright
PLAYWRIGHT_BROWSERS_PATH=/opt/odysseus/browsers \
    /opt/odysseus/playwright/bin/playwright install --with-deps --only-shell chromium
chmod -R a+rX /opt/odysseus/browsers
install -d -m 0755 /opt/odysseus/bin
ln -sf "$(find /opt/odysseus/browsers -type f -name headless_shell | head -n 1)" \
    /opt/odysseus/bin/headless_shell
test -x /opt/odysseus/bin/headless_shell

# SearXNG, from source as its own docs install it.
git clone --depth 1 https://github.com/searxng/searxng.git /opt/odysseus/searxng-src
python3 -m venv /opt/odysseus/searxng
/opt/odysseus/searxng/bin/pip install --no-cache-dir -U pip setuptools wheel pyyaml msgspec
/opt/odysseus/searxng/bin/pip install --no-cache-dir --use-pep517 --no-build-isolation \
    -e /opt/odysseus/searxng-src
chmod -R a+rX /opt/odysseus/searxng /opt/odysseus/searxng-src

apt-get clean
rm -rf /var/lib/apt/lists/*
sync
