"""
AeroTurn - recovery options & passenger-compensation calculator.     [CODE, not AI]

Code prices every recovery option; the AI only compares and recommends; the human
Operations Controller chooses. All money maths is here, never in the LLM.

Compensation uses EU261-style bands as the airline's cost benchmark (INDICATIVE:
whether EU261/UK261 legally applies depends on carrier and route):
  arrival delay >= 3 h:  <=1500 km EUR 250 | <=3500 km EUR 400 | longer EUR 600
  rerouted, >3500 km, arrival delay <= 4 h: 50 % reduction
  care: meals EUR 25 if delay >= 2 h, hotel EUR 150 if overnight
"""
import json
from datetime import timedelta
from types import MappingProxyType

import db
import guards as g
import tools as T

DISTANCE_KM = MappingProxyType({"LHR": 5500, "JFK": 11000, "CDG": 5250, "MAN": 5700, "BOM": 1930,
                                "HKG": 5950, "SYD": 12040, "HAM": 4850})
MEALS_EUR, HOTEL_EUR = 25, 150
STANDBY_CALLOUT_EUR = 375          # per crew member called out
EK813_PAX, EK814_PAX = 310, 350    # EK-813 DXB-MAN load, and the return EK-814 MAN-DXB (knock-on)
CREW_REST_MIN = 12 * 60            # minimum rest before the inbound crew may fly again


def compensation(distance_km, arrival_delay_min, rerouted=False):
    """EU261-style compensation for ONE passenger (indicative)."""
    if arrival_delay_min < 180:
        return 0
    amount = 250 if distance_km <= 1500 else 400 if distance_km <= 3500 else 600
    if rerouted and distance_km > 3500 and arrival_delay_min <= 240:
        amount //= 2
    return amount


def care(arrival_delay_min, overnight=False):
    return (MEALS_EUR if arrival_delay_min >= 120 else 0) + (HOTEL_EUR if overnight else 0)


