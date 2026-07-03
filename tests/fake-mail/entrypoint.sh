#!/bin/bash
set -e

ETCD_URL="${ETCD_URL:-http://etcd:2379}"
SITE="${SITE:-local}"
SERVICE="${SERVICE:-postfix}"   # postfix | dovecot
DOMAIN="${DOMAIN:-securepulse.fr}"

_b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

wait_etcd() {
    until curl -sf "${ETCD_URL}/health" > /dev/null 2>&1; do sleep 1; done
}

etcd_put() {
    curl -sf -X POST "${ETCD_URL}/v3/kv/put" \
        -H 'Content-Type: application/json' \
        -d "{\"key\":\"$(_b64 "$1")\",\"value\":\"$(_b64 "$2")\"}" > /dev/null
}

etcd_del() {
    curl -sf -X POST "${ETCD_URL}/v3/kv/deleterange" \
        -H 'Content-Type: application/json' \
        -d "{\"key\":\"$(_b64 "$1")\"}" > /dev/null
}

get_my_ip() { hostname -i | awk '{print $1}'; }

MY_IP=$(get_my_ip)
PATH_PREFIX="/skydns/$(printf '%s' "$DOMAIN" | awk -F. '{for(i=NF;i>=1;i--) printf "%s/", $i}')"

register() {
    val="{\"host\":\"${MY_IP}\"}"
    etcd_put "${PATH_PREFIX}${SITE}/${SERVICE}/${HOSTNAME}" "$val"
    etcd_put "${PATH_PREFIX}all/${SERVICE}/${HOSTNAME}" "$val"
    echo "[register] ${SERVICE} ${HOSTNAME} site=${SITE} ip=${MY_IP}"
}

deregister() {
    etcd_del "${PATH_PREFIX}${SITE}/${SERVICE}/${HOSTNAME}"
    etcd_del "${PATH_PREFIX}all/${SERVICE}/${HOSTNAME}"
}

cleanup() { deregister; exit 0; }
trap cleanup TERM INT

wait_etcd
register

if [ "$SERVICE" = "postfix" ]; then
    PORTS="25 587 465"
else
    PORTS="143 993"
fi

for p in $PORTS; do
    socat TCP-LISTEN:"$p",fork,reuseaddr EXEC:'/bin/cat' &
done

wait
