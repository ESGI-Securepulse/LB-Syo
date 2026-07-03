#!/usr/bin/env python3
"""
healthcheck.py — Surveillance des srv-mail SecurePulse LB
Ping chaque srv-mail toutes les N secondes et notifie le daemon
local en cas de défaillance. Cible downtime < 5s.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import yaml
import aiohttp
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

log = logging.getLogger("healthcheck")
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
# État partagé avec slave_daemon
# ─────────────────────────────────────────────
# On importe slave_daemon pour accéder à STATE.mail_list et
# appeler report_mail_down(). Healthcheck tourne dans le même process.
import slave_daemon

# ─────────────────────────────────────────────
# Suivi de l'état des srv-mail
# ─────────────────────────────────────────────

# Stocke le dernier état connu de chaque IP : True = up, False = down
_mail_status: dict[str, bool] = {}

# ─────────────────────────────────────────────
# Vérification TCP d'un port
# ─────────────────────────────────────────────

async def tcp_check(ip: str, port: int, timeout: float) -> bool:
    """
    Tente d'ouvrir une connexion TCP sur ip:port.
    Retourne True si la connexion réussit, False sinon.
    """
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def check_mail_server(mail: dict) -> bool:
    """
    Vérifie qu'un srv-mail répond sur au moins un des ports configurés.
    On considère le serveur UP si au moins un port répond.
    """
    ip = mail["ip"]
    timeout = CFG["healthcheck"]["timeout_seconds"]
    ports = CFG["healthcheck"]["check_ports"]

    results = await asyncio.gather(
        *(tcp_check(ip, port, timeout) for port in ports)
    )
    return any(results)


# ─────────────────────────────────────────────
# Boucle principale de healthcheck
# ─────────────────────────────────────────────

async def run_healthcheck() -> None:
    """
    Boucle principale : toutes les `interval_seconds` secondes,
    vérifie chaque srv-mail de la liste courante.
    """
    interval = CFG["healthcheck"]["interval_seconds"]
    log.info(f"[HEALTHCHECK] Démarrage — intervalle={interval}s, timeout={CFG['healthcheck']['timeout_seconds']}s")

    while True:
        mail_list = list(slave_daemon.STATE.mail_list)  # copie pour éviter les mutations

        if not mail_list:
            log.debug("[HEALTHCHECK] Aucun srv-mail connu, attente...")
        else:
            # Vérification de tous les srv-mail en parallèle
            tasks = {mail["ip"]: asyncio.create_task(check_mail_server(mail)) for mail in mail_list}
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)

            for mail, result in zip(mail_list, results):
                ip = mail["ip"]
                dns = mail.get("dns", ip)

                if isinstance(result, Exception):
                    is_up = False
                    log.error(f"[HEALTHCHECK] Exception lors du check de {dns} : {result}")
                else:
                    is_up = result

                prev_status = _mail_status.get(ip, True)  # inconnu = supposé UP

                if not is_up and prev_status:
                    # Transition UP → DOWN
                    log.warning(f"[HEALTHCHECK] ALERTE — {dns} ({ip}) ne répond plus !")
                    _mail_status[ip] = False
                    # Notification asynchrone au daemon esclave
                    asyncio.create_task(slave_daemon.report_mail_down(ip))

                elif is_up and not prev_status:
                    # Transition DOWN → UP (retour en ligne)
                    log.info(f"[HEALTHCHECK] RÉTABLISSEMENT — {dns} ({ip}) est de nouveau joignable")
                    _mail_status[ip] = True
                    # On ne le remet pas automatiquement dans la liste HAProxy :
                    # c'est l'administrateur ou le srv-mail lui-même qui doit se ré-enregistrer.

                elif is_up:
                    log.debug(f"[HEALTHCHECK] OK — {dns} ({ip})")

        await asyncio.sleep(interval)


# ─────────────────────────────────────────────
# Point d'entrée (usage standalone ou importé)
# ─────────────────────────────────────────────

async def main() -> None:
    await run_healthcheck()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("[HEALTHCHECK] Arrêt demandé")
