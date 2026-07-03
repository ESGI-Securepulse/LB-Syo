#!/bin/bash
# entrypoint.sh — lancement conteneur Docker (simulation/tests). Le
# déploiement bare-metal réel reste deploy.sh (Alpine/OpenRC), inchangé.
set -e

ETCD_URL="${ETCD_URL:-http://etcd:2379}"
SITE="${SITE:?SITE env var is required (ex: lyon)}"
ROLE="${ROLE:-slave}"
DOMAIN="${DOMAIN:-securepulse.fr}"
RESOLVER_IP="${RESOLVER_IP:-10.10.0.100}"
MASTER_DNS="${MASTER_DNS:-master.${DOMAIN}}"
NODE_DNS="${NODE_DNS:-lb-${SITE}-${HOSTNAME}.${DOMAIN}}"

wait_etcd() {
    echo "[etcd] Waiting..."
    until curl -sf "${ETCD_URL}/health" > /dev/null 2>&1; do sleep 1; done
    echo "[etcd] Ready"
}

detect_my_ip() {
    hostname -I | awk '{print $1}'
}

wait_etcd
MY_IP=$(detect_my_ip)
echo "[start] site=${SITE} role=${ROLE} ip=${MY_IP} dns=${NODE_DNS}"

python3 - "$SITE" "$ROLE" "$MY_IP" "$NODE_DNS" "$MASTER_DNS" "$DOMAIN" "$ETCD_URL" "$RESOLVER_IP" << 'EOF'
import sys, yaml
site, role, ip, dns, master_dns, domain, etcd_url, resolver_ip = sys.argv[1:9]

with open("/opt/securepulse/config.yaml") as f:
    cfg = yaml.safe_load(f)

cfg["node"]["ip"] = ip
cfg["node"]["dns"] = dns
cfg["node"]["site"] = site
cfg["node"]["role"] = role
cfg["master"]["dns"] = master_dns
cfg.setdefault("dns", {})
cfg["dns"]["domain"] = domain
cfg["dns"]["etcd_url"] = etcd_url
cfg["dns"]["resolver_ip"] = resolver_ip
cfg["haproxy"]["config_path"] = "/etc/haproxy/haproxy.cfg"
cfg["haproxy"]["pid_file"] = "/var/run/haproxy.pid"
cfg["haproxy"]["socket"] = "/var/run/haproxy/admin.sock"
cfg["logging"]["file"] = "/var/log/securepulse/daemon.log"

with open("/opt/securepulse/config.yaml", "w") as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

print("[config] config.yaml généré pour ce conteneur")
EOF

mkdir -p /var/run/haproxy /var/log/securepulse /etc/haproxy

export ROLE
exec python3 /opt/securepulse/run.py
