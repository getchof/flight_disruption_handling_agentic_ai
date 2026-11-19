"""
AeroTurn - immutable safety rules.                                   [CODE, not AI]

Pure Python: no LLM, no database, no network. Every check takes plain data and
returns a list of violations (empty list = OK). tools.py calls these before
ANY write, and again when the Operations Controller approves something.
The LLM never sees or calls these functions, so it cannot bypass them.

  python test_guards.py   # proves every rule works
"""
import re
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType


# ---------- The limits. frozen=True: changing them at runtime raises an error ----------
@dataclass(frozen=True)
class Limits:
    MAX_DUTY_MIN: int = 13 * 60          # legal flight-duty period (13 h)
    POST_FLIGHT_DUTY_MIN: int = 30       # duty time still needed after landing
    MIN_CONNECTION_MIN: int = 60         # passenger minimum connection time at DXB
    MIN_TURNAROUND_MIN: int = 90         # wide-body: landing to next departure
    CREW_BRIEFING_MIN: int = 30          # crew must report and brief before boarding
    BOARDING_MIN: int = 30               # boarding time before doors close
    REQUIRED_CREW: tuple = (("Captain", 1), ("First Officer", 1), ("Purser", 1), ("Cabin Crew", 1))


LIMITS = Limits()

# Vulnerable-passenger priority tiers (lower number = protected first). Read-only.
PRIORITY_RANK = MappingProxyType({
    "MEDA": 1,      # medical case (e.g. oxygen)
    "UM": 2,        # unaccompanied minor
    "WCHR": 3,      # wheelchair
    "INF": 4,       # travelling with an infant
    "STANDARD": 9,
})


def is_vulnerable(tier):
    return PRIORITY_RANK.get(tier, 9) < PRIORITY_RANK["STANDARD"]


# ---------- Crew duty ----------
def duty_minutes_left(duty_start, at, now):
    """Minutes of legal duty a crew member still has at time `at`.
    duty_start=None means a standby crew member whose duty starts when called (now)."""
    start = duty_start or now
    used = (at - start).total_seconds() / 60
    return LIMITS.MAX_DUTY_MIN - used


def check_crew_legal(crew, departure, block_min, now):
    """Every crew member must have enough duty left at departure for the whole flight
    plus post-flight duties. crew = [{"crew_id", "name", "role", "duty_start"}]."""
    needed = block_min + LIMITS.POST_FLIGHT_DUTY_MIN
    violations = []
    for c in crew:
        left = duty_minutes_left(c["duty_start"], departure, now)
        if left < needed:
            violations.append(f"CREW DUTY: {c['name']} ({c['role']}) has {left:.0f} min of legal duty left "
                              f"at departure, the flight needs {needed} min")
    return violations


def check_crew_reachable(crew, departure, now):
    """Called-out standby crew must reach the aircraft and brief before boarding starts.
    crew = [{"name", "minutes_away"}]."""
    latest_ready = departure - timedelta(minutes=LIMITS.BOARDING_MIN)
    violations = []
    for c in crew:
        ready = now + timedelta(minutes=c["minutes_away"] + LIMITS.CREW_BRIEFING_MIN)
        if ready > latest_ready:
            violations.append(f"CREW TIMING: {c['name']} is {c['minutes_away']} min away and would be ready at "
                              f"{ready:%H:%M}, but boarding starts {latest_ready:%H:%M}")
    return violations


def check_crew_complement(crew):
    """The flight must have at least the required crew for each role."""
    violations = []
    for role, count in LIMITS.REQUIRED_CREW:
        have = sum(c["role"] == role for c in crew)
        if have < count:
            violations.append(f"CREW COMPLEMENT: needs {count} {role}, has {have}")
    return violations


# ---------- Maintenance ----------
def check_maintenance(items):
    """No departure while a MANDATORY maintenance item is not signed off.
    items = [{"item_id", "description", "mandatory", "status"}]."""
    return [f"MAINTENANCE: {i['item_id']} '{i['description']}' is {i['status']}, mandatory sign-off missing"
            for i in items if i["mandatory"] and i["status"] != "SIGNED_OFF"]


