"""
AeroTurn - the services the agent can call, plus the human actions.  [CODE, not AI]

AGENT TOOLS (the LLM may call these; type hints + docstrings are what it reads):
  get_turnaround_and_crew_state()        read-only picture of the disruption
  execute_atomic_passenger_rebook(...)   seats + bags in ONE SQLite transaction, full rollback on failure
  stage_controller_schedule_change(...)  departure / delay / crew swap / gate change -> guards -> approval queue
  send_passenger_notification(...)       message whose facts are written by code

HUMAN ACTIONS (only the console buttons call these, never the LLM):
  approve(), reject()          Operations Controller decisions (guards are re-checked on approve)
  engineer_sign_off()          maintenance engineer signs off an inspection

Every write goes through BEGIN / COMMIT / ROLLBACK and is written to audit_log.

  python test_tools.py   # proves rollback, guards and the approval gate (no AI)
"""
import json
from datetime import datetime, timedelta

import db
import guards as g
import turnaround

DB = db.DB_PATH   # tests point this at a temporary copy

# Demo switch: make a "concurrent booking" steal a seat mid-batch, to show the rollback.
SIMULATE_SEAT_CONFLICT = {"enabled": False, "at_item": 3}


class Conflict(Exception):
    """Raised inside a transaction to force a full rollback."""


# ---------- Helpers ----------
def _conn():
    return db.connect(DB)


def _now():
    """Scenario clock: the moment the late aircraft landed (stored with the scenario in the DB)."""
    return db.scenario(_conn())["now"]


def _ts():
    return _now().strftime(db.TIME_FMT)


def _hhmm(dt):
    return dt.strftime("%H:%M")


def _parse_time(text):
    """Accept '11:45' or '2026-11-20 11:45'."""
    text = text.strip()
    return db.to_time(text) if len(text) > 5 else db.to_time(f"{_now():%Y-%m-%d} {text}")


def audit(conn, actor, action, detail, outcome):
    db.append_audit(conn, _ts(), actor, action, detail if isinstance(detail, str) else json.dumps(detail), outcome)


