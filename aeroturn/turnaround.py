"""
AeroTurn - below-wing turnaround critical-path tracker.               [CODE, not AI]

Classic Critical Path Method (CPM): each ground task has a duration and the tasks it
must wait for. A forward pass gives the earliest start/finish of every task, a
backward pass gives the latest, and tasks with zero slack form the CRITICAL PATH:
delay any of them and the departure moves. The earliest departure is calculated
here, not guessed by the AI.
"""
from datetime import timedelta

from guards import LIMITS

# (task id, label, minutes, must wait for, team)
# minutes=None means it is computed (crew: travel time + briefing)
TASKS = [
    ("deboard", "Deboard passengers", 25, [], "Ground ops"),
    ("unload", "Unload & transfer bags", 30, [], "Ramp"),
    ("inspect", "Hydraulic B inspection (M-1001)", 40, [], "Engineering"),
    ("clean", "Cabin cleaning", 25, ["deboard"], "Cleaning"),
    ("cater", "Catering", 35, ["deboard"], "Catering"),
    ("fuel", "Fuelling", 30, ["deboard"], "Fuel"),
    ("crew", "Crew report & briefing", None, [], "Crew"),
    ("load", "Load bags", 25, ["unload"], "Ramp"),
    ("board", "Boarding", LIMITS.BOARDING_MIN, ["clean", "cater", "fuel", "crew"], "Ground ops"),
    ("close", "Doors closed / push-back", 0, ["board", "load", "inspect"], "Flight deck"),
]


def plan(on_block, crew_minutes_away, maintenance_signed):
    """Critical-path plan for the turnaround.
    on_block: landing time. crew_minutes_away: how far the (legal) crew is, None = no legal crew.
    Returns {"tasks": [...], "departure": datetime or None, "critical_path": [...]}."""
    duration = {}
    for tid, _, minutes, _, _ in TASKS:
        duration[tid] = minutes if minutes is not None else (
            (crew_minutes_away + LIMITS.CREW_BRIEFING_MIN) if crew_minutes_away is not None else 0)
    deps = {t[0]: t[3] for t in TASKS}

    # Forward pass: earliest start / earliest finish (minutes after on-block)
    es, ef = {}, {}
    for tid, *_ in TASKS:                       # TASKS is listed in dependency order
        es[tid] = max((ef[d] for d in deps[tid]), default=0)
        ef[tid] = es[tid] + duration[tid]
    end = ef["close"]

    # Backward pass: latest finish / latest start
    lf, ls = {}, {}
    for tid, *_ in reversed(TASKS):
        followers = [t for t, d in deps.items() if tid in d]
        lf[tid] = min((ls[f] for f in followers), default=end)
        ls[tid] = lf[tid] - duration[tid]

    tasks = []
    for tid, label, _, _, team in TASKS:
        status = "PLANNED"
        if tid == "crew" and crew_minutes_away is None:
            status = "BLOCKED: no legal crew"
        if tid == "inspect":
            status = "SIGNED OFF" if maintenance_signed else "NEEDS SIGN-OFF"
        tasks.append({"id": tid, "label": label, "team": team, "minutes": duration[tid],
                      "start": on_block + timedelta(minutes=es[tid]), "end": on_block + timedelta(minutes=ef[tid]),
                      "slack_min": ls[tid] - es[tid], "critical": ls[tid] == es[tid] and duration[tid] > 0,
                      "status": status})
    departure = on_block + timedelta(minutes=end) if crew_minutes_away is not None else None
    return {"tasks": tasks, "departure": departure,
            "critical_path": [t["label"] for t in tasks if t["critical"]]}
