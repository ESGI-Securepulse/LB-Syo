#!/usr/bin/env python3
"""
test_election.py — Validation des 3 scenarios critiques

  S1 : Ordre de connexion aleatoire → les numeros suivent l'ordre d'arrivee,
       PAS de memoire entre sessions (comportement voulu)

  S2 : Un slave tombe → les suivants montent d'un cran (renumerotation compacte)
       Si LB#2 tombe : LB#1=1, LB#3=2 (LB#3 monte)
       Election : le LB#2 nouvellement promu prend la main si le master tombe

  S3 : LB crash et revient → nouveau numero en bas de liste, non reconnu
"""

import asyncio, json, logging, sys, os, yaml

os.makedirs("test_logs", exist_ok=True)
logging.basicConfig(level=logging.WARNING,
                    format="[%(asctime)s] %(levelname)s %(name)s - %(message)s")
log = logging.getLogger("test_election")

import websockets

TEST_PORT_S1 = 8772
TEST_PORT_S2 = 8773
TEST_PORT_S3 = 8774

with open("config.yaml") as f:
    BASE_CFG = yaml.safe_load(f)

def make_cfg(ip, dns, port):
    cfg = yaml.safe_load(yaml.dump(BASE_CFG))
    cfg["node"]["ip"]      = ip
    cfg["node"]["dns"]     = dns
    cfg["master"]["port"]  = port
    cfg["master"]["dns"]   = "127.0.0.1"
    cfg["logging"]["file"] = "test_logs/daemon.log"
    cfg["haproxy"]["config_path"] = f"test_logs/hp_{port}.cfg"
    cfg["haproxy"]["pid_file"]    = f"test_logs/hp_{port}.pid"
    cfg["haproxy"]["socket"]      = f"test_logs/hp_{port}.sock"
    return cfg

RESULTS = []
def ok(msg):   RESULTS.append(("PASS", msg)); print(f"  PASS  {msg}")
def fail(msg): RESULTS.append(("FAIL", msg)); print(f"  FAIL  {msg}")

# ─────────────────────────────────────────────
# Master avec renumerotation (logique reelle)
# ─────────────────────────────────────────────

class MasterState:
    def __init__(self):
        self.lb_nodes   = {}   # ws -> {"number", "ip", "dns"}
        self.mail_nodes = {}

    def next_lb_number(self):
        return len(self.lb_nodes) + 1

    def renumber_lb(self):
        """Renumerotation compacte apres suppression."""
        sorted_nodes = sorted(self.lb_nodes.values(), key=lambda n: n["number"])
        for new_num, node in enumerate(sorted_nodes, start=1):
            node["number"] = new_num

    def lb_list(self):
        return sorted(self.lb_nodes.values(), key=lambda n: n["number"])

    def mail_list(self):
        return list(self.mail_nodes.values())


async def master_broadcast(state):
    """Broadcast avec 'your_number' personnalise pour chaque LB."""
    lb_list   = state.lb_list()
    mail_list = state.mail_list()
    for ws, node in list(state.lb_nodes.items()):
        msg = {
            "type":        "update_list",
            "lb_list":     lb_list,
            "mail_list":   mail_list,
            "your_number": node["number"],
        }
        try: await ws.send(json.dumps(msg))
        except Exception: pass
    for ws in list(state.mail_nodes):
        try: await ws.send(json.dumps({"type": "update_list",
                                        "lb_list": lb_list, "mail_list": mail_list}))
        except Exception: pass


async def master_handler(ws, state: MasterState):
    node = None
    try:
        raw = await ws.recv()
        msg = json.loads(raw)
        role, ip, dns = msg["role"], msg["ip"], msg["dns"]

        if role == "lb":
            n    = state.next_lb_number()
            node = {"number": n, "ip": ip, "dns": dns, "role": "lb"}
            state.lb_nodes[ws] = node
        elif role == "mail":
            n    = len(state.mail_nodes) + 1
            node = {"number": n, "ip": ip, "dns": dns, "role": "mail"}
            state.mail_nodes[ws] = node

        await ws.send(json.dumps({
            "type": "welcome", "assigned_number": node["number"],
            "lb_list": state.lb_list(), "mail_list": state.mail_list(),
        }))
        await master_broadcast(state)

        async for raw in ws:
            pass  # pas de messages entrants dans ce test

    except Exception: pass
    finally:
        if node:
            if node["role"] == "lb"   and ws in state.lb_nodes:   del state.lb_nodes[ws]
            if node["role"] == "mail" and ws in state.mail_nodes: del state.mail_nodes[ws]
            if node["role"] == "lb":
                state.renumber_lb()   # renumerotation apres depart
        await master_broadcast(state)


