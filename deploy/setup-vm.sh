#!/usr/bin/env bash
# One-shot setup for a fresh Ubuntu/Debian VM (tested layout: Oracle Cloud Always Free, Ubuntu 22.04/24.04).
# Run it as a normal sudo-capable user, from a clone of this repository:
#
#   git clone https://github.com/apratim-1407/hospital-event-analytics.git
#   cd hospital-event-analytics
#   ./deploy/setup-vm.sh                 # uses <ip>.sslip.io for HTTPS
#   ./deploy/setup-vm.sh demo.example.com   # or your own domain pointing at this VM
#
# It installs Docker if needed, writes .env with freshly generated secrets, opens ports 80/443,
# builds and starts the stack, loads the demo data and prints the link and logins.
set -euo pipefail
cd "$(dirname "$0")/.."
DOMAIN="${1:-}"
C="docker compose -f docker-compose.prod.yml"

say() { printf '\n==> %s\n' "$*"; }

if ! command -v docker >/dev/null 2>&1; then
  say "installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi
# A fresh group membership is not active in this shell yet; fall back to sudo for this run.
if ! docker info >/dev/null 2>&1; then DOCKER_SUDO=1; docker() { sudo docker "$@"; }; C="sudo docker compose -f docker-compose.prod.yml"; fi

if [ -z "$DOMAIN" ]; then
  say "finding this machine's public address"
  IP="$(curl -fsS https://api.ipify.org)"
  DOMAIN="${IP//./-}.sslip.io"
fi
echo "site address: $DOMAIN"

if [ ! -f .env ]; then
  say "writing .env with generated secrets"
  rand() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
  sed -e "s|^SITE_ADDRESS=.*|SITE_ADDRESS=$DOMAIN|" \
      -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rand 32)|" \
      -e "s|^JWT_SECRET=.*|JWT_SECRET=$(rand 64)|" \
      -e "s|^SEED_ADMIN_PASSWORD=.*|SEED_ADMIN_PASSWORD=$(rand 16)Aa1#|" \
      -e "s|^SEED_ANALYST_PASSWORD=.*|SEED_ANALYST_PASSWORD=$(rand 16)Aa1#|" \
      -e "s|^SEED_VIEWER_PASSWORD=.*|SEED_VIEWER_PASSWORD=$(rand 12)Aa1#|" \
      deploy/.env.production.example > .env
  chmod 600 .env
else
  say ".env already exists; keeping it (delete it to regenerate secrets)"
  sed -i "s|^SITE_ADDRESS=.*|SITE_ADDRESS=$DOMAIN|" .env
fi

say "opening ports 80 and 443 on the VM firewall"
if command -v iptables >/dev/null 2>&1; then
  for p in 80 443; do
    sudo iptables -C INPUT -p tcp --dport "$p" -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 1 -p tcp --dport "$p" -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then sudo netfilter-persistent save >/dev/null 2>&1 || true; fi
fi
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then sudo ufw allow 80/tcp; sudo ufw allow 443/tcp; fi

say "building and starting the stack"
$C up -d --build --scale worker="${WORKERS:-2}"

say "waiting for the API"
for _ in $(seq 1 60); do
  if $C exec -T api curl -fsS http://localhost:8000/health >/dev/null 2>&1; then break; fi
  sleep 3
done

say "loading the demo data and running the first analysis (a few minutes)"
./deploy/seed-demo.sh

set -a; . ./.env; set +a
cat <<MSG

Done. Open:   https://$DOMAIN/
  Viewer   (read-only, safe to share):  $SEED_VIEWER_EMAIL / $SEED_VIEWER_PASSWORD
  Admin    (keep private):              $SEED_ADMIN_EMAIL / $SEED_ADMIN_PASSWORD
  Analyst:                              $SEED_ANALYST_EMAIL / $SEED_ANALYST_PASSWORD

If the page does not load, the cloud provider's own firewall still needs inbound TCP 80 and 443
(Oracle: VCN > Security List > Add Ingress Rules, source 0.0.0.0/0).
MSG
