#!/usr/bin/env python3
"""
wireguard_agent.py — Maillage WireGuard site-à-site auto-géré (SecurePulse).

Brique manquante fusionnée dans LB-Syo (cf. rapport §40, §53 : "Un accès
VPN par DC (Wireguard)", "permet aux techniciens de se connecter à distance
[...] et aux services non-exposés de se synchroniser entre eux").

Comportement :
  1. Génère (ou recharge depuis un volume) une paire de clés WireGuard pour
     ce site au premier démarrage.
  2. Crée l'interface wg0 (implémentation noyau — nécessite un noyau Linux
     >= 5.6 avec WireGuard, standard sur toute distro récente ; capacité
     conteneur NET_ADMIN uniquement, aucune modification de l'hôte).
  3. S'enregistre dans etcd (pubkey, endpoint, IP overlay) — même etcd que
     tout le reste du projet (LDAP/Mail/DNS/storage-lucien).
  4. Boucle de découverte des pairs (toutes les 15s, même pattern que
     LDAP/entrypoint.sh watch_peers) : ajoute/retire les peers WireGuard des
     autres sites automatiquement -> maillage complet, ajout/suppression de
     DC = ajout/suppression automatique du peer (Q&A rapport §72-73).
  5. Génère un profil "road warrior" pour l'accès technicien distant, avec
     accès à l'ensemble du maillage overlay (AllowedIPs = overlay_cidr).
"""

import asyncio
import base64
import ipaddress
import json
import logging
import os
import signal
import subprocess
from pathlib import Path

import aiohttp

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] %(levelname)s wireguard_agent — %(message)s",
)
log = logging.getLogger("wireguard_agent")

ETCD_URL = os.environ.get("ETCD_URL", "http://etcd:2379")
SITE = os.environ.get("SITE", "local")
WG_IFACE = os.environ.get("WG_IFACE", "wg0")
WG_LISTEN_PORT = int(os.environ.get("WG_LISTEN_PORT", "51820"))
OVERLAY_CIDR = os.environ.get("WG_OVERLAY_CIDR", "10.99.0.0/16")
SITE_PREFIX = os.environ.get("WG_SITE_PREFIX")  # ex: "10.10.1." — préfixe IP du site pour l'endpoint
KEY_DIR = Path(os.environ.get("WG_KEY_DIR", "/etc/wireguard"))
TECH_DIR = Path(os.environ.get("WG_TECH_DIR", "/etc/wireguard/technicians"))
WATCH_INTERVAL = int(os.environ.get("WG_WATCH_INTERVAL", "15"))
# Passerelle site-à-site (déploiement multi-hôtes réel uniquement) : si non
# vide, ce conteneur route vers l'overlay le trafic venant des autres
# conteneurs du même DC (storage-lucien, LDAP, Mail, HAProxy) à destination
# des adresses que les AUTRES sites annoncent dans etcd (voir
# sync_gateway_routes). Sur le banc de test mono-hôte (tous les sites sur un
# même sous-réseau plat), ce mécanisme est inutile et reste désactivé :
# activé uniquement quand WG_GATEWAY_ROUTING=1.
GATEWAY_ROUTING = os.environ.get("WG_GATEWAY_ROUTING", "0") == "1"
# Préfixes etcd à surveiller pour découvrir les adresses IP « du site » à
# rendre joignables depuis les autres DC via ce tunnel (storage-nodes pour
# la géo-réplication GlusterFS, skydns pour le failover LDAP/Mail/HAProxy
# inter-site déclenché par résolution DNS vers le pool "all.<domaine>").
GATEWAY_ROUTE_PREFIXES = ["/storage-nodes/", "/skydns/fr/securepulse/"]

_running = True
_technician_pubkey: str | None = None  # exclu du nettoyage "stale" de sync_peers


def sh(*args: str, check: bool = True) -> str:
    res = subprocess.run(args, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(args)}\n{res.stderr}")
    return res.stdout.strip()


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _b64d(s: str) -> str:
    return base64.b64decode(s.encode()).decode()


# ── etcd helpers (même convention que le reste du projet) ───────────────────

async def etcd_put(session: aiohttp.ClientSession, key: str, value: str) -> None:
    await session.post(f"{ETCD_URL}/v3/kv/put", json={"key": _b64(key), "value": _b64(value)})


