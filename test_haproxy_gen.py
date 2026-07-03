#!/usr/bin/env python3
"""
Test isolé de haproxy_manager.generate_config()
Sans HAProxy installé, sans fichiers système.

Depuis la fusion avec LB-Lucien, generate_config() ne prend plus une liste
de srv-mail en mémoire : elle génère une config à résolution DNS dynamique
(resolvers + server-template), à deux paliers (pool local du site, puis pool
"all" en secours/backup — cf. Q&A rapport sur la préférence aux serveurs
internes du DC). Ces tests vérifient donc la structure de la config générée,
pas un rendu par serveur.
"""
import os, sys, yaml

# ── Patch config pour environnement de test Windows ──────────────────────────
os.makedirs("test_logs", exist_ok=True)
os.makedirs("test_etc_haproxy", exist_ok=True)

with open("config.yaml") as f:
    cfg = yaml.safe_load(f)

cfg["logging"]["file"]       = "test_logs/daemon.log"
cfg["haproxy"]["config_path"] = "test_etc_haproxy/haproxy.cfg"
cfg["haproxy"]["pid_file"]    = "test_etc_haproxy/haproxy.pid"
cfg["haproxy"]["socket"]      = "test_etc_haproxy/admin.sock"

with open("config.yaml", "w") as f:
    yaml.dump(cfg, f)

# ── Import après patch ────────────────────────────────────────────────────────
import haproxy_manager

def sep(title): print(f"\n{'='*60}\n  {title}\n{'='*60}")

# ── TEST 1 : structure générale ───────────────────────────────────────────────
sep("TEST 1 — generate_config('lyon') structure de base")
cfg_lyon = haproxy_manager.generate_config("lyon")
assert "frontend fe_smtp" in cfg_lyon
assert "listen stats" in cfg_lyon
assert "resolvers coredns" in cfg_lyon, "FAIL : bloc resolvers manquant"
print("PASS — frontend + stats + bloc resolvers présents")

# ── TEST 2 : résolution DNS par site (pool local + pool de secours) ─────────
sep("TEST 2 — pools local (préféré) et 'all' (secours/backup, via VPN)")
assert "postfix.lyon.securepulse.fr" in cfg_lyon, "FAIL : pool local postfix absent"
assert "postfix.all.securepulse.fr" in cfg_lyon, "FAIL : pool 'all' postfix absent"
assert "dovecot.lyon.securepulse.fr" in cfg_lyon, "FAIL : pool local dovecot absent"
assert "dovecot.all.securepulse.fr" in cfg_lyon, "FAIL : pool 'all' dovecot absent"
# Le pool "all" (autres sites, joignable via VPN) doit être marqué backup :
# HAProxy ne l'utilise que si le pool local est intégralement en panne.
assert "postfix-remote 1-10 postfix.all.securepulse.fr" in cfg_lyon
assert "backup" in cfg_lyon
print("PASS — pool local préféré, pool 'all' en secours (backup) présents")

# ── TEST 3 : un site différent change bien les noms DNS résolus ─────────────
sep("TEST 3 — un autre site cible d'autres FQDN locaux")
cfg_paris = haproxy_manager.generate_config("paris")
assert "postfix.paris.securepulse.fr" in cfg_paris
assert "postfix.lyon.securepulse.fr" not in cfg_paris
print("PASS — les FQDN locaux suivent bien le site passé en argument")

# ── TEST 4 : écriture sur disque ─────────────────────────────────────────────
sep("TEST 4 — write_config()")
haproxy_manager.write_config(cfg_lyon)
assert os.path.exists("test_etc_haproxy/haproxy.cfg"), "FAIL : fichier non créé"
with open("test_etc_haproxy/haproxy.cfg") as f:
    on_disk = f.read()
assert "postfix.lyon.securepulse.fr" in on_disk
print("PASS — fichier écrit et contenu vérifié")

# ── TEST 5 : vérification des 5 frontends et backends ───────────────────────
sep("TEST 5 — présence des 5 frontends/backends")
for proto in ["smtp", "submission", "smtps", "imap", "imaps"]:
    assert f"frontend fe_{proto}" in cfg_lyon, f"FAIL : frontend {proto} manquant"
    assert f"backend be_{proto}" in cfg_lyon, f"FAIL : backend {proto} manquant"
print("PASS — 5 frontends (smtp, submission, smtps, imap, imaps) tous présents")

# ── TEST 6 : idempotence (reload() ne dépend que du site, pas d'un état) ────
sep("TEST 6 — deux générations successives pour le même site sont identiques")
assert haproxy_manager.generate_config("lyon") == cfg_lyon, "FAIL : config non déterministe"
print("PASS — génération déterministe (rejouable sans risque à chaque événement)")

# ── Affichage de la config générée ───────────────────────────────────────────
sep("Config HAProxy générée (site=lyon)")
print(cfg_lyon)

print("\n" + "="*60)
print("  TOUS LES TESTS PASSENT ✓")
print("="*60)