# ---------- Departure (combines the floors above) ----------
def check_departure(crew, maintenance_items, departure, arrival, block_min, now):
    """All the floors a departure proposal must pass. Returns every violation found."""
    violations = []
    turnaround = (departure - arrival).total_seconds() / 60
    if turnaround < LIMITS.MIN_TURNAROUND_MIN:
        violations.append(f"TURNAROUND: only {turnaround:.0f} min after landing, minimum is "
                          f"{LIMITS.MIN_TURNAROUND_MIN}")
    violations += check_crew_complement(crew)
    violations += check_crew_legal(crew, departure, block_min, now)
    violations += check_maintenance(maintenance_items)
    return violations


# ---------- Passengers ----------
def check_connection(arrival, departure):
    """A passenger can only be booked onto a flight they can actually reach."""
    gap = (departure - arrival).total_seconds() / 60
    if gap < 0:
        return [f"CONNECTION: flight already departed {abs(gap):.0f} min before the passenger lands"]
    if gap < LIMITS.MIN_CONNECTION_MIN:
        return [f"CONNECTION: only {gap:.0f} min to connect, minimum is {LIMITS.MIN_CONNECTION_MIN}"]
    return []


def check_priority(batch, waiting, free_seats_by_destination):
    """Vulnerable passengers are protected: a batch may not give scarce seats to standard
    passengers while vulnerable passengers for the same destination are still waiting.
      batch   = [{"passenger_id", "tier", "destination"}]   seats this batch would take
      waiting = [{"passenger_id", "tier", "destination"}]   everyone still needing a seat
      free_seats_by_destination = {"LHR": 14, ...}          free seats on valid alternatives"""
    in_batch = {b["passenger_id"] for b in batch}
    violations = []
    for dest in {b["destination"] for b in batch}:
        standard = [b["passenger_id"] for b in batch if b["destination"] == dest and not is_vulnerable(b["tier"])]
        if not standard:
            continue
        taken = sum(b["destination"] == dest for b in batch)
        left_after = free_seats_by_destination.get(dest, 0) - taken
        unserved = sorted((w for w in waiting if w["destination"] == dest and is_vulnerable(w["tier"])
                           and w["passenger_id"] not in in_batch),
                          key=lambda w: PRIORITY_RANK[w["tier"]])
        if unserved and len(unserved) > max(left_after, 0):
            ids = [f"{w['passenger_id']} ({w['tier']})" for w in unserved]
            violations.append(f"PRIORITY: {len(standard)} standard passenger(s) to {dest} would take seats "
                              f"needed by vulnerable passengers {ids}; rebook them first")
    return violations


# ---------- Gates ----------
def check_gate(gate, body, start, end, bookings):
    """A gate must fit the aircraft and be free for the whole period.
    gate = {"gate_id", "wide_body"}; bookings = [{"flight_id", "start", "end"}] on that gate."""
    violations = []
    if body == "WIDE" and not gate["wide_body"]:
        violations.append(f"GATE: {gate['gate_id']} cannot take a wide-body aircraft")
    for b in bookings:
        if start < b["end"] and b["start"] < end:  # time ranges overlap
            violations.append(f"GATE: {gate['gate_id']} is used by {b['flight_id']} "
                              f"{b['start']:%H:%M}-{b['end']:%H:%M}")
    return violations


# ---------- Input sanitising (runs BEFORE the text reaches the LLM) ----------
INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
    r"system\s*override",
    r"(bypass|disable|skip|ignore|disregard|forget|override)\b.{0,30}\b(guard|rule|limit|check|duty|maintenance"
    r"|sign.?off|approval|safety|polic|instruction)",
    r"(everything|it|this|all) (is|are) (already )?approved",
    r"you are now",
    r"developer mode",
    r"approve (it|this|everything) (yourself|automatically)",
]


def check_injection(text):
    """Flags text that tries to give the agent new orders. Returns the matched patterns."""
    lowered = text.lower()
    return [f"INJECTION: matched '{p}'" for p in INJECTION_PATTERNS if re.search(p, lowered)]
