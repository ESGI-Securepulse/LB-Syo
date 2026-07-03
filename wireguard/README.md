# wireguard — Maillage VPN site-à-site (sidecar LB-Syo)

Brique manquante identifiée lors de l'alignement sur `Projet Annuel.docx`
(§40, §53 : "Un accès VPN par DC (Wireguard)", accès technicien distant +
synchronisation des services non-exposés entre sites).

Un conteneur par site, construit depuis ce dossier, tournant à côté du
conteneur LB-Syo principal (même réseau). Voir `wireguard_agent.py` pour le
détail du comportement (keygen, maillage auto-géré via etcd, profil
technicien).

## Pourquoi l'implémentation noyau plutôt que userspace (boringtun/wireguard-go)

`ip link add wg0 type wireguard` fonctionne directement avec la seule
capacité conteneur `NET_ADMIN`, sans dépendance à `/dev/net/tun` ni à un
binaire userspace tiers, **si le noyau de l'hôte a le support WireGuard**
(mainline depuis Linux 5.6, 2020 — standard sur toute distribution récente :
Ubuntu, Debian, Fedora, Nobara, etc.). C'est le cas testé et validé ici.
Si un hôte cible plus ancien manque ce support, prévoir un fallback
`wireguard-go` (userspace) — non implémenté dans cette passe, à ajouter si
un déploiement réel sur noyau < 5.6 est identifié.

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `ETCD_URL` | `http://etcd:2379` | Backend de découverte |
| `SITE` | `local` | Nom du DC |
| `WG_LISTEN_PORT` | `51820` | Port UDP WireGuard |
| `WG_OVERLAY_CIDR` | `10.99.0.0/16` | Plage overlay du maillage |
| `WG_SITE_PREFIX` | *(auto)* | Préfixe IP à utiliser comme endpoint public (sinon 1ère IP non-loopback) |
| `WG_WATCH_INTERVAL` | `15` | Intervalle (s) de resynchronisation des peers |

## Tests

```sh
cd tests
docker compose -p wgtest -f docker-compose.test.yml up -d --build
docker exec wg-test-a wg show wg0   # handshakes + peers attendus
docker exec wg-test-a ping -c2 10.99.0.2   # ping chiffré à travers le tunnel
docker compose -p wgtest -f docker-compose.test.yml down -v
```

Valide : génération de clés, formation automatique du maillage complet
(3 sites), établissement réel de handshakes WireGuard, connectivité chiffrée
overlay, génération du profil technicien (road warrior) sans perturber le
maillage.

## Limitation connue

Allocation d'IP overlay par site en best-effort (liste etcd + plus petit
entier libre), sans verrou distribué — un risque de collision existe en cas
de démarrage strictement simultané de deux nouveaux sites. Acceptable pour
ce projet (nombre de sites borné, ~18 régions maximum) ; documenté comme
limitation, pas un bug.
