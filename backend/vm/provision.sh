#!/bin/bash
# Turns smolvm's stock Ubuntu guest into the platform VM's clean image. Runs as root, with
# internet access; the disk it leaves behind is what every boot starts from. An optional
# argument is a CA bundle to trust, for networks that inspect TLS.
#
# Each stage records that it finished, tagged with a hash of its own code and the CA, so a
# build that failed part-way resumes from the partial disk the driver keeps: finished
# stages are skipped, and a stage whose code changed runs again. A stage removes what it
# installs before installing it, so a stage that failed half-way starts clean.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# Python's own tools read certifi unless told to use the system store, which holds the
# extra CA as well as the standard roots.
export PIP_CERT=/etc/ssl/certs/ca-certificates.crt
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

stamps=/var/lib/hephaestus/provisioned
ca_sum=none
if [ -n "${1:-}" ]; then
    ca_sum=$(sha256sum "$1" | cut -c1-16)
fi

run_stage() {
    local name=$1 stamp
    stamp="$stamps/$name-$({ declare -f "$name"; echo "$ca_sum"; } | sha256sum | cut -c1-16)"
    if [ -e "$stamp" ]; then
        echo "stage $name: already done"
        return
    fi
    rm -f "$stamps/$name"-*
    "$@"
    install -d "$stamps"
    touch "$stamp"
}

packages() {
    apt-get update
    apt-get install -y --no-install-recommends \
        bubblewrap build-essential ca-certificates curl git python3 python3-dev \
        libnss3-tools python3-pip python3-venv python-is-python3 tar unzip util-linux
    if [ -n "${1:-}" ]; then
        install -D -m 0644 "$1" /usr/local/share/ca-certificates/hephaestus-extra.crt
        update-ca-certificates
    fi
}

# The sandbox user runs workspace commands; the browser and search users run the
# platform services, so neither can read a workspace.
users() {
    local spec name uid
    for spec in sandbox:1100 browser:1101 searxng:1102; do
        name=${spec%%:*}
        uid=${spec##*:}
        id -u "$name" >/dev/null 2>&1 || useradd --create-home --uid "$uid" --shell /bin/bash "$name"
    done
    install -d -m 0755 /opt/hephaestus /work
    # Chromium trusts its own NSS store, not the system one.
    if [ -n "${1:-}" ]; then
        local nssdb=/home/browser/.pki/nssdb
        rm -rf "$nssdb"
        install -d -m 0700 "$nssdb"
        certutil -d "sql:$nssdb" -N --empty-password
        certutil -d "sql:$nssdb" -A -n hephaestus-extra -t "C,," -i "$1"
        chown -R browser:browser /home/browser/.pki
    fi
}

# Headless Chromium for page rendering: Chrome for Testing's headless shell, at the
# version this Playwright release pins. Playwright's own installer knows one CDN and
# gives up after three 30s stalls, so the shell is fetched here, from that CDN and then
# from Google's bucket, over IPv4.
browser() {
    rm -rf /opt/hephaestus/playwright /opt/hephaestus/browsers /opt/hephaestus/bin/headless_shell
    python3 -m venv /opt/hephaestus/playwright
    /opt/hephaestus/playwright/bin/pip install --no-cache-dir playwright
    /opt/hephaestus/playwright/bin/playwright install-deps chromium-headless-shell
    local version platform archive=/tmp/headless-shell.zip fetched="" base
    version=$(/opt/hephaestus/playwright/bin/python -c '
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
    rm -f "$archive"
    chmod -R a+rX /opt/hephaestus/browsers
    ln -sf "/opt/hephaestus/browsers/chrome-headless-shell-$platform/chrome-headless-shell" \
        /opt/hephaestus/bin/headless_shell
    test -x /opt/hephaestus/bin/headless_shell
}

# SearXNG, from source as its own docs install it.
searxng() {
    rm -rf /opt/hephaestus/searxng-src /opt/hephaestus/searxng
    git clone --depth 1 https://github.com/searxng/searxng.git /opt/hephaestus/searxng-src
    python3 -m venv /opt/hephaestus/searxng
    # The editable install imports searx to build its metadata, so its runtime
    # dependencies go in first.
    /opt/hephaestus/searxng/bin/pip install --no-cache-dir -U pip setuptools wheel pyyaml \
        msgspec typing_extensions
    /opt/hephaestus/searxng/bin/pip install --no-cache-dir \
        -r /opt/hephaestus/searxng-src/requirements.txt
    /opt/hephaestus/searxng/bin/pip install --no-cache-dir --use-pep517 --no-build-isolation \
        -e /opt/hephaestus/searxng-src
    chmod -R a+rX /opt/hephaestus/searxng /opt/hephaestus/searxng-src
}

run_stage packages "${1:-}"
run_stage users "${1:-}"
run_stage browser
run_stage searxng

apt-get clean
rm -rf /var/lib/apt/lists/*
sync
