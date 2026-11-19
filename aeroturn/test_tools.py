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
    T.SIMULATE_SEAT_CONFLICT["enabled"] = T.SIMULATE_BAG_CONFLICT["enabled"] = False
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
    assert s["outbound"]["flight"] == "EK-813", "next flight found from the aircraft registration"
    assert all(c["duty_min_left_now"] == 20 and not c["legal_for_outbound"] for c in s["crew_on_outbound"])
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


# ---------- Plan consistency: recovery option vs approvals ----------
def test_cannot_choose_b_after_option_a_steps_are_approved():
    conn = fresh()
    import costs
    gate = T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B14")
    T.approve(gate["approval_id"], "Controller Jane")
    r = costs.select_recovery_plan("EK-813", "B", "Controller Jane")
    assert r["status"] == "refused" and "#1 GATE_CHANGE" in r["reason"], r
    assert T.chosen_plan(conn, "EK-813") is None


def test_choosing_c_rejects_pending_a_steps_and_locks_new_ones():
    conn = fresh()
    import costs
    T.stage_controller_schedule_change("CREW_SWAP", "EK-813", crew_in="S-201,S-203,S-204,S-206")
    r = costs.select_recovery_plan("EK-813", "C", "Controller Jane")
    assert r["status"] == "selected" and r["superseded_approvals"] == [1], r
    assert conn.execute("SELECT status FROM pending_approvals WHERE approval_id=1").fetchone()[0] == "REJECTED"
    blocked = T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B14")
    assert blocked["status"] == "blocked" and "PLAN" in blocked["violations"][0], blocked


def test_choosing_a_keeps_everything_allowed():
    fresh()
    import costs
    assert costs.select_recovery_plan("EK-813", "A", "Controller Jane")["status"] == "selected"
    assert T.stage_controller_schedule_change("GATE_CHANGE", "EK-813", "11:45", gate="B14")["status"] == \
        "pending_controller_approval"


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


def test_ai_note_cannot_state_facts():
    """Found in a live run: the AI's note 'Your new flight details are confirmed.' went to every
    passenger in the call, including those with no new flight. Notes may not state facts."""
    fresh()
    s = T.get_turnaround_and_crew_state()
    vul = missed(s, "LHR", vulnerable=True)
    T.execute_atomic_passenger_rebook(vul, "EK-005")
    waiting = missed(s, "JFK")[0]
    live_note = "We apologise for the disruption. Your new flight details are confirmed."
    r = T.send_passenger_notification([vul[0], waiting], live_note)
    assert "confirmed" not in r["messages"][waiting] and "flight" in r["note_warning"], r
    for bad in ("A hotel voucher is waiting for you.", "Boarding starts soon.", "See you at 14:30!", "x" * 250):
        assert "note_warning" in T.send_passenger_notification([waiting], bad), bad
    ok = T.send_passenger_notification([waiting], "We are very sorry for the disruption.")
    assert ok["messages"][waiting].endswith("We are very sorry for the disruption.") and "note_warning" not in ok


def test_midway_bag_transfer_failure_rolls_back_everything():
    """The baggage step silently fails (no exception): the integrity check must catch it."""
    for held in (False, True):              # bags in transit, and bags already held at the hub
        conn = fresh()
        if held:
            T.hold_missed_bags()
        batch = missed(T.get_turnaround_and_crew_state(), "CDG")[:6]
        before = T.snapshot(conn)
        T.SIMULATE_BAG_CONFLICT.update(enabled=True, at_item=3)
        r = T.execute_atomic_passenger_rebook(batch, "EK-075")
        T.SIMULATE_BAG_CONFLICT["enabled"] = False
        assert r["status"] == "rolled_back" and "orphan_bags" in r["reason"], (held, r)
        assert T.snapshot(conn) == before, "rollback must leave seats and bags exactly as they were"
        assert T.integrity(conn) == {"double_bookings": 0, "orphan_bags": 0}
        assert T.execute_atomic_passenger_rebook(batch, "EK-075")["status"] == "committed"


def test_option_a_keeps_its_cost_while_being_executed():
    """Found in the console: after approving the crew swap, option A showed EUR 0 and lost
    'crew swap + gate change' from its title. Approved steps are still part of the plan."""
    import costs
    fresh()
    a = lambda: next(o for o in costs.get_recovery_options("EK-813")["options"] if o["id"] == "A")
    before = a()
    assert before["total_eur"] == 1500 and before["title"] == "Standby crew swap + gate change + depart 11:45"
    for kind, kw in [("CREW_SWAP", {"crew_in": "S-201,S-203,S-204,S-206"}),
                     ("GATE_CHANGE", {"new_time": "11:45", "gate": "B14"}),
                     ("DELAY", {"new_time": "11:45"})]:
        T.approve(T.stage_controller_schedule_change(kind, "EK-813", **kw)["approval_id"], "Controller Jane")
        now = a()
        assert (now["total_eur"], now["title"]) == (1500, before["title"]), (kind, now)
    assert all(s.endswith("(approved)") for s in now["actions"] if not s.startswith("Engineer")), now["actions"]


