#!/usr/bin/env python3
"""
etcd_client.py — Client etcd v3 minimal (HTTP gateway) pour LB-Syo.

Remplace le update_dns() mocké de master_daemon.py/slave_daemon.py par de
vrais appels etcd, sur le même modèle que les autres briques du projet
(LDAP/, Mail/, DNS/, storage-lucien/) qui s'enregistrent toutes dans le
même etcd, lu dynamiquement par CoreDNS (DNS/Corefile, backend etcd).

Utilisé pour deux choses distinctes :
  - pointer de contrôle "master.<domain>" (qui est le daemon maître actuel)
  - enregistrement/désenregistrement de CE nœud comme point d'entrée mail
    valide pour son site (repris du comportement de LB-Lucien/entrypoint.sh)
"""

import base64
import json
from typing import Optional

import aiohttp


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _b64d(s: str) -> str:
    return base64.b64decode(s.encode()).decode()


async def put(etcd_url: str, key: str, value: str) -> None:
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{etcd_url}/v3/kv/put",
            json={"key": _b64(key), "value": _b64(value)},
        )


async def delete(etcd_url: str, key: str) -> None:
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{etcd_url}/v3/kv/deleterange",
            json={"key": _b64(key)},
        )


async def get(etcd_url: str, key: str) -> Optional[str]:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{etcd_url}/v3/kv/range",
            json={"key": _b64(key)},
        ) as resp:
            data = await resp.json()
            kvs = data.get("kvs") or []
            if not kvs:
                return None
            return _b64d(kvs[0]["value"])


def _reversed_path(domain: str, label: str) -> str:
    """fr/securepulse (from domain=securepulse.fr) + label -> etcd path prefix."""
    return "/skydns/" + "/".join(reversed(domain.split("."))) + f"/{label}"


async def register_mail_entrypoint(etcd_url: str, domain: str, site: str, ip: str) -> None:
    """
    Enregistre ce HAProxy comme point d'entrée mail valide (A record public
    mail.<domain>, résolu par le client/MX — cf. DNS/db.securepulse.fr).
    Reprend exactement la convention de LB-Lucien/entrypoint.sh (fusionné
    ici) : clé etcd .../mail/<site> -> FQDN mail.<domain>, <site> n'est que
    le suffixe d'unicité (comme le fait déjà LDAP/Mail avec <hostname>).
    """
    value = json.dumps({"host": ip})
    await put(etcd_url, f"{_reversed_path(domain, 'mail')}/{site}-lb", value)


async def deregister_mail_entrypoint(etcd_url: str, domain: str, site: str) -> None:
    await delete(etcd_url, f"{_reversed_path(domain, 'mail')}/{site}-lb")
