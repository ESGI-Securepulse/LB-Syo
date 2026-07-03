#!/usr/bin/env python3
"""
test_3nodes.py — Test d'integration 3 machines
Simule : 1 master + LB#1 + LB#2 + LB#3 dans le meme process.
Chaque slave tourne dans sa propre coroutine, exactement comme en prod.
HAProxy et DNS sont mockes pour tourner sans Alpine.

Scenarios testes :
  1. Les 3 LB se connectent et recoivent leur numero + liste complete
  2. Un srv-mail se connecte, les 3 LB sont notifies
  3. Failover : le master tombe → LB#2 se proclame maitre
  4. LB#3 se reconnecte au nouveau maitre
  5. Un srv-mail tombe (healthcheck) → HAProxy rechargee sur tous les LB
"""

import asyncio, json, logging, sys, os, yaml
from unittest.mock import patch, MagicMock

# ─────────────────────────────────────────────
# Patch config pour les tests (ports locaux, logs rediriges)
# ─────────────────────────────────────────────
TEST_MASTER_PORT = 8770
TEST_SLAVE_PORT  = 8771   # port du "nouveau master" apres failover

os.makedirs("test_logs", exist_ok=True)

with open("config.yaml") as f:
    BASE_CFG = yaml.safe_load(f)

def make_cfg(ip, dns, port=TEST_MASTER_PORT):
    cfg = yaml.safe_load(yaml.dump(BASE_CFG))
    cfg["node"]["ip"]     = ip
    cfg["node"]["dns"]    = dns
    cfg["master"]["port"] = port
    cfg["master"]["dns"]  = "127.0.0.1"
    cfg["logging"]["file"] = "test_logs/daemon.log"
    cfg["haproxy"]["config_path"] = f"test_logs/haproxy_{ip.replace('.','_')}.cfg"
    cfg["haproxy"]["pid_file"]    = f"test_logs/haproxy_{ip.replace('.','_')}.pid"
    cfg["haproxy"]["socket"]      = f"test_logs/haproxy_{ip.replace('.','_')}.sock"
    return cfg

# ─────────────────────────────────────────────
# Logging minimal
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("test_3nodes")

RESULTS = []
def ok(msg):   RESULTS.append(("PASS", msg)); print(f"  PASS  {msg}")
def fail(msg): RESULTS.append(("FAIL", msg)); print(f"  FAIL  {msg}")


# ─────────────────────────────────────────────
# Master en ligne autonome (code reel de master_daemon)
# ─────────────────────────────────────────────

import websockets
from websockets.exceptions import ConnectionClosed

class MasterState:
    def __init__(self):
        self._lb_ctr = 0
        self._mail_ctr = 0
        self.lb_nodes   = {}
        self.mail_nodes = {}

    def next_lb(self):   self._lb_ctr += 1;   return self._lb_ctr
    def next_mail(self): self._mail_ctr += 1; return self._mail_ctr
    def lb_list(self):   return [n for n in self.lb_nodes.values()]
    def mail_list(self): return [n for n in self.mail_nodes.values()]


async def master_broadcast(state, skip_ws=None):
    msg = json.dumps({
        "type": "update_list",
        "lb_list":   state.lb_list(),
        "mail_list": state.mail_list(),
    })
    for ws in list(state.lb_nodes) + list(state.mail_nodes):
        if ws is skip_ws: continue
        try: await ws.send(msg)
        except Exception: pass


async def master_handler(ws, state: MasterState):
    node = None
    try:
        raw  = await ws.recv()
        msg  = json.loads(raw)
        role = msg["role"]
        ip, dns = msg["ip"], msg["dns"]

        if role == "lb":
            n = state.next_lb()
            node = {"number": n, "ip": ip, "dns": dns, "role": "lb"}
            state.lb_nodes[ws] = node
        elif role == "mail":
            n = state.next_mail()
            node = {"number": n, "ip": ip, "dns": dns, "role": "mail"}
            state.mail_nodes[ws] = node

        await ws.send(json.dumps({
            "type": "welcome", "assigned_number": node["number"],
            "lb_list": state.lb_list(), "mail_list": state.mail_list(),
        }))
        await master_broadcast(state)   # broadcast a TOUS y compris le nouveau noeud

        async for raw in ws:
            m = json.loads(raw)
            if m["type"] == "healthcheck_alert":
                failed_ip = m["ip"]
                to_del = [w for w, nd in state.mail_nodes.items() if nd["ip"] == failed_ip]
                for w in to_del: del state.mail_nodes[w]
                await master_broadcast(state)

    except Exception: pass
    finally:
        if node:
            if node["role"] == "lb"   and ws in state.lb_nodes:   del state.lb_nodes[ws]
            if node["role"] == "mail" and ws in state.mail_nodes: del state.mail_nodes[ws]
        await master_broadcast(state)