async def etcd_delete(session: aiohttp.ClientSession, key: str) -> None:
    await session.post(f"{ETCD_URL}/v3/kv/deleterange", json={"key": _b64(key)})


async def etcd_list(session: aiohttp.ClientSession, prefix: str) -> dict[str, dict]:
    end = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix.endswith("/") else prefix + "\x00"
    async with session.post(
        f"{ETCD_URL}/v3/kv/range", json={"key": _b64(prefix), "range_end": _b64(end)}
    ) as resp:
        data = await resp.json()
        out = {}
        for kv in data.get("kvs", []):
            key = _b64d(kv["key"])
            out[key] = json.loads(_b64d(kv["value"]))
        return out


async def wait_etcd(session: aiohttp.ClientSession) -> None:
    log.info("waiting for etcd...")
    while True:
        try:
            async with session.get(f"{ETCD_URL}/health") as resp:
                if resp.status == 200:
                    break
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(1)
    log.info("etcd ready")


# ── détection d'IP locale (même logique que storage-lucien/entrypoint.sh) ──

def detect_my_ip() -> str:
    if SITE_PREFIX:
        out = sh("ip", "-4", "addr")
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet ") and SITE_PREFIX in line:
                return line.split()[1].split("/")[0]
        raise RuntimeError(f"no interface found matching WG_SITE_PREFIX={SITE_PREFIX}")
    # Par défaut : première IP non-loopback trouvée.
    out = sh("hostname", "-I")
    return out.split()[0]


# ── allocation d'IP overlay (best-effort via etcd, cf. commentaire) ────────

async def allocate_overlay_ip(session: aiohttp.ClientSession) -> str:
    """
    Alloue une IP dans OVERLAY_CIDR pour ce site. Réutilise l'allocation
    existante si ce site en a déjà une (idempotent au redémarrage). Sinon,
    prend le plus petit entier libre. Best-effort (pas de verrou
    distribué/transaction etcd) : accepte un risque de collision rare en cas
    de démarrage strictement simultané de deux nouveaux sites, cohérent avec
    le niveau de rigueur du reste du projet (etcd utilisé partout en put/get
    simples, jamais de txn). Documenté comme limitation connue.
    """
    existing = await etcd_list(session, "/wireguard/peers/")
    mine = existing.get(f"/wireguard/peers/{SITE}")
    if mine and mine.get("overlay_ip"):
        return mine["overlay_ip"]

    network = ipaddress.ip_network(OVERLAY_CIDR)
    used = {v["overlay_ip"] for v in existing.values() if v.get("overlay_ip")}
    hosts = network.hosts()
    next(hosts)  # .1 réservée (jamais utilisée, marge de manœuvre pour un futur witness/qdevice)
    for candidate in hosts:
        candidate_s = f"{candidate}"
        if candidate_s not in used:
            return candidate_s
    raise RuntimeError("overlay CIDR exhausted")


# ── clés WireGuard ───────────────────────────────────────────────────────────

def ensure_keypair(name: str) -> tuple[str, str]:
    """Génère (ou recharge) une paire de clés persistée sous KEY_DIR/<name>.{priv,pub}."""
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    priv_path = KEY_DIR / f"{name}.priv"
    pub_path = KEY_DIR / f"{name}.pub"
    if not priv_path.exists():
        priv = sh("wg", "genkey")
        priv_path.write_text(priv + "\n")
        priv_path.chmod(0o600)
        pub = sh("bash", "-c", f"echo '{priv}' | wg pubkey")
        pub_path.write_text(pub + "\n")
    return priv_path.read_text().strip(), pub_path.read_text().strip()


# ── interface wg0 ────────────────────────────────────────────────────────────

def setup_interface(privkey: str, overlay_ip: str) -> None:
    existing = sh("ip", "link", "show", WG_IFACE, check=False)
    if not existing:
        sh("ip", "link", "add", WG_IFACE, "type", "wireguard")
        sh("ip", "addr", "add", f"{overlay_ip}/16", "dev", WG_IFACE)
        sh("ip", "link", "set", WG_IFACE, "up")

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    priv_file = KEY_DIR / "wg0.priv.tmp"
    priv_file.write_text(privkey + "\n")
    priv_file.chmod(0o600)
    sh("wg", "set", WG_IFACE, "listen-port", str(WG_LISTEN_PORT), "private-key", str(priv_file))
    priv_file.unlink()


