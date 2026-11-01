#!/bin/bash
# Runs as root inside an ubuntu:24.04 builder machine with internet access and /out
# mounted: provisions the builder itself, then packs its root filesystem as a single-layer
# `docker save` archive at /out/image.tar. An optional argument is a CA bundle to trust,
# for networks that inspect TLS.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# Python's own tools read certifi unless told to use the system store, which holds the
# extra CA as well as the standard roots.
export PIP_CERT=/etc/ssl/certs/ca-certificates.crt
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
ca=${1:-}

apt-get update
apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git python3 python3-dev libnss3-tools \
    python3-pip python3-venv python-is-python3 ripgrep tar unzip util-linux
if [ -n "$ca" ]; then
    install -D -m 0644 "$ca" /usr/local/share/ca-certificates/hephaestus-extra.crt
    update-ca-certificates
fi

# The browser and search users run the shared services; agent commands run as root in a
# machine of their own.
for spec in browser:1101 searxng:1102; do
    id -u "${spec%%:*}" >/dev/null 2>&1 ||
        useradd --create-home --uid "${spec##*:}" --shell /bin/bash "${spec%%:*}"
done
install -d -m 0755 /opt/hephaestus /home/agent
# Chromium serves CDP on loopback only, so the services machine relays it outward.
install -m 0644 /out/portfwd.py /opt/hephaestus/portfwd.py
# Chromium trusts its own NSS store, not the system one.
if [ -n "$ca" ]; then
    nssdb=/home/browser/.pki/nssdb
    install -d -m 0700 "$nssdb"
    certutil -d "sql:$nssdb" -N --empty-password
    certutil -d "sql:$nssdb" -A -n hephaestus-extra -t "C,," -i "$ca"
    chown -R browser:browser /home/browser/.pki
fi

# Chrome for Testing's headless shell, at the version this Playwright release pins.
# Playwright's own installer knows one CDN and gives up after three 30s stalls, so the
# shell is fetched here, from that CDN and then from Google's bucket, over IPv4.
python3 -m venv /tmp/playwright
/tmp/playwright/bin/pip install --no-cache-dir playwright
/tmp/playwright/bin/playwright install-deps chromium-headless-shell
version=$(/tmp/playwright/bin/python -c '
import json, pathlib, playwright
listing = pathlib.Path(playwright.__file__).parent / "driver/package/browsers.json"
browsers = json.loads(listing.read_text())["browsers"]
print(next(b["browserVersion"] for b in browsers if b["name"] == "chromium-headless-shell"))
')
case "$(uname -m)" in
    x86_64) platform=linux64 ;;
    aarch64) platform=linux-arm64 ;;
    *) echo "no headless Chromium build for $(uname -m)" >&2; exit 1 ;;
esac
archive=/tmp/headless-shell.zip
fetched=""
for base in https://cdn.playwright.dev/builds/cft \
    https://storage.googleapis.com/chrome-for-testing-public; do
    if curl -fL4 --retry 3 --retry-all-errors --connect-timeout 30 \
        --speed-limit 1024 --speed-time 60 -o "$archive" \
        "$base/$version/$platform/chrome-headless-shell-$platform.zip"; then
        fetched=1
        break
    fi
done
test -n "$fetched"
install -d -m 0755 /opt/hephaestus/browsers /opt/hephaestus/bin
unzip -q -o "$archive" -d /opt/hephaestus/browsers
chmod -R a+rX /opt/hephaestus/browsers
ln -sf "/opt/hephaestus/browsers/chrome-headless-shell-$platform/chrome-headless-shell" \
    /opt/hephaestus/bin/headless_shell
test -x /opt/hephaestus/bin/headless_shell

# SearXNG, from source as its own docs install it. The editable install imports searx to
# build its metadata, so its runtime dependencies go in first.
git clone --depth 1 https://github.com/searxng/searxng.git /opt/hephaestus/searxng-src
python3 -m venv /opt/hephaestus/searxng
/opt/hephaestus/searxng/bin/pip install --no-cache-dir -U pip setuptools wheel pyyaml \
    msgspec typing_extensions
/opt/hephaestus/searxng/bin/pip install --no-cache-dir \
    -r /opt/hephaestus/searxng-src/requirements.txt
/opt/hephaestus/searxng/bin/pip install --no-cache-dir --use-pep517 --no-build-isolation \
    -e /opt/hephaestus/searxng-src
chmod -R a+rX /opt/hephaestus/searxng /opt/hephaestus/searxng-src

apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/playwright "$archive" /root/.cache

# --one-file-system leaves out /proc, /sys, /dev, /out and the other mounts.
work=/var/tmp/image
rm -rf "$work" && mkdir -p "$work"
tar -C / --one-file-system --exclude=./var/tmp/image --exclude=./tmp/* -cf "$work/layer.tar" .
python3 - "$work" "$(uname -m)" <<'PY'
import hashlib, json, pathlib, sys
work = pathlib.Path(sys.argv[1])
arch = {"aarch64": "arm64", "x86_64": "amd64"}[sys.argv[2]]
digest = hashlib.sha256()
with (work / "layer.tar").open("rb") as layer:
    while chunk := layer.read(1 << 20):
        digest.update(chunk)
config = {
    "architecture": arch,
    "os": "linux",
    "config": {
        "Env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG=C.UTF-8", "HOME=/root"],
        "Cmd": ["/bin/sh"],
        "WorkingDir": "/home/agent",
    },
    "rootfs": {"type": "layers", "diff_ids": ["sha256:" + digest.hexdigest()]},
}
raw = json.dumps(config).encode()
name = hashlib.sha256(raw).hexdigest() + ".json"
(work / name).write_bytes(raw)
(work / "manifest.json").write_text(json.dumps(
    [{"Config": name, "RepoTags": ["hephaestus/agent:latest"], "Layers": ["layer.tar"]}]))
PY
# The archive's entries must not carry a ./ prefix.
cd "$work" && tar -cf /out/image.tar.partial -- *
mv /out/image.tar.partial /out/image.tar
