# SecurePulse LB — Couche Load Balancer + VPN

Système de load balancers distribués et auto-gérés pour la plateforme
SecurePulse. Architecture : `CLIENT → DNS → LB (HAProxy + Daemon Python +
WireGuard) → Srv-Mail`

Depuis la fusion avec `LB-Lucien` (voir `LB-Lucien/README.md`), ce repo
couvre à la fois :
- l'élection maître/esclave et la HA du LB lui-même (code historique de ce
  repo) ;
- la résolution DNS dynamique des backends mail via CoreDNS/etcd (repris de
  LB-Lucien) ;
- le maillage **WireGuard** site-à-site (`wireguard/`, nouveau — cf. rapport
  §40, §53).

---

## Fichiers

| Fichier | Rôle |
|---|---|
| `master_daemon.py` | Daemon maître — registre des nœuds, propagation des listes et configs |
| `slave_daemon.py` | Daemon esclave — connexion au maître, failover, rechargement HAProxy |
| `healthcheck.py` | Surveillance des srv-mail, alerte en cas de panne (tourne **dans le même process** que slave_daemon, cf. Notes) |
| `haproxy_manager.py` | Génération de la config HAProxy (résolveurs DNS dynamiques) et rechargement sans downtime |
| `etcd_client.py` | Client etcd v3 minimal — DNS réel (remplace le mock `update_dns()`) |
| `run.py` | Point d'entrée du conteneur Docker (orchestre master+slave dans un seul process) |
| `entrypoint.sh` / `Dockerfile` | Image du conteneur (config générée depuis les variables d'env à chaque démarrage) |
| `deploy/` | **Déploiement réel actuel (containerisé)** : `generate-config.sh` + `docker-compose.prod.yml` + `deploy.sh <site>`, un LB + son sidecar WireGuard par serveur |
| `deploy.sh` (racine) | Déploiement bare-metal historique (Alpine/OpenRC) — antérieur à la contrainte projet "tout containerisé", conservé tel quel mais **superseded par `deploy/`** pour tout nouveau déploiement |
| `wireguard/` | Sidecar maillage VPN site-à-site auto-géré |
| `config.yaml` | Configuration centralisée (banc de test mono-hôte ; régénérée depuis l'env en déploiement réel) |

---

## Prérequis

- **Bare-metal (`deploy.sh`)** : Alpine Linux (OpenRC), Python 3.11+, HAProxy 2.x
- **Docker (`Dockerfile`)** : rien à installer, tout est dans l'image (Debian
  bookworm-slim + haproxy + python3)
- **WireGuard** : noyau Linux ≥ 5.6 avec le module WireGuard (mainline
  depuis 2020, standard sur toute distro récente) — capacité conteneur
  `NET_ADMIN` uniquement, aucune installation sur l'hôte

---

## Déploiement réel (containerisé, un serveur par DC/nœud)

```sh
cd deploy
./generate-config.sh --site lyon --role master --etcd-url http://<etcd-reachable>:2379 \
    --wg-site-prefix 10.20.1. --resolver-ip 10.20.1.100
./deploy.sh lyon
```

Démarre le conteneur `lb` (HAProxy + daemon) et son sidecar `wireguard`
(passerelle site-à-site de ce DC, `WG_GATEWAY_ROUTING=1`) dans le **même**
namespace réseau (`network_mode: service:wireguard`) — le LB hérite
directement de `wg0` et des routes overlay, cohérent avec "un accès VPN par
DC" (rapport §40). Les autres serveurs de ce même DC (storage-lucien,
Mail, LDAP) utilisent l'IP affichée en fin de `deploy.sh` comme
`WG_GATEWAY_IP` pour router leur trafic inter-site à travers cette
passerelle. Voir `add_new_dc.sh` à la racine du projet pour générer la
config de tous les serveurs d'un nouveau DC en une seule commande.

## Déploiement bare-metal (historique, superseded)