def test_realistic_extra_recovery_options():
    """D aircraft swap, E cancel but rebook today where possible, F fallback crew further away."""
    import costs
    fresh()
    opts = {o["id"]: o for o in costs.get_recovery_options("EK-813")["options"]}
    assert set(opts) == {"A", "B", "C", "D", "E", "F"}, set(opts)
    d, e, f = opts["D"], opts["E"], opts["F"]
    # D: spare A6-EQS, no maintenance sign-off or gate change needed for EK-813, costs more than A
    assert "A6-EQS" in d["title"] and d["blockers"] == [] and d["delay_min"] == 30
    assert d["total_eur"] == costs.AIRCRAFT_SWAP_EUR + 4 * costs.STANDBY_CALLOUT_EUR
    # E: 45 on EK-017 + 60 via the partner today, 205 tomorrow; cheaper than cancelling everyone (C)
    assert "105 passengers today, 205 tomorrow" in e["title"] and e["total_eur"] < opts["C"]["total_eur"]
    # F: next legal crew (Capt. Grace Lee from the hotel), later than A, never the 'lowest cost' over A
    assert "Capt. Grace Lee" in f["title"] and f["delay_min"] > opts["A"]["delay_min"]
    assert costs.get_recovery_options("EK-813")["lowest_cost_feasible"] == "A"


def test_choosing_the_same_option_twice_records_it_once():
    import costs
    conn = fresh()
    count = lambda: conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='recovery_plan_selected'").fetchone()[0]
    assert costs.select_recovery_plan("EK-813", "A", "Controller Jane")["status"] == "selected"
    assert costs.select_recovery_plan("EK-813", "A", "Controller Jane")["status"] == "already_selected"
    assert count() == 1 and costs.get_recovery_options("EK-813")["selected_by_controller"] == "A"
    assert costs.select_recovery_plan("EK-813", "C", "Controller Jane")["status"] == "selected"   # change of mind
    assert count() == 2 and costs.get_recovery_options("EK-813")["selected_by_controller"] == "C"


def test_controller_can_stop_the_agent_between_steps():
    """Console 'Stop agent': the run ends after the current step and the rest goes to human review.
    No AI involved: a fake model does one step and the controller presses Stop during it."""
    import agent
    fresh()
    calls = []

    def fake_model(messages, chain, state):
        calls.append(1)
        agent.STOP.set()                    # the controller presses Stop while this step runs
        return ({"role": "assistant", "content": "", "tool_calls": [{"id": "x1", "type": "function", "function": {
            "name": "get_turnaround_and_crew_state", "arguments": '{"inbound_flight": "EK-812"}'}}]},
            {"name": "fake model"})

    real_model, real_screen = agent.call_with_fallback, agent.screen_input
    agent.call_with_fallback, agent.screen_input = fake_model, (lambda m: [])
    try:
        out = agent.run_agent("EK-812 landed late", local_only=True, inbound_flight="EK-812")
    finally:
        agent.call_with_fallback, agent.screen_input = real_model, real_screen
        agent.STOP.clear()
    assert len(calls) == 1, "no further AI step after Stop"
    assert "stopped by the Operations Controller" in out and "HUMAN REVIEW" in out, out
    assert [e["tool"] for e in agent.ACTION_LOG] == ["get_turnaround_and_crew_state"], "the current step finished"


# ---------- Bags of passengers who missed their connection ----------
def test_missed_connection_bags_are_held_until_rebooked():
    conn = fresh()
    s = T.get_turnaround_and_crew_state()
    missed_ids = missed(s)
    bag_count = lambda where, *a: conn.execute(f"SELECT COUNT(*) FROM bags WHERE {where}", a).fetchone()[0]
    expected = sum(bag_count("passenger_id=?", pid) for pid in missed_ids)

    r = T.hold_missed_bags()
    assert r["held"] == expected and bag_count("status='HELD'") == expected, r
    assert bag_count("status='HELD' AND routed_to IS NOT NULL") == 0, "held bags are routed to no flight"
    assert T.hold_missed_bags()["held"] == 0, "idempotent: nothing new to hold"
    assert T.integrity(conn) == {"double_bookings": 0, "orphan_bags": 0}

    vul = missed(s, "LHR", vulnerable=True)                       # rebooking releases their bags
    T.execute_atomic_passenger_rebook(vul, "EK-005")
    for pid in vul:
        assert {tuple(b) for b in conn.execute("SELECT routed_to, status FROM bags WHERE passenger_id=?", (pid,))} \
            == {("EK-005", "IN_TRANSFER")}
    assert T.verified_facts()["bags_held"] == expected - sum(bag_count("passenger_id=?", pid) for pid in vul)

    waiting = missed(s, "JFK")[0]                                 # unresolved: told their bags are safe
    msg = T.send_passenger_notification([waiting], "")["messages"][waiting]
    assert "bags are held safely in DXB" in msg, msg
    assert T.integrity(conn) == {"double_bookings": 0, "orphan_bags": 0}