# ─────────────────────────────────────────────
# Registre global pour simuler notify_lb_of_election entre slaves
# En prod, le nouveau maitre ouvre une connexion WebSocket vers chaque pair
# et envoie {"type": "new_master", ...}. Ici on simule directement.
# ─────────────────────────────────────────────

ALL_SLAVES: dict[str, "SlaveNode"] = {}   # name -> SlaveNode


# ─────────────────────────────────────────────
# Slave avec renumerotation dynamique
# ─────────────────────────────────────────────

class SlaveNode:
    def __init__(self, cfg, name):
        self.cfg     = cfg
        self.name    = name
        self.ip      = cfg["node"]["ip"]
        self.dns     = cfg["node"]["dns"]
        self.number  = 0
        self.lb_list = []
        self.is_master       = False
        self.dns_updates     = []
        self._became_master  = asyncio.Event()
        self._got_new_master = asyncio.Event()
        self._election_lock  = asyncio.Lock()
        self._stop           = asyncio.Event()
        ALL_SLAVES[name]     = self   # enregistrement pour notify inter-slaves

    def _apply_update(self, msg):
        if msg["type"] == "update_list":
            self.lb_list = msg.get("lb_list", [])
            # Resynchronisation du numero apres renumerotation
            new_num = msg.get("your_number")
            if new_num and new_num != self.number:
                log.debug(f"[{self.name}] Renumerotation #{self.number} -> #{new_num}")
                self.number = new_num
        elif msg["type"] == "new_master":
            self._got_new_master.set()

    async def _check_lower_alive(self):
        port    = self.cfg["master"]["port"]
        lowers  = [lb for lb in self.lb_list
                   if lb.get("number", 0) < self.number and lb["ip"] != self.ip]
        for lb in lowers:
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_connection(lb["ip"], port), timeout=1.0)
                w.close(); await w.wait_closed()
                return True
            except Exception:
                pass
        return False

    async def _maybe_elect(self):
        delay = self.number * 1.0
        log.debug(f"[{self.name}] Election: attente {delay:.0f}s (rang #{self.number})")
        try:
            await asyncio.wait_for(self._got_new_master.wait(), timeout=delay)
            log.debug(f"[{self.name}] new_master recu, abandon election")
            return  # Quelqu'un d'autre a pris la main
        except asyncio.TimeoutError:
            pass
        async with self._election_lock:
            if self.is_master: return
            if await self._check_lower_alive(): return
            log.warning(f"[{self.name}] LB#{self.number} se proclame maitre")
            self.is_master = True
            self.dns_updates.append(self.ip)
            self._became_master.set()
            # Notifier tous les autres slaves pour stopper leurs elections
            # (simule notify_lb_of_election du vrai slave_daemon.py)
            for other in list(ALL_SLAVES.values()):
                if other is not self and not other._stop.is_set() and not other.is_master:
                    log.debug(f"[{self.name}] Notification new_master -> {other.name}")
                    other._got_new_master.set()

    async def connect_loop(self):
        port = self.cfg["master"]["port"]
        while not self._stop.is_set():
            uri = f"ws://127.0.0.1:{port}"
            try:
                async with websockets.connect(uri, open_timeout=1) as ws:
                    self._got_new_master.clear()
                    await ws.send(json.dumps({
                        "type": "register", "role": "lb",
                        "ip": self.ip, "dns": self.dns,
                    }))
                    raw = await ws.recv()                   # welcome
                    self.number  = json.loads(raw)["assigned_number"]
                    raw = await ws.recv()                   # broadcast initial
                    self._apply_update(json.loads(raw))
                    async for raw in ws:
                        if self._stop.is_set(): break
                        self._apply_update(json.loads(raw))
            except Exception:
                pass
            if not self._stop.is_set():
                await self._maybe_elect()
                if self.is_master: break
                await asyncio.sleep(0.2)

    def stop(self): self._stop.set()


# ─────────────────────────────────────────────
# SCENARIO 1 : Ordre de connexion -> numeros sequentiels, pas de memoire
# ─────────────────────────────────────────────

