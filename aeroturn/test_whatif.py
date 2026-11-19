"""
AeroTurn - what-if scenario tests: the same rules and tools must adapt to a different
delay, crew situation, maintenance state and seat supply. No AI involved (free, instant).

  python test_whatif.py      # or: pytest test_whatif.py
"""
import tempfile
from pathlib import Path

import costs
import db
import tools as T


def build(**settings):
    T.DB = Path(tempfile.mkdtemp()) / "whatif.sqlite"
    db.build(T.DB, **settings)
    return T.get_turnaround_and_crew_state()


def test_default_is_the_brief():
    s = build()
    assert s["inbound"]["actual_arrival"] == "10:15" and s["inbound"]["delay_min"] == 75
    assert s["missed_connections"]["count"] == 45 and s["missed_connections"]["vulnerable"] == 10
    assert db.scenario(db.connect(T.DB))["now"] == db.NOW


def test_short_delay_breaks_no_connections():
    s = build(delay_min=30)
    assert s["inbound"]["actual_arrival"] == "09:30"
    assert s["missed_connections"]["count"] == 0
    assert s["outbound"]["earliest_legal_departure"] == "11:00" and not s["outbound"]["gate_problem"]


def test_45_minutes_breaks_only_new_york():
    s = build(delay_min=45)
    assert set(s["rebooking_options"]) == {"JFK"} and s["missed_connections"]["count"] == 12


def test_long_delay_breaks_mumbai_and_offers_a_new_flight():
    s = build(delay_min=150)
    assert "BOM" in s["rebooking_options"]
    assert any(o["flight_id"] == "EK-513" and o["bookable"] for o in s["rebooking_options"]["BOM"])
    assert s["outbound"]["earliest_legal_departure"] == "13:00"
    ek003 = [o for o in s["rebooking_options"]["LHR"] if o["flight_id"] == "EK-003"][0]
    assert "already departed" in ek003["note"]


def test_crew_duty_setting_drives_the_crew_guard():
    tired = build(crew_duty_left_min=20)
    assert not any(c["legal_for_EK-813"] for c in tired["crew_on_EK-813"])
    rested = build(delay_min=30, crew_duty_left_min=600)
    assert all(c["legal_for_EK-813"] for c in rested["crew_on_EK-813"])
    assert T.find_legal_standby_crew("EK-813")["replace"] == []


def test_departure_guard_uses_the_scenario_clock():
    build(delay_min=30, crew_duty_left_min=600, maintenance_open=False)
    assert T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:00")["status"] == "pending_controller_approval"
    late = T.stage_controller_schedule_change("DEPARTURE", "EK-813", "11:45")   # rested crew runs out by then
    assert late["status"] == "blocked" and "CREW DUTY" in late["violations"][0]


def test_maintenance_setting():
    build(maintenance_open=False)
    assert T.verified_facts()["open_mandatory_maintenance"] == []


def test_seat_factor_changes_supply():
    s = build(seats_factor=0.5)
    ek005 = [o for o in s["rebooking_options"]["LHR"] if o["flight_id"] == "EK-005"][0]
    assert ek005["free_seats"] == 4


def test_costs_adapt():
    build(delay_min=30, crew_duty_left_min=600, maintenance_open=False)
    opts = costs.get_recovery_options()
    assert [o["id"] for o in opts["options"]] == ["A", "C"], "no 'wait for crew rest' when the crew is legal"
    assert opts["options"][0]["total_eur"] == 0 and opts["options"][0]["ek813_delay_min"] == 0


def test_rollback_and_integrity_hold_in_any_scenario():
    s = build(delay_min=150, seats_factor=0.5)
    conn = db.connect(T.DB)
    before = T.snapshot(conn)
    T.SIMULATE_SEAT_CONFLICT.update(enabled=True, at_item=2)
    bom = [p["passenger_id"] for p in s["missed_connections"]["passengers"] if p["destination"] == "BOM"][:3]
    r = T.execute_atomic_passenger_rebook(bom, "EK-513")
    T.SIMULATE_SEAT_CONFLICT["enabled"] = False
    assert r["status"] == "rolled_back" and T.snapshot(conn) == before
    assert T.integrity(conn) == {"double_bookings": 0, "orphan_bags": 0}


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