# ---------- Any flight, not just EK-812: a second, different disruption at the same time ----------
def add_second_disruption(conn):
    """EK-433 from Singapore lands 70 min late; its aircraft A6-ENX next flies EK-441 to Mumbai.
    Unlike EK-812: the crew is legal, the gate is free, no open maintenance - only a delay is needed."""
    t = db.t
    conn.executemany("INSERT INTO flights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("EK-433", "SIN", "DXB", "A6-ENX", "B777-300ER", "WIDE", t("02:00"), t("03:10"), t("09:30"), t("10:40"),
         450, "C12", "LANDED"),
        ("EK-441", "DXB", "BOM", "A6-ENX", "B777-300ER", "WIDE", t("11:30"), t("11:30"), t("15:55"), t("15:55"),
         185, "C12", "SCHEDULED")])
    conn.execute("INSERT INTO gates VALUES ('C12','C',1)")
    conn.execute("INSERT INTO gate_bookings VALUES ('C12','EK-441',?,?)", (t("10:00"), t("11:30")))
    for i, tier in enumerate(["WCHR", "STANDARD", "STANDARD"]):   # 3 miss EK-073 to Paris (dep 11:00)
        pid = f"Q{i + 1:03d}"
        conn.execute("INSERT INTO passengers VALUES (?,?,?,?,?,?,?)",
                     (pid, f"Second Pax {i}", "EK-433", "EK-073", "CDG", tier, db.NEEDS[tier]))
        conn.execute("INSERT INTO seats VALUES ('EK-073',?,?)", (f"9{i}A", pid))
        conn.execute("INSERT INTO bags VALUES (?,?,'EK-073','IN_TRANSFER')", (f"BAG-Q{i}", pid))
    for i, role in enumerate(["Captain", "First Officer", "Purser", "Cabin Crew"]):
        conn.execute("INSERT INTO crew VALUES (?,?,?,'INBOUND','EK-441','Aircraft A6-ENX',0,?)",
                     (f"C-4{i}", f"{role} Two", role, "2026-11-20 08:00"))


def test_any_late_flight_not_just_ek812():
    conn = fresh()
    add_second_disruption(conn)
    late = {d["inbound"]: d for d in T.disruptions()}
    assert set(late) == {"EK-812", "EK-433"} and late["EK-433"]["outbound"] == "EK-441", late

    s = T.get_turnaround_and_crew_state("EK-433")
    assert s["outbound"]["flight"] == "EK-441" and s["inbound"]["delay_min"] == 70
    assert s["missed_connections"]["count"] == 3 and s["missed_connections"]["vulnerable"] == 1
    assert all(c["legal_for_outbound"] for c in s["crew_on_outbound"]), "fresh crew: no swap needed"
    assert not s["outbound"]["gate_problem"]
    assert s["outbound"]["earliest_legal_departure"] == "12:10", "10:40 landing + 90 min turnaround"
    assert T.get_turnaround_and_crew_state("EK-812")["missed_connections"]["count"] == 45, "disruptions kept apart"
    assert T.get_turnaround_and_crew_state()["status"] == "error", "two disruptions: must say which one"

    # Rebooking uses EK-433's own landing time; guards still apply (vulnerable first)
    r = T.execute_atomic_passenger_rebook(missed(s, "CDG"), "EK-075")
    assert r["status"] == "committed" and len(r["rebooked"]) == 3, r
    assert T.integrity(conn) == {"double_bookings": 0, "orphan_bags": 0}
    note = T.send_passenger_notification(["Q001"], "")["messages"]["Q001"]
    assert note.startswith("EK-433 landed late") and "EK-075" in note, note

    # Only the delay is needed for EK-441, and it is checked against EK-441's own turnaround
    assert T.stage_controller_schedule_change("DELAY", "EK-441", "11:50")["status"] == "blocked"
    assert T.stage_controller_schedule_change("DELAY", "EK-441", "12:10")["status"] == "pending_controller_approval"

    import agent, costs
    assert agent.pick_disruption("EK-433 landed late")[0]["inbound"] == "EK-433"
    assert agent.pick_disruption("two flights are late")[0] is None, "ambiguous: code must not guess"
    todo = agent.remaining_work("EK-433")
    assert not any("CREW_SWAP" in x or "GATE_CHANGE" in x or "DELAY" in x for x in todo), todo
    opts = costs.get_recovery_options("EK-441")
    ids = [o["id"] for o in opts["options"]]
    assert "B" not in ids and "F" not in ids, f"crew is legal: no crew-rest or fallback-crew option ({ids})"
    assert "crew swap" not in opts["options"][0]["title"].lower()


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
