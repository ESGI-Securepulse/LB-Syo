# SecurePulse LB — Couche Load Balancer

Système de load balancers distribués et auto-gérés pour la plateforme SecurePulse.  
Architecture : `CLIENT → DNS → LB (HAProxy + Daemon Python) → Srv-Mail`

---

## Fichiers

| Fichier | Rôle |
|---|---|
| `master_daemon.py` | Daemon maître — registre des nœuds, propagation des listes et configs |
| `slave_daemon.py` | Daemon esclave — connexion au maître, failover, rechargement HAProxy |
| `healthcheck.py` | Surveillance des srv-mail, alerte en cas de panne |
| `haproxy_manager.py` | Génération de la config HAProxy et rechargement sans downtime |
| `deploy.sh` | Script de déploiement Alpine Linux |
| `config.yaml` | Configuration centralisée |

---

## Prérequis

- Alpine Linux (ou toute distro avec OpenRC)
- Python 3.11+
- HAProxy 2.x
- Paquets Python : `websockets`, `aiohttp`, `pyyaml`

---

## Déploiement rapide

```sh
# Sur chaque machine LB (en root)
chmod +x deploy.sh
./deploy.sh
# Le script demande : IP publique, DNS souhaité
```

---

## Désignation du maître initial

**Le LB #1 ne se proclame jamais maître automatiquement.**  
L'administrateur doit désigner le maître manuellement via le CLI :

```sh
# Lancer le daemon maître sur la machine désignée
cd /opt/securepulse
python3 master_daemon.py

# Les LB esclaves se connecteront automatiquement à master.securepulse.fr
# Assurez-vous que master.securepulse.fr pointe vers l'IP du maître
```

---

## Failover automatique

En cas de chute du maître :

1. Le **LB #2** détecte le broken pipe
2. Il se proclame nouveau maître
3. Il met à jour le DNS `master.securepulse.fr` (via `update_dns()`)
4. Il notifie tous les autres LB
5. Il démarre le daemon maître intégré
6. Les esclaves se reconnectent automatiquement

---

## CLI administrateur (daemon maître)

Disponible sur `stdin` du process `master_daemon.py` :

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

Stats HAProxy accessibles sur `http://<IP>:9000/stats` (admin/securepulse).

---

## Logs

```sh
tail -f /var/log/securepulse/daemon.log      # daemon esclave + haproxy_manager
tail -f /var/log/securepulse/healthcheck.log # healthcheck
```

---

## Hors scope

- Couche mail (Postfix, Dovecot, LDAP)
- Couche stockage (GlusterFS, LUKS)
- API OVH réelle (`update_dns()` est mockée — voir `master_daemon.py` et `slave_daemon.py`)
- Multi-site
