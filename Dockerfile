FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# haproxy       : le load balancer lui-même
# python3 + pip : daemons d'élection maître/esclave, healthcheck, etcd_client
# dnsutils      : dig, pour le debug/tests de résolution DNS dynamique
RUN apt-get update && apt-get install -y --no-install-recommends \
        haproxy \
        python3 python3-pip \
        curl jq iproute2 dnsutils \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --break-system-packages --no-cache-dir websockets aiohttp pyyaml

WORKDIR /opt/securepulse

COPY master_daemon.py slave_daemon.py healthcheck.py haproxy_manager.py \
     etcd_client.py run.py config.yaml ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 25 587 465 143 993 9000 8765

ENTRYPOINT ["/entrypoint.sh"]
