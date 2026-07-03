#!/bin/sh
# deploy.sh — Déploiement SecurePulse LB sur Alpine Linux
# Ce script installe les dépendances, configure le nœud et démarre le daemon esclave.

set -e

# ─────────────────────────────────────────────
# Couleurs pour les messages
# ─────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'  # No Color

info()    { printf "${GREEN}[INFO]${NC} %s\n" "$1"; }
warn()    { printf "${YELLOW}[WARN]${NC} %s\n" "$1"; }
error()   { printf "${RED}[ERROR]${NC} %s\n" "$1" >&2; exit 1; }

# ─────────────────────────────────────────────
# Vérification des privilèges root
# ─────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    error "Ce script doit être exécuté en tant que root."
fi

# ─────────────────────────────────────────────
# Collecte des informations de l'administrateur
# ─────────────────────────────────────────────
echo ""
echo "=== Déploiement SecurePulse Load Balancer ==="
echo ""

# IP publique de la machine
printf "Entrez l'IP publique de cette machine : "
read -r NODE_IP

# Validation basique de l'IP
echo "$NODE_IP" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$' \
    || error "Format d'IP invalide : $NODE_IP"

# DNS souhaité pour ce LB
printf "Entrez le DNS souhaité pour ce LB (ex: lb1.securepulse.fr) : "
read -r NODE_DNS

[ -z "$NODE_DNS" ] && error "Le DNS ne peut pas être vide."

# Site/DC de rattachement (utilisé pour la résolution DNS des backends
# postfix.<site>./dovecot.<site>. en priorité sur le pool postfix.all./dovecot.all.)
printf "Entrez le nom du site/DC de rattachement (ex: lyon) : "
read -r NODE_SITE
[ -z "$NODE_SITE" ] && error "Le site ne peut pas être vide."

# Vérification que le DNS n'est pas déjà pris
info "Vérification que $NODE_DNS est disponible..."
if host "$NODE_DNS" > /dev/null 2>&1; then
    warn "Le DNS $NODE_DNS résout déjà vers une IP. Vérifiez qu'il n'est pas déjà utilisé."
    printf "Continuer quand même ? [o/N] : "
    read -r CONFIRM
    [ "$CONFIRM" = "o" ] || [ "$CONFIRM" = "O" ] || error "Déploiement annulé."
else
    info "DNS $NODE_DNS disponible (ne résout pas encore)."
fi

# ─────────────────────────────────────────────
# Installation des dépendances Alpine Linux
# ─────────────────────────────────────────────
info "Mise à jour des paquets Alpine..."
apk update

info "Installation de Python 3, HAProxy, certbot..."
apk add --no-cache \
    python3 \
    py3-pip \
    py3-yaml \
    haproxy \
    certbot \
    bind-tools \
    curl \
    bash \
    openrc

info "Installation des bibliothèques Python..."
pip3 install --break-system-packages websockets aiohttp

# ─────────────────────────────────────────────
# Création des répertoires nécessaires
# ─────────────────────────────────────────────
info "Création des répertoires..."
mkdir -p /etc/haproxy
mkdir -p /var/run/haproxy
mkdir -p /var/log/securepulse
mkdir -p /opt/securepulse

# ─────────────────────────────────────────────
# Copie des fichiers du projet
# ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
info "Copie des fichiers depuis $SCRIPT_DIR vers /opt/securepulse..."
cp "$SCRIPT_DIR/master_daemon.py"  /opt/securepulse/
cp "$SCRIPT_DIR/slave_daemon.py"   /opt/securepulse/
cp "$SCRIPT_DIR/healthcheck.py"    /opt/securepulse/
cp "$SCRIPT_DIR/haproxy_manager.py" /opt/securepulse/
cp "$SCRIPT_DIR/config.yaml"       /opt/securepulse/

# ─────────────────────────────────────────────
# Personnalisation de la config YAML
# ─────────────────────────────────────────────
info "Configuration du nœud (IP=$NODE_IP, DNS=$NODE_DNS, site=$NODE_SITE)..."

# Remplacement des valeurs dans config.yaml via Python (plus fiable que sed sur Alpine)
python3 - <<EOF
import yaml

with open('/opt/securepulse/config.yaml', 'r') as f:
    cfg = yaml.safe_load(f)

cfg['node']['ip']   = '$NODE_IP'
cfg['node']['dns']  = '$NODE_DNS'
cfg['node']['role'] = 'slave'
cfg['node']['site'] = '$NODE_SITE'

with open('/opt/securepulse/config.yaml', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

print("  config.yaml mis à jour.")
EOF

# ─────────────────────────────────────────────
# Génération d'une config HAProxy initiale
# ─────────────────────────────────────────────
info "Génération de la config HAProxy initiale (résolveurs DNS, site=$NODE_SITE)..."
cd /opt/securepulse && python3 -c "
import haproxy_manager
cfg = haproxy_manager.generate_config('$NODE_SITE')
haproxy_manager.write_config(cfg)
print('  Config HAProxy initiale générée.')
"

# ─────────────────────────────────────────────
# Service OpenRC pour le daemon esclave
# ─────────────────────────────────────────────
info "Création du service OpenRC pour le daemon esclave..."
cat > /etc/init.d/securepulse-slave << 'INITD'
#!/sbin/openrc-run

name="securepulse-slave"
description="SecurePulse LB Daemon Esclave"
command="/usr/bin/python3"
command_args="/opt/securepulse/slave_daemon.py"
command_background="yes"
pidfile="/var/run/securepulse-slave.pid"
output_log="/var/log/securepulse/slave.log"
error_log="/var/log/securepulse/slave.log"
directory="/opt/securepulse"

depend() {
    need net
    after firewall
}
INITD
chmod +x /etc/init.d/securepulse-slave

# ─────────────────────────────────────────────
# Démarrage des services
# ─────────────────────────────────────────────
# Le healthcheck tourne désormais DANS le process du daemon esclave
# (slave_daemon.py importe healthcheck.py et lance sa boucle comme tâche
# asyncio) — nécessaire pour qu'il partage le même état STATE.mail_list.
# Lancé comme process OpenRC séparé, il aurait sa propre instance vide de
# STATE et ne détecterait jamais rien. Pas de service securepulse-health.
info "Activation et démarrage des services..."
rc-update add securepulse-slave default
rc-update add haproxy default

service haproxy start        || warn "HAProxy n'a pas démarré (config vide attendue)"
service securepulse-slave start

# ─────────────────────────────────────────────
# Résumé
# ─────────────────────────────────────────────
echo ""
echo "========================================"
info "Déploiement terminé !"
echo "  IP publique  : $NODE_IP"
echo "  DNS          : $NODE_DNS"
echo "  Maître       : $(grep 'dns' /opt/securepulse/config.yaml | grep master | awk '{print $2}')"
echo "  Logs         : /var/log/securepulse/"
echo "  Config       : /opt/securepulse/config.yaml"
echo ""
info "Le daemon esclave tente de se connecter au maître."
info "Vérifiez les logs : tail -f /var/log/securepulse/daemon.log"
echo "========================================"
