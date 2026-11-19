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
                                "HKG": 5950, "SYD": 12040, "HAM": 4850, "DOH": 380})
MEALS_EUR, HOTEL_EUR = 25, 150
STANDBY_CALLOUT_EUR = 375          # per crew member called out
CREW_REST_MIN = 12 * 60            # minimum rest before the inbound crew may fly again

# Commercial data that a real deployment reads from the reservations system: passenger load
# of each outbound flight, and the return leg the same aircraft flies next (knock-on).
PAX_LOAD = MappingProxyType({"EK-813": 310})
RETURN_LEG = MappingProxyType({"EK-813": ("EK-814", 350)})
DEFAULT_PAX_LOAD = 300


def distance_km(dest):
    """Unknown routes are priced in the long-haul band (the highest compensation): never under-price."""
    return DISTANCE_KM.get(dest, 5000)


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


def passenger_recovery(inbound_flight):
    """Cost of the CONNECTING passengers of one late inbound flight right now, versus doing
    nothing (everyone flies tomorrow)."""
    conn = db.connect(T.DB)
    arrival = db.to_time(conn.execute("SELECT est_arr FROM flights WHERE flight_id=?",
                                      (inbound_flight,)).fetchone()[0])
    rows = conn.execute(
        "SELECT p.final_destination AS dest, p.original_onward AS orig, o.est_dep AS orig_dep, o.sched_arr AS orig_arr, "
        "s.flight_id AS now_on, n.est_arr AS new_arr FROM passengers p "
        "JOIN flights o ON o.flight_id = p.original_onward "
        "JOIN seats s ON s.occupant = p.passenger_id JOIN flights n ON n.flight_id = s.flight_id "
        "WHERE p.inbound_flight = ?", (inbound_flight,)).fetchall()
    now_cost = nothing_cost = rebooked = unresolved = 0
    for r in rows:
        if not g.check_connection(arrival, db.to_time(r["orig_dep"])):
            continue                                   # this passenger never missed their connection
        km = distance_km(r["dest"])
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


