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
    MIN_CONNECTION_MIN: int = 60         # passenger minimum connection time at DXB, between concourses
    MIN_CONNECTION_SAME_CONCOURSE_MIN: int = 30   # passenger connection inside one concourse (short walk)
    MIN_BAG_TRANSFER_MIN: int = 45       # a bag needs 45 min to move between aircraft, wherever the gates are
    MAX_REBOOK_BATCH: int = 10           # OCC policy: standard passengers are rebooked in batches of at most 10
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


def check_signoff(opened_at, engineer, aircraft_type, signed_at, now):
    """A maintenance sign-off is valid only if (U-4.3):
      - the engineer is on the licensed roster (engineer = roster row, None if not found),
      - their licence covers this aircraft type,
      - it was signed AFTER the defect was opened, and not in the future.
    opened_at None = defect time unknown -> cannot be validated."""
    violations = []
    if engineer is None:
        violations.append("SIGN-OFF: engineer is not on the licensed roster")
    elif aircraft_type not in [t.strip() for t in engineer["aircraft_types"].split(",")]:
        violations.append(f"SIGN-OFF: {engineer['name']} is not licensed for the {aircraft_type}")
    if signed_at is None:
        violations.append("SIGN-OFF: no sign-off time")
    elif opened_at is None:
        violations.append("SIGN-OFF: defect opening time unknown, sign-off cannot be checked")
    elif signed_at <= opened_at:
        violations.append(f"SIGN-OFF: signed {signed_at:%H:%M}, before the defect was opened at {opened_at:%H:%M}")
    if signed_at is not None and signed_at > now:
        violations.append(f"SIGN-OFF: signed {signed_at:%H:%M}, which is in the future")
    return violations


# ---------- Crew channel: messages are DATA, never instructions (U-4.2) ----------
DUTY_CLAIM_PATTERNS = [
    r"\bextend(ed|ing)?\b.{0,25}\b(duty|hours|limit)",
    r"\b(duty|hours)\b.{0,25}\bextend",
    r"\b(push|release|go|depart)\b.{0,20}\b(departure|flight|now)\b",
    r"\b(fine|ok|okay|happy|agree|willing)\b.{0,30}\b(fly|extend|continue|operate)",
    r"\b(waive|ignore|skip)\b.{0,20}\b(limit|rest|duty)",
]


def classify_crew_message(text):
    """What a crew-channel message CLAIMS. Nothing in it can change a rule: duty limits are legal limits.
    Returns a list of claims found (empty = plain information)."""
    lowered = text.lower()
    claims = []
    if any(re.search(p, lowered) for p in DUTY_CLAIM_PATTERNS):
        claims.append("asks to extend crew duty or push the departure: ignored, duty limits are legal limits")
    claims += check_injection(text)
    return claims


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
def passenger_mct(arrival_gate, departure_gate):
    """Passenger minimum connection time: a short walk inside one concourse (same first letter,
    e.g. B12 -> B10), the standard time between concourses or when a gate is unknown."""
    if arrival_gate and departure_gate and arrival_gate[0] == departure_gate[0]:
        return LIMITS.MIN_CONNECTION_SAME_CONCOURSE_MIN
    return LIMITS.MIN_CONNECTION_MIN


def check_connection(arrival, departure, minimum=None):
    """A passenger can only be booked onto a flight they can actually reach."""
    minimum = LIMITS.MIN_CONNECTION_MIN if minimum is None else minimum
    gap = (departure - arrival).total_seconds() / 60
    if gap < 0:
        return [f"CONNECTION: flight already departed {abs(gap):.0f} min before the passenger lands"]
    if gap < minimum:
        return [f"CONNECTION: only {gap:.0f} min to connect, minimum is {minimum}"]
    return []


def bag_can_make(arrival, departure):
    """Can a checked bag be moved from the arriving aircraft onto this departure? (45 min, any gates)"""
    return (departure - arrival).total_seconds() / 60 >= LIMITS.MIN_BAG_TRANSFER_MIN


def check_batch_policy(batch, seatable_vulnerable_waiting):
    """OCC rebooking policy (U-4.1): vulnerable passengers first, in their own transaction; then
    everyone else in batches of at most MAX_REBOOK_BATCH.
      batch = [{"passenger_id", "tier"}]
      seatable_vulnerable_waiting = ids of vulnerable passengers still waiting who CAN be seated
                                    (a vulnerable passenger with no seat anywhere does not block others)"""
    vul = [b for b in batch if is_vulnerable(b["tier"])]
    std = [b for b in batch if not is_vulnerable(b["tier"])]
    violations = []
    if vul and std:
        violations.append("PRIORITY: vulnerable passengers are rebooked in their own transaction; "
                          "do not mix them with standard passengers")
    if std and len(std) > LIMITS.MAX_REBOOK_BATCH:
        violations.append(f"BATCH: {len(std)} standard passengers in one batch, policy maximum is "
                          f"{LIMITS.MAX_REBOOK_BATCH}")
    waiting = sorted(set(seatable_vulnerable_waiting) - {b["passenger_id"] for b in batch})
    if std and waiting:
        violations.append(f"PRIORITY: vulnerable passengers {waiting} must be rebooked first, in their own "
                          f"transaction")
    return violations


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
