"""
Airport disruption-recovery agent - Stage 1 (terminal) - AI Agent Arena practice

Scenario: flight EK-101 (DXB -> LHR) is delayed 3 hours. The agent finds the
connecting passengers, rebooks them, reroutes their bags, checks crew duty
limits, notifies passengers and escalates risky cases to a duty manager.

Same pattern as refund_agent.py: Gemini decides which tools to call, but every
safety/business rule is enforced IN CODE inside the action tools, so the
model cannot skip a rule even if it tries.

RUN (PowerShell)
  pip install google-genai python-dotenv ollama
  python aviation_agent.py           # Gemini, falls back to local Ollama if all Gemini models fail
  python aviation_agent.py --local   # local Ollama model only (offline, no API quota)

GEMINI_API_KEY is loaded from .env by python-dotenv.
All times are UTC. All data below is mock data.
"""
import copy
import functools
import inspect
import json
import re
import sys
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()            # loads GEMINI_API_KEY from .env
client = genai.Client()  # reads GEMINI_API_KEY from the environment

# Tried in order. On 404 (bad name) or 429 (quota used up) we skip to the next one.
MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
]
MAX_STEPS = 25        # max model calls per request (a disruption needs many tool calls)
MAX_STEPS_LOCAL = 60  # small local models make one tool call per step, so they need more

# Local fallback: Ollama runs a model on this PC (free, offline, no API key).
# Used when every Gemini model fails, or always with --local / the UI toggle.
USE_OLLAMA_FALLBACK = True
OLLAMA_MODEL = "qwen2.5:3b"  # better at tool calls than "llama3.2" (also installed)
OLLAMA_CONTEXT = 8192       # tokens the local model can "see"; the tool history is long

# ---------- Business / safety rules (constants so they are easy to change) ----------
MIN_CONNECTION_MIN = 60       # passenger needs at least 60 min between flights
MIN_BAG_CONNECTION_MIN = 45   # bags need at least 45 min to be transferred
SAFETY_CRITICAL_ROLES = {"Captain"}  # changing these crew members needs a human

TIME_FMT = "%Y-%m-%d %H:%M"


def to_time(text):
    return datetime.strptime(text, TIME_FMT)


def to_text(dt):
    return dt.strftime(TIME_FMT)


# ---------- Mock data: the starting state. load_scenario() copies it. ----------
BASE_FLIGHTS = {
    "EK-101": {"origin": "DXB", "destination": "LHR", "departure": "2026-11-20 06:00",
               "arrival": "2026-11-20 13:00", "delay_min": 180, "seats_left": 0, "duration_h": 7},
    "EK-201": {"origin": "LHR", "destination": "JFK", "departure": "2026-11-20 14:30",
               "arrival": "2026-11-20 22:30", "delay_min": 0, "seats_left": 4, "duration_h": 8},
    "EK-203": {"origin": "LHR", "destination": "JFK", "departure": "2026-11-20 17:30",
               "arrival": "2026-11-21 01:30", "delay_min": 0, "seats_left": 6, "duration_h": 8},
}

# connection = the onward flight they were booked on (None = trip ends at LHR)
BASE_PASSENGERS = {
    "P1": {"name": "Ahmed Khan", "flight": "EK-101", "connection": "EK-201", "final_destination": "JFK",
           "vip": True, "special_assistance": None, "unaccompanied_minor": False},
    "P2": {"name": "Maria Lopez", "flight": "EK-101", "connection": "EK-201", "final_destination": "JFK",
           "vip": False, "special_assistance": None, "unaccompanied_minor": False},
    "P3": {"name": "John Smith", "flight": "EK-101", "connection": "EK-201", "final_destination": "JFK",
           "vip": False, "special_assistance": "wheelchair", "unaccompanied_minor": False},
    "P4": {"name": "Fatima Ali", "flight": "EK-101", "connection": "EK-201", "final_destination": "JFK",
           "vip": False, "special_assistance": None, "unaccompanied_minor": False},
    "P5": {"name": "Chen Wei", "flight": "EK-101", "connection": None, "final_destination": "LHR",
           "vip": False, "special_assistance": None, "unaccompanied_minor": False},
    "P6": {"name": "Priya Nair", "flight": "EK-101", "connection": "EK-201", "final_destination": "JFK",
           "vip": False, "special_assistance": "visually impaired", "unaccompanied_minor": False},
    "P7": {"name": "Leo Martin (age 10)", "flight": "EK-101", "connection": "EK-201", "final_destination": "JFK",
           "vip": False, "special_assistance": None, "unaccompanied_minor": True},
    "P8": {"name": "Sofia Rossi", "flight": "EK-101", "connection": None, "final_destination": "LHR",
           "vip": True, "special_assistance": None, "unaccompanied_minor": False},
}