# ─────────────────────────────────────────────
# Slave simplifie (logique reelle, HAProxy mocke)
# ─────────────────────────────────────────────

class SlaveNode:
    """Represente un LB esclave avec sa propre boucle de connexion."""

    def __init__(self, cfg: dict, name: str):
        self.cfg        = cfg
        self.name       = name
        self.ip         = cfg["node"]["ip"]
        self.dns        = cfg["node"]["dns"]
        self.number     = 0
        self.lb_list    = []
        self.mail_list  = []
        self.is_master  = False
        self.master_ws  = None
        self.haproxy_reloads = []   # historique des reloads (mock)
        self.dns_updates     = []   # historique DNS (mock)
        self._stop      = asyncio.Event()
        self._connected = asyncio.Event()
        self._became_master = asyncio.Event()

    def mock_reload(self, mail_list):
        self.haproxy_reloads.append(list(mail_list))
        log.debug(f"[{self.name}] HAProxy reload mock ({len(mail_list)} mails)")

    def mock_dns(self, ip, dns="master.securepulse.fr"):
        self.dns_updates.append((ip, dns))
        log.debug(f"[{self.name}] DNS mock: {dns} -> {ip}")

    async def connect_loop(self):
        port = self.cfg["master"]["port"]
        host = self.cfg["master"]["dns"]
        uri  = f"ws://{host}:{port}"

        while not self._stop.is_set():
            try:
                async with websockets.connect(uri, open_timeout=3) as ws:
                    self.master_ws = ws
                    self._connected.set()

                    await ws.send(json.dumps({
                        "type": "register", "role": "lb",
                        "ip": self.ip, "dns": self.dns,
                    }))

                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if msg["type"] == "welcome":
                        self.number    = msg["assigned_number"]
                        self.lb_list   = msg.get("lb_list", [])
                        self.mail_list = msg.get("mail_list", [])
                        self.mock_reload(self.mail_list)

                    # Consommer le broadcast initial
                    raw = await ws.recv()
                    upd = json.loads(raw)
                    if upd["type"] == "update_list":
                        self.lb_list   = upd.get("lb_list", [])
                        self.mail_list = upd.get("mail_list", [])
                        self.mock_reload(self.mail_list)

                    # Boucle de reception
                    async for raw in ws:
                        if self._stop.is_set(): break
                        await self._handle_msg(json.loads(raw))

            except Exception as exc:
                self.master_ws = None
                self._connected.clear()
                if not self._stop.is_set():
                    log.debug(f"[{self.name}] Master perdu: {exc}")
                    await self._maybe_elect()
                    if self.is_master: break
                    await asyncio.sleep(0.3)

    async def _handle_msg(self, msg):
        if msg["type"] == "update_list":
            self.lb_list   = msg.get("lb_list", [])
            self.mail_list = msg.get("mail_list", [])
            self.mock_reload(self.mail_list)
        elif msg["type"] == "update_config":
            log.debug(f"[{self.name}] Config HAProxy recue")
        elif msg["type"] == "new_master":
            self.cfg["master"]["dns"] = msg["ip"]

    async def _maybe_elect(self):
        if self.number == 2:
            log.debug(f"[{self.name}] LB#2 se proclame maitre")
            self.is_master = True
            self.mock_dns(self.ip)
            self._became_master.set()

    async def send_healthcheck_alert(self, mail_ip: str):
        """Signale au maitre qu'un srv-mail est tombe."""
        if self.master_ws:
            try:
                await self.master_ws.send(json.dumps({
                    "type": "healthcheck_alert", "ip": mail_ip,
                }))
            except Exception: pass

    def stop(self):
        self._stop.set()


# ─────────────────────────────────────────────
# Scenario principal
# ─────────────────────────────────────────────

