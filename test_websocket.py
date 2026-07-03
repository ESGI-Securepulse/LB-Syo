#!/usr/bin/env python3
"""
test_websocket.py — Test d'integration WebSocket master <-> slave
Lance le master en arriere-plan, connecte 2 LB et 1 mail, verifie
les messages, teste la deconnexion et la propagation de liste.
"""
import asyncio, json, sys
import websockets

PORT = 8766   # Port dedie aux tests (evite le 8765 de prod)
RESULTS = []

def ok(msg):  RESULTS.append(("PASS", msg)); print(f"  PASS -- {msg}")
def fail(msg): RESULTS.append(("FAIL", msg)); print(f"  FAIL -- {msg}")


# ─────────────────────────────────────────────
# Serveur master minimal (version test)
# Meme logique que master_daemon mais sur le port de test
# ─────────────────────────────────────────────

lb_nodes = {}
mail_nodes = {}
lb_counter = 0
mail_counter = 0


async def send(ws, payload):
    try:
        await ws.send(json.dumps(payload))
    except Exception:
        pass


async def broadcast(lb_list, mail_list):
    msg = {"type": "update_list", "lb_list": lb_list, "mail_list": mail_list}
    for ws in list(lb_nodes) + list(mail_nodes):
        await send(ws, msg)


async def master_handler(ws):
    global lb_counter, mail_counter
    node = None
    try:
        raw = await ws.recv()
        msg = json.loads(raw)
        role = msg["role"]

        if role == "lb":
            lb_counter += 1
            node = {"number": lb_counter, "ip": msg["ip"], "dns": msg["dns"], "role": "lb"}
            lb_nodes[ws] = node
        elif role == "mail":
            mail_counter += 1
            node = {"number": mail_counter, "ip": msg["ip"], "dns": msg["dns"], "role": "mail"}
            mail_nodes[ws] = node

        lb_list = list(lb_nodes.values())
        mail_list = list(mail_nodes.values())

        await send(ws, {"type": "welcome", "assigned_number": node["number"],
                        "lb_list": lb_list, "mail_list": mail_list})
        await broadcast(lb_list, mail_list)

        async for _ in ws:
            pass  # Pas de messages entrants dans ce test

    except Exception:
        pass
    finally:
        if node:
            if node["role"] == "lb" and ws in lb_nodes:
                del lb_nodes[ws]
            elif node["role"] == "mail" and ws in mail_nodes:
                del mail_nodes[ws]
            await broadcast(list(lb_nodes.values()), list(mail_nodes.values()))


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

