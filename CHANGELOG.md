# Changelog — LB-Syo

## [Unreleased]

### Ajouté
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
