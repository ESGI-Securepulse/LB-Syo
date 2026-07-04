# Changelog — LB-Syo

## [Unreleased]

### Corrigé
- **`enable_forwarding()` plantait le conteneur au démarrage** (`WG_GATEWAY_ROUTING=1`) :
  `/proc/sys/net/ipv4/ip_forward` est en lecture seule à l'intérieur du
  conteneur même avec `NET_ADMIN` (Docker le masque par défaut) —
  découvert en construisant la validation réseaux-isolés. L'écriture n'est
  plus bloquante (avertissement si elle échoue) ; `net.ipv4.ip_forward=1`
  doit être posé depuis l'EXTÉRIEUR du conteneur via `sysctls:` dans le
  docker-compose (ajouté à `deploy/docker-compose.prod.yml`).
- **`sync_peers()` utilisait toujours SON PROPRE `WG_LISTEN_PORT`** pour
  construire l'endpoint de CHAQUE pair, au lieu du port que ce pair a lui-même
  annoncé — invisible tant que tous les sites utilisent le port par défaut
  (51820), mais casse le handshake dès que deux sites utilisent des ports
  différents (ex. contrainte de pare-feu, ou 2 sites de test sur le même
  hôte ne pouvant pas publier le même port). Le port est désormais annoncé
  explicitement dans l'enregistrement etcd de chaque site et utilisé tel
  quel pour construire son endpoint.

### Ajouté
- `wireguard/` : `WG_ENDPOINT_OVERRIDE` permet d'annoncer une adresse
  publique/flottante différente de l'IP locale détectée (NAT) — nécessaire
  en production dès que l'IP visible localement par le conteneur diffère
  de celle par laquelle les autres sites doivent le joindre, et utilisé
  par la validation en réseaux isolés (`integration/tests/topology-isolated/`).
- `wireguard/` : le sidecar peut désormais agir comme **passerelle
  site-à-site** (`WG_GATEWAY_ROUTING=1`, déploiement réel multi-hôtes
  uniquement) — forwarding IP + annonce dans les `allowed-ips` des
  adresses que chaque site distant enregistre dans etcd
  (`/storage-nodes/<site>/`, `/skydns/fr/securepulse/<site>/`), avec
  installation des routes noyau correspondantes. Sans ce mécanisme, seul
  le conteneur wireguard lui-même était joignable depuis un autre DC ; ses
  voisins du même serveur/DC (storage-lucien, LDAP, Mail, HAProxy) ne
  l'étaient pas. Désactivé par défaut, aucun changement pour le banc de
  test mono-hôte existant.
- `deploy/` : déploiement réel containerisé (un LB + son sidecar
  WireGuard par serveur, `network_mode: service:wireguard` — cohérent avec
  "un accès VPN par DC" du rapport). `generate-config.sh` produit le
  `.env` du site, `deploy.sh <site>` lance la stack et affiche l'IP de
  passerelle à donner aux autres serveurs du même DC.
- Fusion avec `LB-Lucien` : résolution DNS dynamique des backends mail
  (`resolvers` + `server-template` HAProxy, pools local préféré + global en
  secours/backup) — remplace la génération de config à partir d'une liste
  statique de srv-mail.
- `etcd_client.py` : DNS réel (etcd/CoreDNS), remplace le mock
  `update_dns()` de `master_daemon.py`/`slave_daemon.py`.
- Auto-enregistrement/désenregistrement de chaque nœud LB comme point
  d'entrée mail public (`mail.<domain>`), repris du comportement de
  LB-Lucien.
- `wireguard/` : nouveau sidecar de maillage VPN site-à-site auto-géré
  (keygen, peer discovery etcd, profil technicien road-warrior).
- `Dockerfile` + `entrypoint.sh` + `run.py` : déploiement Docker (le
  déploiement bare-metal historique `deploy.sh` reste inchangé et
  fonctionnel).
- Suite de tests d'intégration Docker (`tests/`) : élection maître/esclave
  réelle multi-conteneurs, DNS dynamique, proxying HAProxy bout-en-bout via
  résolution DNS, failover réel (arrêt du conteneur maître).

### Corrigé
- **Bug de correction critique (santé du service)** : `healthcheck.py`
  était conçu pour tourner comme process OS séparé (service OpenRC
  `securepulse-health`) mais fait `import slave_daemon` pour accéder à
  `STATE.mail_list` — dans un process séparé, il obtient sa PROPRE instance
  de `STATE`, toujours vide, rendant le healthcheck muet en pratique.
  Corrigé : `healthcheck.run_healthcheck()` tourne désormais comme tâche
  asyncio dans le même process que `slave_daemon` (`run.py` et
  `slave_daemon.main()`), service OpenRC séparé retiré de `deploy.sh`.
- **Bug de split-brain au démarrage à froid** (trouvé en test
  d'intégration Docker multi-nœuds) : plusieurs LB démarrés simultanément
  tentaient tous de se connecter à `master.<domain>` avant qu'aucun
  enregistrement DNS n'existe encore, échouaient tous, et s'auto-élisaient
  tous maîtres indépendamment. Corrigé via `SlaveState.ever_connected` :
  l'auto-élection ne se déclenche plus qu'après une première connexion
  réussie à un vrai maître (démarrage à froid = nouvelle tentative
  silencieuse, pas d'élection). `ROLE=master` (Docker) enregistre
  lui-même le premier pointeur DNS, équivalent automatisé de la
  désignation manuelle documentée dans le README.
- `master_daemon.cli_admin()` levait une `PermissionError` non gérée
  (bruyante dans les logs) à chaque promotion de maître en conteneur
  (pas de TTY attaché à stdin). Désactivé proprement si `sys.stdin` n'est
  pas un TTY.
- Génération HAProxy : la continuation de ligne `\` en fin de directive
  `server-template` n'est pas supportée par cette version de HAProxy
  (`unknown keyword '\'`) — chaque directive est désormais sur une seule
  ligne.
- `reload_from_list(mail_list)` retiré : la config HAProxy ne dépend plus
  d'une liste de srv-mail suivie en mémoire (résolution DNS dynamique),
  simplifié en `reload(site)` — plus de rechargement HAProxy déclenché à
  chaque mise à jour de `STATE.mail_list` (HAProxy redécouvre les backends
  tout seul via DNS + son propre tcp-check).

### Documenté
- Limitation connue : fenêtre de course possible lors d'une panne quasi
  simultanée détectée par plusieurs esclaves (double-maître transitoire,
  DNS converge malgré tout vers une seule valeur — voir README).
