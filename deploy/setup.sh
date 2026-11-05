#!/usr/bin/env bash
# One-time setup of a fresh Vultr Cloud Compute VM (Ubuntu 24.04, Seattle) for Hufu.
# Run as root from the unpacked code at /opt/hufu:   bash /opt/hufu/deploy/setup.sh
# Safe to run again after an update: it rebuilds the venv and the sandbox image and restarts.
#
# What it installs: Docker (Ubuntu's docker.io), gVisor (runsc, registered as a Docker runtime),
# Caddy (HTTPS on <ip>.sslip.io), uv. What it creates: a system user `hufu` that runs the app,
# /etc/hufu/hufu.env (settings, root-only), /var/lib/hufu (per-run data), a systemd unit.
set -euo pipefail

APP_DIR=/opt/hufu
DATA_DIR=/var/lib/hufu
ETC_DIR=/etc/hufu
export DEBIAN_FRONTEND=noninteractive

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ -f "$APP_DIR/pyproject.toml" ] || { echo "code not found in $APP_DIR"; exit 1; }

PUBLIC_IP="${HUFU_PUBLIC_IP:-$(hostname -I | awk '{print $1}')}"
DOMAIN="${HUFU_DOMAIN:-${PUBLIC_IP//./-}.sslip.io}"
echo "== Hufu setup: domain $DOMAIN"

echo "== packages"
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg ufw docker.io docker-buildx \
  debian-keyring debian-archive-keyring apt-transport-https
systemctl enable --now docker

echo "== gVisor (runsc)"
if ! command -v runsc >/dev/null; then
  curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
    > /etc/apt/sources.list.d/gvisor.list
  apt-get update -q && apt-get install -y -q runsc
fi
runsc install          # adds the runsc runtime to /etc/docker/daemon.json
systemctl restart docker
docker info --format '{{json .Runtimes}}' | grep -q runsc || { echo "runsc not registered"; exit 1; }

echo "== Caddy"
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi

echo "== uv"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

echo "== user and directories"
id hufu >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin hufu
# The app (trusted code) starts sandboxes, so it is in the docker group. Model-written code never
# sees the socket: it runs inside the containers, with no network and no mounts but /data.
usermod -aG docker hufu
install -d -o hufu -g hufu -m 750 "$DATA_DIR"
install -d -o root -g root -m 755 "$ETC_DIR"
chown -R root:root "$APP_DIR"   # the service can read its code but not change it

echo "== settings"
if [ ! -f "$ETC_DIR/hufu.env" ]; then
  sed -e "s#^HUFU_PUBLIC_URL=.*#HUFU_PUBLIC_URL=https://$DOMAIN#" \
      "$APP_DIR/deploy/hufu.env.example" > "$ETC_DIR/hufu.env"
fi
touch "$ETC_DIR/secrets.env"
chmod 600 "$ETC_DIR/hufu.env" "$ETC_DIR/secrets.env"

echo "== Python environment"
cd "$APP_DIR"
UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --frozen --no-dev
chmod -R a+rX /opt/uv-python 2>/dev/null || true

echo "== sandbox image"
docker build -q -t hufu-sandbox:1 "$APP_DIR/sandbox"

echo "== service"
install -m 644 "$APP_DIR/deploy/hufu.service" /etc/systemd/system/hufu.service
sed -e "s#__DOMAIN__#$DOMAIN#" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable hufu >/dev/null
systemctl restart hufu
systemctl reload caddy || systemctl restart caddy

echo "== firewall"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "== smoke test"
for _ in $(seq 1 20); do
  curl -fsS http://127.0.0.1:8000/api/status >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS http://127.0.0.1:8000/api/status | python3 -c '
import json, sys
s = json.load(sys.stdin)
print("sandbox ready:", s["sandbox"]["ready"], "| runtime:", s["sandbox"].get("runtime"),
      "| inference configured:", s["inference"]["configured"], "| model:", s["inference"]["model"])'
echo
echo "Done. Open https://$DOMAIN"
if ! grep -q '^VULTR_INFERENCE_API_KEY=.\+' "$ETC_DIR/secrets.env"; then
  echo "No inference key yet: put VULTR_INFERENCE_API_KEY=... in $ETC_DIR/secrets.env"
  echo "(or run deploy/deploy.ps1 -PushKey from your laptop), then: systemctl restart hufu"
fi
