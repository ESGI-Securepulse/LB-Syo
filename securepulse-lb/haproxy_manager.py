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

def generate_config(mail_list: list[dict]) -> str:
    """
    Génère une configuration HAProxy complète à partir
    de la liste des srv-mail actifs.
    Supporte SMTP (25/587/465), IMAP (143/993).
    """
    ha = CFG["haproxy"]

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
    protocols = [
        {
            "name": "smtp",
            "port": ha["frontend_port"],
            "backend": "be_smtp",
            "check_port": 25,
        },
        {
            "name": "submission",
            "port": ha["frontend_port_submission"],
            "backend": "be_submission",
            "check_port": 587,
        },
        {
            "name": "smtps",
            "port": ha["frontend_port_smtps"],
            "backend": "be_smtps",
            "check_port": 465,
        },
        {
            "name": "imap",
            "port": ha["frontend_port_imap"],
            "backend": "be_imap",
            "check_port": 143,
        },
        {
            "name": "imaps",
            "port": ha["frontend_port_imaps"],
            "backend": "be_imaps",
            "check_port": 993,
        },
    ]

    for proto in protocols:
        # Frontend
        config += f"""frontend fe_{proto['name']}
    bind *:{proto['port']}
    mode tcp
    default_backend {proto['backend']}

"""
        # Backend
        config += f"""backend {proto['backend']}
    mode tcp
    balance roundrobin
    option tcp-check
    tcp-check connect port {proto['check_port']}
"""

        if not mail_list:
            # Aucun serveur mail : on ajoute un commentaire explicatif
            config += "    # Aucun srv-mail actif\n"
        else:
            for mail in mail_list:
                server_name = mail.get("dns", mail["ip"]).replace(".", "-").replace(":", "-")
                config += (
                    f"    server {server_name} {mail['ip']}:{proto['check_port']} "
                    f"check inter 2s fall 3 rise 2\n"
                )

        config += "\n"

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


def reload_from_list(mail_list: list[dict]) -> bool:
    """
    Raccourci : génère la config à partir de mail_list,
    l'écrit et recharge HAProxy.
    """
    config = generate_config(mail_list)
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
    # Exemple de test avec une liste fictive
    test_mail_list = [
        {"ip": "192.168.1.10", "dns": "mail1.securepulse.fr", "number": 1},
        {"ip": "192.168.1.11", "dns": "mail2.securepulse.fr", "number": 2},
    ]
    cfg = generate_config(test_mail_list)
    print(cfg)
    write_config(cfg)
    print("[TEST] Config écrite. Exécutez reload_haproxy() pour recharger.")
