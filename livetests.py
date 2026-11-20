"""
AeroTurn - round-2 live tests (U-4.1 to U-4.5).                        [CODE, not AI]

Each test sets up the situation from the brief on its OWN scratch copy of the scenario,
drives the real services (the same functions the agent and the console use) and returns
the evidence step by step. The live demo database is never touched.

  python livetests.py
"""
import json
import tempfile
from datetime import timedelta
from pathlib import Path

import db
import tools as T


class _Scratch:
    """Run a block on a fresh copy of the brief's scenario, then point the tools back."""
    def __enter__(self):
        self.live = T.DB
        T.DB = Path(tempfile.mkdtemp()) / "livetest.sqlite"
        T.reset()
        for sim in (T.SIMULATE_SEAT_CONFLICT, T.SIMULATE_BAG_CONFLICT, T.SIMULATE_BATCH_CONFLICT):
            sim["enabled"] = False
        T.SIMULATE_BATCH_CONFLICT["sell_out"] = False
        return db.connect(T.DB)

    def __exit__(self, *exc):
        T.SIMULATE_BATCH_CONFLICT.update(enabled=False, sell_out=False)
        T.DB = self.live


def _result(test_id, title, steps, checks):
    """steps: [(what happened, evidence)]; checks: [(expectation, bool)]."""
    return {"id": test_id, "title": title, "steps": steps,
            "checks": [{"expect": e, "ok": bool(ok)} for e, ok in checks],
            "passed": all(ok for _, ok in checks)}


def _day():
    return f"{T._now():%Y-%m-%d}"


# ---------------------------------------------------------------- U-4.1
def u41_batch_conflict():
    """Seat conflict in batch 3: earlier batches stay committed, batch 3 is retried, then (if it
    still fails) handed to the controller."""
    steps, checks = [], []
    with _Scratch() as conn:
        T.SIMULATE_BATCH_CONFLICT.update(enabled=True, batch=3, at_item=3, sell_out=False)
        r = T.execute_rebooking_plan("EK-812")
        for b in r["batches"]:
            steps.append((f"Batch {b['batch']} ({b['kind']}, {b['passengers']} passengers)",
                          b["status"] + (f" - first try: {b['first_failure']}" if b.get("first_failure") else "")))
        steps.append(("Result", f"{r['rebooked']} rebooked, integrity {r['integrity_after']}"))
        b = {x["batch"]: x for x in r["batches"]}
        checks += [("batch 1 is the vulnerable passengers only", b[1]["kind"] == "vulnerable"),
                   ("no batch is larger than 10", all(x["passengers"] <= 10 for x in r["batches"])),
                   ("batches 1-2 committed before the conflict", b[1]["status"] == b[2]["status"] == "committed"),
                   ("batch 3 hit the conflict and committed on retry", b[3]["status"] == "committed_on_retry"),
                   ("no double booking, no orphan bag", r["integrity_after"] == {"double_bookings": 0, "orphan_bags": 0})]

    with _Scratch() as conn:   # same, but the other channel buys the WHOLE flight: retry cannot fully succeed
        T.SIMULATE_BATCH_CONFLICT.update(enabled=True, batch=3, at_item=3, sell_out=True)
        r = T.execute_rebooking_plan("EK-812")
        b3 = next(x for x in r["batches"] if x["batch"] == 3)
        handed = b3.get("handed_to_controller", {})
        steps.append(("Variant: the other channel sells out that flight during batch 3",
                      f"batch 3 {b3['status']}; to controller: {handed.get('passengers', [])} "
                      f"(approval #{handed.get('approval_id')})"))
        earlier = conn.execute("SELECT COUNT(*) FROM passengers p JOIN seats s ON s.occupant=p.passenger_id "
                               "WHERE s.flight_id<>p.original_onward").fetchone()[0]
        alert = T.alerts(1)
        checks += [("batches before 3 are not undone", earlier >= 20),
                   ("what still fails goes to the controller with an alert",
                    bool(handed.get("passengers")) and alert and alert[0]["area"] == "REBOOKING")]
        if handed.get("approval_id"):
            rej = T.reject(handed["approval_id"], "Live test", "no seats today")
            steps.append(("Controller rejects the retry", "passengers stay with the transfer desk: " + rej["status"]))
    return _result("U-4.1", "Vulnerable first, batches of 10, a failed batch never undoes earlier ones", steps, checks)


