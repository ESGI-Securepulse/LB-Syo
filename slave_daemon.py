#!/usr/bin/env python3
"""
slave_daemon.py — Daemon esclave SecurePulse LB
Se connecte au maître, reçoit sa liste de nœuds, gère le failover
et recharge HAProxy à chaque mise à jour de configuration.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import yaml
import websockets
from websockets.exceptions import ConnectionClosed
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

log = logging.getLogger("slave")
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
# État local du daemon esclave
# ─────────────────────────────────────────────

class SlaveState:
    def __init__(self):
        self.my_number: int = 0
        self.my_ip: str = CFG["node"]["ip"]
        self.my_dns: str = CFG["node"]["dns"]
        self.is_master: bool = False

        # Listes reçues du maître
        self.lb_list: list[dict] = []
        self.mail_list: list[dict] = []

        # Référence sur la connexion WebSocket vers le maître
        self.master_ws: Optional[object] = None

        # Verrou pour éviter les élections concurrentes
        self._election_lock = asyncio.Lock()


STATE = SlaveState()

# ─────────────────────────────────────────────
# Import local de haproxy_manager
# ─────────────────────────────────────────────
# Importé ici pour éviter la circularité ; haproxy_manager n'a pas
# de dépendance sur slave_daemon.
import haproxy_manager

# ─────────────────────────────────────────────
# DNS (mocké — brancher l'API OVH plus tard)
# ─────────────────────────────────────────────

def update_dns(ip: str, dns: str = "master.securepulse.fr") -> None:
    """
    Met à jour l'entrée DNS pour pointer vers ip.
    MOCKÉ : log uniquement. À remplacer par l'appel API OVH.
    """
    log.info(f"[DNS-MOCK] Mise à jour DNS : {dns} → {ip}")


# ─────────────────────────────────────────────
# Connexion au maître
# ─────────────────────────────────────────────

async def connect_to_master() -> None:
    """
    Boucle de connexion au maître avec reconnexion automatique.
    En cas d'échec, déclenche la procédure d'élection.
    """
    master_uri = f"ws://{CFG['master']['dns']}:{CFG['master']['port']}"

    while True:
        try:
            log.info(f"[CONNECT] Tentative de connexion au maître : {master_uri}")
            async with websockets.connect(master_uri) as ws:
                STATE.master_ws = ws
                log.info("[CONNECT] Connecté au maître")

                # ── Enregistrement ───────────────────────────────────────
                register_msg = {
                    "type": "register",
                    "role": "lb",
                    "ip": STATE.my_ip,
                    "dns": STATE.my_dns,
                }
                await ws.send(json.dumps(register_msg))
                log.info(f"[REGISTER] Message d'enregistrement envoyé : IP={STATE.my_ip} DNS={STATE.my_dns}")

                # ── Attente du welcome ───────────────────────────────────
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == "welcome":
                    STATE.my_number = msg["assigned_number"]
                    STATE.lb_list = msg.get("lb_list", [])
                    STATE.mail_list = msg.get("mail_list", [])
                    log.info(
                        f"[WELCOME] Numéro attribué : #{STATE.my_number} | "
                        f"LB connus : {len(STATE.lb_list)} | Mail connus : {len(STATE.mail_list)}"
                    )
                    # Génération initiale de la config HAProxy
                    haproxy_manager.reload_from_list(STATE.mail_list)

                # ── Boucle de réception ──────────────────────────────────
                async for raw in ws:
                    await handle_master_message(json.loads(raw))

        except (ConnectionClosed, OSError, websockets.exceptions.WebSocketException) as exc:
            log.warning(f"[DISCONNECT] Connexion maître perdue : {exc}")
            STATE.master_ws = None

            # ── Procédure d'élection si on est LB #2 ────────────────────
            await maybe_trigger_election()

            # Pause avant nouvelle tentative (si on n'est pas devenu maître)
            if not STATE.is_master:
                log.info("[RECONNECT] Nouvelle tentative dans 5 secondes...")
                await asyncio.sleep(5)
            else:
                break  # On est devenu maître, on sort de la boucle de connexion

        except Exception as exc:
            log.exception(f"[ERROR] Erreur inattendue : {exc}")
            await asyncio.sleep(5)


# ─────────────────────────────────────────────
# Traitement des messages du maître
# ─────────────────────────────────────────────

async def handle_master_message(msg: dict) -> None:
    """Traite un message reçu du daemon maître."""
    mtype = msg.get("type")

    if mtype == "update_list":
        STATE.lb_list   = msg.get("lb_list", [])
        STATE.mail_list = msg.get("mail_list", [])

        # Le maître nous envoie notre numéro mis à jour après chaque renumérotation.
        # On se resynchronise immédiatement pour que l'élection reste cohérente.
        new_number = msg.get("your_number")
        if new_number and new_number != STATE.my_number:
            log.info(f"[RENUMBER] Mon numéro mis à jour : #{STATE.my_number} → #{new_number}")
            STATE.my_number = new_number

        log.info(
            f"[UPDATE] Liste mise à jour — LB: {len(STATE.lb_list)}, "
            f"Mail: {len(STATE.mail_list)}, Mon numéro: #{STATE.my_number}"
        )
        # Régénère et recharge la config HAProxy avec la nouvelle liste
        haproxy_manager.reload_from_list(STATE.mail_list)

    elif mtype == "update_config":
        config = msg.get("haproxy_config", "")
        log.info("[CONFIG] Nouvelle config HAProxy reçue, rechargement...")
        haproxy_manager.write_and_reload(config)

    elif mtype == "new_master":
        new_ip = msg.get("ip")
        new_dns = msg.get("dns")
        log.info(f"[ELECTION] Nouveau maître désigné : {new_dns} ({new_ip})")
        # Mise à jour de l'URI du maître dans la config runtime
        CFG["master"]["dns"] = new_dns

    else:
        log.debug(f"[MSG] Message non géré du maître : {msg}")


# ─────────────────────────────────────────────
# Élection de nouveau maître
# ─────────────────────────────────────────────

async def maybe_trigger_election() -> None:
    """
    Élection en chaîne : le LB avec le numéro le plus bas parmi les survivants
    attend (number * 1s) avant de se proclamer maître.
    Si quelqu'un d'autre se proclame maître pendant ce délai, on abandonne.

    Exemple : LB#2 attend 2s, LB#3 attend 3s.
    Si LB#2 est aussi mort, LB#3 attend 3s sans recevoir de 'new_master' → il prend la main.
    """
    async with STATE._election_lock:
        if STATE.is_master:
            return

    # Délai proportionnel au numéro : LB#2 réagit en 2s, LB#3 en 3s, etc.
    delay = STATE.my_number * 1.0
    log.info(
        f"[ELECTION] LB #{STATE.my_number} attend {delay:.0f}s avant de se proclamer maître"
    )

    # On attend : si quelqu'un d'autre devient maître pendant ce délai,
    # on recevra un message 'new_master' via la connexion entrante.
    await asyncio.sleep(delay)

    async with STATE._election_lock:
        if STATE.is_master:
            return  # Quelqu'un d'autre a déjà pris la main pendant notre attente

        # Vérification : est-ce qu'un autre LB avec un numéro inférieur est encore joignable ?
        lower_alive = await _check_lower_lb_alive()
        if lower_alive:
            log.info(
                f"[ELECTION] LB #{STATE.my_number} — un LB de rang inférieur est encore en vie, abandon"
            )
            return

        log.warning(
            f"[ELECTION] LB #{STATE.my_number} se proclame maître (aucun rang inférieur joignable)"
        )
        await become_master()


async def _check_lower_lb_alive() -> bool:
    """
    Tente de contacter chaque LB avec un numéro inférieur au nôtre.
    Retourne True si au moins un répond (connexion TCP acceptée).
    Cela évite qu'un LB#3 prenne la main alors que LB#2 est juste lent.
    """
    port = CFG["master"]["port"]
    others_lower = [lb for lb in STATE.lb_list
                    if lb.get("number", 0) < STATE.my_number and lb["ip"] != STATE.my_ip]

    for lb in others_lower:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(lb["ip"], port), timeout=1.0
            )
            writer.close()
            await writer.wait_closed()
            log.info(f"[ELECTION] LB #{lb.get('number')} ({lb['ip']}) répond encore")
            return True
        except Exception:
            log.info(f"[ELECTION] LB #{lb.get('number')} ({lb['ip']}) injoignable")

    return False


async def become_master() -> None:
    """
    Procédure de promotion en tant que nouveau maître :
    1. Met à jour le DNS
    2. Notifie tous les autres LB
    3. Devient le daemon maître
    """
    STATE.is_master = True
    log.info(f"[ELECTION] Ce LB ({STATE.my_dns}) se proclame nouveau maître")

    # Mise à jour DNS
    update_dns(STATE.my_ip, "master.securepulse.fr")

    # Message d'élection à propager
    election_msg = {
        "type": "new_master",
        "ip": STATE.my_ip,
        "dns": STATE.my_dns,
    }

    # Notification des autres LB dans l'ordre numérique
    others = sorted(
        [lb for lb in STATE.lb_list if lb["ip"] != STATE.my_ip],
        key=lambda x: x.get("number", 0),
    )

    log.info(f"[ELECTION] Notification de {len(others)} LB pair(s)")
    for lb in others:
        await notify_lb_of_election(lb, election_msg)

    # Démarrage du daemon maître intégré
    log.info("[ELECTION] Démarrage du daemon maître intégré...")
    await start_embedded_master()


async def notify_lb_of_election(lb: dict, msg: dict) -> None:
    """Contacte un LB pair pour lui annoncer le changement de maître."""
    uri = f"ws://{lb['ip']}:{CFG['master']['port']}"
    try:
        async with websockets.connect(uri, open_timeout=5) as ws:
            await ws.send(json.dumps(msg))
            log.info(f"[ELECTION] LB {lb['dns']} ({lb['ip']}) notifié")
    except Exception as exc:
        log.warning(f"[ELECTION] Impossible de joindre LB {lb.get('dns')} : {exc}")


async def start_embedded_master() -> None:
    """
    Démarre le daemon maître en important master_daemon directement.
    Cela permet au LB #2 de devenir maître sans redémarrage de processus.
    """
    import master_daemon
    # Mise à jour de la config du master avec l'état actuel connu
    master_daemon.STATE.lb_nodes = {}  # Les esclaves se reconnecteront
    master_daemon.STATE.mail_nodes = {}
    log.info("[ELECTION] Daemon maître démarré, en attente de reconnexions des esclaves")
    await master_daemon.main()


# ─────────────────────────────────────────────
# Healthcheck alert (appelé par healthcheck.py)
# ─────────────────────────────────────────────

async def report_mail_down(mail_ip: str) -> None:
    """
    Signale au maître (ou gère localement si maître) qu'un srv-mail est tombé.
    Appelé par healthcheck.py.
    """
    log.warning(f"[HEALTHCHECK] Srv-mail {mail_ip} signalé hors ligne")

    if STATE.is_master:
        # On est le maître : on retire directement et on recharge
        STATE.mail_list = [m for m in STATE.mail_list if m["ip"] != mail_ip]
        haproxy_manager.reload_from_list(STATE.mail_list)
        log.info(f"[HEALTHCHECK] Mail {mail_ip} retiré et HAProxy rechargé")
    elif STATE.master_ws is not None:
        # On envoie l'alerte au maître
        alert = {"type": "healthcheck_alert", "ip": mail_ip}
        try:
            await STATE.master_ws.send(json.dumps(alert))
        except ConnectionClosed:
            log.warning("[HEALTHCHECK] Maître injoignable pour l'alerte")
    else:
        # Pas de maître joignable : on recharge HAProxy localement
        log.warning("[HEALTHCHECK] Aucun maître joignable, rechargement HAProxy local")
        STATE.mail_list = [m for m in STATE.mail_list if m["ip"] != mail_ip]
        haproxy_manager.reload_from_list(STATE.mail_list)


# ─────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────

async def main() -> None:
    log.info(
        f"[SLAVE] Démarrage du daemon esclave — IP={STATE.my_ip} DNS={STATE.my_dns}"
    )
    await connect_to_master()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("[SLAVE] Arrêt demandé")
