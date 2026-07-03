#!/usr/bin/env python3
"""
Test isolé de haproxy_manager.generate_config()
Sans HAProxy installé, sans fichiers système.
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

MAIL_LIST_0 = []

MAIL_LIST_2 = [
    {"ip": "192.168.1.10", "dns": "mail1.securepulse.fr", "number": 1},
    {"ip": "192.168.1.11", "dns": "mail2.securepulse.fr", "number": 2},
]

MAIL_LIST_3 = [
    {"ip": "192.168.1.10", "dns": "mail1.securepulse.fr", "number": 1},
    {"ip": "192.168.1.11", "dns": "mail2.securepulse.fr", "number": 2},
    {"ip": "192.168.1.12", "dns": "mail3.securepulse.fr", "number": 3},
]

def sep(title): print(f"\n{'='*60}\n  {title}\n{'='*60}")

# ── TEST 1 : liste vide ───────────────────────────────────────────────────────
sep("TEST 1 — generate_config() avec liste vide")
cfg0 = haproxy_manager.generate_config(MAIL_LIST_0)
assert "Aucun srv-mail actif" in cfg0, "FAIL : commentaire 'Aucun srv-mail' manquant"
assert "frontend fe_smtp" in cfg0
assert "listen stats" in cfg0
print("PASS — config générée, frontend + stats présents, commentaire 'Aucun srv-mail' OK")

# ── TEST 2 : 2 srv-mail ───────────────────────────────────────────────────────
sep("TEST 2 — generate_config() avec 2 srv-mail")
cfg2 = haproxy_manager.generate_config(MAIL_LIST_2)
assert "mail1-securepulse-fr" in cfg2, "FAIL : serveur mail1 absent"
assert "mail2-securepulse-fr" in cfg2, "FAIL : serveur mail2 absent"
assert "192.168.1.10:25" in cfg2
assert "192.168.1.11:143" in cfg2
assert "balance roundrobin" in cfg2
assert "check inter 2s fall 3 rise 2" in cfg2
print("PASS — 2 serveurs présents sur tous les protocols, balance roundrobin OK")

# ── TEST 3 : 3 srv-mail ───────────────────────────────────────────────────────
sep("TEST 3 — generate_config() avec 3 srv-mail")
cfg3 = haproxy_manager.generate_config(MAIL_LIST_3)
assert cfg3.count("server mail") == 15  # 5 protocols × 3 servers
print(f"PASS — {cfg3.count('server mail')} entrées server (5 protocols × 3 mails)")

# ── TEST 4 : écriture sur disque ─────────────────────────────────────────────
sep("TEST 4 — write_config()")
haproxy_manager.write_config(cfg2)
assert os.path.exists("test_etc_haproxy/haproxy.cfg"), "FAIL : fichier non créé"
with open("test_etc_haproxy/haproxy.cfg") as f:
    on_disk = f.read()
assert "mail1-securepulse-fr" in on_disk
print("PASS — fichier écrit et contenu vérifié")

# ── TEST 5 : vérification des 5 frontends et backends ───────────────────────
sep("TEST 5 — présence des 5 frontends/backends")
for proto in ["smtp", "submission", "smtps", "imap", "imaps"]:
    assert f"frontend fe_{proto}" in cfg2, f"FAIL : frontend {proto} manquant"
    assert f"backend be_{proto}" in cfg2, f"FAIL : backend {proto} manquant"
print("PASS — 5 frontends (smtp, submission, smtps, imap, imaps) tous présents")

# ── Affichage de la config générée ───────────────────────────────────────────
sep("Config HAProxy générée (2 srv-mail)")
print(cfg2)

print("\n" + "="*60)
print("  TOUS LES TESTS PASSENT ✓")
print("="*60)