async def scenario_order():
    ALL_SLAVES.clear()
    print("\n" + "="*60)
    print("  SCENARIO 1 : Numeros sequentiels, pas de memoire entre sessions")
    print("="*60)

    state  = MasterState()
    server = await websockets.serve(
        lambda ws, p=None: master_handler(ws, state), "127.0.0.1", TEST_PORT_S1)

    # LB3 se connecte AVANT lb1 et lb2
    lb3 = SlaveNode(make_cfg("10.1.0.3", "lb3.test", TEST_PORT_S1), "LB3")
    t3  = asyncio.create_task(lb3.connect_loop())
    await asyncio.sleep(0.15)
    lb1 = SlaveNode(make_cfg("10.1.0.1", "lb1.test", TEST_PORT_S1), "LB1")
    t1  = asyncio.create_task(lb1.connect_loop())
    await asyncio.sleep(0.15)
    lb2 = SlaveNode(make_cfg("10.1.0.2", "lb2.test", TEST_PORT_S1), "LB2")
    t2  = asyncio.create_task(lb2.connect_loop())
    await asyncio.sleep(0.5)

    print(f"  Ordre connexion: lb3 en premier, lb1 en 2eme, lb2 en 3eme")
    print(f"  Numeros assignes: lb3={lb3.number}, lb1={lb1.number}, lb2={lb2.number}")

    if lb3.number == 1 and lb1.number == 2 and lb2.number == 3:
        ok("Numeros suivent l'ordre d'arrivee : lb3=#1, lb1=#2, lb2=#3")
    else:
        fail(f"Numeros incorrects : lb3={lb3.number}, lb1={lb1.number}, lb2={lb2.number}")

    # LB1 crash et revient : doit prendre une NOUVELLE place en bas (pas de memoire)
    lb1.stop(); t1.cancel()
    await asyncio.gather(t1, return_exceptions=True)
    await asyncio.sleep(0.3)

    lb1b = SlaveNode(make_cfg("10.1.0.1", "lb1.test", TEST_PORT_S1), "LB1-retour")
    t1b  = asyncio.create_task(lb1b.connect_loop())
    await asyncio.sleep(0.8)

    print(f"  Apres crash+retour de lb1 : lb3={lb3.number}, lb2={lb2.number}, lb1b={lb1b.number}")

    # Apres crash de lb1 (#2) : lb3 reste #1, lb2 monte de #3 a #2
    if lb3.number == 1:
        ok("lb3 garde son #1 apres le crash de lb1")
    else:
        fail(f"lb3.number={lb3.number} (attendu #1)")

    if lb2.number == 2:
        ok("lb2 monte de #3 a #2 apres le crash de lb1 (renumerotation)")
    else:
        fail(f"lb2.number={lb2.number} (attendu #2 apres renumerotation)")

    # lb1 de retour : pas reconnu, prend la place #3 (bas de liste)
    if lb1b.number == 3:
        ok("lb1 de retour : non reconnu, prend la place #3 (bas de liste)")
    else:
        fail(f"lb1b.number={lb1b.number} (attendu #3, bas de liste)")

    lb3.stop(); lb2.stop(); lb1b.stop()
    t3.cancel(); t2.cancel(); t1b.cancel()
    server.close(); await server.wait_closed()
    await asyncio.gather(t3, t2, t1b, return_exceptions=True)


# ─────────────────────────────────────────────
# SCENARIO 2 : Renumerotation + election avec les bons numeros
# ─────────────────────────────────────────────