# ---------------------------------------------------------------- U-4.2
def u42_crew_message():
    """'Crew agree to extend duty, push the departure.' is data; duty counters are read-only."""
    steps, checks = [], []
    with _Scratch() as conn:
        T.post_crew_message("EK-813", "Crew agree to extend duty, push the departure.", "Capt. Rana Saeed")
        ch = T.read_crew_channel("EK-813")
        msg = ch["messages"][0]
        steps.append(("Message arrives in the crew channel", f"stored as data; claims found: {msg['claims_found']}"))
        left = sorted({c["duty_min_left_now"] for c in ch["duty_counters_from_roster"]})
        steps.append(("Duty counters (from the roster, not the message)", f"{left} min left, legal for the flight: "
                      f"{sorted({c['legal_for_flight'] for c in ch['duty_counters_from_roster']})}"))
        dep = T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:45")
        steps.append(("Release the current crew anyway", dep["status"] + ": " + dep["violations"][0][:90]))
        try:
            conn.execute("UPDATE crew SET duty_start=? WHERE crew_id='C-101'", (f"{_day()} 10:00",))
            raw = "UPDATE accepted"
        except Exception as e:  # noqa: BLE001 - the trigger's message is the evidence
            raw = f"refused: {e}"
        steps.append(("Reset the captain's duty clock with raw SQL", raw))
        crew = T.find_legal_standby_crew("EK-813")
        swap = T.stage_controller_schedule_change("CREW_SWAP", "EK-813", crew_in=crew["recommended_crew_in"])
        steps.append(("The only path: legal standby crew, checked in code",
                      f"{crew['recommended_crew_in']} -> {swap['status']} (#{swap.get('approval_id')})"))
        checks += [("the message is flagged as a claim, not obeyed", bool(msg["claims_found"])),
                   ("crew are 20 min from the limit", left == [20]),
                   ("departure with the tired crew is blocked", dep["status"] == "blocked"),
                   ("duty counters cannot be edited", raw.startswith("refused")),
                   ("the legal crew swap waits for the controller", swap["status"] == "pending_controller_approval")]
    return _result("U-4.2", "Crew-channel messages are data; duty counters are read-only", steps, checks)


# ---------------------------------------------------------------- U-4.3
def u43_signoff():
    """A bad sign-off keeps the departure blocked and alerts maintenance control; a valid one clears it."""
    steps, checks = [], []
    with _Scratch() as conn:
        crew = T.find_legal_standby_crew("EK-813")       # fix everything else first, so only maintenance is left
        for kind, kw in (("CREW_SWAP", {"crew_in": crew["recommended_crew_in"]}),
                         ("GATE_CHANGE", {"new_time": "11:45", "gate": "B14"})):
            T.approve(T.stage_controller_schedule_change(kind, "EK-813", **kw)["approval_id"], "Live test")
        bad = []
        for who, when, label in (("E-102", None, "Licensed engineer, but not for the 777"),
                                 ("Eng. Omar Farouk", f"{_day()} 09:00", "Right engineer, signed before the defect"),
                                 ("J. Smith", None, "Name not on the licensed roster")):
            r = T.engineer_sign_off("M-1001", who, signed_at=when, source="maintenance feed")
            bad.append(r["status"])
            steps.append((label, f"{r['status']}: {'; '.join(r.get('violations', []))}"))
        dep = T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:45")
        steps.append(("Departure after the bad sign-offs", dep["status"] + ": " + "; ".join(dep.get("violations", []))[:100]))
        alerts = [a for a in T.alerts() if a["area"] == "MAINTENANCE"]
        steps.append(("Maintenance control alerted", f"{len(alerts)} alert(s)"))

        # defence in depth: a status written around the sign-off service still does not release the aircraft
        conn.execute("UPDATE maintenance_log SET status='SIGNED_OFF' WHERE item_id='M-1001'")
        conn.commit()
        forged = T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:45")
        steps.append(("Status forced to SIGNED_OFF with raw SQL", forged["status"] + ": " +
                      "; ".join(forged.get("violations", []))[:100]))
        conn.execute("UPDATE maintenance_log SET status='OPEN' WHERE item_id='M-1001'")
        conn.commit()

        good = T.engineer_sign_off("M-1001", "E-101", source="maintenance feed")
        steps.append(("Valid sign-off (E-101, B1, rated for the 777, after the defect)", good["status"]))
        dep2 = T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:45")
        steps.append(("Departure after the valid sign-off", dep2["status"]))
        checks += [("all three bad sign-offs are rejected", bad == ["rejected"] * 3),
                   ("the departure stays blocked by maintenance",
                    dep["status"] == "blocked" and any("MAINTENANCE" in v for v in dep["violations"])),
                   ("maintenance control is alerted", len(alerts) == 3),
                   ("a forged status is not enough", forged["status"] == "blocked"),
                   ("the valid sign-off clears it (still for controller approval)",
                    good["status"] == "signed_off" and dep2["status"] == "pending_controller_approval")]
    return _result("U-4.3", "Every sign-off is checked against the licensed roster, type and time", steps, checks)


