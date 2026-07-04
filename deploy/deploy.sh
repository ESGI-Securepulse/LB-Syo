#!/bin/bash
# LB-Syo/deploy/deploy.sh <SITE> — lance le LB (HAProxy + daemon) et son
# sidecar WireGuard (passerelle site-à-site de ce DC) sur ce serveur.
# Suppose generate-config.sh déjà exécuté pour ce SITE.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

SITE="${1:?usage: ./deploy.sh <site>}"
[ -f "sites/${SITE}/.env" ] || { echo "sites/${SITE}/.env introuvable — lancez d'abord generate-config.sh --site ${SITE} ..." >&2; exit 1; }

docker compose -f docker-compose.prod.yml --env-file "sites/${SITE}/.env" up -d --build
echo "[deploy] lb-${SITE} + lb-${SITE}-wireguard démarrés."
echo "[deploy] IP de passerelle site-à-site pour ce DC (à donner à storage-lucien/Mail/LDAP via WG_GATEWAY_IP) :"
docker exec "lb-${SITE}-wireguard" hostname -I | awk '{print "  " $1}'