async def run_tests():
    URI = f"ws://127.0.0.1:{PORT}"

    # ── TEST 1 : Connexion LB #1 ─────────────────────────────────────────
    print("\n[TEST 1] Connexion et enregistrement LB #1")
    async with websockets.connect(URI) as ws1:
        await ws1.send(json.dumps({"type": "register", "role": "lb",
                                    "ip": "10.0.0.1", "dns": "lb1.test"}))
        raw = await asyncio.wait_for(ws1.recv(), timeout=2)
        msg = json.loads(raw)

        assert msg["type"] == "welcome",             "type != welcome"
        assert msg["assigned_number"] == 1,          "numero != 1"
        assert len(msg["lb_list"]) == 1,             "lb_list vide"
        assert msg["lb_list"][0]["dns"] == "lb1.test"
        ok("LB #1 recoit welcome avec numero=1 et lb_list=[lb1]")

        # Consommer le broadcast initial envoye quand LB1 s'est connecte
        # (le master diffuse a tous les noeuds juste apres le welcome)
        _initial_bcast = json.loads(await asyncio.wait_for(ws1.recv(), timeout=2))
        assert _initial_bcast["type"] == "update_list"
        ok("LB #1 : broadcast initial consomme (lb_list=[lb1])")

        # ── TEST 2 : Connexion LB #2 ─────────────────────────────────────
        print("\n[TEST 2] Connexion LB #2, propagation a LB #1")
        async with websockets.connect(URI) as ws2:
            await ws2.send(json.dumps({"type": "register", "role": "lb",
                                        "ip": "10.0.0.2", "dns": "lb2.test"}))
            raw2 = await asyncio.wait_for(ws2.recv(), timeout=2)
            msg2 = json.loads(raw2)

            assert msg2["type"] == "welcome",        "type != welcome"
            assert msg2["assigned_number"] == 2,     "numero != 2"
            assert len(msg2["lb_list"]) == 2,        "lb2 ne voit pas les 2 LB"
            ok("LB #2 recoit welcome avec numero=2 et lb_list=[lb1, lb2]")

            # LB #1 doit recevoir update_list avec les 2 LB
            raw_update = await asyncio.wait_for(ws1.recv(), timeout=2)
            upd = json.loads(raw_update)
            assert upd["type"] == "update_list",     "LB1 pas notifie"
            assert len(upd["lb_list"]) == 2,         "update_list incorrect pour LB1"
            ok("LB #1 recoit update_list avec les 2 LB")

            # Consommer le broadcast initial de LB2 (envoi apres son propre welcome)
            _bcast_lb2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2))
            assert _bcast_lb2["type"] == "update_list"
            ok("LB #2 : broadcast initial consomme (lb_list=[lb1,lb2])")

            # ── TEST 3 : Connexion srv-mail ───────────────────────────────
            print("\n[TEST 3] Connexion srv-mail, propagation aux LB")
            async with websockets.connect(URI) as wsm:
                await wsm.send(json.dumps({"type": "register", "role": "mail",
                                            "ip": "10.0.1.1", "dns": "mail1.test"}))
                rawm = await asyncio.wait_for(wsm.recv(), timeout=2)
                msgm = json.loads(rawm)

                assert msgm["type"] == "welcome",    "mail: type != welcome"
                assert msgm["assigned_number"] == 1, "mail: numero != 1"
                ok("Srv-mail recoit welcome avec numero=1")

                # LB1 et LB2 doivent recevoir update_list avec le mail
                upd1 = json.loads(await asyncio.wait_for(ws1.recv(), timeout=2))
                upd2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2))

                assert len(upd1["mail_list"]) == 1,  "LB1: mail_list vide"
                assert len(upd2["mail_list"]) == 1,  "LB2: mail_list vide"
                assert upd1["mail_list"][0]["dns"] == "mail1.test"
                ok("LB1 et LB2 recoivent update_list avec mail1")

            # ── TEST 4 : Deconnexion srv-mail → propagation ──────────────
            print("\n[TEST 4] Deconnexion srv-mail, propagation de la liste vide")
            await asyncio.sleep(0.2)  # Laisser le master traiter la fermeture
            upd1 = json.loads(await asyncio.wait_for(ws1.recv(), timeout=2))
            assert upd1["type"] == "update_list",    "LB1: pas d'update apres deconnexion mail"
            assert len(upd1["mail_list"]) == 0,      "LB1: mail_list devrait etre vide"
            ok("Deconnexion mail detectee, LB1 recoit liste vide")

        # ── TEST 5 : Deconnexion LB #2 → propagation ─────────────────────
        print("\n[TEST 5] Deconnexion LB #2, LB #1 recoit liste a 1 element")
        await asyncio.sleep(0.2)
        upd = json.loads(await asyncio.wait_for(ws1.recv(), timeout=2))
        assert upd["type"] == "update_list",         "LB1: pas d'update apres deconnexion LB2"
        assert len(upd["lb_list"]) == 1,             "LB1: lb_list devrait avoir 1 element"
        assert upd["lb_list"][0]["dns"] == "lb1.test"
        ok("Deconnexion LB2 detectee, LB1 recoit lb_list=[lb1]")


async def main():
    print("=" * 55)
    print("  Test integration WebSocket Master <-> Slaves")
    print("=" * 55)

    server = await websockets.serve(master_handler, "127.0.0.1", PORT)
    print(f"Master de test demarre sur ws://127.0.0.1:{PORT}\n")

    try:
        await run_tests()
    except AssertionError as e:
        fail(f"Assertion : {e}")
    except asyncio.TimeoutError:
        fail("Timeout : pas de reponse dans les 2s")
    except Exception as e:
        fail(f"Exception inattendue : {e}")
        import traceback; traceback.print_exc()
    finally:
        server.close()
        await server.wait_closed()

    passed = sum(1 for r in RESULTS if r[0] == "PASS")
    failed = sum(1 for r in RESULTS if r[0] == "FAIL")
    print(f"\n{'='*55}")
    print(f"  Resultats : {passed} PASS / {failed} FAIL")
    print(f"{'='*55}")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