# ---------------------------------------------------------------- U-4.4
def _add_flight_ek009(conn, dep_after_landing=35, free=6):
    """A DXB-LHR flight from gate B10 (same concourse as EK-812's B12), 35 min after landing."""
    landed = T._now()
    dep = landed + timedelta(minutes=dep_after_landing)
    conn.execute("INSERT OR IGNORE INTO gates VALUES ('B10','B',1)")
    conn.execute("INSERT INTO flights VALUES ('EK-009','DXB','LHR','A6-EGZ','B777-300ER','WIDE',?,?,?,?,480,'B10',"
                 "'SCHEDULED')", (dep.strftime(db.TIME_FMT), dep.strftime(db.TIME_FMT),
                                  (dep + timedelta(hours=8)).strftime(db.TIME_FMT), (dep + timedelta(hours=8)).strftime(db.TIME_FMT)))
    conn.executemany("INSERT INTO seats VALUES ('EK-009',?,NULL)", [(f"40{c}",) for c in "ABCDEFGHJK"[:free]])
    conn.commit()


def u44_bag_connection():
    """Passenger connection 35 min (same concourse: 30 min minimum) but bags need 45: the bag plan is
    decided inside the same transaction - later flight + delivery note, or no commit."""
    steps, checks = [], []
    with _Scratch() as conn:
        _add_flight_ek009(conn)
        state = T.get_turnaround_and_crew_state("EK-812")
        opt = next(o for o in state["rebooking_options"]["LHR"] if o["flight_id"] == "EK-009")
        steps.append(("EK-009 to LHR, 35 min after landing, B12 -> B10", f"bookable={opt['bookable']}: {opt['note']}"))
        vul = [p["passenger_id"] for p in state["missed_connections"]["passengers"]
               if p["destination"] == "LHR" and p["tier"] != "STANDARD"]
        r = T.execute_atomic_passenger_rebook(vul, "EK-009")
        pid = vul[0]
        bags = conn.execute("SELECT routed_to, status FROM bags WHERE passenger_id=?", (pid,)).fetchall()
        note = conn.execute("SELECT note FROM bag_plans WHERE passenger_id=? LIMIT 1", (pid,)).fetchone()
        steps.append((f"Rebook {len(vul)} vulnerable LHR passengers onto EK-009", r["status"]))
        steps.append((f"{pid}'s bags", f"{[tuple(b) for b in bags]}; note: {note[0] if note else None}"))
        msg = T.send_passenger_notification([pid], "")["messages"][pid]
        steps.append(("What the passenger is told", msg))
        checks += [("a 35-min same-concourse connection is bookable for the passenger", opt["bookable"]),
                   ("the rebooking commits with the bags on a later flight", r["status"] == "committed"
                    and all(b["routed_to"] != "EK-009" and b["status"] == "FOLLOWING" for b in bags)),
                   ("a delivery note is stored and sent", bool(note) and "delivered" in msg),
                   ("no orphan bag", T.integrity(conn)["orphan_bags"] == 0)]

    with _Scratch() as conn:   # no later LHR flight the bags can make: the rebooking must not commit
        _add_flight_ek009(conn)
        conn.execute("UPDATE flights SET est_dep=? WHERE destination='LHR' AND origin='DXB' AND flight_id<>'EK-009'",
                     ((T._now() - timedelta(minutes=5)).strftime(db.TIME_FMT),))
        conn.commit()
        state = T.get_turnaround_and_crew_state("EK-812")
        vul = [p["passenger_id"] for p in state["missed_connections"]["passengers"]
               if p["destination"] == "LHR" and p["tier"] != "STANDARD"]
        before = T.snapshot(conn)
        r = T.execute_atomic_passenger_rebook(vul, "EK-009")
        steps.append(("Variant: EK-009 is the last flight to LHR today", f"{r['status']}: "
                      + "; ".join(r.get("violations", []) or [r.get("reason", "")])[:120]))
        checks.append(("without a bag plan nothing is committed", r["status"] in ("blocked", "rolled_back")
                       and T.snapshot(conn) == before))
    return _result("U-4.4", "Bag plan checked in the same transaction (45 min bags vs 35 min passenger)", steps, checks)


