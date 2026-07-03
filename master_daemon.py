#!/usr/bin/env python3
"""
master_daemon.py — Daemon maître SecurePulse LB
Gère l'enregistrement des LB et srv-mail, la propagation des listes
et des configs HAProxy, ainsi que l'élection de maître.

Règle de numérotation :
  - Chaque nouveau LB reçoit le numéro suivant dans la liste (len + 1).
  - Quand un LB disparaît, tous ceux qui avaient un numéro supérieur
    descendent d'un cran (renumérotation compacte).
  - Pas de mémoire entre sessions : un LB qui revient après un crash
    n'est pas reconnu et prend une nouvelle place en bas de liste.
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
from dataclasses import dataclass
from typing import Optional

import etcd_client

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

log = logging.getLogger("master")
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
# Structures de données
# ─────────────────────────────────────────────

@dataclass
class NodeInfo:
    number: int
    ip: str
    dns: str
    role: str          # "lb" | "mail"
    websocket: object  # websockets.WebSocketServerProtocol (non sérialisé)

    def to_dict(self) -> dict:
        """Sérialise le nœud sans le websocket (pour propagation JSON)."""
        return {"number": self.number, "ip": self.ip, "dns": self.dns, "role": self.role}


class State:
    """État global du daemon maître. Aucune mémoire persistante entre sessions."""

    def __init__(self):
        # clé = websocket, valeur = NodeInfo — ordre d'insertion conservé (dict Python 3.7+)
        self.lb_nodes:   dict[object, NodeInfo] = {}
        self.mail_nodes: dict[object, NodeInfo] = {}

    # ── Numérotation des LB ──────────────────────────────────────────────────

    def next_lb_number(self) -> int:
        """Attribue le numéro suivant : simplement len(lb_nodes) + 1."""
        return len(self.lb_nodes) + 1

    def renumber_lb(self) -> None:
        """
        Renumérotation compacte après suppression d'un LB.
        Les LB sont triés par leur numéro courant, puis renumérotés 1, 2, 3…
        Exemple : LB#1, LB#3 → LB#1, LB#2 (LB#3 monte d'un cran).
        """
        sorted_nodes = sorted(self.lb_nodes.values(), key=lambda n: n.number)
        for new_number, node in enumerate(sorted_nodes, start=1):
            if node.number != new_number:
                log.info(
                    f"[RENUMBER] LB {node.dns} : #{node.number} → #{new_number}"
                )
            node.number = new_number

    # ── Sérialisation ────────────────────────────────────────────────────────

    def lb_list(self) -> list[dict]:
        """Retourne la liste LB triée par numéro."""
        return [n.to_dict() for n in sorted(self.lb_nodes.values(), key=lambda n: n.number)]

    def mail_list(self) -> list[dict]:
        return [n.to_dict() for n in self.mail_nodes.values()]


STATE = State()

# ─────────────────────────────────────────────
# DNS (etcd/CoreDNS, lu par toutes les briques du projet)
# ─────────────────────────────────────────────
# Sur le déploiement On-Prem retenu (cf. rapport), le DNS public est géré via
# CoreDNS (backend etcd) plutôt que l'API OVH — l'API OVH n'entre en jeu que
# pour la délégation de zone elle-même (hors scope de ce daemon). Ce nom
# "master.<domain>" est un pointeur de contrôle interne (les esclaves s'y
# connectent pour trouver le maître courant), distinct du DNS public
# mail.<domain> utilisé par les clients (cf. etcd_client.register_mail_entrypoint,
# appelé depuis entrypoint.sh au démarrage/arrêt de chaque nœud HAProxy).

DNS_ETCD_URL = CFG.get("dns", {}).get("etcd_url", "http://etcd:2379")


async def update_dns(ip: str, dns: str) -> None:
    """Met à jour l'enregistrement DNS `dns` -> `ip` dans etcd/CoreDNS."""
    label = dns.split(".")[0]
    domain = ".".join(dns.split(".")[1:]) or CFG.get("dns", {}).get("domain", "securepulse.fr")
    path = "/skydns/" + "/".join(reversed(domain.split(".")))
    try:
        await etcd_client.put(DNS_ETCD_URL, f"{path}/{label}", json.dumps({"host": ip}))
        log.info(f"[DNS] Mise à jour : {dns} → {ip}")
    except Exception as exc:
        log.error(f"[DNS] Échec de mise à jour de {dns} : {exc}")


# ─────────────────────────────────────────────
# Envoi de messages
# ─────────────────────────────────────────────

async def send(ws, payload: dict) -> None:
    """Envoie un message JSON sur un websocket, ignore si déjà fermé."""
    try:
        await ws.send(json.dumps(payload))
    except ConnectionClosed:
        pass