```sh
# Sur chaque machine LB (en root) — installe des paquets directement sur
# l'hôte (apk, OpenRC) : antérieur à la contrainte projet "tout
# containerisé", conservé tel quel pour mémoire, mais plus le chemin
# recommandé (préférer deploy/ ci-dessus).
chmod +x deploy.sh
./deploy.sh
```

## Banc de test (Docker / simulation mono-hôte)

```sh
docker build -t lb-syo .
docker run -e SITE=lyon -e ROLE=master -e ETCD_URL=http://etcd:2379 \
    -e MASTER_DNS=master.securepulse.fr -e RESOLVER_IP=10.10.0.100 lb-syo
```

Voir `tests/docker-compose.test.yml` pour un exemple complet (3 nœuds, DNS,
backends factices).

---

## Désignation du maître initial

**Un LB ne se proclame jamais maître automatiquement au premier démarrage**
(évite le split-brain : plusieurs LB démarrés en même temps ne doivent pas
tous s'auto-élire avant qu'aucun n'ait pu enregistrer `master.<domain>`).

- **Bare-metal** : l'administrateur lance `master_daemon.py` à la main sur
  la machine désignée (les autres esclaves s'y connecteront automatiquement).
- **Docker** : `ROLE=master` sur le conteneur désigné — `run.py` démarre
  `master_daemon` et enregistre lui-même le premier pointeur DNS
  `master.<domain>`, équivalent automatisé de la désignation manuelle.

Une fois qu'un nœud esclave s'est connecté **avec succès** au moins une fois
(`STATE.ever_connected`), une déconnexion ultérieure déclenche la procédure
d'élection normale (failover légitime, cf. plus bas).

---

## Failover automatique

En cas de chute du maître (après qu'au moins un esclave s'y soit déjà
connecté avec succès) :

1. Le LB de rang le plus bas parmi les survivants attend `numéro × 1s`
2. Il vérifie qu'aucun LB de rang inférieur n'est encore joignable
3. Il se proclame nouveau maître, met à jour `master.<domain>` (etcd/CoreDNS)
4. Il notifie tous les autres LB, démarre le daemon maître intégré
5. Les esclaves se reconnectent automatiquement

### Limitation connue (constatée en test d'intégration Docker)

Si **plusieurs esclaves** perdent le maître de façon quasi simultanée, il
existe une fenêtre de course où le LB de rang inférieur n'a pas encore fini
de démarrer son serveur websocket au moment où le LB de rang supérieur
exécute sa vérification "rang inférieur encore joignable ?" — les deux
peuvent alors se proclamer maîtres en parallèle (double-maître transitoire).
Le système ne plante pas et le trafic client continue d'être servi (DNS
converge vers une seule valeur, dernier écrivain gagnant), mais les deux
maîtres locaux peuvent avoir un registre de nœuds temporairement incohérent
jusqu'à la prochaine reconnexion. Corriger complètement cette course
demanderait un algorithme de consensus distribué (quorum) plus robuste que
la vérification TCP ponctuelle actuelle — hors scope de cette passe de
correction, documenté ici pour transparence.

---

## Résolution DNS dynamique des backends (fusion avec LB-Lucien)

`haproxy_manager.generate_config(site)` génère une config HAProxy à base de
`resolvers` + `server-template` (plus de liste statique de srv-mail) :

- pool **local** préféré : `postfix.<site>.<domain>` / `dovecot.<site>.<domain>`
- pool **global** en secours (`backup`, via WireGuard) :
  `postfix.all.<domain>` / `dovecot.all.<domain>`

HAProxy redécouvre les backends tout seul (TTL DNS court, `hold valid 5s` +
son propre `tcp-check`) — plus besoin de recharger la config à chaque
enregistrement/désenregistrement d'un srv-mail (contrairement à l'ancienne
implémentation par liste statique).

## DNS réel (remplace le mock)

`update_dns()` (dans `master_daemon.py`/`slave_daemon.py`) écrit désormais
réellement dans etcd via `etcd_client.py`, lu par CoreDNS (`DNS/`) — même
etcd que LDAP/Mail/storage-lucien. Chaque nœud LB s'enregistre aussi comme
point d'entrée mail public (`mail.<domain>`, repris du comportement de
LB-Lucien) au démarrage, se désenregistre proprement sur SIGTERM.

