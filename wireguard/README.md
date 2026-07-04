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
| `WG_GATEWAY_ROUTING` | `0` | `1` = ce conteneur devient la passerelle site-à-site de son DC (voir ci-dessous) |

## Passerelle site-à-site réelle (`WG_GATEWAY_ROUTING=1`)

Sur le banc de test mono-hôte (tous les DC sur un même sous-réseau Docker
plat), n'importe quel conteneur peut déjà joindre n'importe quel autre —
le maillage WireGuard lui-même fonctionne, mais rien ne prouve qu'il soit
réellement *nécessaire*. En déploiement réel (chaque DC sur son propre
réseau/serveur), seul CE conteneur est joignable depuis les autres sites
par défaut : ses voisins du même DC (storage-lucien, LDAP, Mail, HAProxy)
ne le sont pas tant que ce conteneur n'agit pas comme passerelle.

Avec `WG_GATEWAY_ROUTING=1` :
1. `net.ipv4.ip_forward=1` est activé **dans le namespace réseau de ce
   conteneur uniquement** (capacité `NET_ADMIN`, aucune modification de
   l'hôte — ce réglage est per-netns, pas un sysctl noyau global) et la
   politique `FORWARD` d'iptables passe à `ACCEPT`.
2. À chaque cycle, l'agent interroge etcd pour les adresses que chaque
   site distant a enregistrées (`/storage-nodes/<site>/` — géo-réplication
   GlusterFS — et `/skydns/fr/securepulse/<site>/` — LDAP/Mail/HAProxy,
   pour le failover DNS vers le pool `all.<domaine>`), les ajoute aux
   `allowed-ips` du peer WireGuard correspondant, et installe une route
   noyau locale (`ip route replace <ip>/32 dev wg0`) pour chacune.
3. Les autres conteneurs du même DC doivent alors router leur trafic
   sortant vers ces adresses distantes via l'IP locale de CE conteneur
   (variable d'env `WG_GATEWAY_IP` côté storage-lucien/LDAP/Mail — voir
   leurs README respectifs) : la table de routage noyau de wireguard_agent
   ne peut pas, à elle seule, faire apparaître une route chez un voisin.

Désactivé par défaut (`0`) pour ne rien changer au comportement du banc de
test mono-hôte existant. Voir `integration/tests/topology-isolated/` pour
la validation en réseaux réellement isolés (sans sous-réseau partagé).

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