async def broadcast_list() -> None:
    """
    Propage la liste complète LB + mail à tous les nœuds connectés.
    Chaque LB reçoit également son propre numéro mis à jour dans
    le champ 'your_number' pour se resynchroniser après renumérotation.
    """
    lb_list   = STATE.lb_list()
    mail_list = STATE.mail_list()

    # Construction d'un index ip→numéro pour le champ 'your_number'
    number_by_ip = {n.ip: n.number for n in STATE.lb_nodes.values()}

    log.info(
        f"[BROADCAST] Propagation liste → "
        f"LB: {len(STATE.lb_nodes)}, Mail: {len(STATE.mail_nodes)}"
    )

    tasks = []
    for ws, node in list(STATE.lb_nodes.items()):
        msg = {
            "type":        "update_list",
            "lb_list":     lb_list,
            "mail_list":   mail_list,
            "your_number": node.number,   # numéro à jour pour ce LB spécifique
        }
        tasks.append(send(ws, msg))

    # Les srv-mail reçoivent aussi la liste (sans 'your_number')
    for ws in list(STATE.mail_nodes.keys()):
        tasks.append(send(ws, {
            "type":      "update_list",
            "lb_list":   lb_list,
            "mail_list": mail_list,
        }))

    await asyncio.gather(*tasks)


async def broadcast_haproxy_config(haproxy_config: str) -> None:
    """Propage une nouvelle config HAProxy à tous les LB."""
    msg = {"type": "update_config", "haproxy_config": haproxy_config}
    log.info(f"[BROADCAST] Propagation config HAProxy → {len(STATE.lb_nodes)} LB")
    await asyncio.gather(*(send(ws, msg) for ws in STATE.lb_nodes.keys()))


# ─────────────────────────────────────────────
# Gestion des connexions entrantes
# ─────────────────────────────────────────────

async def handle_connection(ws) -> None:
    """
    Gère le cycle de vie complet d'une connexion entrante :
    enregistrement → échanges → déconnexion + renumérotation.
    """
    remote = ws.remote_address
    log.info(f"[CONNECT] Nouvelle connexion depuis {remote}")

    node: Optional[NodeInfo] = None

    try:
        # ── Attente du message d'enregistrement ──────────────────────────
        raw = await ws.recv()
        msg = json.loads(raw)

        if msg.get("type") != "register":
            log.warning(f"[REGISTER] Message inattendu depuis {remote} : {msg}")
            await ws.close(1002, "Premier message doit être 'register'")
            return

        role = msg.get("role")
        ip   = msg.get("ip", "")
        dns  = msg.get("dns", "")

        if role == "lb":
            # Numéro = position dans la liste (pas de mémoire entre sessions)
            number = STATE.next_lb_number()
            node = NodeInfo(number=number, ip=ip, dns=dns, role="lb", websocket=ws)
            STATE.lb_nodes[ws] = node
            log.info(f"[REGISTER] LB #{number} enregistré — IP={ip} DNS={dns}")

        elif role == "mail":
            number = len(STATE.mail_nodes) + 1
            node = NodeInfo(number=number, ip=ip, dns=dns, role="mail", websocket=ws)
            STATE.mail_nodes[ws] = node
            log.info(f"[REGISTER] Mail #{number} enregistré — IP={ip} DNS={dns}")

        else:
            log.warning(f"[REGISTER] Rôle inconnu '{role}' depuis {remote}")
            await ws.close(1002, "Rôle inconnu")
            return

        # ── Réponse de bienvenue ─────────────────────────────────────────
        welcome = {
            "type":            "welcome",
            "assigned_number": node.number,
            "lb_list":         STATE.lb_list(),
            "mail_list":       STATE.mail_list(),
        }
        await send(ws, welcome)

        # ── Propagation de la nouvelle liste à tout le monde ─────────────
        await broadcast_list()

        # ── Boucle de réception des messages du nœud ─────────────────────
        async for raw in ws:
            await handle_message(ws, node, json.loads(raw))

    except ConnectionClosed as exc:
        log.warning(f"[DISCONNECT] Connexion fermée depuis {remote} : {exc}")
    except json.JSONDecodeError as exc:
        log.error(f"[PROTOCOL] JSON invalide depuis {remote} : {exc}")
    except Exception as exc:
        log.exception(f"[ERROR] Erreur inattendue pour {remote} : {exc}")
    finally:
        # ── Nettoyage + renumérotation ────────────────────────────────────
        if node is not None:
            if node.role == "lb" and ws in STATE.lb_nodes:
                del STATE.lb_nodes[ws]
                log.info(
                    f"[DISCONNECT] LB #{node.number} ({node.dns}) retiré — "
                    f"renumérotation en cours"
                )
                # Tous les LB avec un numéro > au LB retiré descendent d'un cran
                STATE.renumber_lb()

            elif node.role == "mail" and ws in STATE.mail_nodes:
                del STATE.mail_nodes[ws]
                log.info(f"[DISCONNECT] Mail #{node.number} ({node.dns}) retiré")

            # Repropagate la liste avec les numéros mis à jour
            await broadcast_list()