def passenger_recovery():
    """Cost of the CONNECTING passengers right now, versus doing nothing (everyone flies tomorrow)."""
    conn = db.connect(T.DB)
    arrival = db.to_time(conn.execute("SELECT est_arr FROM flights WHERE flight_id='EK-812'").fetchone()[0])
    rows = conn.execute(
        "SELECT p.final_destination AS dest, p.original_onward AS orig, o.est_dep AS orig_dep, o.sched_arr AS orig_arr, "
        "s.flight_id AS now_on, n.est_arr AS new_arr FROM passengers p "
        "JOIN flights o ON o.flight_id = p.original_onward "
        "JOIN seats s ON s.occupant = p.passenger_id JOIN flights n ON n.flight_id = s.flight_id").fetchall()
    now_cost = nothing_cost = rebooked = unresolved = 0
    for r in rows:
        if not g.check_connection(arrival, db.to_time(r["orig_dep"])):
            continue                                   # this passenger never missed their connection
        km = DISTANCE_KM[r["dest"]]
        tomorrow = compensation(km, 24 * 60) + care(24 * 60, overnight=True)
        nothing_cost += tomorrow
        if r["now_on"] != r["orig"]:                   # rebooked onto a later flight today
            delay = int((db.to_time(r["new_arr"]) - db.to_time(r["orig_arr"])).total_seconds() // 60)
            rebooked += 1
            now_cost += compensation(km, delay, rerouted=True) + care(delay)
        else:                                          # still waiting: flies tomorrow
            unresolved += 1
            now_cost += tomorrow
    return {"affected": rebooked + unresolved, "rebooked_same_day": rebooked, "unresolved_overnight": unresolved,
            "cost_now_eur": now_cost, "cost_if_nobody_rebooked_eur": nothing_cost,
            "saved_by_rebooking_eur": nothing_cost - now_cost}


def get_recovery_options() -> dict:
    """Get the recovery options for EK-813 (the late aircraft's next flight), each PRICED BY
    THE SYSTEM: passenger compensation (EU261-style), care (meals/hotels), crew call-out and
    network knock-on cost, plus what still blocks it. Also returns how much the passenger
    rebooking has saved. Compare the options and recommend one to the controller; you
    cannot choose it yourself."""
    conn = db.connect(T.DB)
    flight = T._flight(conn, "EK-813")
    sched = db.to_time(flight["sched_dep"])
    km = DISTANCE_KM["MAN"]
    match = T.find_legal_standby_crew("EK-813")
    maint_open = g.check_maintenance(T._maintenance(conn, flight["aircraft_reg"]))
    options = []

    # A: swap crew, move gate, short delay (needs controller approvals + engineer sign-off)
    sc = db.scenario(conn)
    state = T.get_turnaround_and_crew_state()
    crew_ok = all(c["legal_for_EK-813"] for c in state["crew_on_EK-813"])
    dep_a = db.to_time(f"{sc['now']:%Y-%m-%d} {match['projected_departure']}") if match["projected_departure"] else None
    if dep_a:
        dep_a = max(dep_a, sched)                       # never earlier than the timetable
        delay = int((dep_a - sched).total_seconds() // 60)
        actions = ([] if crew_ok else [f"CREW_SWAP {match['recommended_crew_in']}"]) + \
                  ([f"GATE_CHANGE ({state['outbound']['gate_problem'][0]})"] if state["outbound"]["gate_problem"] else []) + \
                  ([f"DELAY to {dep_a:%H:%M}"] if delay > 0 else []) + \
                  (["Engineer sign-off M-1001"] if maint_open else [])
        options.append({
            "id": "A", "title": (("Standby crew swap + " if not crew_ok else "") +
                                 (f"depart {dep_a:%H:%M}" if delay > 0 else "depart on time")),
            "actions": actions or ["No change needed"],
            "ek813_delay_min": delay,
            "compensation_eur": EK813_PAX * compensation(km, delay),
            "care_eur": EK813_PAX * care(delay),
            "operational_eur": STANDBY_CALLOUT_EUR * len(match["team"]),
            "knock_on": "None: return flight EK-814 keeps its slot",
            "feasible": match["status"] == "ok",
            "blockers": (["M-1001 sign-off still needed"] if maint_open else []) + (
                [] if match["status"] == "ok" else [f"No legal standby for {match['roles_without_legal_standby']}"])})

    # B: wait for the inbound crew to finish legal rest (only an option when the crew is the problem)
    duty_end = sc["now"] + timedelta(minutes=max(sc["crew_duty_left_min"], 0))
    dep_b = duty_end + timedelta(minutes=CREW_REST_MIN + 60)
    delay_b = int((dep_b - sched).total_seconds() // 60)
    if not crew_ok:
        options.append({
            "id": "B", "title": f"Wait for inbound crew rest, depart {dep_b:%H:%M}",
            "actions": [f"DELAY to {dep_b:%H:%M}", f"Meals for {EK813_PAX} passengers"] +
                       (["Engineer sign-off M-1001"] if maint_open else []),
            "ek813_delay_min": delay_b,
            "compensation_eur": EK813_PAX * compensation(km, delay_b),
            "care_eur": EK813_PAX * care(delay_b),
            "operational_eur": 0,
            "knock_on": f"EK-814 return delayed ~{delay_b // 60} h: {EK814_PAX} more passengers affected "
                        f"(EUR {EK814_PAX * compensation(km, delay_b):,})",
            "knock_on_eur": EK814_PAX * compensation(km, delay_b),
            "feasible": True, "blockers": ["M-1001 sign-off still needed"] if maint_open else []})

    # C: cancel and re-accommodate tomorrow
    options.append({
        "id": "C", "title": "Cancel EK-813, re-accommodate tomorrow",
        "actions": ["Cancel EK-813", "Hotels + rebooking for 310 passengers"],
        "ek813_delay_min": 24 * 60,
        "compensation_eur": EK813_PAX * compensation(km, 24 * 60),
        "care_eur": EK813_PAX * care(24 * 60, overnight=True),
        "operational_eur": 0,
        "knock_on": f"EK-814 cancelled: {EK814_PAX} more passengers affected (EUR {EK814_PAX * 600:,})",
        "knock_on_eur": EK814_PAX * 600,
        # Cancelling avoids today's departure, but the aircraft stays grounded until the inspection is signed off
        "feasible": True, "blockers": ["Aircraft A6-EQX stays grounded until M-1001 is signed off "
                                       "(no departure today, so nothing blocks the cancellation itself)"]
        if maint_open else []})

    for o in options:
        o["total_eur"] = o["compensation_eur"] + o["care_eur"] + o["operational_eur"] + o.get("knock_on_eur", 0)
    cheapest = min((o for o in options if o["feasible"]), key=lambda o: o["total_eur"])
    executing = T.approved_changes(conn)
    return {"currency": "EUR", "basis": "EU261-style bands, indicative", "options": options,
            "lowest_cost_feasible": cheapest["id"], "selected_by_controller": T.chosen_plan(conn),
            "option_a_in_execution": executing,
            "connecting_passengers": passenger_recovery()}


def select_recovery_plan(option_id, controller):
    """HUMAN action (console only): the Operations Controller chooses a recovery option.
    Code keeps the decision consistent with what has already happened:
      - option A steps already APPROVED  -> B or C is refused (A is being executed)
      - option A steps only PENDING      -> choosing B or C rejects them automatically"""
    conn = db.connect(T.DB)
    if option_id != "A":
        done = T.approved_changes(conn)
        if done:
            T.audit(conn, "CODE", "recovery_plan_refused", option_id,
                    f"option A already being executed: {', '.join(done)}")
            return {"status": "refused", "reason": f"Option A is already being executed (approved: {', '.join(done)}). "
                                                   f"Option {option_id} would contradict it."}
        superseded = []
        for r in conn.execute("SELECT approval_id, payload FROM pending_approvals WHERE status='PENDING'").fetchall():
            if json.loads(r["payload"])["flight_id"] == "EK-813":
                T._set_approval_status(r["approval_id"], "REJECTED", controller, f"superseded by option {option_id}")
                superseded.append(r["approval_id"])
    else:
        superseded = []
    T.audit(conn, "HUMAN", "recovery_plan_selected", option_id, f"chosen by {controller}")
    return {"status": "selected", "option": option_id, "superseded_approvals": superseded}
