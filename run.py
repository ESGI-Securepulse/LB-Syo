#!/usr/bin/env python3
"""
run.py — point d'entrée process pour le conteneur Docker LB-Syo.

Le déploiement bare-metal historique (deploy.sh, Alpine/OpenRC) lance
slave_daemon.py seul (+ master_daemon.py à la main sur le nœud
désigné maître, cf. README "Désignation du maître initial"). Cette version
Docker reproduit exactement ce même modèle, mais dans un seul process
asyncio (ROLE=master lance master_daemon.main() ET slave_daemon.main()
ensemble, comme le fait déjà slave_daemon.start_embedded_master() lors
d'une promotion live) plutôt que deux services système séparés.
"""

import asyncio
import logging
import os
import signal

import etcd_client
import master_daemon
import slave_daemon

log = logging.getLogger("run")

ROLE = os.environ.get("ROLE", "slave")


async def main() -> None:
    cfg = slave_daemon.CFG
    etcd_url = cfg.get("dns", {}).get("etcd_url", "http://etcd:2379")
    domain = cfg.get("dns", {}).get("domain", "securepulse.fr")
    site = cfg["node"].get("site", "local")
    my_ip = cfg["node"]["ip"]

    tasks = []
    if ROLE == "master":
        log.info("[RUN] Rôle=master — démarrage de master_daemon + slave_daemon")
        tasks.append(asyncio.create_task(master_daemon.main()))
        # Laisse le serveur websocket du maître démarrer avant que l'esclave
        # local ne tente de s'y connecter (auto-connexion à soi-même).
        await asyncio.sleep(1)
        # Équivalent Docker de la désignation manuelle du maître initial
        # (cf. README "Le LB #1 ne se proclame jamais maître automatiquement
        # [...] l'administrateur doit désigner le maître manuellement") :
        # ROLE=master EST cette désignation, donc c'est ce process qui pousse
        # le premier enregistrement DNS master.<domain> lui-même, plutôt que
        # de compter sur le mécanisme d'élection (qui ne se déclenche
        # légitimement qu'après une PREMIÈRE connexion réussie, cf.
        # slave_daemon.STATE.ever_connected).
        await slave_daemon.update_dns(my_ip, f"master.{domain}")
    else:
        log.info("[RUN] Rôle=slave")

    tasks.append(asyncio.create_task(slave_daemon.main()))

    await etcd_client.register_mail_entrypoint(etcd_url, domain, site, my_ip)
    log.info(f"[RUN] Point d'entrée mail.{domain} enregistré (site={site}, ip={my_ip})")

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _stop(*_args):
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop)

    await stop.wait()
    log.info("[RUN] Arrêt demandé — désenregistrement DNS")
    await etcd_client.deregister_mail_entrypoint(etcd_url, domain, site)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="[%(asctime)s] %(levelname)s %(name)s — %(message)s")
    asyncio.run(main())