def enable_forwarding() -> None:
    """Fait de ce conteneur la passerelle site-à-site de son DC (déploiement
    réel multi-hôtes uniquement, cf. GATEWAY_ROUTING). Namespace réseau du
    conteneur uniquement (capacité NET_ADMIN) — aucune modification de
    l'hôte : `net.ipv4.ip_forward` est per-netns, pas un réglage sysctl
    global du noyau hôte."""
    Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n")
    sh("iptables", "-P", "FORWARD", "ACCEPT", check=False)
    log.info("gateway routing enabled: ip_forward=1, FORWARD policy=ACCEPT")


async def discover_site_addresses(session: aiohttp.ClientSession, site: str) -> set[str]:
    """Adresses IP « appartenant » à un site (nœuds storage-lucien pour la
    géo-réplication, services enregistrés dans etcd/skydns pour le failover
    LDAP/Mail/HAProxy inter-site) à rendre joignables depuis les autres DC."""
    addrs: set[str] = set()
    for prefix in GATEWAY_ROUTE_PREFIXES:
        try:
            entries = await etcd_list(session, f"{prefix}{site}/")
        except (aiohttp.ClientError, json.JSONDecodeError):
            continue
        for info in entries.values():
            host = info.get("host")
            if host:
                addrs.add(host)
    return addrs


def sync_gateway_routes(wanted_ips: set[str]) -> None:
    """Installe les routes noyau locales (conteneur wireguard uniquement)
    permettant de forwarder vers wg0 le trafic à destination des adresses
    des autres sites. Complément indispensable de allowed-ips : `wg set`
    (contrairement à wg-quick) ne fait la gestion des routes tout seul."""
    for ip in wanted_ips:
        sh("ip", "route", "replace", f"{ip}/32", "dev", WG_IFACE, check=False)


def sync_peers(
    current_peers: dict[str, dict],
    my_pubkey: str,
    my_ip: str,
    gateway_addrs: dict[str, set[str]] | None = None,
) -> None:
    """Ajoute/retire les peers WireGuard pour matcher exactement ce qu'annonce etcd."""
    gateway_addrs = gateway_addrs or {}
    wanted = {}
    for key, info in current_peers.items():
        site = key.rsplit("/", 1)[-1]
        if site == SITE:
            continue
        pubkey = info.get("pubkey")
        endpoint = info.get("endpoint")
        overlay_ip = info.get("overlay_ip")
        if not (pubkey and endpoint and overlay_ip):
            continue
        wanted[pubkey] = (endpoint, overlay_ip, site)

    dump = sh("wg", "show", WG_IFACE, "dump", check=False)
    existing_pubkeys = set()
    for line in dump.splitlines()[1:]:  # 1ère ligne = interface elle-même
        parts = line.split("\t")
        if parts:
            existing_pubkeys.add(parts[0])

    # Le peer technicien (road warrior) n'est pas annoncé dans etcd (ce n'est
    # pas un site) : sans cette exclusion, sync_peers le retirerait comme
    # "stale" dès son premier passage, juste après generate_technician_profile()
    # l'ait ajouté.
    if _technician_pubkey:
        existing_pubkeys.discard(_technician_pubkey)

    all_gateway_ips: set[str] = set()
    for pubkey, (endpoint, overlay_ip, site) in wanted.items():
        extra_ips = gateway_addrs.get(site, set())
        all_gateway_ips |= extra_ips
        allowed = ",".join([f"{overlay_ip}/32"] + [f"{ip}/32" for ip in sorted(extra_ips)])
        if pubkey not in existing_pubkeys:
            log.info(f"adding peer site={site} pubkey={pubkey[:12]}... endpoint={endpoint}")
            sh(
                "wg", "set", WG_IFACE, "peer", pubkey,
                "endpoint", f"{endpoint}:{WG_LISTEN_PORT}",
                "allowed-ips", allowed,
                "persistent-keepalive", "25",
            )
        elif GATEWAY_ROUTING and extra_ips:
            # Peer déjà là (handshake établi) : ne réinitialise que
            # allowed-ips, sans toucher endpoint/keepalive, pour ne pas
            # perturber une session en cours si de nouveaux nœuds
            # storage/service apparaissent côté distant après coup.
            sh("wg", "set", WG_IFACE, "peer", pubkey, "allowed-ips", allowed, check=False)

    if GATEWAY_ROUTING and all_gateway_ips:
        sync_gateway_routes(all_gateway_ips)

    stale = existing_pubkeys - set(wanted.keys())
    for pubkey in stale:
        log.info(f"removing stale peer pubkey={pubkey[:12]}...")
        sh("wg", "set", WG_IFACE, "peer", pubkey, "remove", check=False)