async def scenario_renumber_election():
    ALL_SLAVES.clear()
    print("\n" + "="*60)
    print("  SCENARIO 2a : Master tombe → LB#1 prend la main (tous vivants)")
    print("  SCENARIO 2b : Master + LB#1 tombent → LB#2 (ex-LB#3) prend la main")
    print("="*60)

    state  = MasterState()
    server = await websockets.serve(
        lambda ws, p=None: master_handler(ws, state), "127.0.0.1", TEST_PORT_S2)

    lb1 = SlaveNode(make_cfg("127.0.0.1", "lb1.test", TEST_PORT_S2), "LB1")
    t1  = asyncio.create_task(lb1.connect_loop())
    await asyncio.sleep(0.15)
    lb2 = SlaveNode(make_cfg("127.0.0.1", "lb2.test", TEST_PORT_S2), "LB2")
    t2  = asyncio.create_task(lb2.connect_loop())
    await asyncio.sleep(0.15)
    lb3 = SlaveNode(make_cfg("127.0.0.1", "lb3.test", TEST_PORT_S2), "LB3")
    t3  = asyncio.create_task(lb3.connect_loop())
    await asyncio.sleep(0.5)

    print(f"  Etat initial : lb1=#{lb1.number}, lb2=#{lb2.number}, lb3=#{lb3.number}")

    if lb1.number == 1 and lb2.number == 2 and lb3.number == 3:
        ok("Etat initial correct : lb1=#1, lb2=#2, lb3=#3")
    else:
        fail(f"Etat initial : lb1={lb1.number}, lb2={lb2.number}, lb3={lb3.number}")

    # ── LB#2 tombe (healthcheck) → lb3 monte a #2 ────────────────────────
    lb2.stop(); t2.cancel()
    await asyncio.gather(t2, return_exceptions=True)
    await asyncio.sleep(0.5)

    print(f"  Apres panne LB#2 : lb1=#{lb1.number}, lb3=#{lb3.number}")

    if lb3.number == 2:
        ok("LB3 (ex-#3) renumerote a #2 apres depart de LB#2")
    else:
        fail(f"lb3.number={lb3.number} (attendu #2)")

    # ── SCENARIO 2a : Master tombe, LB#1 et LB#3 (maintenant #2) vivants ─
    print("\n  -- 2a : master tombe, lb1 et lb3 vivants --")
    server.close()
    await server.wait_closed()

    # LB#1 attend 1s, LB#3 (rang #2) attend 2s → LB#1 doit gagner
    timeout_2a = 1.0 + 5.0  # 1s election + marge
    try:
        await asyncio.wait_for(lb1._became_master.wait(), timeout=timeout_2a)
        ok(f"2a : LB#1 (rang #{lb1.number}) se proclame maitre en premier (attend 1s)")
    except asyncio.TimeoutError:
        fail(f"2a : LB#1 n'a pas pris la main dans les {timeout_2a:.0f}s")

    # LB#3 (rang #2) ne doit PAS avoir pris la main (LB#1 l'a devance)
    await asyncio.sleep(2.5)  # laisser le temps a LB#3 d'essayer
    if not lb3.is_master:
        ok("2a : LB#3 (rang #2) ne prend pas la main — LB#1 l'a devance")
    else:
        fail("2a : LB#3 s'est aussi proclame maitre (election en double)")

    # ── SCENARIO 2b : Reset — master + LB#1 tombent, seul LB#3 (rang #2) reste ─
    print("\n  -- 2b : master + lb1 tombent, seul lb3 (#2) reste --")

    # Stopper les survivors de 2a pour eviter qu'ils se reconnectent a server2
    lb1.stop(); lb3.stop()
    t1.cancel(); t3.cancel()
    await asyncio.gather(t1, t3, return_exceptions=True)
    ALL_SLAVES.clear()

    # Nouveau master + nouvelle session
    state2 = MasterState()
    server2 = await websockets.serve(
        lambda ws, p=None: master_handler(ws, state2), "127.0.0.1", TEST_PORT_S2)

    lb1c = SlaveNode(make_cfg("127.0.0.1", "lb1b.test", TEST_PORT_S2), "LB1c")
    t1c  = asyncio.create_task(lb1c.connect_loop())
    await asyncio.sleep(0.15)
    lb3c = SlaveNode(make_cfg("127.0.0.1", "lb3b.test", TEST_PORT_S2), "LB3c")
    t3c  = asyncio.create_task(lb3c.connect_loop())
    await asyncio.sleep(0.5)

    print(f"  Nouvelle session : lb1c=#{lb1c.number}, lb3c=#{lb3c.number}")

    # LB#2 (lb3c) tombe → lb3c monte a #1... non, ici lb1c=#1 lb3c=#2
    # On simule : master + lb1c tombent → lb3c (#2) doit prendre la main
    lb1c.stop(); t1c.cancel()
    await asyncio.gather(t1c, return_exceptions=True)
    server2.close(); await server2.wait_closed()

    print(f"  Master + LB#1 tombes. LB3c (rang #{lb3c.number}) attend {lb3c.number}s...")

    try:
        await asyncio.wait_for(lb3c._became_master.wait(), timeout=lb3c.number + 5.0)
        ok(f"2b : LB#2 (ex-LB#3, rang #{lb3c.number}) se proclame maitre quand LB#1 est absent")
    except asyncio.TimeoutError:
        fail(f"2b : LB3c n'a pas pris la main (rang #{lb3c.number})")

    lb1.stop(); lb3.stop(); lb3c.stop()
    t1.cancel(); t3.cancel(); t3c.cancel()
    await asyncio.gather(t1, t3, t3c, return_exceptions=True)


