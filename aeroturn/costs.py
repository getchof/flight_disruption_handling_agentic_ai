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
import turnaround

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

# Operational data a real deployment reads from fleet and partner systems.
# Option D: a serviceable spare aircraft parked at the hub (no flights today), ready after preparation.
SPARE_AIRCRAFT = ({"reg": "A6-EQS", "type": "B777-300ER", "body": "WIDE", "stand": "B18", "prep_min": 45},)
AIRCRAFT_SWAP_EUR = 4000           # towing, catering/fuel on the other aircraft, ground handling
# Option E: seats a partner airline can offer today (arrival time at the destination, ticket cost).
PARTNER_SEATS = MappingProxyType({"MAN": ({"via": "partner airline via London", "seats": 60, "arrives": "20:30",
                                           "ticket_eur": 350},)})


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


def _backup_crew(conn, flight, match):
    """Option F: for each role the matcher fills, the NEXT legal standby crew member (further away).
    Returns (departure, team, names of the people who differ from the best team) or None."""
    standby = [{**dict(r), "duty_start": db.to_time(r["duty_start"]) if r["duty_start"] else None}
               for r in conn.execute("SELECT * FROM crew WHERE status='STANDBY' ORDER BY minutes_away, crew_id")]
    floor = T._floor_departure(conn, flight)
    team, changed = [], []
    for pick in match["team"]:
        legal = [s for s in standby if s["role"] == pick["role"]
                 and not g.check_crew_legal([s], floor, flight["block_min"], T._now())]
        others = [s for s in legal if s["crew_id"] != pick["crew_id"]]
        chosen = others[0] if others else next(s for s in legal if s["crew_id"] == pick["crew_id"])
        team.append(chosen)
        if others:
            changed.append(chosen["name"])
    if not changed:
        return None
    kept = [c for c in T._crew_on(conn, flight["flight_id"]) if c["crew_id"] not in {p["replaces"] for p in match["team"]}]
    minutes = max(c["minutes_away"] for c in team + kept)
    signed = not g.check_maintenance(T._maintenance(conn, flight["aircraft_reg"]))
    cpm = turnaround.plan(T._inbound_arrival(conn, flight), minutes, signed)["departure"]
    dep = max(floor, cpm, db.to_time(flight["sched_dep"]))
    if g.check_crew_legal(team + kept, dep, flight["block_min"], T._now()) or g.check_crew_reachable(team, dep, T._now()):
        return None
    return dep, team, changed


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

    # D: aircraft swap - fly the outbound with a ready spare aircraft; the late aircraft stays for its inspection
    spare = next((a for a in SPARE_AIRCRAFT if a["body"] == flight["body"]), None)
    if spare and (open_items or not crew_ok or (dep_a and dep_a > sched)):
        crew_ready = T._now() if crew_ok else (
            db.to_time(f"{T._now():%Y-%m-%d} {match['team_ready_at']}") if match["team_ready_at"] else None)
        if crew_ready:
            boarding = timedelta(minutes=g.LIMITS.BOARDING_MIN)
            dep_d = max(sched, T._now() + timedelta(minutes=spare["prep_min"]) + boarding, crew_ready + boarding)
            delay_d = int((dep_d - sched).total_seconds() // 60)
            options.append({
                "id": "D", "title": f"Swap aircraft to spare {spare['reg']}" + ("" if crew_ok else " + standby crew") +
                                    f", depart {dep_d:%H:%M}",
                "actions": [f"AIRCRAFT SWAP: {flight_id} on {spare['reg']} ({spare['type']}, stand {spare['stand']})"] +
                           ([] if crew_ok else [f"CREW_SWAP {match['recommended_crew_in']}"]) +
                           ([f"DELAY to {dep_d:%H:%M}"] if delay_d > 0 else []),
                "delay_min": delay_d,
                "compensation_eur": pax * compensation(km, delay_d),
                "care_eur": pax * care(delay_d),
                "operational_eur": AIRCRAFT_SWAP_EUR + (0 if crew_ok else STANDBY_CALLOUT_EUR * len(match["team"])),
                "knock_on": f"None: {ret_id} flies on {spare['reg']}; {flight['aircraft_reg']} stays for "
                            f"{', '.join(open_items) or 'its turnaround'} and becomes the spare",
                "feasible": crew_ok or match["status"] == "ok",
                # The spare has no open item and its own stand: no sign-off or gate change needed for this flight
                "blockers": []})

    # E: cancel, but move as many of the flight's passengers as possible onto other flights TODAY
    today = []
    for r in conn.execute(
            "SELECT f.flight_id, f.est_arr, SUM(s.occupant IS NULL) AS free FROM flights f JOIN seats s "
            "ON s.flight_id = f.flight_id WHERE f.origin=? AND f.destination=? AND f.flight_id<>? AND f.est_dep>? "
            "AND substr(f.est_dep, 1, 10) = substr(?, 1, 10) GROUP BY f.flight_id ORDER BY f.est_dep",
            (db.HUB, flight["destination"], flight_id, flight["sched_dep"], flight["sched_dep"])):
        if r["free"]:
            today.append({"name": r["flight_id"], "seats": r["free"], "arrives": db.to_time(r["est_arr"]), "ticket": 0})
    for p in PARTNER_SEATS.get(flight["destination"], ()):
        today.append({"name": p["via"], "seats": p["seats"], "ticket": p["ticket_eur"],
                      "arrives": db.to_time(f"{T._now():%Y-%m-%d} {p['arrives']}")})
    if today:
        left, comp_e, care_e, tickets, moved = pax, 0, 0, 0, []
        sched_arr = db.to_time(flight["sched_arr"])
        for alt in today:
            n = min(alt["seats"], left)
            if not n:
                break
            late = int((alt["arrives"] - sched_arr).total_seconds() // 60)
            comp_e += n * compensation(km, late, rerouted=True)
            care_e += n * care(late)
            tickets += n * alt["ticket"]
            left -= n
            moved.append(f"{n} on {alt['name']} (arrives {alt['arrives']:%H:%M})")
        comp_e += left * compensation(km, 24 * 60)
        care_e += left * care(24 * 60, overnight=True)
        options.append({
            "id": "E", "title": f"Cancel {flight_id}, rebook {pax - left} passengers today, {left} tomorrow",
            "actions": [f"Cancel {flight_id}"] + [f"Rebook {m}" for m in moved] +
                       ([f"Hotels + rebooking tomorrow for {left} passengers"] if left else []),
            "delay_min": 24 * 60,
            "compensation_eur": comp_e,
            "care_eur": care_e,
            "operational_eur": tickets,
            "knock_on": f"{ret_id} cancelled: {ret_pax} more passengers affected "
                        f"(EUR {ret_pax * compensation(km, 24 * 60):,})",
            "knock_on_eur": ret_pax * compensation(km, 24 * 60),
            "feasible": True,
            "blockers": [f"Aircraft {flight['aircraft_reg']} stays grounded until {', '.join(open_items)} is signed off "
                         f"(no departure today, so nothing blocks the cancellation itself)"] if open_items else []})

    # F: fallback if the closest standby crew cannot be used - the next legal crew, further away
    backup = None if crew_ok or match["status"] != "ok" else _backup_crew(conn, flight, match)
    if backup:
        dep_f, team_f, changed = backup
        delay_f = int((dep_f - sched).total_seconds() // 60)
        options.append({
            "id": "F", "title": f"Fallback crew ({', '.join(changed)}) + " + ("gate change + " if gate_problem else "") +
                                f"depart {dep_f:%H:%M}",
            "actions": [f"CREW_SWAP {','.join(c['crew_id'] for c in team_f)}"] +
                       ([f"GATE_CHANGE ({gate_problem[0]})"] if gate_problem else []) +
                       ([f"DELAY to {dep_f:%H:%M}"] if delay_f > 0 else []) + signoffs,
            "delay_min": delay_f,
            "compensation_eur": pax * compensation(km, delay_f),
            "care_eur": pax * care(delay_f),
            "operational_eur": STANDBY_CALLOUT_EUR * len(team_f),
            "knock_on": f"None: {ret_id} keeps its slot. Use only if the closest standby crew is not available",
            "feasible": True,
            "blockers": maint_blockers})

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
    if T.chosen_plan(conn, flight_id) == option_id:      # same decision again: nothing to record
        return {"status": "already_selected", "flight": flight_id, "option": option_id, "superseded_approvals": []}
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