def _flight(conn, flight_id):
    row = conn.execute("SELECT * FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
    return dict(row) if row else None


def _crew_on(conn, flight_id):
    return [{**dict(r), "duty_start": db.to_time(r["duty_start"]) if r["duty_start"] else None}
            for r in conn.execute("SELECT * FROM crew WHERE assigned_flight=?", (flight_id,))]


def _maintenance(conn, reg):
    return [dict(r) for r in conn.execute("SELECT * FROM maintenance_log WHERE aircraft_reg=?", (reg,))]


def _inbound_arrival(conn, flight):
    """Landing time of the aircraft that will operate `flight` (same registration)."""
    row = conn.execute("SELECT est_arr FROM flights WHERE aircraft_reg=? AND destination='DXB'",
                       (flight["aircraft_reg"],)).fetchone()
    return db.to_time(row["est_arr"]) if row else _now()


def _floor_departure(conn, flight):
    """Hard floor: landing + minimum wide-body turnaround."""
    return _inbound_arrival(conn, flight) + timedelta(minutes=g.LIMITS.MIN_TURNAROUND_MIN)


def _pending_crew_swap(conn, flight_id):
    for r in conn.execute("SELECT approval_id, payload FROM pending_approvals WHERE kind='CREW_SWAP' "
                          "AND status='PENDING' ORDER BY approval_id DESC"):
        p = json.loads(r["payload"])
        if p["flight_id"] == flight_id:
            return {**p, "approval_id": r["approval_id"]}
    return None


def _best_team(conn, flight, dep):
    """Standby crew matcher (CODE): for every role whose current crew is not legal at `dep`,
    pick the CLOSEST standby crew member of that role who is legal and can reach the aircraft."""
    current = _crew_on(conn, flight["flight_id"])
    picks, missing, rejected = [], [], []
    standby = [{**dict(r), "duty_start": db.to_time(r["duty_start"]) if r["duty_start"] else None}
               for r in conn.execute("SELECT * FROM crew WHERE status='STANDBY' ORDER BY minutes_away")]
    for c in current:
        if not g.check_crew_legal([c], dep, flight["block_min"], _now()):
            continue
        choice = None
        for s in (x for x in standby if x["role"] == c["role"]):
            why = g.check_crew_legal([s], dep, flight["block_min"], _now()) + g.check_crew_reachable([s], dep, _now())
            if why:
                rejected.append({"crew_id": s["crew_id"], "name": s["name"], "why": why[0]})
            elif choice is None:
                choice = s
        if choice:
            picks.append({"replaces": c["crew_id"], "crew_id": choice["crew_id"], "name": choice["name"],
                          "role": choice["role"], "minutes_away": choice["minutes_away"], "location": choice["location"]})
        else:
            missing.append(c["role"])
    kept = [c for c in current if c["crew_id"] not in {p["replaces"] for p in picks}]
    return picks, missing, rejected, kept


def _crew_minutes(conn, flight):
    """How far away the crew that will operate is (for the critical path). None = no legal crew."""
    floor = _floor_departure(conn, flight)
    current = _crew_on(conn, flight["flight_id"])
    if current and not g.check_crew_legal(current, floor, flight["block_min"], _now()):
        return max(c["minutes_away"] for c in current)
    pending = _pending_crew_swap(conn, flight["flight_id"])
    if pending:
        ids = [x.strip() for x in pending["crew_in"].split(",") if x.strip()]
        rows = conn.execute(f"SELECT minutes_away FROM crew WHERE crew_id IN ({','.join('?' * len(ids))})", ids)
        return max(r[0] for r in rows)
    picks, missing, _, kept = _best_team(conn, flight, floor)
    if missing:
        return None
    return max([p["minutes_away"] for p in picks] + [c["minutes_away"] for c in kept if c["crew_id"]])


def turnaround_plan_for(flight_id="EK-813"):
    """Critical-path plan for a turnaround, from the live data (used by the console)."""
    conn = _conn()
    flight = _flight(conn, flight_id)
    signed = not g.check_maintenance(_maintenance(conn, flight["aircraft_reg"]))
    return turnaround.plan(_inbound_arrival(conn, flight), _crew_minutes(conn, flight), signed)


def _earliest_departure(conn, flight):
    """Earliest legal departure = later of the hard floor and the critical-path finish."""
    floor = _floor_departure(conn, flight)
    signed = not g.check_maintenance(_maintenance(conn, flight["aircraft_reg"]))
    cpm = turnaround.plan(_inbound_arrival(conn, flight), _crew_minutes(conn, flight), signed)["departure"]
    return max(floor, cpm) if cpm else floor


def _gate_bookings(conn, gate_id, except_flight=None):
    return [{"flight_id": r["flight_id"], "start": db.to_time(r["start"]), "end": db.to_time(r["end"])}
            for r in conn.execute('SELECT flight_id, start, "end" FROM gate_bookings WHERE gate_id=?', (gate_id,))
            if r["flight_id"] != except_flight]


def _options(conn, destination, arrival):
    """Onward flights to a destination with free seats and whether a connection is possible."""
    rows = conn.execute(
        "SELECT f.flight_id, f.est_dep, SUM(s.occupant IS NULL) AS free FROM flights f "
        "JOIN seats s ON s.flight_id = f.flight_id WHERE f.origin='DXB' AND f.destination=? "
        "GROUP BY f.flight_id ORDER BY f.est_dep", (destination,)).fetchall()
    out = []
    for r in rows:
        problem = g.check_connection(arrival, db.to_time(r["est_dep"]))
        out.append({"flight_id": r["flight_id"], "departs": r["est_dep"][-5:], "free_seats": r["free"],
                    "bookable": not problem and r["free"] > 0,
                    "note": problem[0] if problem else ("full" if r["free"] == 0 else "ok")})
    return out


def snapshot(conn):
    """Exact copy of seats + bags, used to PROVE a rollback changed nothing."""
    seats = conn.execute("SELECT flight_id, seat_no, occupant FROM seats ORDER BY 1, 2").fetchall()
    bags = conn.execute("SELECT bag_tag, routed_to, status FROM bags ORDER BY 1").fetchall()
    return [tuple(r) for r in seats], [tuple(r) for r in bags]


def integrity(conn):
    return {"double_bookings": len(db.double_bookings(conn)), "orphan_bags": len(db.orphan_bags(conn))}


# =====================================================================
# THE SINGLE WRITE GATEWAY: every change to the system goes through here
# =====================================================================
def write_gateway(action, actor, request, apply, recheck=None):
    """The ONLY function that changes data (manual, stage 8).
      1. recheck(conn) -> violations: guards re-run right before writing (optional)
      2. BEGIN IMMEDIATE
      3. apply(tx, progress) does the writes and returns a result dict
      4. integrity check inside the transaction (no double booking, no orphan bag)
      5. COMMIT - or ROLLBACK everything on ANY failure
      6. audit the outcome (hash-chained log)
    Only the audit log itself is written outside, because it is append-only."""
    conn = _conn()
    if recheck:
        violations = recheck(conn)
        if violations:
            audit(conn, "CODE", f"{action}:blocked", request, "; ".join(violations))
            return {"status": "blocked", "violations": violations, "changed": "nothing"}
    progress = {"done": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = apply(conn, progress)
        check = integrity(conn)
        if check["double_bookings"] or check["orphan_bags"]:
            raise Conflict(f"integrity check failed: {check}")
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        audit(conn, "CODE", f"{action}:rolled_back", request, str(e))
        return {"status": "rolled_back", "reason": str(e), "items_done_before_failure": progress["done"],
                "committed": 0, "integrity_after": integrity(conn),
                "message": "Everything undone. Nothing was changed."}
    audit(conn, actor, action, request, result.pop("summary", "ok"))
    return result


# =====================================================================
# AGENT TOOL 1: read-only state
# =====================================================================
def get_turnaround_and_crew_state() -> dict:
    """Get the full disruption picture: the late inbound flight, the outbound flight it must
    operate, crew legal duty left, maintenance items, gate situation, passengers who miss
    their connection (vulnerable passengers first) with their rebooking options, and any
    changes waiting for Operations Controller approval."""
    conn = _conn()
    inbound = _flight(conn, "EK-812")
    outbound = _flight(conn, "EK-813")
    arrival = db.to_time(inbound["est_arr"])
    earliest = _earliest_departure(conn, outbound)
    planned = max(db.to_time(outbound["est_dep"]), earliest)

    def crew_row(c):
        start = db.to_time(c["duty_start"]) if c["duty_start"] else None
        problems = g.check_crew_legal([{**dict(c), "duty_start": start}], planned, outbound["block_min"], _now())
        return {"crew_id": c["crew_id"], "name": c["name"], "role": c["role"], "status": c["status"],
                "location": c["location"], "minutes_away": c["minutes_away"],
                "duty_min_left_now": round(g.duty_minutes_left(start, _now(), _now())),
                "legal_for_EK-813": not problems}

    crew = [crew_row(c) for c in conn.execute("SELECT * FROM crew WHERE assigned_flight='EK-813'")]
    standby = sorted((crew_row(c) for c in conn.execute("SELECT * FROM crew WHERE status='STANDBY'")),
                     key=lambda c: (not c["legal_for_EK-813"], c["minutes_away"]))

    missed = sorted(db.missed_connections(conn), key=lambda p: (g.PRIORITY_RANK[p["tier"]], p["passenger_id"]))
    options = {dest: _options(conn, dest, arrival) for dest in sorted({p["final_destination"] for p in missed})}

    gate = outbound["gate"]
    gate_problem = g.check_gate({"gate_id": gate, "wide_body": 1}, outbound["body"], _now(), planned,
                                _gate_bookings(conn, gate, except_flight="EK-813"))
    pending = [dict(r) for r in conn.execute(
        "SELECT approval_id, kind, payload, status FROM pending_approvals WHERE status='PENDING'")]
    return {
        "now": _ts(),
        "inbound": {"flight": "EK-812", "route": "BKK-DXB", "aircraft": f"{inbound['aircraft_reg']} {inbound['aircraft_type']}",
                    "scheduled_arrival": inbound["sched_arr"][-5:], "actual_arrival": inbound["est_arr"][-5:],
                    "delay_min": int((arrival - db.to_time(inbound["sched_arr"])).total_seconds() // 60)},
        "outbound": {"flight": "EK-813", "route": "DXB-MAN", "scheduled_departure": outbound["sched_dep"][-5:],
                     "current_departure": outbound["est_dep"][-5:], "status": outbound["status"],
                     "earliest_legal_departure": _hhmm(earliest), "block_min": outbound["block_min"],
                     "turnaround_critical_path": " -> ".join(turnaround_plan_for("EK-813")["critical_path"]),
                     "gate": gate, "gate_problem": gate_problem},
        "crew_on_EK-813": crew,
        "standby_crew": standby,
        "maintenance": [{k: m[k] for k in ("item_id", "description", "mandatory", "status")}
                        for m in _maintenance(conn, outbound["aircraft_reg"])],
        "missed_connections": {"count": len(missed),
                               "vulnerable": sum(g.is_vulnerable(p["tier"]) for p in missed),
                               "passengers": [{"passenger_id": p["passenger_id"], "name": p["name"], "tier": p["tier"],
                                               "needs": p["needs"], "destination": p["final_destination"],
                                               "missed_flight": p["original_onward"]} for p in missed]},
        "rebooking_options": options,
        "pending_approvals": pending,
        "integrity": integrity(conn),
    }


# =====================================================================
# AGENT TOOL 2: atomic batch rebooking
# =====================================================================
def execute_atomic_passenger_rebook(passenger_ids: list[str], new_flight: str) -> dict:
    """Rebook a batch of passengers who missed their connection onto ONE new flight, moving
    their seats and bags together in a single all-or-nothing transaction. If anything fails
    mid-way, everything is rolled back. The system enforces: connection time, free seats,
    vulnerable-passenger priority, no double-booking and no orphaned bags."""
    conn = _conn()
    request = {"passenger_ids": passenger_ids, "new_flight": new_flight}
    flight = _flight(conn, new_flight)
    if not flight:
        return {"status": "blocked", "violations": [f"Unknown flight {new_flight}"]}
    if not passenger_ids:
        return {"status": "blocked", "violations": ["Empty batch"]}

    # ---- Guards BEFORE touching anything ----
    missed = {p["passenger_id"]: p for p in db.missed_connections(conn)}
    violations = [f"{pid} does not need rebooking (not a missed connection)" for pid in passenger_ids if pid not in missed]
    if len(set(passenger_ids)) != len(passenger_ids):
        violations.append("The same passenger appears twice in the batch")
    batch = [missed[pid] for pid in dict.fromkeys(passenger_ids) if pid in missed]
    arrival = db.to_time(_flight(conn, "EK-812")["est_arr"])
    violations += [f"{p['passenger_id']} goes to {p['final_destination']}, {new_flight} goes to {flight['destination']}"
                   for p in batch if p["final_destination"] != flight["destination"]]
    violations += g.check_connection(arrival, db.to_time(flight["est_dep"]))
    free = conn.execute("SELECT COUNT(*) FROM seats WHERE flight_id=? AND occupant IS NULL", (new_flight,)).fetchone()[0]
    if len(batch) > free:
        violations.append(f"SEATS: batch of {len(batch)} but {new_flight} has only {free} free seat(s)")
    free_by_dest = {flight["destination"]: sum(o["free_seats"] for o in _options(conn, flight["destination"], arrival)
                                               if o["bookable"])}
    waiting = [{"passenger_id": p["passenger_id"], "tier": p["tier"], "destination": p["final_destination"]}
               for p in missed.values()]
    violations += g.check_priority([{"passenger_id": p["passenger_id"], "tier": p["tier"],
                                     "destination": p["final_destination"]} for p in batch], waiting, free_by_dest)
    if violations:
        audit(conn, "CODE", "rebook_blocked", request, "; ".join(violations))
        return {"status": "blocked", "violations": violations, "changed": "nothing"}

    # ---- Through the single write gateway: seats + bags for every passenger, or nothing ----
    before = integrity(conn)

    def apply(tx, progress):
        done = []
        for i, p in enumerate(batch, start=1):
            pid = p["passenger_id"]
            seat = tx.execute("SELECT seat_no FROM seats WHERE flight_id=? AND occupant IS NULL ORDER BY seat_no LIMIT 1",
                              (new_flight,)).fetchone()
            if seat is None:
                raise Conflict(f"item {i} ({pid}): no free seat left on {new_flight}")
            if SIMULATE_SEAT_CONFLICT["enabled"] and i == SIMULATE_SEAT_CONFLICT["at_item"]:
                # Simulated race: another booking channel grabs this exact seat a moment before us.
                SIMULATE_SEAT_CONFLICT["enabled"] = False   # one-shot: a retry of the batch can succeed
                tx.execute("UPDATE seats SET occupant='EXT' WHERE flight_id=? AND seat_no=?", (new_flight, seat["seat_no"]))
            tx.execute("UPDATE seats SET occupant=NULL WHERE occupant=?", (pid,))           # release old seat
            taken = tx.execute("UPDATE seats SET occupant=? WHERE flight_id=? AND seat_no=? AND occupant IS NULL",
                               (pid, new_flight, seat["seat_no"])).rowcount                  # claim new seat
            if taken != 1:
                raise Conflict(f"item {i} ({pid}): seat {new_flight}/{seat['seat_no']} was taken by another booking")
            tx.execute("UPDATE bags SET routed_to=?, status='IN_TRANSFER' WHERE passenger_id=?", (new_flight, pid))
            done.append({"passenger_id": pid, "seat": seat["seat_no"]})
            progress["done"] = len(done)
        return {"status": "committed", "new_flight": new_flight, "departs": flight["est_dep"][-5:],
                "rebooked": done, "vulnerable_in_batch": sum(g.is_vulnerable(p["tier"]) for p in batch),
                "integrity_before": before,
                "summary": f"{len(done)} passengers to {new_flight}"}

    result = write_gateway("rebook_committed", "CODE", request, apply)
    if result["status"] == "committed":
        result["integrity_after"] = integrity(conn)
    return result


# =====================================================================
# AGENT TOOL 3: schedule / gate / crew changes -> guards -> human approval queue
# =====================================================================
KINDS = ("DEPARTURE", "DELAY", "CREW_SWAP", "GATE_CHANGE")


def _pending_gate_change(conn, flight_id):
    """The latest gate change for this flight still waiting for the controller, if any."""
    for r in conn.execute("SELECT approval_id, payload FROM pending_approvals WHERE kind='GATE_CHANGE' "
                          "AND status='PENDING' ORDER BY approval_id DESC"):
        p = json.loads(r["payload"])
        if p["flight_id"] == flight_id:
            return {**p, "approval_id": r["approval_id"]}
    return None


def chosen_plan(conn=None):
    """The recovery option (A/B/C) the controller chose for EK-813, or None."""
    row = (conn or _conn()).execute("SELECT detail FROM audit_log WHERE action='recovery_plan_selected' "
                                    "ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def approved_changes(conn=None, flight_id="EK-813"):
    """Changes for a flight that a controller has already approved (= option A is being executed)."""
    rows = (conn or _conn()).execute("SELECT approval_id, kind, payload FROM pending_approvals "
                                     "WHERE status='APPROVED' ORDER BY approval_id").fetchall()
    return [f"#{r['approval_id']} {r['kind']}" for r in rows if json.loads(r["payload"])["flight_id"] == flight_id]


def _check_change(conn, kind, flight_id, new_time, gate, crew_in):
    """Run the guards for a proposed change. Used when staging AND again on approval."""
    flight = _flight(conn, flight_id)
    if not flight:
        return [f"Unknown flight {flight_id}"], None
    dep = _parse_time(new_time) if new_time else max(db.to_time(flight["est_dep"]), _earliest_departure(conn, flight))
    arrival = _inbound_arrival(conn, flight)

    # Plan consistency: crew swap / gate / delay / departure belong to option A. If the controller
    # chose option B (wait for crew rest) or C (cancel), these changes would contradict that decision.
    plan = chosen_plan(conn)
    if flight_id == "EK-813" and plan in ("B", "C"):
        return [f"PLAN: the controller chose option {plan} for EK-813; a {kind} belongs to option A and "
                f"would contradict that decision"], dep

    if kind == "DEPARTURE":
        return g.check_departure(_crew_on(conn, flight_id), _maintenance(conn, flight["aircraft_reg"]),
                                 dep, arrival, flight["block_min"], _now()), dep
    if kind == "DELAY":
        v = []
        if dep < _earliest_departure(conn, flight):
            v.append(f"TURNAROUND: {dep:%H:%M} is before the earliest legal departure "
                     f"{_earliest_departure(conn, flight):%H:%M}")
        # Check the gate the flight WILL use: a pending gate change counts (the controller
        # must approve it first; approve() enforces that order).
        gate_id = flight["gate"]
        pending_gate = _pending_gate_change(conn, flight_id)
        if pending_gate:
            gate_id = pending_gate["gate"]
        row = conn.execute("SELECT * FROM gates WHERE gate_id=?", (gate_id,)).fetchone()
        v += [x + ("" if pending_gate else " (stage a GATE_CHANGE first)") for x in
              g.check_gate(dict(row), flight["body"], _now(), dep, _gate_bookings(conn, gate_id, except_flight=flight_id))]
        return v, dep
    if kind == "GATE_CHANGE":
        row = conn.execute("SELECT * FROM gates WHERE gate_id=?", (gate,)).fetchone()
        if not row:
            return [f"Unknown gate {gate}"], dep
        return g.check_gate(dict(row), flight["body"], _now(), dep, _gate_bookings(conn, gate, except_flight=flight_id)), dep
    if kind == "CREW_SWAP":
        ids = [c.strip() for c in crew_in.split(",") if c.strip()]
        incoming = [dict(r) for r in conn.execute(
            f"SELECT * FROM crew WHERE crew_id IN ({','.join('?' * len(ids))})", ids)] if ids else []
        v = [f"Unknown crew {x}" for x in ids if x not in {c["crew_id"] for c in incoming}]
        v += [f"{c['crew_id']} is not on standby ({c['status']})" for c in incoming if c["status"] != "STANDBY"]
        new_team = [c for c in _crew_on(conn, flight_id) if c["role"] not in {i["role"] for i in incoming}]
        new_team += [{**c, "duty_start": db.to_time(c["duty_start"]) if c["duty_start"] else None} for c in incoming]
        v += g.check_crew_complement(new_team)
        v += g.check_crew_legal(new_team, dep, flight["block_min"], _now())
        v += g.check_crew_reachable(incoming, dep, _now())
        return v, dep
    return [f"Unknown kind {kind}; use one of {KINDS}"], dep


def stage_controller_schedule_change(kind: str, flight_id: str, new_time: str = "", gate: str = "",
                                     crew_in: str = "") -> dict:
    """Propose a schedule, gate or crew change. Nothing changes immediately: the system checks
    the legal crew-duty, maintenance, turnaround and gate rules, and if they pass the change
    waits for a human Operations Controller to approve it.
    kind: DEPARTURE (release the flight), DELAY (new departure time), CREW_SWAP or GATE_CHANGE.
    new_time: planned departure 'HH:MM' (optional). gate: e.g. 'B14' (GATE_CHANGE only).
    crew_in: comma-separated standby crew IDs, e.g. 'S-201,S-203' (CREW_SWAP only); they
    replace the current crew members with the same roles."""
    kind = kind.upper().strip()
    conn = _conn()
    request = {"kind": kind, "flight_id": flight_id, "new_time": new_time, "gate": gate, "crew_in": crew_in}
    violations, dep = _check_change(conn, kind, flight_id, new_time, gate, crew_in)
    if violations:
        audit(conn, "CODE", f"{kind.lower()}_blocked", request, "; ".join(violations))
        return {"status": "blocked", "violations": violations, "changed": "nothing"}
    request["new_time"] = _hhmm(dep)
    for r in conn.execute("SELECT approval_id, payload FROM pending_approvals WHERE status='PENDING' AND kind=?", (kind,)):
        if json.loads(r["payload"]) == request:   # same change already waiting: don't queue it twice
            return {"status": "pending_controller_approval", "approval_id": r["approval_id"], "change": request,
                    "message": "This exact change is already waiting for the controller. Move on."}
    def apply(tx, progress):
        approval_id = tx.execute(
            "INSERT INTO pending_approvals (kind, payload, reason, status, requested_by, requested_at) "
            "VALUES (?,?,?,?,?,?)", (kind, json.dumps(request), "guards passed", "PENDING", "AI agent", _ts())).lastrowid
        return {"status": "pending_controller_approval", "approval_id": approval_id, "change": request,
                "message": "Guards passed. Nothing is changed until the Operations Controller approves.",
                "summary": f"waiting for controller (approval #{approval_id})"}

    return write_gateway(f"{kind.lower()}_staged", "CODE", request, apply)


# =====================================================================
# AGENT TOOL 4: passenger notification (facts written by code)
# =====================================================================
def send_passenger_notification(passenger_ids: list[str], note: str) -> dict:
    """Notify passengers. The system writes each passenger's real situation (new flight, seat,
    departure, or that staff are arranging their travel). `note` is only a short friendly
    message from you (e.g. an apology); do not put flight details in it."""
    conn = _conn()
    sent = {}
    for pid in passenger_ids:
        p = conn.execute("SELECT * FROM passengers WHERE passenger_id=?", (pid,)).fetchone()
        if not p:
            continue
        seat = conn.execute("SELECT s.flight_id, s.seat_no, f.est_dep FROM seats s JOIN flights f "
                            "ON f.flight_id=s.flight_id WHERE s.occupant=?", (pid,)).fetchone()
        if seat and seat["flight_id"] != p["original_onward"]:
            facts = (f"EK-812 landed late. You are rebooked on {seat['flight_id']} to {p['final_destination']}, "
                     f"seat {seat['seat_no']}, departing {seat['est_dep'][-5:]}. Your bags go with you.")
        elif pid in {m["passenger_id"] for m in db.missed_connections(conn)}:
            facts = (f"EK-812 landed late and you will miss {p['original_onward']}. Our team is arranging your "
                     f"onward travel and will meet you at the transfer desk.")
        else:
            facts = f"EK-812 landed late, but your connection {p['original_onward']} is still on time."
        if p["tier"] != "STANDARD":
            facts += f" Assistance ({p['needs']}) will meet you at the aircraft door."
        sent[pid] = facts + (f" {note.strip()}" if note and not any(f in note for f in ("EK-", "seat ")) else "")

    def apply(tx, progress):
        tx.executemany("INSERT INTO notifications (ts, passenger_id, text) VALUES (?,?,?)",
                       [(_ts(), pid, text) for pid, text in sent.items()])
        return {"status": "sent", "count": len(sent), "messages": sent, "summary": f"{len(sent)} sent"}

    return write_gateway("notifications_sent", "CODE", {"passenger_ids": passenger_ids}, apply)


# =====================================================================
# AGENT TOOL 5: standby crew matcher
# =====================================================================
def find_legal_standby_crew(flight_id: str) -> dict:
    """Find the fastest LEGAL replacement crew for a flight. For every crew member who would
    break duty limits, the system picks the closest standby crew member of the same role who
    is within duty limits and can reach the aircraft and brief before boarding. Returns the
    crew IDs to use in a CREW_SWAP and the departure time the critical path then allows."""
    conn = _conn()
    flight = _flight(conn, flight_id)
    if not flight:
        return {"status": "error", "error": f"Unknown flight {flight_id}"}
    floor = _floor_departure(conn, flight)
    picks, missing, rejected, kept = _best_team(conn, flight, floor)
    minutes = None if missing else max([p["minutes_away"] for p in picks] + [c["minutes_away"] for c in kept])
    cpm = turnaround.plan(_inbound_arrival(conn, flight), minutes,
                          not g.check_maintenance(_maintenance(conn, flight["aircraft_reg"])))
    return {"status": "ok" if not missing else "incomplete", "flight": flight_id,
            "replace": [p["replaces"] for p in picks], "recommended_crew_in": ",".join(p["crew_id"] for p in picks),
            "team": picks, "roles_without_legal_standby": missing, "rejected_standby": rejected,
            "team_ready_at": (_now() + timedelta(minutes=(minutes or 0) + g.LIMITS.CREW_BRIEFING_MIN)).strftime("%H:%M")
            if minutes is not None else None,
            "projected_departure": _hhmm(max(floor, cpm["departure"])) if cpm["departure"] else None}


AGENT_TOOLS = [get_turnaround_and_crew_state, find_legal_standby_crew, execute_atomic_passenger_rebook,
               stage_controller_schedule_change, send_passenger_notification]


# =====================================================================
# HUMAN ACTIONS (console buttons only - never given to the LLM)
# =====================================================================
def approve(approval_id, controller):
    """Operations Controller approves a staged change. Guards are RE-CHECKED against the
    current state first (things may have changed since staging), then applied atomically."""
    conn = _conn()
    row = conn.execute("SELECT * FROM pending_approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if not row or row["status"] != "PENDING":
        return {"status": "error", "message": f"Approval #{approval_id} is not pending"}
    req = json.loads(row["payload"])
    if req["kind"] == "DELAY":
        gate_first = _pending_gate_change(conn, req["flight_id"])
        if gate_first:   # order matters: the flight must be at its new gate before it can be delayed
            return {"status": "waiting", "message": f"Approve gate change #{gate_first['approval_id']} first"}
    held = {}

    def recheck(c):   # guards re-run at the moment of approval (the state may have changed)
        violations, held["dep"] = _check_change(c, req["kind"], req["flight_id"], req["new_time"], req["gate"],
                                                req["crew_in"])
        return violations

    def apply(tx, progress):
        fid, dep = req["flight_id"], held["dep"]
        new = dep.strftime(db.TIME_FMT)
        if req["kind"] == "GATE_CHANGE":
            tx.execute("UPDATE flights SET gate=? WHERE flight_id=?", (req["gate"], fid))
            tx.execute("DELETE FROM gate_bookings WHERE flight_id=?", (fid,))
            tx.execute("INSERT INTO gate_bookings VALUES (?,?,?,?)", (req["gate"], fid, _ts(), new))
        elif req["kind"] == "DELAY":
            f = _flight(tx, fid)
            shift = dep - db.to_time(f["est_dep"])
            tx.execute("UPDATE flights SET est_dep=?, est_arr=?, status='DELAYED' WHERE flight_id=?",
                       (new, (db.to_time(f["est_arr"]) + shift).strftime(db.TIME_FMT), fid))
            tx.execute('UPDATE gate_bookings SET "end"=? WHERE flight_id=?', (new, fid))
        elif req["kind"] == "CREW_SWAP":
            for cid in [c.strip() for c in req["crew_in"].split(",") if c.strip()]:
                role = tx.execute("SELECT role FROM crew WHERE crew_id=?", (cid,)).fetchone()["role"]
                tx.execute("UPDATE crew SET assigned_flight=NULL, status='RELEASED_TO_REST' "
                           "WHERE assigned_flight=? AND role=?", (fid, role))
                tx.execute("UPDATE crew SET assigned_flight=?, status='ASSIGNED', duty_start=? WHERE crew_id=?",
                           (fid, _ts(), cid))
        elif req["kind"] == "DEPARTURE":
            tx.execute("UPDATE flights SET est_dep=?, status='RELEASED' WHERE flight_id=?", (new, fid))
        tx.execute("UPDATE pending_approvals SET status='APPROVED', decided_by=?, decided_at=? WHERE approval_id=?",
                   (controller, _ts(), approval_id))
        return {"status": "approved", "change": req, "summary": f"approved by {controller}"}

    result = write_gateway(f"{req['kind'].lower()}_approved", "HUMAN", req, apply, recheck)
    if result["status"] == "blocked":
        _set_approval_status(approval_id, "REFUSED_BY_GUARD", controller, "refused by guard re-check")
        return {"status": "refused_by_guard", "violations": result["violations"]}
    return result


def _set_approval_status(approval_id, status, who, summary):
    def apply(tx, progress):
        n = tx.execute("UPDATE pending_approvals SET status=?, decided_by=?, decided_at=? "
                       "WHERE approval_id=? AND status='PENDING'", (status, who, _ts(), approval_id)).rowcount
        if n != 1:
            raise Conflict(f"approval #{approval_id} is not pending")
        return {"status": status.lower(), "summary": summary}
    return write_gateway(f"approval_{status.lower()}", "HUMAN", {"approval_id": approval_id}, apply)


def reject(approval_id, controller, reason=""):
    r = _set_approval_status(approval_id, "REJECTED", controller, f"{controller}: {reason}")
    return {"status": "rejected" if r["status"] == "rejected" else "error"}


def engineer_sign_off(item_id, engineer):
    """A licensed engineer signs off a maintenance item (human action, never the AI)."""
    def apply(tx, progress):
        n = tx.execute("UPDATE maintenance_log SET status='SIGNED_OFF', signed_off_by=?, signed_off_at=? "
                       "WHERE item_id=? AND status<>'SIGNED_OFF'", (engineer, _ts(), item_id)).rowcount
        if n != 1:
            raise Conflict(f"{item_id} is unknown or already signed off")
        return {"status": "signed_off", "summary": f"signed off by {engineer}"}
    r = write_gateway("maintenance_signed_off", "HUMAN", {"item_id": item_id}, apply)
    return {"status": "signed_off" if r["status"] == "signed_off" else "error"}


def verified_facts():
    """The key facts straight from the database (CODE), shown next to the AI's brief so the
    controller never has to trust the AI's numbers."""
    conn = _conn()
    rebooked = conn.execute(
        "SELECT s.flight_id, COUNT(*) n, SUM(p.tier <> 'STANDARD') v FROM passengers p JOIN seats s "
        "ON s.occupant = p.passenger_id WHERE s.flight_id <> p.original_onward GROUP BY s.flight_id").fetchall()
    unresolved = sorted(db.missed_connections(conn), key=lambda p: (g.PRIORITY_RANK[p["tier"]], p["passenger_id"]))
    pending = [{"approval_id": r["approval_id"], "kind": r["kind"], **json.loads(r["payload"])}
               for r in conn.execute("SELECT * FROM pending_approvals WHERE status='PENDING'")]
    return {
        "rebooked": {r["flight_id"]: {"passengers": r["n"], "vulnerable": r["v"]} for r in rebooked},
        "unresolved": [f"{p['passenger_id']} ({p['tier']}, {p['final_destination']})" for p in unresolved],
        "awaiting_approval": pending,
        "open_mandatory_maintenance": [r["item_id"] for r in conn.execute(
            "SELECT item_id FROM maintenance_log WHERE mandatory=1 AND status<>'SIGNED_OFF'")],
        "integrity": integrity(conn),
    }


def reset(**settings):
    """Rebuild the database (console buttons). No settings = the brief's scenario."""
    db.build(DB, **settings)


def current_settings():
    """The what-if settings the current database was built with (without the clock)."""
    s = db.scenario(_conn())
    return {k: s[k] for k in db.DEFAULT_SCENARIO}
