#!/bin/bash
# LB-Syo/deploy/generate-config.sh — génère le .env d'un site pour un
# déploiement réel (un LB par serveur, à côté de son sidecar WireGuard).
#
# config.yaml lui-même n'a pas besoin d'être régénéré : entrypoint.sh du
# conteneur lb-syo le réécrit déjà entièrement à partir des variables
# d'environnement à chaque démarrage (voir entrypoint.sh) — ce script ne
# fait donc que produire ces variables pour CE site/nœud, plus celles du
# sidecar wireguard/.
#
# Usage:
#   ./generate-config.sh --site grenoble --role master --etcd-url http://10.10.1.5:2379 \
#       --wg-site-prefix 10.10.1. [--domain securepulse.fr] [--resolver-ip 10.10.1.100]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

SITE=""; ROLE="slave"; ETCD_URL=""; DOMAIN="securepulse.fr"
RESOLVER_IP=""; WG_SITE_PREFIX=""

while [ $# -gt 0 ]; do
    case "$1" in
        --site) SITE="$2"; shift 2 ;;
        --role) ROLE="$2"; shift 2 ;;
        --etcd-url) ETCD_URL="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --resolver-ip) RESOLVER_IP="$2"; shift 2 ;;
        --wg-site-prefix) WG_SITE_PREFIX="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

[ -n "$SITE" ] || { echo "--site is required" >&2; exit 1; }
[ -n "$ETCD_URL" ] || { echo "--etcd-url is required" >&2; exit 1; }
[ -n "$WG_SITE_PREFIX" ] || { echo "--wg-site-prefix is required (ex: 10.10.1. — préfixe IP local du DC)" >&2; exit 1; }
[ -n "$RESOLVER_IP" ] || { echo "--resolver-ip is required (IP du conteneur DNS/CoreDNS de ce site)" >&2; exit 1; }

OUT_DIR="sites/${SITE}"
mkdir -p "$OUT_DIR"

cat > "${OUT_DIR}/.env" <<EOF
SITE=${SITE}
ROLE=${ROLE}
ETCD_URL=${ETCD_URL}
DOMAIN=${DOMAIN}
RESOLVER_IP=${RESOLVER_IP}
WG_SITE_PREFIX=${WG_SITE_PREFIX}
WG_ETCD_URL=${ETCD_URL}
WG_GATEWAY_ROUTING=1
EOF

echo "[generate-config] écrit ${OUT_DIR}/.env"
echo "[generate-config] déploiement : ./deploy.sh ${SITE}"