BASE_BAGS = {
    f"BAG-00{i}": {"passenger_id": f"P{i}", "on_flight": "EK-101",
                   "next_flight": BASE_PASSENGERS[f"P{i}"]["connection"], "status": "loaded"}
    for i in range(1, 9)
}

# duty_hours_left = how many more hours this person may legally work today
BASE_CREW = {
    "CR1": {"name": "Omar Haddad", "role": "Captain", "flight": "EK-101", "duty_hours_left": 12.0},
    "CR2": {"name": "Anna Berg", "role": "First Officer", "flight": "EK-101", "duty_hours_left": 11.0},
    "CR3": {"name": "Lina Saeed", "role": "Cabin Crew", "flight": "EK-101", "duty_hours_left": 10.5},
    "CR4": {"name": "Raj Patel", "role": "First Officer", "flight": None, "duty_hours_left": 12.0},  # standby
}

# Each scenario = a text for the agent + changes to the base data + the expected outcome.
# A change is (table, key, field, new_value). test_scenarios.py checks the expected outcome.
DEFAULT_MESSAGE = "Flight EK-101 (DXB to LHR) is delayed 3 hours. Handle the disruption."
SCENARIOS = {
    "normal": {
        "title": "Normal 3-hour delay",
        "message": DEFAULT_MESSAGE,
        "changes": [],
        "expected": "5 connecting passengers rebooked to EK-203 with their 5 bags. No crew changes. "
                    "Unaccompanied minor (P7) escalated. Everyone else notified.",
    },
    "crew_over_limit": {
        "title": "Crew over the duty limit",
        "message": DEFAULT_MESSAGE,
        "changes": [("CREW", "CR2", "duty_hours_left", 9.0)],  # First Officer now short of 10h
        "expected": "Same rebooking as normal, plus First Officer Anna Berg (CR2, 9h left, needs 10h) "
                    "replaced by standby Raj Patel (CR4).",
    },
    "flight_full": {
        "title": "Alternative flight almost full",
        "message": DEFAULT_MESSAGE,
        "changes": [("FLIGHTS", "EK-203", "seats_left", 2)],  # only 2 seats for 6 connecting passengers
        "expected": "The 2 seats go to the special-assistance passengers (P3, P6). P1, P2, P4 cannot be "
                    "rebooked and are escalated. No one is booked onto a full flight.",
    },
}

# ---------- Live state (tools read and change these) ----------
FLIGHTS, PASSENGERS, BAGS, CREW = {}, {}, {}, {}
ACTION_LOG = []  # every tool call: time, tool, inputs, result, blocked


def load_scenario(name="normal"):
    """Reset all data to the base state, apply the scenario's changes, clear the log."""
    tables = {"FLIGHTS": FLIGHTS, "PASSENGERS": PASSENGERS, "BAGS": BAGS, "CREW": CREW}
    for live, base in ((FLIGHTS, BASE_FLIGHTS), (PASSENGERS, BASE_PASSENGERS),
                       (BAGS, BASE_BAGS), (CREW, BASE_CREW)):
        live.clear()
        live.update(copy.deepcopy(base))  # update in place so imports (app.py) see it
    for p in PASSENGERS.values():
        p["rebooked_to"] = None
        p["notified"] = False
        p["escalated"] = False
    for c in CREW.values():
        c["off_duty"] = False   # True once replaced: not on a flight, but not standby either
        c["escalated"] = False
    for table, key, field, value in SCENARIOS[name]["changes"]:
        tables[table][key][field] = value
    ACTION_LOG.clear()
    return SCENARIOS[name]


# ---------- Small helpers ----------
def new_departure(flight_number):
    f = FLIGHTS[flight_number]
    return to_time(f["departure"]) + timedelta(minutes=f["delay_min"])


def new_arrival(flight_number):
    f = FLIGHTS[flight_number]
    return to_time(f["arrival"]) + timedelta(minutes=f["delay_min"])


