#!/usr/bin/env python3
"""
haproxy_manager.py — Gestion de la config HAProxy SecurePulse LB
Génère la configuration à partir de la liste des srv-mail,
l'écrit sur disque et recharge HAProxy sans downtime.
"""

import logging
import logging.handlers
import os
import subprocess
import yaml
from typing import Optional

# ─────────────────────────────────────────────
# Chargement de la configuration
# ─────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)

CFG = load_config()

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

os.makedirs(os.path.dirname(CFG["logging"]["file"]), exist_ok=True)

log = logging.getLogger("haproxy_manager")
log.setLevel(CFG["logging"]["level"])

_fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s — %(message)s")
_fh = logging.handlers.RotatingFileHandler(
    CFG["logging"]["file"],
    maxBytes=CFG["logging"]["max_bytes"],
    backupCount=CFG["logging"]["backup_count"],
)
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_fh)
log.addHandler(_sh)

# ─────────────────────────────────────────────
# Génération de la configuration HAProxy
# ─────────────────────────────────────────────

def generate_config(site: str) -> str:
    """
    Génère une configuration HAProxy complète à base de résolution DNS
    dynamique (resolvers + server-template), fusion de deux implémentations
    du projet :
      - LB-Syo (ce repo) : daemon Python d'élection maître/esclave, HA du LB.
      - LB-Lucien (fusionné ici) : découverte dynamique des backends via
        CoreDNS/etcd plutôt qu'une liste statique d'IP.

    Deux paliers par protocole (Q&A rapport : "HAProxy préfère les serveurs
    internes du DC, redirige via VPN si chargé/en panne") :
      - pool local  : postfix.<site>.<domain> / dovecot.<site>.<domain>
      - pool replis : postfix.all.<domain> / dovecot.all.<domain> (backup ;
        HAProxy ne les utilise que si tous les serveurs du pool local sont
        down, atteignables via le maillage WireGuard inter-DC)

    Ne dépend plus d'une liste de srv-mail suivie en mémoire par le daemon
    (STATE.mail_list) : les backends apparaissent/disparaissent tout seuls
    via la résolution DNS (TTL court, `hold valid 5s`), exactement comme le
    reste de la plateforme (LDAP, Mail, DNS) le fait déjà via etcd/CoreDNS.
    """
    ha = CFG["haproxy"]
    dns_cfg = CFG.get("dns", {})
    domain = dns_cfg.get("domain", "securepulse.fr")
    resolver_ip = dns_cfg.get("resolver_ip", "10.10.0.100")

    # ── Section global & defaults ────────────────────────────────────────
    config = f"""global
    log /dev/log local0
    log /dev/log local1 notice
    chroot /var/lib/haproxy
    stats socket {ha['socket']} mode 660 level admin expose-fd listeners
    stats timeout 30s
    user haproxy
    group haproxy
    daemon
    maxconn 50000

defaults
    log     global
    mode    tcp
    option  tcplog
    option  dontlognull
    timeout connect 5s
    timeout client  1m
    timeout server  1m
    retries 3

# ── Résolution DNS dynamique (CoreDNS + etcd) ────────────────────────────
# Les backends (postfix/dovecot) s'enregistrent et se désenregistrent
# eux-mêmes dans etcd au démarrage/arrêt (voir Mail/postfix, Mail/dovecot) ;
# HAProxy les redécouvre automatiquement, sans rechargement de config.
resolvers coredns
    nameserver dns {resolver_ip}:53
    accepted_payload_size 8192
    hold valid 5s
    hold other 10s
    resolve_retries 3
    timeout retry 1s

# ── Statistiques ─────────────────────────────────────────────────────────
listen stats
    bind *:{ha['stats_port']}
    mode http
    stats enable
    stats uri /stats
    stats refresh 10s
    stats auth admin:securepulse
    stats show-legends
    stats show-node

"""

    # ── Frontends & backends pour chaque protocole ───────────────────────
    # "service" = nom du backend applicatif enregistré dans etcd par Mail/
    # (postfix pour smtp/submission/smtps, dovecot pour imap/imaps).
    protocols = [
        {"name": "smtp",       "port": ha["frontend_port"],            "backend": "be_smtp",       "check_port": 25,  "service": "postfix"},
        {"name": "submission", "port": ha["frontend_port_submission"], "backend": "be_submission", "check_port": 587, "service": "postfix"},
        {"name": "smtps",      "port": ha["frontend_port_smtps"],      "backend": "be_smtps",      "check_port": 465, "service": "postfix"},
        {"name": "imap",       "port": ha["frontend_port_imap"],       "backend": "be_imap",       "check_port": 143, "service": "dovecot"},
        {"name": "imaps",      "port": ha["frontend_port_imaps"],      "backend": "be_imaps",      "check_port": 993, "service": "dovecot"},
    ]

    for proto in protocols:
        service = proto["service"]
        local_fqdn = f"{service}.{site}.{domain}"
        all_fqdn = f"{service}.all.{domain}"

        # Frontend
        config += f"""frontend fe_{proto['name']}
    bind *:{proto['port']}
    mode tcp
    default_backend {proto['backend']}

"""
        # Backend : pool local préféré, pool global (toutes régions, via VPN)
        # en secours uniquement si le pool local est intégralement down.
        config += f"""backend {proto['backend']}
    mode tcp
    balance roundrobin
    option tcp-check
    tcp-check connect port {proto['check_port']}
    server-template {service}-local 1-10 {local_fqdn}:{proto['check_port']} resolvers coredns resolve-prefer ipv4 check inter 5s fall 2 rise 2
    server-template {service}-remote 1-10 {all_fqdn}:{proto['check_port']} resolvers coredns resolve-prefer ipv4 check inter 5s fall 2 rise 2 backup

"""

    return config