def get_recovery_options(flight_id: str) -> dict:
    """Get the recovery options for an outbound flight delayed by its late inbound aircraft
    (outbound.flight in the state, e.g. 'EK-813'), each PRICED BY THE SYSTEM: passenger
    compensation (EU261-style), care (meals/hotels), crew call-out and network knock-on cost,
    plus what still blocks it. Also returns how much the passenger rebooking has saved.
    Compare the options and recommend one to the controller; you cannot choose it yourself."""
    conn = db.connect(T.DB)
    flight = T._flight(conn, flight_id)
    inbound = T._inbound_for(conn, flight) if flight else None
    if not inbound:
        return {"status": "error", "error": f"{flight_id} is not a departure operated by a late inbound aircraft"}
    sched = db.to_time(flight["sched_dep"])
    km = distance_km(flight["destination"])
    pax = PAX_LOAD.get(flight_id, DEFAULT_PAX_LOAD)
    ret_id, ret_pax = RETURN_LEG.get(flight_id, ("the return flight", pax))
    match = T.find_legal_standby_crew(flight_id)
    state = T.get_turnaround_and_crew_state(inbound["flight_id"])
    open_items = [i["item_id"] for i in T._maintenance(conn, flight["aircraft_reg"])
                  if i["mandatory"] and i["status"] != "SIGNED_OFF"]
    signoffs = [f"Engineer sign-off {i}" for i in open_items]
    maint_blockers = [f"{i} sign-off still needed" for i in open_items]
    gate_problem = state["outbound"]["gate_problem"]
    crew_ok = all(c["legal_for_outbound"] for c in state["crew_on_outbound"])
    # Steps of option A a controller has ALREADY approved still belong to the plan and keep their
    # cost (the standby crew were called out when the swap was approved): count them, don't drop them.
    approved = {}
    for r in conn.execute("SELECT kind, payload FROM pending_approvals WHERE status='APPROVED' ORDER BY approval_id"):
        p = json.loads(r["payload"])
        if p.get("flight_id") == flight_id:
            approved[r["kind"]] = p
    called_out = [c.strip() for c in approved.get("CREW_SWAP", {}).get("crew_in", "").split(",") if c.strip()]
    options = []

    # A: swap crew (if needed), move gate (if needed), short delay (controller approvals + sign-offs)
    dep_a = db.to_time(f"{T._now():%Y-%m-%d} {match['projected_departure']}") if match["projected_departure"] else None
    if dep_a:
        dep_a = max(dep_a, sched)                       # never earlier than the timetable
        delay = int((dep_a - sched).total_seconds() // 60)
        swap = "CREW_SWAP" in approved or not crew_ok
        gate = "GATE_CHANGE" in approved or bool(gate_problem)
        steps = ([f"CREW_SWAP {approved['CREW_SWAP']['crew_in']} (approved)"] if "CREW_SWAP" in approved else
                 [] if crew_ok else [f"CREW_SWAP {match['recommended_crew_in']}"]) + \
                ([f"GATE_CHANGE to {approved['GATE_CHANGE']['gate']} (approved)"] if "GATE_CHANGE" in approved else
                 [f"GATE_CHANGE ({gate_problem[0]})"] if gate_problem else []) + \
                ([f"DELAY to {dep_a:%H:%M}" + (" (approved)" if "DELAY" in approved else "")] if delay > 0 else [])
        options.append({
            "id": "A", "title": " + ".join((["Standby crew swap"] if swap else []) +
                                           (["gate change"] if gate else []) +
                                           [f"depart {dep_a:%H:%M}" if delay > 0 else "depart on time"]),
            "actions": (steps + signoffs) or ["No change needed"],
            "delay_min": delay,
            "compensation_eur": pax * compensation(km, delay),
            "care_eur": pax * care(delay),
            "operational_eur": STANDBY_CALLOUT_EUR * (len(called_out) + (0 if crew_ok else len(match["team"]))),
            "knock_on": f"None: {ret_id} keeps its slot",
            "feasible": match["status"] == "ok",
            "blockers": maint_blockers + (
                [] if match["status"] == "ok" else [f"No legal standby for {match['roles_without_legal_standby']}"])})

    # B: keep the current crew and wait until they have rested (only if the crew is the problem)
    if not crew_ok:
        current = T._crew_on(conn, flight_id)
        left = min((g.duty_minutes_left(c["duty_start"], T._now(), T._now()) for c in current if c["duty_start"]), default=0)
        duty_end = T._now() + timedelta(minutes=max(left, 0))
        dep_b = duty_end + timedelta(minutes=CREW_REST_MIN + 60)
        delay_b = int((dep_b - sched).total_seconds() // 60)
        options.append({
            "id": "B", "title": f"Wait for current crew rest, depart {dep_b:%H:%M}",
            "actions": [f"DELAY to {dep_b:%H:%M}", f"Meals for {pax} passengers"] + signoffs,
            "delay_min": delay_b,
            "compensation_eur": pax * compensation(km, delay_b),
            "care_eur": pax * care(delay_b),
            "operational_eur": 0,
            "knock_on": f"{ret_id} delayed ~{delay_b // 60} h: {ret_pax} more passengers affected "
                        f"(EUR {ret_pax * compensation(km, delay_b):,})",
            "knock_on_eur": ret_pax * compensation(km, delay_b),
            "feasible": True, "blockers": maint_blockers})

    # C: cancel and re-accommodate tomorrow
    options.append({
        "id": "C", "title": f"Cancel {flight_id}, re-accommodate tomorrow",
        "actions": [f"Cancel {flight_id}", f"Hotels + rebooking for {pax} passengers"],
        "delay_min": 24 * 60,
        "compensation_eur": pax * compensation(km, 24 * 60),
        "care_eur": pax * care(24 * 60, overnight=True),
        "operational_eur": 0,
        "knock_on": f"{ret_id} cancelled: {ret_pax} more passengers affected "
                    f"(EUR {ret_pax * compensation(km, 24 * 60):,})",
        "knock_on_eur": ret_pax * compensation(km, 24 * 60),
        # Cancelling avoids today's departure, but the aircraft stays grounded until the inspection is signed off
        "feasible": True,
        "blockers": [f"Aircraft {flight['aircraft_reg']} stays grounded until {', '.join(open_items)} is signed off "
                     f"(no departure today, so nothing blocks the cancellation itself)"] if open_items else []})

    for o in options:
        o["total_eur"] = o["compensation_eur"] + o["care_eur"] + o["operational_eur"] + o.get("knock_on_eur", 0)
    cheapest = min((o for o in options if o["feasible"]), key=lambda o: o["total_eur"])
    return {"flight": flight_id, "currency": "EUR", "basis": "EU261-style bands, indicative", "options": options,
            "lowest_cost_feasible": cheapest["id"], "selected_by_controller": T.chosen_plan(conn, flight_id),
            "option_a_in_execution": T.approved_changes(conn, flight_id),
            "connecting_passengers": passenger_recovery(inbound["flight_id"])}


def select_recovery_plan(flight_id, option_id, controller):
    """HUMAN action (console only): the Operations Controller chooses a recovery option.
    Code keeps the decision consistent with what has already happened:
      - option A steps already APPROVED  -> B or C is refused (A is being executed)
      - option A steps only PENDING      -> choosing B or C rejects them automatically"""
    conn = db.connect(T.DB)
    superseded = []
    if option_id != "A":
        done = T.approved_changes(conn, flight_id)
        if done:
            T.audit(conn, "CODE", "recovery_plan_refused", {"flight_id": flight_id, "option": option_id},
                    f"option A already being executed: {', '.join(done)}")
            return {"status": "refused", "reason": f"Option A is already being executed for {flight_id} "
                                                   f"(approved: {', '.join(done)}). Option {option_id} would contradict it."}
        for r in conn.execute("SELECT approval_id, payload FROM pending_approvals WHERE status='PENDING'").fetchall():
            if json.loads(r["payload"])["flight_id"] == flight_id:
                T._set_approval_status(r["approval_id"], "REJECTED", controller, f"superseded by option {option_id}")
                superseded.append(r["approval_id"])
    T.audit(conn, "HUMAN", "recovery_plan_selected", {"flight_id": flight_id, "option": option_id},
            f"chosen by {controller}")
    return {"status": "selected", "flight": flight_id, "option": option_id, "superseded_approvals": superseded}