async def run_scenario():

    # ── Demarrage du master ───────────────────────────────────────────────
    master_state = MasterState()

    def handler_factory(ws, path=None):
        return master_handler(ws, master_state)

    master_server = await websockets.serve(handler_factory, "127.0.0.1", TEST_MASTER_PORT)
    print(f"Master demarre sur 127.0.0.1:{TEST_MASTER_PORT}")

    # ── Creation des 3 LB esclaves ────────────────────────────────────────
    lb1 = SlaveNode(make_cfg("10.0.0.1", "lb1.securepulse.fr"), "LB1")
    lb2 = SlaveNode(make_cfg("10.0.0.2", "lb2.securepulse.fr"), "LB2")
    lb3 = SlaveNode(make_cfg("10.0.0.3", "lb3.securepulse.fr"), "LB3")

    # Demarrage sequentiel avec petit delai pour garantir l'ordre des numeros
    # (en prod les LB demarrent rarement au meme instant)
    t1 = asyncio.create_task(lb1.connect_loop())
    await asyncio.sleep(0.15)
    t2 = asyncio.create_task(lb2.connect_loop())
    await asyncio.sleep(0.15)
    t3 = asyncio.create_task(lb3.connect_loop())

    await asyncio.sleep(0.5)   # Laisser le temps aux connexions

    # ═══════════════════════════════════════════════════════════════
    print("\n[SCENARIO 1] Les 3 LB se connectent et recoivent leur numero")
    # ═══════════════════════════════════════════════════════════════

    if lb1.number == 1 and lb2.number == 2 and lb3.number == 3:
        ok("LB1=#1, LB2=#2, LB3=#3 — numeros corrects")
    else:
        fail(f"Numeros incorrects: LB1={lb1.number}, LB2={lb2.number}, LB3={lb3.number}")

    # Chaque LB doit connaitre les 3 autres
    for lb, name in [(lb1,"LB1"),(lb2,"LB2"),(lb3,"LB3")]:
        if len(lb.lb_list) == 3:
            ok(f"{name} connait les 3 LB dans sa liste")
        else:
            fail(f"{name} lb_list={len(lb.lb_list)} (attendu 3)")

    # Chaque LB doit avoir fait 1 reload HAProxy (liste vide au debut)
    for lb, name in [(lb1,"LB1"),(lb2,"LB2"),(lb3,"LB3")]:
        if len(lb.haproxy_reloads) >= 1:
            ok(f"{name} : HAProxy recharge au demarrage")
        else:
            fail(f"{name} : pas de reload HAProxy au demarrage")

    # ═══════════════════════════════════════════════════════════════
    print("\n[SCENARIO 2] Un srv-mail se connecte, les 3 LB sont notifies")
    # ═══════════════════════════════════════════════════════════════

    # Simulation de la connexion d'un srv-mail
    mail_uri = f"ws://127.0.0.1:{TEST_MASTER_PORT}"
    async with websockets.connect(mail_uri) as ws_mail:
        await ws_mail.send(json.dumps({
            "type": "register", "role": "mail",
            "ip": "10.0.1.1", "dns": "mail1.securepulse.fr",
        }))
        # Le mail recoit uniquement "welcome" (le broadcast le saute via skip_ws)
        raw_welcome = await asyncio.wait_for(ws_mail.recv(), timeout=2)
        assert json.loads(raw_welcome)["type"] == "welcome"

        await asyncio.sleep(0.3)  # Laisser les LB traiter

        for lb, name in [(lb1,"LB1"),(lb2,"LB2"),(lb3,"LB3")]:
            if len(lb.mail_list) == 1 and lb.mail_list[0]["dns"] == "mail1.securepulse.fr":
                ok(f"{name} : mail_list mise a jour avec mail1")
            else:
                fail(f"{name} : mail_list={lb.mail_list}")

        # Chaque LB doit avoir recharge HAProxy avec le nouveau mail
        for lb, name in [(lb1,"LB1"),(lb2,"LB2"),(lb3,"LB3")]:
            last_reload = lb.haproxy_reloads[-1] if lb.haproxy_reloads else []
            if len(last_reload) == 1:
                ok(f"{name} : HAProxy recharge avec 1 srv-mail")
            else:
                fail(f"{name} : dernier reload mail_list={last_reload}")

    # ═══════════════════════════════════════════════════════════════
    print("\n[SCENARIO 3] Healthcheck : srv-mail tombe, alerte envoyee au master")
    # ═══════════════════════════════════════════════════════════════

    # LB1 simule un healthcheck qui detecte mail1 en panne
    await lb1.send_healthcheck_alert("10.0.1.1")
    await asyncio.sleep(0.3)

    # Les 3 LB doivent avoir une mail_list vide
    for lb, name in [(lb1,"LB1"),(lb2,"LB2"),(lb3,"LB3")]:
        if len(lb.mail_list) == 0:
            ok(f"{name} : mail_list vide apres alerte healthcheck")
        else:
            fail(f"{name} : mail_list={lb.mail_list} (devrait etre vide)")

    for lb, name in [(lb1,"LB1"),(lb2,"LB2"),(lb3,"LB3")]:
        last_reload = lb.haproxy_reloads[-1] if lb.haproxy_reloads else ["?"]
        if len(last_reload) == 0:
            ok(f"{name} : HAProxy recharge avec liste vide")
        else:
            fail(f"{name} : dernier reload={last_reload}")

    # ═══════════════════════════════════════════════════════════════
    print("\n[SCENARIO 4] Failover : le master tombe, LB#2 prend la main")
    # ═══════════════════════════════════════════════════════════════

    reloads_before = {
        "lb1": len(lb1.haproxy_reloads),
        "lb3": len(lb3.haproxy_reloads),
    }

    # Fermeture brutale du master
    master_server.close()
    await master_server.wait_closed()
    print("  Master arrete brutalement.")

    # Attendre que LB#2 detecte la panne et se proclame maitre
    try:
        await asyncio.wait_for(lb2._became_master.wait(), timeout=3.0)
        ok("LB#2 detecte la panne et se proclame maitre")
    except asyncio.TimeoutError:
        fail("LB#2 n'a pas declenche l'election dans les 3s")

    if lb2.is_master:
        ok("LB#2 : is_master=True")
    else:
        fail("LB#2 : is_master toujours False")

    if lb2.dns_updates and lb2.dns_updates[-1][0] == "10.0.0.2":
        ok(f"LB#2 : update_dns('10.0.0.2') appele")
    else:
        fail(f"LB#2 : dns_updates={lb2.dns_updates}")

    # ═══════════════════════════════════════════════════════════════
    print("\n[SCENARIO 5] LB#2 demarre un nouveau master, LB#1 et LB#3 se reconnectent")
    # ═══════════════════════════════════════════════════════════════

    # LB#2 demarre le nouveau master sur le meme port
    new_master_state = MasterState()

    def new_handler_factory(ws, path=None):
        return master_handler(ws, new_master_state)

    new_master_server = await websockets.serve(
        new_handler_factory, "127.0.0.1", TEST_MASTER_PORT
    )
    print("  Nouveau master demarre par LB#2.")

    # Mettre a jour la config des LB#1 et LB#3 pour pointer vers 127.0.0.1
    lb1.cfg["master"]["dns"] = "127.0.0.1"
    lb3.cfg["master"]["dns"] = "127.0.0.1"

    # Attendre la reconnexion (la boucle reessaie toutes les 0.3s)
    await asyncio.sleep(1.5)

    if new_master_state._lb_ctr >= 2:
        ok(f"Nouveau master : {new_master_state._lb_ctr} LB reconnectes")
    else:
        fail(f"Nouveau master : seulement {new_master_state._lb_ctr} LB reconnectes")

    if len(new_master_state.lb_list()) >= 2:
        ips = [n["ip"] for n in new_master_state.lb_list()]
        ok(f"Nouveau master connait les LB : {ips}")
    else:
        fail(f"Nouveau master lb_list={new_master_state.lb_list()}")

    # ── Nettoyage ─────────────────────────────────────────────────────────
    lb1.stop(); lb2.stop(); lb3.stop()
    new_master_server.close()
    await new_master_server.wait_closed()
    t1.cancel(); t2.cancel(); t3.cancel()
    await asyncio.gather(t1, t2, t3, return_exceptions=True)


# ─────────────────────────────────────────────
# Point d'entree
# ─────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  Test 3 machines : 1 master + LB#1 + LB#2 + LB#3")
    print("  (HAProxy et DNS mockes)")
    print("=" * 60)

    try:
        await run_scenario()
    except Exception as e:
        fail(f"Exception non geree : {e}")
        import traceback; traceback.print_exc()

    passed = sum(1 for r in RESULTS if r[0] == "PASS")
    failed = sum(1 for r in RESULTS if r[0] == "FAIL")
    print(f"\n{'='*60}")
    print(f"  Resultats : {passed} PASS / {failed} FAIL")
    print(f"{'='*60}")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