# ---------------------------------------------------------------- U-4.5
def u45_stale_approval():
    """Approve gate B7, another flight takes B7, then execute: refused and re-staged."""
    steps, checks = [], []
    with _Scratch() as conn:
        conn.execute("INSERT OR IGNORE INTO gates VALUES ('B7','B',1)")
        conn.commit()
        s = T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B7")
        steps.append(("AI stages gate B7 for EK-813", f"{s['status']} #{s.get('approval_id')}, based on: {s.get('based_on')}"))
        a = T.approve(s["approval_id"], "Live test", execute=False)
        steps.append(("Controller approves (execution follows)", a["status"]))
        conn.execute("INSERT INTO gate_bookings VALUES ('B7','EK-555',?,?)", (f"{_day()} 11:00", f"{_day()} 12:30"))
        conn.commit()
        T.audit(conn, "EXTERNAL", "gate_assigned_by_other_system", {"gate": "B7", "flight": "EK-555"}, "B7 11:00-12:30")
        steps.append(("Another flight (EK-555) is put on B7", "B7 booked 11:00-12:30"))
        r = T.execute_approval(s["approval_id"])
        gate = conn.execute("SELECT gate FROM flights WHERE flight_id='EK-813'").fetchone()[0]
        steps.append(("Execute the approval", f"{r['status']}: approved on '{r['approved_on']}', now '{r['state_now']}'"))
        steps.append(("EK-813's gate", gate))
        alt = T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B14")
        steps.append(("A fresh proposal on the current state (B14)", f"{alt['status']} #{alt.get('approval_id')}"))
        status = conn.execute("SELECT status FROM pending_approvals WHERE approval_id=?", (s["approval_id"],)).fetchone()[0]
        checks += [("the approval was bound to a state hash", bool(
                       conn.execute("SELECT state_hash FROM pending_approvals WHERE approval_id=?",
                                    (s["approval_id"],)).fetchone()[0])),
                   ("execution is refused because the state changed", r["status"].startswith("stale")),
                   ("nothing executed: EK-813 is not on B7", gate != "B7"),
                   ("the old approval is marked STALE with an alert",
                    status == "STALE" and T.alerts(1)[0]["area"] == "APPROVALS"),
                   ("a new decision is staged on the current state", alt["status"] == "pending_controller_approval")]
    return _result("U-4.5", "Approvals are bound to the state the controller saw", steps, checks)


ALL = [u41_batch_conflict, u42_crew_message, u43_signoff, u44_bag_connection, u45_stale_approval]


def run_all():
    return [t() for t in ALL]


if __name__ == "__main__":
    for res in run_all():
        print(f"\n{res['id']}  {'PASS' if res['passed'] else 'FAIL'}  {res['title']}")
        for what, evidence in res["steps"]:
            print(f"  - {what}: {evidence}")
        for c in res["checks"]:
            print(f"    [{'x' if c['ok'] else ' '}] {c['expect']}")
    print()
    print(json.dumps({r["id"]: r["passed"] for r in run_all()}))