# ─────────────────────────────────────────────
# Écriture et rechargement
# ─────────────────────────────────────────────

def write_config(config: str, path: Optional[str] = None) -> None:
    """Écrit la configuration HAProxy sur disque."""
    target = path or CFG["haproxy"]["config_path"]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(config)
    log.info(f"[HAPROXY] Config écrite dans {target}")


def reload_haproxy() -> bool:
    """
    Recharge HAProxy sans interruption de service via `haproxy -sf <pid>`.
    Retourne True si le rechargement a réussi.
    """
    config_path = CFG["haproxy"]["config_path"]
    pid_file = CFG["haproxy"]["pid_file"]

    # Vérification de la config avant rechargement
    check = subprocess.run(
        ["haproxy", "-c", "-f", config_path],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        log.error(f"[HAPROXY] Config invalide :\n{check.stderr}")
        return False

    # Lecture du PID courant pour le rechargement sans downtime
    old_pids = ""
    if os.path.exists(pid_file):
        with open(pid_file) as f:
            old_pids = f.read().strip()

    cmd = ["haproxy", "-f", config_path, "-p", pid_file, "-D"]
    if old_pids:
        # -sf = soft reload : les anciennes connexions terminent proprement
        cmd += ["-sf"] + old_pids.split()

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        log.info("[HAPROXY] Rechargement réussi (sans downtime)")
        return True
    else:
        log.error(f"[HAPROXY] Échec du rechargement :\n{result.stderr}")
        return False


def reload(site: str) -> bool:
    """
    Raccourci : génère la config résolveur-DNS pour ce site, l'écrit et
    recharge HAProxy. Idempotent — le contenu ne dépend que de `site` (pas
    d'une liste de mail_list suivie en mémoire), donc rappelable sans risque
    à chaque événement de topologie (nouveau nœud, alerte healthcheck...).
    """
    config = generate_config(site)
    write_config(config)
    return reload_haproxy()


def write_and_reload(config: str) -> bool:
    """
    Raccourci : écrit une config fournie telle quelle et recharge HAProxy.
    Utilisé lors de la réception d'un message update_config du maître.
    """
    write_config(config)
    return reload_haproxy()


# ─────────────────────────────────────────────
# Usage standalone (test / debug)
# ─────────────────────────────────────────────

if __name__ == "__main__":
    cfg = generate_config(CFG.get("node", {}).get("site", "local"))
    print(cfg)
    write_config(cfg)
    print("[TEST] Config écrite. Exécutez reload_haproxy() pour recharger.")
