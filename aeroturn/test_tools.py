"""
AeroTurn - Stage 2 tests: the services, the atomic rollback and the approval gate.
No AI involved (free, instant). Every test runs on its own fresh copy of the database.

  python test_tools.py      # or: pytest test_tools.py
"""
import tempfile
from pathlib import Path

import db
import tools as T


def fresh():
    """Point the tools at a brand-new database and return a connection to it."""
    T.DB = Path(tempfile.mkdtemp()) / "aeroturn_test.sqlite"
    db.build(T.DB)
    T.SIMULATE_SEAT_CONFLICT["enabled"] = False
    return db.connect(T.DB)


def missed(state, dest=None, vulnerable=None):
    out = state["missed_connections"]["passengers"]
    if dest:
        out = [p for p in out if p["destination"] == dest]
    if vulnerable is not None:
        out = [p for p in out if (p["tier"] != "STANDARD") == vulnerable]
    return [p["passenger_id"] for p in out]


# ---------- Tool 1: state ----------
def test_state_shows_the_disruption():
    fresh()
    s = T.get_turnaround_and_crew_state()
    assert s["inbound"]["delay_min"] == 75
    assert s["missed_connections"]["count"] == 45 and s["missed_connections"]["vulnerable"] == 10
    tiers = [p["tier"] for p in s["missed_connections"]["passengers"]]
    assert tiers[:10].count("STANDARD") == 0, "vulnerable passengers must be listed first"
    assert all(c["duty_min_left_now"] == 20 and not c["legal_for_EK-813"] for c in s["crew_on_EK-813"])
    assert s["outbound"]["earliest_legal_departure"] == "11:45"
    assert s["outbound"]["gate_problem"], "B12 conflict with EK-555 should be visible"
    ek003 = [o for o in s["rebooking_options"]["LHR"] if o["flight_id"] == "EK-003"][0]
    assert not ek003["bookable"] and "only 55 min" in ek003["note"]


# ---------- Tool 2: atomic rebooking ----------
def test_rebook_vulnerable_first_commits():
    conn = fresh()
    s = T.get_turnaround_and_crew_state()
    batch = missed(s, "LHR", vulnerable=True)
    r = T.execute_atomic_passenger_rebook(batch, "EK-005")
    assert r["status"] == "committed" and len(r["rebooked"]) == 5, r
    assert T.integrity(conn) == {"double_bookings": 0, "orphan_bags": 0}
    for pid in batch:  # bags followed the passenger
        assert {b[0] for b in conn.execute("SELECT routed_to FROM bags WHERE passenger_id=?", (pid,))} == {"EK-005"}
    assert T.get_turnaround_and_crew_state()["missed_connections"]["count"] == 40


def test_standard_passengers_cannot_jump_the_queue():
    conn = fresh()
    s = T.get_turnaround_and_crew_state()
    before = T.snapshot(conn)
    # JFK: 9 seats for 12 people, 4 vulnerable. 6 standard first would leave 3 seats for 4 vulnerable.
    r = T.execute_atomic_passenger_rebook(missed(s, "JFK", vulnerable=False)[:6], "EK-203")
    assert r["status"] == "blocked" and "PRIORITY" in r["violations"][0], r
    assert T.snapshot(conn) == before


def test_trap_flight_ek003_blocked():
    fresh()
    s = T.get_turnaround_and_crew_state()
    r = T.execute_atomic_passenger_rebook(missed(s, "LHR", vulnerable=True), "EK-003")
    assert r["status"] == "blocked" and any("only 55 min" in v for v in r["violations"]), r


def test_too_many_for_the_seats_blocked():
    fresh()
    s = T.get_turnaround_and_crew_state()
    r = T.execute_atomic_passenger_rebook(missed(s, "JFK"), "EK-203")   # 12 people, 9 seats
    assert r["status"] == "blocked" and any("SEATS" in v for v in r["violations"]), r


def test_wrong_destination_and_duplicates_blocked():
    fresh()
    s = T.get_turnaround_and_crew_state()
    cdg = missed(s, "CDG")
    r = T.execute_atomic_passenger_rebook([cdg[0], cdg[0]], "EK-005")
    assert r["status"] == "blocked" and len(r["violations"]) >= 2, r


def test_midway_seat_conflict_rolls_back_everything():
    conn = fresh()
    s = T.get_turnaround_and_crew_state()
    batch = missed(s, "CDG")[:6]
    before = T.snapshot(conn)
    T.SIMULATE_SEAT_CONFLICT.update(enabled=True, at_item=4)
    r = T.execute_atomic_passenger_rebook(batch, "EK-075")
    assert r["status"] == "rolled_back" and r["items_done_before_failure"] == 3, r
    assert T.snapshot(conn) == before, "rollback must leave seats and bags exactly as they were"
    assert T.integrity(conn) == {"double_bookings": 0, "orphan_bags": 0}
    T.SIMULATE_SEAT_CONFLICT["enabled"] = False                      # same batch now succeeds
    assert T.execute_atomic_passenger_rebook(batch, "EK-075")["status"] == "committed"


# ---------- Tool 3: guards + human approval gate ----------
def test_departure_proposal_blocked_by_crew_and_maintenance():
    conn = fresh()
    r = T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:45")
    text = " ".join(r["violations"])
    assert r["status"] == "blocked" and "CREW DUTY" in text and "MAINTENANCE" in text, r
    assert conn.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0] == 0


def test_illegal_standby_crew_blocked():
    fresh()
    r = T.stage_controller_schedule_change("CREW_SWAP", "EK-813", crew_in="S-201,S-205,S-204,S-206")
    assert r["status"] == "blocked" and any("FO Ken Ito" in v for v in r["violations"]), r