## WireGuard (`wireguard/`)

Sidecar (image séparée, même repo) formant un maillage complet site-à-site
automatique + génération d'un profil "road warrior" pour l'accès
technicien distant. Voir `wireguard/wireguard_agent.py` et
`wireguard/tests/`.

---

## CLI administrateur (daemon maître)

Disponible sur `stdin` du process `master_daemon.py`, **uniquement si un
vrai TTY est attaché** (désactivé silencieusement en conteneur/sans TTY,
y compris lors d'une promotion de maître embarqué) :

```
list                        — affiche tous les nœuds actifs
push_config <fichier>       — propage une config HAProxy à tous les LB
help                        — affiche l'aide
```

---

## Messages WebSocket

```json
// Enregistrement
{"type": "register", "role": "lb|mail", "ip": "1.2.3.4", "dns": "lb1.securepulse.fr"}

// Réponse du maître
{"type": "welcome", "assigned_number": 2, "lb_list": [...], "mail_list": [...]}

// Mise à jour de liste
{"type": "update_list", "lb_list": [...], "mail_list": [...]}

// Mise à jour de config HAProxy
{"type": "update_config", "haproxy_config": "..."}

// Élection nouveau maître
{"type": "new_master", "ip": "1.2.3.4", "dns": "lb2.securepulse.fr"}

// Alerte healthcheck (esclave → maître)
{"type": "healthcheck_alert", "ip": "1.2.3.4"}
```

---

## Ports HAProxy

| Service | Port |
|---|---|
| SMTP | 25 |
| Submission | 587 |
| SMTPS | 465 |
| IMAP | 143 |
| IMAPS | 993 |
| Stats | 9000 |
| WebSocket (élection) | 8765 |
| WireGuard | 51820/udp |

Stats HAProxy accessibles sur `http://<IP>:9000/stats` (admin/securepulse).

---

## Logs

```sh
tail -f /var/log/securepulse/daemon.log      # daemon esclave + haproxy_manager
```

Le healthcheck tourne désormais **dans le même process** que le daemon
esclave (voir Notes) — plus de fichier de log séparé.

---

## Notes d'implémentation importantes

- **`healthcheck.py` doit tourner dans le même process que `slave_daemon.py`**
  (importé et lancé comme tâche asyncio depuis `slave_daemon.main()`/`run.py`),
  pas comme un service système séparé. `healthcheck.py` fait
  `import slave_daemon` pour accéder à `STATE.mail_list` : lancé comme
  process OS distinct (ce que faisait l'ancien service OpenRC
  `securepulse-health`, retiré de `deploy.sh`), il obtient sa **propre**
  instance de `STATE`, toujours vide — le healthcheck ne détectait jamais
  rien en pratique. Corrigé.

## Tests

```sh
python3 test_election.py     # logique d'élection (13 assertions, sans réseau)
python3 test_websocket.py    # protocole websocket maître/esclave (9 assertions)
python3 test_3nodes.py       # simulation 1 maître + 3 LB (24 assertions)
python3 test_haproxy_gen.py  # génération config HAProxy (résolveurs DNS)

cd tests && docker compose -f docker-compose.test.yml up -d --build
# 3 nœuds LB (1 maître désigné + 2 esclaves), CoreDNS, backends mail factices
```

## Hors scope

- Couche mail (Postfix, Dovecot, LDAP) — voir `Mail/`, `LDAP/`
- Couche stockage (GlusterFS, Pacemaker/Corosync) — voir `storage-lucien/`
- API OVH réelle pour la délégation de zone elle-même (le DNS applicatif
  passe par CoreDNS/etcd, cf. ci-dessus — l'API OVH n'entrerait en jeu que
  pour pointer un vrai domaine public vers ces serveurs CoreDNS)
- Multi-site réel (jamais eu l'infra physique) — simulation Docker uniquement