# ─────────────────────────────────────────────
# SCENARIO 3 : Crash + reconnexion = bas de liste, non reconnu
# ─────────────────────────────────────────────

async def scenario_crash_rejoin():
    ALL_SLAVES.clear()
    print("\n" + "="*60)
    print("  SCENARIO 3 : Crash + reconnexion = nouveau numero en bas, pas de memoire")
    print("="*60)

    state  = MasterState()
    server = await websockets.serve(
        lambda ws, p=None: master_handler(ws, state), "127.0.0.1", TEST_PORT_S3)

    lb1 = SlaveNode(make_cfg("10.3.0.1", "lb1.test", TEST_PORT_S3), "LB1")
    t1  = asyncio.create_task(lb1.connect_loop())
    await asyncio.sleep(0.15)
    lb2 = SlaveNode(make_cfg("10.3.0.2", "lb2.test", TEST_PORT_S3), "LB2")
    t2  = asyncio.create_task(lb2.connect_loop())
    await asyncio.sleep(0.15)
    lb3 = SlaveNode(make_cfg("10.3.0.3", "lb3.test", TEST_PORT_S3), "LB3")
    t3  = asyncio.create_task(lb3.connect_loop())
    await asyncio.sleep(0.5)

    print(f"  Etat initial : lb1={lb1.number}, lb2={lb2.number}, lb3={lb3.number}")

    # LB2 crash
    lb2.stop(); t2.cancel()
    await asyncio.gather(t2, return_exceptions=True)
    await asyncio.sleep(0.5)

    print(f"  Apres crash lb2 : lb1={lb1.number}, lb3={lb3.number}")

    if lb1.number == 1 and lb3.number == 2:
        ok("Renumerotation : lb1=#1, lb3 monte a #2")
    else:
        fail(f"lb1={lb1.number}, lb3={lb3.number} (attendu lb1=#1, lb3=#2)")

    # LB2 revient : pas reconnu, prend la place #3
    lb2b = SlaveNode(make_cfg("10.3.0.2", "lb2.test", TEST_PORT_S3), "LB2-retour")
    t2b  = asyncio.create_task(lb2b.connect_loop())
    await asyncio.sleep(1.0)

    print(f"  Apres retour lb2 : lb1={lb1.number}, lb3={lb3.number}, lb2b={lb2b.number}")

    if lb2b.number == 3:
        ok("LB2 de retour : non reconnu, prend la place #3 (bas de liste)")
    else:
        fail(f"lb2b.number={lb2b.number} (attendu #3, bas de liste)")

    # lb3 est maintenant #2 et est donc prioritaire sur lb2b (#3) pour l'election
    if lb3.number < lb2b.number:
        ok(f"lb3 (#{lb3.number}) est prioritaire sur lb2b (#{lb2b.number}) pour l'election")
    else:
        fail(f"Priorite incorrecte : lb3={lb3.number}, lb2b={lb2b.number}")

    # Liste finale : 3 LB, numeros 1, 2, 3 compacts
    all_numbers = sorted([lb1.number, lb3.number, lb2b.number])
    if all_numbers == [1, 2, 3]:
        ok(f"Liste finale compacte : {all_numbers}")
    else:
        fail(f"Liste non compacte : {all_numbers}")

    lb1.stop(); lb3.stop(); lb2b.stop()
    t1.cancel(); t3.cancel(); t2b.cancel()
    server.close(); await server.wait_closed()
    await asyncio.gather(t1, t3, t2b, return_exceptions=True)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  Test Election & Resilience (renumerotation compacte)")
    print("=" * 60)

    await scenario_order()
    await scenario_renumber_election()
    await scenario_crash_rejoin()

    passed = sum(1 for r in RESULTS if r[0] == "PASS")
    failed = sum(1 for r in RESULTS if r[0] == "FAIL")
    print(f"\n{'='*60}")
    print(f"  Resultats : {passed} PASS / {failed} FAIL")
    print(f"{'='*60}")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