# ── profil technicien (road warrior) ─────────────────────────────────────────

def generate_technician_profile(server_pubkey: str, my_ip: str, overlay_cidr: str) -> None:
    global _technician_pubkey
    TECH_DIR.mkdir(parents=True, exist_ok=True)
    admin_priv, admin_pub = ensure_keypair("technician")
    _technician_pubkey = admin_pub
    admin_ip_path = KEY_DIR / "technician.overlay_ip"
    if not admin_ip_path.exists():
        # .254 de la plage overlay du site, réservée aux techniciens (best-effort,
        # collision improbable vu le nombre de sites en jeu dans ce projet).
        network = ipaddress.ip_network(overlay_cidr)
        admin_ip_path.write_text(f"{list(network.hosts())[-2]}\n")
    admin_ip = admin_ip_path.read_text().strip()

    sh(
        "wg", "set", WG_IFACE, "peer", admin_pub,
        "allowed-ips", f"{admin_ip}/32",
    )

    profile = f"""# Profil technicien — accès distant SecurePulse (site={SITE})
# A distribuer hors-bande a l'administrateur reseau (pas de secret en clair
# dans les repos Git : ce fichier est généré à l'exécution uniquement).
[Interface]
PrivateKey = {admin_priv}
Address = {admin_ip}/32

[Peer]
PublicKey = {server_pubkey}
Endpoint = {my_ip}:{WG_LISTEN_PORT}
AllowedIPs = {overlay_cidr}
PersistentKeepalive = 25
"""
    (TECH_DIR / f"{SITE}.conf").write_text(profile)
    log.info(f"technician profile written to {TECH_DIR / f'{SITE}.conf'}")


# ── cycle de vie ──────────────────────────────────────────────────────────────

async def register(session: aiohttp.ClientSession, pubkey: str, my_ip: str, overlay_ip: str) -> None:
    value = json.dumps({"pubkey": pubkey, "endpoint": my_ip, "overlay_ip": overlay_ip})
    await etcd_put(session, f"/wireguard/peers/{SITE}", value)
    log.info(f"registered site={SITE} endpoint={my_ip} overlay_ip={overlay_ip}")


async def deregister(session: aiohttp.ClientSession) -> None:
    await etcd_delete(session, f"/wireguard/peers/{SITE}")
    log.info(f"deregistered site={SITE}")


async def watch_loop(session: aiohttp.ClientSession, my_pubkey: str, my_ip: str) -> None:
    while _running:
        try:
            peers = await etcd_list(session, "/wireguard/peers/")
            gateway_addrs = {}
            if GATEWAY_ROUTING:
                for key in peers:
                    site = key.rsplit("/", 1)[-1]
                    if site != SITE:
                        gateway_addrs[site] = await discover_site_addresses(session, site)
            sync_peers(peers, my_pubkey, my_ip, gateway_addrs)
        except Exception as exc:
            log.warning(f"peer sync failed: {exc}")
        await asyncio.sleep(WATCH_INTERVAL)


async def main() -> None:
    global _running

    async with aiohttp.ClientSession() as session:
        await wait_etcd(session)

        my_ip = detect_my_ip()
        overlay_ip = await allocate_overlay_ip(session)
        privkey, pubkey = ensure_keypair("wg0")

        setup_interface(privkey, overlay_ip)
        if GATEWAY_ROUTING:
            enable_forwarding()
        await register(session, pubkey, my_ip, overlay_ip)
        generate_technician_profile(pubkey, my_ip, OVERLAY_CIDR)

        loop = asyncio.get_running_loop()

        def _stop(*_args):
            global _running
            _running = False

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _stop)

        log.info(f"ready — site={SITE} overlay_ip={overlay_ip} pubkey={pubkey[:12]}...")
        try:
            await watch_loop(session, pubkey, my_ip)
        finally:
            await deregister(session)


if __name__ == "__main__":
    asyncio.run(main())
