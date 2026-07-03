FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# wireguard-tools : `wg`/`ip link ... type wireguard` (implémentation noyau,
#   nécessite Linux >= 5.6 avec WireGuard — mainline depuis 2020, standard
#   sur toute distro récente ; NET_ADMIN suffit, aucune install sur l'hôte)
# python3 + pip : agent de maillage auto-géré (wireguard_agent.py)
RUN apt-get update && apt-get install -y --no-install-recommends \
        wireguard-tools iproute2 iputils-ping \
        python3 python3-pip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --break-system-packages --no-cache-dir aiohttp

WORKDIR /opt/wireguard
COPY wireguard_agent.py .

EXPOSE 51820/udp

ENTRYPOINT ["python3", "/opt/wireguard/wireguard_agent.py"]
