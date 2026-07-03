# Backend mail factice pour les tests d'intégration LB-Syo : écoute sur les
# ports SMTP/IMAP habituels et s'enregistre dans etcd comme le font les
# vrais entrypoints Postfix/Dovecot (Mail/), pour que la config HAProxy à
# résolution DNS dynamique ait quelque chose à découvrir et proxifier.
FROM alpine:3.20
RUN apk add --no-cache socat curl jq bash
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 25 587 465 143 993
ENTRYPOINT ["/entrypoint.sh"]