def minutes_between(earlier, later):
    return int((later - earlier).total_seconds() // 60)


def misses_connection(p):
    """True if the passenger's delayed arrival leaves too little time for their connection."""
    if not p["connection"]:
        return False
    gap = minutes_between(new_arrival(p["flight"]), new_departure(p["connection"]))
    return gap < MIN_CONNECTION_MIN


def is_standby(c):
    """Standby = not on a flight and not taken off duty after being replaced."""
    return c["flight"] is None and not c.get("off_duty")


def blocked(rule, next_step):
    """Standard result when a rule stops an action. The log marks these in red."""
    return {"blocked": True, "rule": rule, "next_step": next_step}


# ---------- Read-only tools ----------
def get_flight_status(flight_number: str) -> dict:
    """Get a flight's route, scheduled and estimated times, delay in minutes and seats left."""
    f = FLIGHTS.get(flight_number)
    if not f:
        return {"error": f"Flight {flight_number} not found"}
    return {
        "flight": flight_number, "origin": f["origin"], "destination": f["destination"],
        "scheduled_departure": f["departure"], "estimated_departure": to_text(new_departure(flight_number)),
        "scheduled_arrival": f["arrival"], "estimated_arrival": to_text(new_arrival(flight_number)),
        "delay_min": f["delay_min"], "seats_left": f["seats_left"],
    }


def get_affected_passengers(flight_number: str) -> dict:
    """List passengers on a flight, whether they now miss their connection, and their
    VIP / special-assistance / unaccompanied-minor flags. Priority passengers come first."""
    if flight_number not in FLIGHTS:
        return {"error": f"Flight {flight_number} not found"}
    rows = []
    for pid, p in PASSENGERS.items():
        if p["flight"] != flight_number:
            continue
        rows.append({
            "passenger_id": pid, "name": p["name"], "connection": p["connection"],
            "final_destination": p["final_destination"], "misses_connection": misses_connection(p),
            "vip": p["vip"], "special_assistance": p["special_assistance"],
            "unaccompanied_minor": p["unaccompanied_minor"], "rebooked_to": p["rebooked_to"],
        })
    rows.sort(key=lambda r: not r["special_assistance"])  # priority passengers first
    return {"flight": flight_number, "estimated_arrival": to_text(new_arrival(flight_number)),
            "passengers": rows}


def find_alternative_flights(passenger_id: str) -> dict:
    """Find onward flights to the passenger's final destination, with seats left and
    whether the connection time after the delayed arrival is long enough."""
    p = PASSENGERS.get(passenger_id)
    if not p:
        return {"error": f"Passenger {passenger_id} not found"}
    arrive = new_arrival(p["flight"])
    hub = FLIGHTS[p["flight"]]["destination"]
    options = []
    for fn, f in FLIGHTS.items():
        if f["origin"] != hub or f["destination"] != p["final_destination"]:
            continue
        gap = minutes_between(arrive, new_departure(fn))
        if gap < 0:
            note = f"already departed before {p['flight']} lands"
        elif gap < MIN_CONNECTION_MIN:
            note = f"only {gap} min to connect (minimum {MIN_CONNECTION_MIN})"
        elif f["seats_left"] <= 0:
            note = "full"
        else:
            note = f"{gap} min to connect"
        options.append({
            "flight": fn, "departure": to_text(new_departure(fn)), "seats_left": f["seats_left"],
            "connection": note,
            "bookable": f["seats_left"] > 0 and gap >= MIN_CONNECTION_MIN,
        })
    return {"passenger_id": passenger_id, "arrives_at": to_text(arrive), "options": options}


def get_baggage_status(passenger_id: str) -> dict:
    """Get the bags checked in by a passenger and where each bag is going next."""
    bags = [{"bag_tag": tag, **b} for tag, b in BAGS.items() if b["passenger_id"] == passenger_id]
    if not bags:
        return {"error": f"No bags found for passenger {passenger_id}"}
    return {"passenger_id": passenger_id, "bags": bags}


def check_crew_duty(flight_number: str) -> dict:
    """Check each crew member on a flight against their legal duty hours, including the delay.
    Also lists standby crew who could replace someone."""
    f = FLIGHTS.get(flight_number)
    if not f:
        return {"error": f"Flight {flight_number} not found"}
    needed = f["duration_h"] + f["delay_min"] / 60  # hours of duty this flight now needs
    crew = [{"crew_id": cid, "name": c["name"], "role": c["role"], "duty_hours_left": c["duty_hours_left"],
             "hours_needed": needed, "legal": c["duty_hours_left"] >= needed}
            for cid, c in CREW.items() if c["flight"] == flight_number]
    standby = [{"crew_id": cid, "name": c["name"], "role": c["role"], "duty_hours_left": c["duty_hours_left"]}
               for cid, c in CREW.items() if is_standby(c)]
    return {"flight": flight_number, "hours_needed": needed, "crew": crew, "standby": standby}


# ---------- Action tools: every rule is checked here, in code ----------
def rebook_passenger(passenger_id: str, new_flight: str) -> dict:
    """Rebook a passenger onto another flight. The system enforces seat availability,
    minimum connection time, special-assistance priority and minor-safety rules."""
    p = PASSENGERS.get(passenger_id)
    f = FLIGHTS.get(new_flight)
    if not p:
        return {"error": f"Unknown passenger {passenger_id}. Valid IDs: {list(PASSENGERS)}"}
    if not f:
        return {"error": f"Unknown flight {new_flight}. Valid flights: {list(FLIGHTS)}. "
                         f"Use find_alternative_flights first."}
    if p["rebooked_to"]:
        return blocked(f"{passenger_id} is already rebooked to {p['rebooked_to']}", "no action needed")
    hub = FLIGHTS[p["flight"]]["destination"]
    if f["origin"] != hub:
        return blocked(f"{new_flight} does not leave from {hub}, where {p['flight']} lands", "pick another flight")
    if f["destination"] != p["final_destination"]:
        return blocked(f"{new_flight} does not go to {p['final_destination']}", "pick another flight")
    if not misses_connection(p):
        why = (f"{passenger_id}'s trip ends at {hub}" if not p["connection"]
               else f"{passenger_id} still makes their connection {p['connection']}")
        return blocked(f"{why}, so no rebooking is needed", "notify_passengers with a delay notice")

    # Rule: safety-critical -> human. An unaccompanied minor's new itinerary needs a duty manager.
    if p["unaccompanied_minor"]:
        return blocked("unaccompanied minor: rebooking is safety-critical",
                       "escalate_to_duty_manager")

    # Rule: no booking onto full flights.
    if f["seats_left"] <= 0:
        return blocked(f"{new_flight} is full (0 seats left)", "try another flight or escalate_to_duty_manager")

    # Rule: minimum connection time after the delayed arrival.
    gap = minutes_between(new_arrival(p["flight"]), new_departure(new_flight))
    if gap < 0:
        return blocked(f"{new_flight} already departed before {p['flight']} lands", "pick a later flight")
    if gap < MIN_CONNECTION_MIN:
        return blocked(f"only {gap} min to connect, minimum is {MIN_CONNECTION_MIN}", "pick a later flight")

    # Rule: special-assistance passengers get priority when seats are scarce.
    if not p["special_assistance"]:
        waiting = [pid for pid, q in PASSENGERS.items()
                   if q["special_assistance"] and not q["rebooked_to"]
                   and q["final_destination"] == f["destination"] and misses_connection(q)]
        if waiting and f["seats_left"] <= len(waiting):
            return blocked(f"last {f['seats_left']} seat(s) on {new_flight} are reserved for "
                           f"special-assistance passengers {waiting}", "rebook those passengers first")

    f["seats_left"] -= 1
    p["rebooked_to"] = new_flight
    p["notified"] = False  # any earlier notice is now out of date: they must get the new plan
    return {"status": "rebooked", "passenger_id": passenger_id, "new_flight": new_flight,
            "departure": to_text(new_departure(new_flight)), "seats_left_now": f["seats_left"]}


def reroute_baggage(bag_tag: str, new_flight: str) -> dict:
    """Send a bag to a new onward flight. The system checks the bag's owner is booked on
    that flight and that there is enough time to transfer the bag."""
    b = BAGS.get(bag_tag)
    if not b:
        return {"error": f"Unknown bag {bag_tag}. Bag tags look like BAG-001; use get_baggage_status first."}
    if new_flight not in FLIGHTS:
        return {"error": f"Unknown flight {new_flight}. Valid flights: {list(FLIGHTS)}"}
    p = PASSENGERS[b["passenger_id"]]

    # Rule: a bag only flies with its passenger (passenger-bag match).
    if p["rebooked_to"] != new_flight and not (p["connection"] == new_flight and not misses_connection(p)):
        return blocked(f"owner {b['passenger_id']} is not booked on {new_flight}",
                       "rebook the passenger first")

    # Rule: minimum bag connection time.
    gap = minutes_between(new_arrival(b["on_flight"]), new_departure(new_flight))
    if gap < 0:
        return blocked(f"{new_flight} already departed before {b['on_flight']} lands", "pick a later flight")
    if gap < MIN_BAG_CONNECTION_MIN:
        b["status"] = "held at LHR"
        return blocked(f"only {gap} min to transfer the bag, minimum is {MIN_BAG_CONNECTION_MIN}",
                       "bag held; escalate_to_duty_manager to arrange forwarding")

    b["next_flight"] = new_flight
    b["status"] = "rerouted"
    return {"status": "rerouted", "bag_tag": bag_tag, "new_flight": new_flight}


def reassign_crew(flight_number: str, crew_out: str, crew_in: str) -> dict:
    """Replace a crew member on a flight with a standby crew member. The system enforces
    duty-hour limits and role matching; changing a Captain must go to a human."""
    f = FLIGHTS.get(flight_number)
    out, inn = CREW.get(crew_out), CREW.get(crew_in)
    if not f or not out or not inn:
        return {"error": "Unknown flight or crew member"}
    if out["flight"] != flight_number:
        return blocked(f"{crew_out} is not on {flight_number}", "check_crew_duty first")

    # Rule: only replace crew who are actually over their duty limit.
    needed = f["duration_h"] + f["delay_min"] / 60
    if out["duty_hours_left"] >= needed:
        return blocked(f"{crew_out} is within duty hours ({out['duty_hours_left']}h left, needs {needed}h)",
                       "no crew change needed")

    # Rule: safety-critical crew changes go to a human.
    if out["role"] in SAFETY_CRITICAL_ROLES:
        return blocked(f"replacing the {out['role']} is safety-critical", "escalate_to_duty_manager")

    if not is_standby(inn):
        return blocked(f"{crew_in} is not on standby", "pick a standby crew member")
    if inn["role"] != out["role"]:
        return blocked(f"{crew_in} is a {inn['role']}, need a {out['role']}", "pick a matching role")

    # Rule: no crew beyond duty hours.
    if inn["duty_hours_left"] < needed:
        return blocked(f"{crew_in} has {inn['duty_hours_left']}h left, flight needs {needed}h",
                       "escalate_to_duty_manager (no legal replacement)")

    out["flight"], inn["flight"] = None, flight_number
    out["off_duty"] = True  # over the limit: must not show up as standby for another flight
    return {"status": "reassigned", "flight": flight_number, "removed": crew_out, "added": crew_in}


def booking_facts(pid):
    """The true travel facts for one passenger, written by CODE from the live data
    (so a model can never tell a passenger the wrong flight)."""
    p = PASSENGERS[pid]
    lands = to_text(new_arrival(p["flight"]))
    if p["rebooked_to"]:
        f = FLIGHTS[p["rebooked_to"]]
        return (f"{p['flight']} now lands {lands}. Your new connection: {p['rebooked_to']} to "
                f"{f['destination']}, departing {to_text(new_departure(p['rebooked_to']))}.")
    if misses_connection(p):
        return (f"{p['flight']} now lands {lands} and you will miss {p['connection']}. "
                f"Our duty manager is arranging your onward travel; staff will meet you on arrival.")
    return f"{p['flight']} now lands {lands}."


def notify_passengers(passenger_ids: list[str], message: str) -> dict:
    """Send an SMS/email to one or more passengers. The system adds each passenger's
    real flight and booking details automatically; message is only a short friendly
    note (e.g. an apology), so do not put flight details in it."""
    unknown = [pid for pid in passenger_ids if pid not in PASSENGERS]
    if unknown:
        return {"error": f"Unknown passengers: {unknown}"}
    sent = {}
    for pid in passenger_ids:
        p = PASSENGERS[pid]
        # Rule: the model's note may not mention a flight this passenger is not on
        # (any spelling: EK-203, EK203, EK 203), and may not give times, because the
        # real times come from booking_facts() and a model-written time could be wrong.
        allowed = {p["flight"], p["rebooked_to"]}
        mentioned = {f"{a}-{n}" for a, n in re.findall(r"\b([A-Z]{2})\s?-?\s?(\d{2,4})\b", message)}
        wrong = sorted(mentioned - allowed)
        if re.search(r"\b\d{1,2}[:.]\d{2}\b", message):
            wrong.append("a time")
        note = "" if wrong else message
        text = booking_facts(pid) + (f" {note}" if note else "")
        print(f"\n  NOTIFY {p['name']} ({pid})\n  {text}"
              + (f"\n  (agent note removed: it mentioned {', '.join(wrong)})" if wrong else "") + "\n")
        p["notified"] = True
        sent[pid] = text
    return {"status": "sent", "count": len(sent), "messages": sent}


def escalate_to_duty_manager(subject: str, details: str, passenger_ids: list[str],
                             crew_ids: list[str]) -> dict:
    """Send a risky or blocked case to the human duty manager with a clear summary.
    passenger_ids and crew_ids list exactly who the duty manager must handle
    (use [] for none). Only the IDs listed here count as escalated."""
    unknown = [x for x in passenger_ids if x not in PASSENGERS] + [x for x in crew_ids if x not in CREW]
    if unknown:
        return {"error": f"Unknown IDs: {unknown}. Passenger IDs look like P1, crew IDs like CR1."}
    for pid in passenger_ids:
        PASSENGERS[pid]["escalated"] = True
    for cid in crew_ids:
        CREW[cid]["escalated"] = True
    print(f"\n  ESCALATION: {subject}  (passengers {passenger_ids}, crew {crew_ids})\n  {details}\n")
    return {"status": "escalated", "subject": subject, "passenger_ids": passenger_ids, "crew_ids": crew_ids}


# ---------- Logging: every tool call goes into ACTION_LOG ----------
_on_step = None  # optional callback (the Streamlit UI uses it to show steps live)
MODELS_USED = []  # which models answered during the current run (shown in the UI)


def logged(fn):
    """Record every tool call (time, tool, inputs, result, blocked) and turn crashes into errors."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            inspect.signature(fn).bind(*args, **kwargs)  # check argument names before calling
        except TypeError:  # wrong argument names (common with small local models)
            out = {"error": f"Wrong arguments. Correct call: {fn.__name__}{inspect.signature(fn)}"}
        else:
            try:
                out = fn(*args, **kwargs)
            except Exception as e:  # a bug inside the tool: report it as what it is
                out = {"error": f"{fn.__name__} failed: {type(e).__name__}: {e}"}
        is_blocked = isinstance(out, dict) and out.get("blocked", False)
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "tool": fn.__name__,
                 "inputs": kwargs, "result": out, "blocked": is_blocked,
                 "model": MODELS_USED[-1] if MODELS_USED else None}
        ACTION_LOG.append(entry)
        flag = "  [BLOCKED BY RULE]" if is_blocked else ""
        print(f"-> {fn.__name__}({kwargs})\n<- {out}{flag}")
        if _on_step:
            _on_step(entry)
        return out
    return wrapper


TOOL_FUNCS = [logged(f) for f in (
    get_flight_status, get_affected_passengers, find_alternative_flights, rebook_passenger,
    get_baggage_status, reroute_baggage, check_crew_duty, reassign_crew,
    notify_passengers, escalate_to_duty_manager,
)]
TOOLS_BY_NAME = {f.__name__: f for f in TOOL_FUNCS}

SYSTEM = f"""You are the disruption-recovery agent in an airport operations control centre.
All times are UTC. When a flight is disrupted, work through these steps:
1. get_flight_status for the disrupted flight, then get_affected_passengers.
2. For each passenger who misses their connection, special-assistance passengers FIRST:
   find_alternative_flights, rebook_passenger, then get_baggage_status and
   reroute_baggage for each of their bags to the new flight.
3. check_crew_duty for the disrupted flight. If a crew member is not legal,
   reassign_crew with a standby crew member of the same role.
4. notify_passengers: tell each affected passenger their new plan. Passengers whose trip
   ends at the disrupted flight's destination only need a delay notice. One call can
   cover several passengers who get the same message.
5. If a tool result says blocked, never try to work around the rule. If its next_step
   mentions escalate_to_duty_manager, or a passenger cannot be rebooked, escalate with
   a clear summary (who, what, why), including any of their bags that were not rerouted.
   List exactly the passengers and crew being escalated in passenger_ids and crew_ids.
6. In notify_passengers, the message is only a short friendly note: no flight numbers
   or times (the system adds the real ones).
Rules (also enforced by the system): minimum connection {MIN_CONNECTION_MIN} min, bag
transfer {MIN_BAG_CONNECTION_MIN} min, no full flights, no crew beyond duty hours,
special-assistance priority, safety-critical cases go to a human.
Only use the tools. Never invent data.
Finish with a short summary: passengers rebooked, bags rerouted, crew changes,
escalations, and any actions blocked by rules."""

CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM,
    tools=TOOL_FUNCS,
    # We run the tool loop ourselves (same as refund_agent.py) so history survives a failed call.
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)


# ---------- Agent loop (same pattern as refund_agent.py) ----------
def has_content(resp):
    """True if the response has a model turn we can add to the history."""
    return bool(resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts)


def generate_with_fallback(contents, state, retries_per_model=2):
    """One model call. On errors, retry, then move on to the next model.
    state["model_index"] remembers which model works, so later steps start there."""
    while state["model_index"] < len(MODELS):
        model = MODELS[state["model_index"]]
        for attempt in range(1, retries_per_model + 1):
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=CONFIG)
            except Exception as e:
                msg = str(e).replace("\n", " ")[:150]
                print(f"  ({model} attempt {attempt}: {type(e).__name__}: {msg})")
                if "404" in msg or "429" in msg:  # wrong name / quota used up: next model
                    break
                time.sleep(3 * attempt)
                continue
            if has_content(resp):
                return resp
            # Blocked by a safety filter or cut off: no usable turn. Try another model.
            reason = resp.candidates[0].finish_reason if resp.candidates else "no candidates"
            print(f"  ({model} attempt {attempt}: empty response, {reason})")
            break
        state["model_index"] += 1
    return None


# ---------- Local fallback: Ollama ----------
def to_ollama_messages(contents):
    """Convert the Gemini-format history into Ollama chat messages, so a run that
    started on Gemini can continue locally without starting over."""
    messages = [{"role": "system", "content": SYSTEM}]
    for c in contents:
        for part in c.parts:
            if part.text:
                messages.append({"role": "user" if c.role == "user" else "assistant", "content": part.text})
            elif part.function_call:
                messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": part.function_call.name, "arguments": dict(part.function_call.args or {})}}]})
            elif part.function_response:
                messages.append({"role": "tool", "tool_name": part.function_response.name,
                                 "content": json.dumps(part.function_response.response)})
    return messages


def tool_calls_from_text(text):
    """Small local models sometimes WRITE a tool call as JSON text, e.g.
    {"name": "get_order", "parameters": {...}}, instead of making a real call.
    Find those and turn them into real calls."""
    calls, decoder, i = [], json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and obj.get("name") in TOOLS_BY_NAME:
            args = obj.get("parameters") or obj.get("arguments") or {}
            calls.append((obj["name"], args if isinstance(args, dict) else {}))
        i = end
    return calls


def ollama_step(contents):
    """One call to the local Ollama model. Adds its turn to contents (Gemini format)
    and returns (text, calls). Returns None if Ollama is not available."""
    try:
        import ollama  # imported here so the Gemini version works even without it
        resp = ollama.Client(timeout=600).chat(
            model=OLLAMA_MODEL, messages=to_ollama_messages(contents), tools=TOOL_FUNCS,
            options={"temperature": 0, "num_ctx": OLLAMA_CONTEXT},
        )
    except Exception as e:
        print(f"  (ollama {OLLAMA_MODEL}: {type(e).__name__}: {str(e)[:150]})")
        return None
    msg = resp.message
    calls = [(tc.function.name, dict(tc.function.arguments or {})) for tc in (msg.tool_calls or [])]
    if not calls:
        calls = tool_calls_from_text(msg.content or "")
    parts = [types.Part.from_function_call(name=n, args=a) for n, a in calls] or [types.Part(text=msg.content or "")]
    contents.append(types.Content(role="model", parts=parts))
    return msg.content or "", calls


def model_step(contents, state, local_only):
    """Ask the next model what to do. Tries Gemini first (unless local_only), then Ollama.
    Returns (text, calls) or None if no model is available."""
    if not local_only:
        resp = generate_with_fallback(contents, state)
        if resp is not None:
            note_model(MODELS[state["model_index"]])
            contents.append(resp.candidates[0].content)  # keep the model's turn in the history
            calls = [(c.name, dict(c.args or {})) for c in (resp.function_calls or [])]
            return ("" if calls else resp.text), calls
        if not USE_OLLAMA_FALLBACK:
            return None
        if not state.get("switched"):  # say it once, not on every step
            print(f"  (all Gemini models failed, switching to local {OLLAMA_MODEL})")
            state["switched"] = True
    note_model(f"local: {OLLAMA_MODEL}")
    return ollama_step(contents)


def remaining_work():
    """Checklist guard: what is still undone, worked out by CODE from the live data.
    The agent may not finish while this list has items (weak models stop early)."""
    crew_checked = {e["inputs"].get("flight_number") for e in ACTION_LOG if e["tool"] == "check_crew_duty"}
    todo = []
    for fn, f in FLIGHTS.items():
        if f["delay_min"] <= 0:
            continue
        for pid, p in PASSENGERS.items():
            if p["flight"] != fn:
                continue
            if misses_connection(p) and not p["rebooked_to"] and not p["escalated"]:
                todo.append(f"{pid} misses their connection: rebook_passenger, or "
                            f"escalate_to_duty_manager with passenger_ids=['{pid}'] if blocked")
            if p["rebooked_to"]:
                for tag, b in BAGS.items():
                    if b["passenger_id"] == pid and b["next_flight"] != p["rebooked_to"] and b["status"] != "held at LHR":
                        todo.append(f"reroute_baggage {tag} to {p['rebooked_to']}")
            if not p["notified"]:
                todo.append(f"notify_passengers {pid}")
        if fn not in crew_checked:
            todo.append(f"check_crew_duty {fn}")
        needed = f["duration_h"] + f["delay_min"] / 60
        for cid, c in CREW.items():
            if c["flight"] == fn and c["duty_hours_left"] < needed and not c["escalated"]:
                todo.append(f"{cid} is over duty hours on {fn}: reassign_crew, or "
                            f"escalate_to_duty_manager with crew_ids=['{cid}']")
    return todo


def note_model(name):
    if not MODELS_USED or MODELS_USED[-1] != name:
        MODELS_USED.append(name)


def run_agent(message, on_step=None, local_only=False):
    """Run the agent on one disruption message. on_step(entry) is called after each tool call.
    local_only=True skips Gemini and uses the local Ollama model."""
    global _on_step
    _on_step = on_step
    MODELS_USED.clear()
    contents = [types.Content(role="user", parts=[types.Part(text=message)])]
    state = {"model_index": 0}

    nudges_left = 4
    for _ in range(MAX_STEPS_LOCAL if local_only else MAX_STEPS):
        step = model_step(contents, state, local_only)
        if step is None:
            return "Failed: no model available (Gemini and local Ollama). Try again later."
        text, calls = step
        if not calls:
            # Checklist guard: weak models sometimes "finish" while work is still undone,
            # or describe actions they never took. Send them back with a concrete to-do list.
            todo = remaining_work()
            if todo and nudges_left > 0:
                nudges_left -= 1
                print(f"  (guard: {len(todo)} item(s) still undone, sending the model back to work)")
                contents.append(types.Content(role="user", parts=[types.Part(text=(
                    "You are not finished. Do not describe actions: CALL the tools. Still to do:\n- "
                    + "\n- ".join(todo)))]))
                continue
            return text  # no more tools to call: final answer

        results = []
        for name, args in calls:
            fn = TOOLS_BY_NAME.get(name)
            out = fn(**args) if fn else {"error": f"unknown tool {name}"}
            results.append(types.Part.from_function_response(name=name, response=out))
        contents.append(types.Content(role="user", parts=results))

    return "Stopped: too many steps without a final answer."


def summarize_log():
    """Counts for the status cards (used by the terminal and the Streamlit UI)."""
    ok = [e for e in ACTION_LOG if not e["blocked"] and "error" not in e["result"]]
    return {
        "passengers_rebooked": sum(e["tool"] == "rebook_passenger" for e in ok),
        "bags_rerouted": sum(e["tool"] == "reroute_baggage" for e in ok),
        "crew_changes": sum(e["tool"] == "reassign_crew" for e in ok),
        "escalations": sum(e["tool"] == "escalate_to_duty_manager" for e in ok),
        "blocked": [e for e in ACTION_LOG if e["blocked"]],
        "models_used": list(MODELS_USED),
    }


if __name__ == "__main__":
    local = "--local" in sys.argv
    scenario = load_scenario("normal")
    print("=" * 60, f"\nSCENARIO: {scenario['title']}\nMESSAGE: {scenario['message']}")
    print(f"MODE: {'local Ollama ' + OLLAMA_MODEL if local else 'Gemini (local Ollama as fallback)'}\n")
    print("AGENT:", run_agent(scenario["message"], local_only=local))

    s = summarize_log()
    print("=" * 60)
    print(f"Models used: {' -> '.join(s['models_used']) or 'none'}")
    print(f"Rebooked: {s['passengers_rebooked']} | Bags rerouted: {s['bags_rerouted']} | "
          f"Crew changes: {s['crew_changes']} | Escalations: {s['escalations']} | "
          f"Blocked by rule: {len(s['blocked'])}")
    for e in s["blocked"]:
        print(f"  BLOCKED {e['tool']}({e['inputs']}): {e['result']['rule']}")