def test_bad_gates_blocked():
    fresh()
    assert T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="C40")["status"] == "blocked"
    assert T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="A3")["status"] == "blocked"


def test_delay_needs_a_free_gate_first():
    fresh()
    r = T.stage_controller_schedule_change("DELAY", "EK-813", "11:45")
    assert r["status"] == "blocked" and "GATE_CHANGE first" in r["violations"][0], r


def test_delay_can_be_staged_after_a_pending_gate_change():
    conn = fresh()
    gate = T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B14")
    delay = T.stage_controller_schedule_change("DELAY", "EK-813", "11:45")
    assert delay["status"] == "pending_controller_approval", delay
    # The controller must approve in the right order
    assert T.approve(delay["approval_id"], "Controller Jane")["status"] == "waiting"
    assert T.approve(gate["approval_id"], "Controller Jane")["status"] == "approved"
    assert T.approve(delay["approval_id"], "Controller Jane")["status"] == "approved"
    assert conn.execute("SELECT est_dep FROM flights WHERE flight_id='EK-813'").fetchone()[0].endswith("11:45")


def test_same_change_is_not_queued_twice():
    conn = fresh()
    a = T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B14")
    b = T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B14")
    assert a["approval_id"] == b["approval_id"]
    assert conn.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0] == 1


def test_verified_facts_come_from_the_database():
    fresh()
    s = T.get_turnaround_and_crew_state()
    T.execute_atomic_passenger_rebook(missed(s, "LHR", vulnerable=True), "EK-005")
    T.stage_controller_schedule_change("CREW_SWAP", "EK-813", crew_in="S-201,S-203,S-204,S-206")
    f = T.verified_facts()
    assert f["rebooked"] == {"EK-005": {"passengers": 5, "vulnerable": 5}}, f["rebooked"]
    assert len(f["unresolved"]) == 40 and f["awaiting_approval"][0]["kind"] == "CREW_SWAP"
    assert f["open_mandatory_maintenance"] == ["M-1001"]


def test_nothing_changes_until_controller_approves():
    conn = fresh()
    r = T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B14")
    assert r["status"] == "pending_controller_approval", r
    assert conn.execute("SELECT gate FROM flights WHERE flight_id='EK-813'").fetchone()[0] == "B12"
    T.approve(r["approval_id"], "Controller Jane")
    assert conn.execute("SELECT gate FROM flights WHERE flight_id='EK-813'").fetchone()[0] == "B14"
    assert conn.execute("SELECT actor FROM audit_log WHERE action='gate_change_approved'").fetchone()[0] == "HUMAN"


def test_full_recovery_path_to_departure():
    conn = fresh()
    for kind, kw in [("CREW_SWAP", {"crew_in": "S-201,S-203,S-204,S-206"}),
                     ("GATE_CHANGE", {"new_time": "11:45", "gate": "B14"}),
                     ("DELAY", {"new_time": "11:45"})]:
        r = T.stage_controller_schedule_change(kind, "EK-813", **kw)
        assert r["status"] == "pending_controller_approval", (kind, r)
        assert T.approve(r["approval_id"], "Controller Jane")["status"] == "approved", kind
    r = T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:45")
    assert r["status"] == "blocked" and r["violations"] == [v for v in r["violations"] if "MAINTENANCE" in v], r
    T.engineer_sign_off("M-1001", "Eng. Omar Farouk")
    r = T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:45")
    assert r["status"] == "pending_controller_approval", r
    assert T.approve(r["approval_id"], "Controller Jane")["status"] == "approved"
    assert conn.execute("SELECT status FROM flights WHERE flight_id='EK-813'").fetchone()[0] == "RELEASED"


def test_approval_rechecks_guards():
    conn = fresh()
    for kind, kw in [("CREW_SWAP", {"crew_in": "S-201,S-203,S-204,S-206"}),
                     ("GATE_CHANGE", {"new_time": "11:45", "gate": "B14"})]:
        T.approve(T.stage_controller_schedule_change(kind, "EK-813", **kw)["approval_id"], "Controller Jane")
    T.engineer_sign_off("M-1001", "Eng. Omar Farouk")
    r = T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:45")
    assert r["status"] == "pending_controller_approval", r
    # Something changes after staging: a new mandatory defect is found before approval
    conn.execute("INSERT INTO maintenance_log VALUES ('M-1003','A6-EQX','Bird strike check',1,'OPEN',NULL,NULL)")
    a = T.approve(r["approval_id"], "Controller Jane")
    assert a["status"] == "refused_by_guard" and "M-1003" in a["violations"][0], a
    assert conn.execute("SELECT status FROM flights WHERE flight_id='EK-813'").fetchone()[0] != "RELEASED"


def test_reject_changes_nothing():
    conn = fresh()
    r = T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B14")
    T.reject(r["approval_id"], "Controller Jane", "prefer A-concourse")
    assert conn.execute("SELECT gate FROM flights WHERE flight_id='EK-813'").fetchone()[0] == "B12"


# ---------- Tool 4: notifications ----------
def test_notifications_use_real_facts():
    fresh()
    s = T.get_turnaround_and_crew_state()
    vul = missed(s, "LHR", vulnerable=True)
    T.execute_atomic_passenger_rebook(vul, "EK-005")
    waiting = missed(s, "JFK")[0]
    r = T.send_passenger_notification([vul[0], waiting], "Sorry! You are now on EK-999.")
    assert "rebooked on EK-005" in r["messages"][vul[0]] and "EK-999" not in r["messages"][vul[0]]
    assert "arranging your onward travel" in r["messages"][waiting]


if __name__ == "__main__":
    tests = [fn for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
