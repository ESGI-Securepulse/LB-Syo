FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# wireguard-tools : `wg`/`ip link ... type wireguard` (implémentation noyau,
#   nécessite Linux >= 5.6 avec WireGuard — mainline depuis 2020, standard
#   sur toute distro récente ; NET_ADMIN suffit, aucune install sur l'hôte)
# python3 + pip : agent de maillage auto-géré (wireguard_agent.py)
# iptables : ce conteneur agit comme passerelle site-à-site (forwarding
#   entre le réseau local du DC et l'overlay, cf.
#   wireguard_agent.py::sync_gateway_routes) pour les déploiements
#   multi-hôtes réels — sans quoi seul le conteneur wireguard lui-même
#   serait joignable depuis les autres sites, pas ses voisins du même DC.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wireguard-tools iproute2 iputils-ping iptables \
        python3 python3-pip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --break-system-packages --no-cache-dir aiohttp

WORKDIR /opt/wireguard
COPY wireguard_agent.py .

EXPOSE 51820/udp

ENTRYPOINT ["python3", "/opt/wireguard/wireguard_agent.py"]