async def handle_message(ws, node: NodeInfo, msg: dict) -> None:
    """Traite un message reçu d'un nœud déjà enregistré."""
    mtype = msg.get("type")

    if mtype == "update_config":
        haproxy_config = msg.get("haproxy_config", "")
        log.info(f"[CONFIG] Nouvelle config reçue de LB #{node.number}, propagation...")
        await broadcast_haproxy_config(haproxy_config)

    elif mtype == "new_master":
        log.info(
            f"[ELECTION] Nouveau maître annoncé par LB #{node.number} : "
            f"IP={msg.get('ip')} DNS={msg.get('dns')}"
        )
        await update_dns(msg.get("ip", ""), msg.get("dns", ""))
        await asyncio.gather(
            *(send(w, msg) for w in STATE.lb_nodes.keys() if w is not ws)
        )

    elif mtype == "healthcheck_alert":
        # Un LB signale qu'un srv-mail est hors ligne
        failed_ip = msg.get("ip")
        log.warning(
            f"[HEALTHCHECK] Alerte : srv-mail {failed_ip} "
            f"signalé hors ligne par LB #{node.number}"
        )
        to_remove = [w for w, n in STATE.mail_nodes.items() if n.ip == failed_ip]
        for w in to_remove:
            del STATE.mail_nodes[w]
            log.info(f"[HEALTHCHECK] Mail {failed_ip} retiré")
        if to_remove:
            await broadcast_list()

    else:
        log.debug(f"[MSG] Message non géré de LB #{node.number} : {msg}")


# ─────────────────────────────────────────────
# Interface CLI admin
# ─────────────────────────────────────────────

async def cli_admin() -> None:
    # En conteneur Docker (pas de TTY attaché, ou promotion d'un maître
    # embarqué via slave_daemon.start_embedded_master()), stdin pointe vers
    # /dev/null ou un pipe fermé : l'enregistrer auprès du sélecteur epoll
    # échoue avec EPERM. Cet échec se produit de façon DIFFÉRÉE (callback
    # asyncio interne programmé par connect_read_pipe), un try/except autour
    # du seul `await` ne suffit pas à l'intercepter — d'où la vérification
    # stricte d'un vrai TTY avant même de tenter quoi que ce soit. Le CLI
    # admin est un outil d'exploitation bare-metal (cf. README) : sans TTY,
    # on le désactive silencieusement plutôt que de polluer les logs à
    # chaque promotion de maître.
    if not sys.stdin.isatty():
        log.debug("[CLI] pas de TTY, CLI admin désactivé")
        return

    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    print("=== SecurePulse Master CLI ===")
    print("Commandes : list | push_config <fichier> | help")

    while True:
        try:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode().strip()
            if not line:
                continue

            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()

            if cmd == "list":
                print(f"\n-- LB actifs ({len(STATE.lb_nodes)}) --")
                for n in sorted(STATE.lb_nodes.values(), key=lambda x: x.number):
                    print(f"  #{n.number}  {n.dns}  {n.ip}")
                print(f"-- Mail actifs ({len(STATE.mail_nodes)}) --")
                for n in STATE.mail_nodes.values():
                    print(f"  #{n.number}  {n.dns}  {n.ip}")
                print()

            elif cmd == "push_config":
                if len(parts) < 2:
                    print("Usage : push_config <chemin_fichier>")
                    continue
                try:
                    with open(parts[1], "r", encoding="utf-8") as f:
                        await broadcast_haproxy_config(f.read())
                    print(f"Config propagee a {len(STATE.lb_nodes)} LB.")
                except FileNotFoundError:
                    print(f"Fichier introuvable : {parts[1]}")

            elif cmd == "help":
                print("  list                  — affiche tous les noeuds actifs")
                print("  push_config <fichier> — propage une config HAProxy")

            else:
                print(f"Commande inconnue : {cmd}")

        except Exception as exc:
            log.error(f"[CLI] Erreur : {exc}")


# ─────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────

async def main() -> None:
    host = "0.0.0.0"
    port = CFG["master"]["port"]
    log.info(f"[MASTER] Démarrage sur {host}:{port}")
    server = await websockets.serve(handle_connection, host, port)
    log.info(f"[MASTER] En écoute sur ws://{host}:{port}")
    asyncio.create_task(cli_admin())
    await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("[MASTER] Arrêt demandé")
