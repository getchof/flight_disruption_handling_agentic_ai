"""
AeroTurn - database layer.                                           [CODE, not AI]

Builds aircraft_turnaround_disruption.sqlite with ONE synthetic late-aircraft scenario.
All data is fake, generated with a fixed random seed, so every rebuild is identical.
All times are Dubai local time.

  python db.py   # (re)build the database and print a summary

The scenario
  EK-812 (Bangkok -> Dubai, Boeing 777) lands 75 min late at 10:15.
  - 280 passengers connect in Dubai; 45 now miss their onward flight
    (10 of them are vulnerable: medical, unaccompanied minors, wheelchair, infants).
  - The inbound crew is 20 min away from their legal duty limit.
  - Hydraulic inspection M-1001 is open and needs a mandatory sign-off.
  - The same aircraft should fly EK-813 to Manchester; its gate B12 is taken from 11:00.
"""
import hashlib
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).with_name("aircraft_turnaround_disruption.sqlite")
NOW = datetime(2026, 11, 20, 10, 15)  # scenario clock: EK-812 has just landed
TIME_FMT = "%Y-%m-%d %H:%M"
SEED = 812

SCHEMA = """
CREATE TABLE flights (
    flight_id TEXT PRIMARY KEY, origin TEXT, destination TEXT,
    aircraft_reg TEXT, aircraft_type TEXT, body TEXT,          -- body: WIDE or NARROW
    sched_dep TEXT, est_dep TEXT, sched_arr TEXT, est_arr TEXT,
    block_min INTEGER, gate TEXT, status TEXT);

CREATE TABLE passengers (
    passenger_id TEXT PRIMARY KEY, name TEXT, inbound_flight TEXT,
    original_onward TEXT, final_destination TEXT, tier TEXT, needs TEXT);

-- occupant = a passenger_id, 'EXT' (another traveller not in this scenario) or NULL (free seat)
CREATE TABLE seats (
    flight_id TEXT, seat_no TEXT, occupant TEXT,
    PRIMARY KEY (flight_id, seat_no));
-- Database-level double-booking guard: a passenger can hold at most ONE seat.
CREATE UNIQUE INDEX one_seat_per_passenger ON seats(occupant)
    WHERE occupant IS NOT NULL AND occupant <> 'EXT';

CREATE TABLE bags (bag_tag TEXT PRIMARY KEY, passenger_id TEXT, routed_to TEXT, status TEXT);

-- duty_start NULL = standby crew, duty starts when they are called
CREATE TABLE crew (
    crew_id TEXT PRIMARY KEY, name TEXT, role TEXT, status TEXT, assigned_flight TEXT,
    location TEXT, minutes_away INTEGER, duty_start TEXT);

CREATE TABLE maintenance_log (
    item_id TEXT PRIMARY KEY, aircraft_reg TEXT, description TEXT, mandatory INTEGER,
    status TEXT, signed_off_by TEXT, signed_off_at TEXT);

CREATE TABLE gates (gate_id TEXT PRIMARY KEY, concourse TEXT, wide_body INTEGER);
CREATE TABLE gate_bookings (gate_id TEXT, flight_id TEXT, start TEXT, "end" TEXT);

-- Human-in-the-loop queue: schedule, gate and crew changes wait here for the Operations Controller
CREATE TABLE pending_approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, payload TEXT, reason TEXT,
    status TEXT, requested_by TEXT, requested_at TEXT, decided_by TEXT, decided_at TEXT);

-- The what-if settings this database was built with, plus "now" (the scenario clock = landing time)
CREATE TABLE scenario (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, passenger_id TEXT, text TEXT);

-- actor = AI, CODE or HUMAN, so the demo shows who did what.
-- Tamper-evident: each row stores hash = SHA-256(previous row's hash + this row's content).
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, detail TEXT, outcome TEXT,
    prev_hash TEXT, hash TEXT);
"""

GENESIS = "0" * 64


def _row_hash(prev_hash, ts, actor, action, detail, outcome):
    text = "|".join([prev_hash, ts, actor, action, detail, outcome])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def append_audit(conn, ts, actor, action, detail, outcome):
    """Add one audit row, chained to the previous row by its hash."""
    last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev = last[0] if last else GENESIS
    conn.execute("INSERT INTO audit_log (ts, actor, action, detail, outcome, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                 (ts, actor, action, detail, outcome, prev, _row_hash(prev, ts, actor, action, detail, outcome)))


def verify_audit_chain(conn):
    """Recompute every hash. Returns {"ok", "rows", "broken_at"}: broken_at = first edited row id."""
    prev = GENESIS
    rows = conn.execute("SELECT id, ts, actor, action, detail, outcome, prev_hash, hash FROM audit_log ORDER BY id").fetchall()
    for r in rows:
        if r[6] != prev or _row_hash(prev, *r[1:6]) != r[7]:
            return {"ok": False, "rows": len(rows), "broken_at": r[0]}
        prev = r[7]
    return {"ok": True, "rows": len(rows), "broken_at": None}


def t(hhmm, day=20):
    """'10:45' -> '2026-11-20 10:45'"""
    return f"2026-11-{day:02d} {hhmm}"


# ---------- Flights ----------
# (flight_id, origin, dest, reg, type, body, sched_dep, est_dep, sched_arr, est_arr, block_min, gate, status)
FLIGHTS = [
    # The late inbound aircraft and its next flight
    ("EK-812", "BKK", "DXB", "A6-EQX", "B777-300ER", "WIDE", t("03:25"), t("04:40"), t("09:00"), t("10:15"), 395, "B12", "LANDED"),
    ("EK-813", "DXB", "MAN", "A6-EQX", "B777-300ER", "WIDE", t("11:00"), t("11:00"), t("15:15"), t("15:15"), 465, "B12", "SCHEDULED"),
    # Onward flights the 45 passengers MISS (they leave before 10:15 + 60 min)
    ("EK-201", "DXB", "JFK", "A6-EGA", "B777-300ER", "WIDE", t("10:30"), t("10:30"), t("16:40"), t("16:40"), 850, "A10", "BOARDING"),
    ("EK-001", "DXB", "LHR", "A6-EUB", "A380-800", "WIDE", t("10:45"), t("10:45"), t("14:55"), t("14:55"), 490, "A1", "BOARDING"),
    ("EK-073", "DXB", "CDG", "A6-EPD", "B777-300ER", "WIDE", t("11:00"), t("11:00"), t("15:50"), t("15:50"), 410, "B20", "BOARDING"),
    # Onward flights the other 235 passengers still make
    ("EK-511", "DXB", "BOM", "A6-ENK", "B777-300ER", "WIDE", t("12:00"), t("12:00"), t("16:25"), t("16:25"), 185, "B6", "SCHEDULED"),
    ("EK-384", "DXB", "HKG", "A6-EQA", "B777-300ER", "WIDE", t("12:30"), t("12:30"), t("23:55"), t("23:55"), 445, "C8", "SCHEDULED"),
    ("EK-412", "DXB", "SYD", "A6-EUC", "A380-800", "WIDE", t("13:30"), t("13:30"), t("09:05", 21), t("09:05", 21), 815, "A5", "SCHEDULED"),
    ("EK-061", "DXB", "HAM", "A6-EGB", "B777-300ER", "WIDE", t("14:00"), t("14:00"), t("18:30"), t("18:30"), 410, "B30", "SCHEDULED"),
    # Rebooking options (limited free seats). EK-003 is a trap: it leaves too soon to connect.
    ("EK-003", "DXB", "LHR", "A6-EUD", "A380-800", "WIDE", t("11:10"), t("11:10"), t("15:20"), t("15:20"), 490, "A3", "SCHEDULED"),
    ("EK-005", "DXB", "LHR", "A6-EUE", "A380-800", "WIDE", t("14:30"), t("14:30"), t("18:40"), t("18:40"), 490, "A2", "SCHEDULED"),
    ("EK-007", "DXB", "LHR", "A6-EGC", "B777-300ER", "WIDE", t("16:00"), t("16:00"), t("20:10"), t("20:10"), 490, "B4", "SCHEDULED"),
    ("EK-203", "DXB", "JFK", "A6-EGD", "B777-300ER", "WIDE", t("15:00"), t("15:00"), t("21:10"), t("21:10"), 850, "A12", "SCHEDULED"),
    ("EK-075", "DXB", "CDG", "A6-EPE", "B777-300ER", "WIDE", t("15:30"), t("15:30"), t("20:20"), t("20:20"), 410, "B22", "SCHEDULED"),
    # Extra options used only in long-delay what-if scenarios (Mumbai / Hong Kong connections break too)
    ("EK-513", "DXB", "BOM", "A6-ENL", "B777-300ER", "WIDE", t("17:00"), t("17:00"), t("21:25"), t("21:25"), 185, "C2", "SCHEDULED"),
    ("EK-386", "DXB", "HKG", "A6-EQC", "B777-300ER", "WIDE", t("18:30"), t("18:30"), t("05:55", 21), t("05:55", 21), 445, "C10", "SCHEDULED"),
    ("EK-555", "DXB", "DOH", "A6-EQB", "B777-300ER", "WIDE", t("12:30"), t("12:30"), t("12:45"), t("12:45"), 75, "B12", "SCHEDULED"),
]

# Free seats on each rebooking option. LHR and JFK are SHORT on seats -> priority matters.
FREE_SEATS = {"EK-003": 20, "EK-005": 8, "EK-007": 6, "EK-203": 9, "EK-075": 16, "EK-513": 30, "EK-386": 20}
OTHER_TRAVELLERS = 12  # 'EXT' seats per flight, just so seat maps look realistic

# ---------- The 45 passengers who miss their connection ----------
# (missed flight, destination, count, vulnerable tiers among them)
MISSED = [
    ("EK-001", "LHR", 18, ["MEDA", "UM", "WCHR", "WCHR", "INF"]),
    ("EK-201", "JFK", 12, ["UM", "WCHR", "INF", "INF"]),
    ("EK-073", "CDG", 15, ["WCHR"]),
]
# Passengers who make their connection in the brief's scenario (235). Longer what-if delays can break these too.
MADE = [("EK-511", "BOM", 70, ["WCHR", "INF", "UM"]), ("EK-384", "HKG", 60, ["MEDA", "WCHR"]),
        ("EK-412", "SYD", 55, ["INF"]), ("EK-061", "HAM", 50, ["WCHR"])]

NEEDS = {"MEDA": "medical oxygen on board", "UM": "unaccompanied minor, age 9",
         "WCHR": "wheelchair to aircraft seat", "INF": "travelling with an infant", "STANDARD": ""}

FIRST = ["Aisha", "Omar", "Lena", "Ravi", "Mei", "Lucas", "Sofia", "Yusuf", "Hana", "Daniel", "Priya", "Tom",
         "Fatima", "Chen", "Elena", "Kwame", "Noor", "James", "Maya", "Ali", "Zara", "Ivan", "Leila", "Sam"]
LAST = ["Haddad", "Patel", "Nguyen", "Smith", "Rossi", "Khan", "Garcia", "Okafor", "Müller", "Silva",
        "Tanaka", "Ahmed", "Novak", "Brown", "Kaya", "Dubois", "Mensah", "Larsen", "Costa", "Rahman"]

# ---------- Crew ----------
# Inbound EK-812 crew started duty at 21:35 yesterday: at 10:15 they have 20 min of legal duty left.
INBOUND_DUTY_START = "2026-11-19 21:35"
CREW = [
    # (crew_id, name, role, status, assigned_flight, location, minutes_away, duty_start)
    ("C-101", "Capt. Rana Saeed", "Captain", "INBOUND", "EK-813", "Aircraft A6-EQX", 0, INBOUND_DUTY_START),
    ("C-102", "FO Marco Bianchi", "First Officer", "INBOUND", "EK-813", "Aircraft A6-EQX", 0, INBOUND_DUTY_START),
    ("C-103", "Purser Amina Yusuf", "Purser", "INBOUND", "EK-813", "Aircraft A6-EQX", 0, INBOUND_DUTY_START),
    ("C-104", "CC Leo Park", "Cabin Crew", "INBOUND", "EK-813", "Aircraft A6-EQX", 0, INBOUND_DUTY_START),
    # Standby (reserve) crew. S-205 has already worked 9 h today, so is NOT legal for EK-813.
    ("S-201", "Capt. Hassan Ali", "Captain", "STANDBY", None, "Concourse B crew room", 8, None),
    ("S-202", "Capt. Grace Lee", "Captain", "STANDBY", None, "Crew hotel, Al Qusais", 45, None),
    ("S-203", "FO Nadia Rahman", "First Officer", "STANDBY", None, "Concourse B crew room", 8, None),
    ("S-204", "Purser Diego Costa", "Purser", "STANDBY", None, "Concourse A crew room", 15, None),
    ("S-205", "FO Ken Ito", "First Officer", "STANDBY", None, "Concourse B crew room", 8, "2026-11-20 01:15"),
    ("S-206", "CC Sara Mensah", "Cabin Crew", "STANDBY", None, "Concourse B crew room", 8, None),
    ("S-207", "CC Ola Novak", "Cabin Crew", "STANDBY", None, "Concourse C crew room", 20, None),
]

MAINTENANCE = [
    ("M-1001", "A6-EQX", "Hydraulic system B pressure loss - inspection required", 1, "OPEN", None, None),
    ("M-1002", "A6-EQX", "Cabin reading light 34C inoperative (deferred, MEL)", 0, "DEFERRED", None, None),
]

GATES = [("B12", "B", 1), ("A3", "A", 1), ("A4", "A", 1), ("C40", "C", 0), ("B14", "B", 1)]
GATE_BOOKINGS = [
    ("B12", "EK-813", t("09:30"), t("11:00")),
    ("B12", "EK-555", t("11:00"), t("12:30")),   # EK-813 cannot stay at B12 if it leaves late
    ("A3", "EK-003", t("09:40"), t("11:10")),
    ("A4", "EK-019", t("10:30"), t("13:30")),
    ("B14", "EK-431", t("12:00"), t("14:00")),   # free until 12:00
]


def seat_numbers(n):
    """Seat labels like 10A, 10B, ... (9 per row)."""
    letters = "ABCDEFGHK"
    return [f"{10 + i // len(letters)}{letters[i % len(letters)]}" for i in range(n)]


# ---------- What-if scenario settings (the defaults ARE the brief's scenario) ----------
DEFAULT_SCENARIO = {
    "delay_min": 75,             # how late EK-812 lands (scheduled 09:00)
    "crew_duty_left_min": 20,    # legal duty the inbound crew has left at landing
    "maintenance_open": True,    # is hydraulic inspection M-1001 still open?
    "seats_factor": 1.0,         # x free seats on the rebooking options
}
EK812_SCHED_ARR = "2026-11-20 09:00"


def build(path=DB_PATH, **settings):
    """Delete and rebuild the database for a scenario. With no settings it is exactly the
    brief's scenario (75 min late, crew 20 min from the limit, M-1001 open). Returns the path."""
    s = {**DEFAULT_SCENARIO, **settings}
    landed = to_time(EK812_SCHED_ARR) + timedelta(minutes=int(s["delay_min"]))   # = the scenario clock
    path = Path(path)
    rng = random.Random(SEED)
    conn = sqlite3.connect(path)
    # Empty the database IN PLACE (Windows cannot delete a file the console still has open)
    old = conn.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table','index') "
                       "AND name NOT LIKE 'sqlite_%'").fetchall()
    for kind, name in sorted(old, key=lambda r: r[0] != "index"):   # indexes first, then tables
        conn.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
    conn.commit()
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO scenario VALUES (?,?)",
                     [(k, str(v)) for k, v in s.items()] + [("now", landed.strftime(TIME_FMT))])

    flights = []
    for f in FLIGHTS:
        if f[0] == "EK-812":   # the late aircraft: shift its departure and arrival by the delay
            dep = to_time(f[6]) + timedelta(minutes=int(s["delay_min"]))
            f = f[:7] + (dep.strftime(TIME_FMT), f[8], landed.strftime(TIME_FMT)) + f[10:]
        flights.append(f)
    conn.executemany("INSERT INTO flights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", flights)

    # Passengers: 45 missed (with fixed vulnerable tiers) + 235 who make it
    passengers, n = [], 0
    used_names = set()

    def new_name():
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if name not in used_names:
                used_names.add(name)
                return name

    for flight, dest, count, tiers in MISSED:
        for i in range(count):
            n += 1
            tier = tiers[i] if i < len(tiers) else "STANDARD"
            passengers.append((f"P{n:03d}", new_name(), "EK-812", flight, dest, tier, NEEDS[tier]))
    for flight, dest, count, tiers in MADE:
        for i in range(count):
            n += 1
            tier = tiers[i] if i < len(tiers) else "STANDARD"
            passengers.append((f"P{n:03d}", new_name(), "EK-812", flight, dest, tier, NEEDS[tier]))
    rng.shuffle(passengers)  # so vulnerable passengers are not simply first in the list
    conn.executemany("INSERT INTO passengers VALUES (?,?,?,?,?,?,?)", passengers)

    # Seats: every passenger holds their original onward seat; options have a few free seats
    onward = {}
    for p in passengers:
        onward.setdefault(p[3], []).append(p[0])
    for flight_id in [f[0] for f in FLIGHTS if f[1] == "DXB"]:
        holders = sorted(onward.get(flight_id, []))
        free = round(FREE_SEATS.get(flight_id, 0) * float(s["seats_factor"]))
        labels = seat_numbers(len(holders) + OTHER_TRAVELLERS + free)
        occupants = holders + ["EXT"] * OTHER_TRAVELLERS + [None] * free
        conn.executemany("INSERT INTO seats VALUES (?,?,?)", zip([flight_id] * len(labels), labels, occupants))

    # Bags: 1 or 2 per passenger, routed to their original onward flight
    bags, b = [], 0
    for p in sorted(passengers):
        for _ in range(rng.choice([1, 1, 2])):
            b += 1
            bags.append((f"BAG-{b:04d}", p[0], p[3], "IN_TRANSFER"))
    conn.executemany("INSERT INTO bags VALUES (?,?,?,?)", bags)

    # Crew duty is set RELATIVE to landing: inbound crew have exactly crew_duty_left_min left,
    # standby FO Ken Ito has always already worked 9 hours.
    max_duty = 13 * 60
    crew = []
    for c in CREW:
        if c[3] == "INBOUND":
            start = landed - timedelta(minutes=max_duty - int(s["crew_duty_left_min"]))
            c = c[:7] + (start.strftime(TIME_FMT),)
        elif c[0] == "S-205":
            c = c[:7] + ((landed - timedelta(hours=9)).strftime(TIME_FMT),)
        crew.append(c)
    conn.executemany("INSERT INTO crew VALUES (?,?,?,?,?,?,?,?)", crew)

    maintenance = list(MAINTENANCE)
    if str(s["maintenance_open"]) in ("False", "0"):   # what-if: inspection already signed off
        maintenance[0] = MAINTENANCE[0][:4] + ("SIGNED_OFF", "Eng. on duty (before arrival)", landed.strftime(TIME_FMT))
    conn.executemany("INSERT INTO maintenance_log VALUES (?,?,?,?,?,?,?)", maintenance)
    conn.executemany("INSERT INTO gates VALUES (?,?,?)", GATES)
    conn.executemany("INSERT INTO gate_bookings VALUES (?,?,?,?)", GATE_BOOKINGS)
    append_audit(conn, landed.strftime(TIME_FMT), "CODE", "database_built", f"seed {SEED}, scenario {s}", "ok")
    conn.commit()
    conn.close()
    return path


def connect(path=DB_PATH):
    """Open the database. isolation_level=None means WE control transactions with explicit
    BEGIN / COMMIT / ROLLBACK (see tools.py), exactly as the brief asks."""
    if not Path(path).exists():
        build(path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def to_time(text):
    return datetime.strptime(text, TIME_FMT)


def scenario(conn):
    """The settings this database was built with. s["now"] is the scenario clock (a datetime)."""
    try:
        rows = dict(conn.execute("SELECT key, value FROM scenario").fetchall())
    except sqlite3.OperationalError:          # database from before the what-if feature
        rows = {}
    s = {**DEFAULT_SCENARIO, **rows}
    return {"delay_min": int(s["delay_min"]), "crew_duty_left_min": int(s["crew_duty_left_min"]),
            "maintenance_open": str(s["maintenance_open"]) not in ("False", "0"),
            "seats_factor": float(s["seats_factor"]),
            "now": to_time(rows["now"]) if "now" in rows else NOW}


# ---------- Integrity checks (used by tests and shown live in the console) ----------
def double_bookings(conn):
    """Passengers holding more than one seat. Must always be empty."""
    return [r["occupant"] for r in conn.execute(
        "SELECT occupant FROM seats WHERE occupant IS NOT NULL AND occupant <> 'EXT' "
        "GROUP BY occupant HAVING COUNT(*) > 1")]


def orphan_bags(conn):
    """Bags routed to a flight their passenger has no seat on. Must always be empty."""
    return [r["bag_tag"] for r in conn.execute(
        "SELECT b.bag_tag FROM bags b WHERE b.status <> 'HELD' AND NOT EXISTS ("
        "  SELECT 1 FROM seats s WHERE s.occupant = b.passenger_id AND s.flight_id = b.routed_to)")]


def missed_connections(conn):
    """Passengers whose onward flight leaves before they can connect (rule from guards.py)."""
    from guards import check_connection
    arrival = {r["flight_id"]: to_time(r["est_arr"]) for r in conn.execute("SELECT flight_id, est_arr FROM flights")}
    rows = conn.execute(
        "SELECT p.*, f.est_dep FROM passengers p JOIN seats s ON s.occupant = p.passenger_id "
        "JOIN flights f ON f.flight_id = s.flight_id WHERE s.flight_id = p.original_onward").fetchall()
    return [dict(r) for r in rows if check_connection(arrival[r["inbound_flight"]], to_time(r["est_dep"]))]


if __name__ == "__main__":
    build()
    conn = connect()
    count = lambda sql: conn.execute(sql).fetchone()[0]
    missed = missed_connections(conn)
    print(f"Built {DB_PATH.name}")
    print(f"  passengers on EK-812 : {count('SELECT COUNT(*) FROM passengers')}")
    print(f"  missed connections   : {len(missed)} "
          f"({sum(m['tier'] != 'STANDARD' for m in missed)} vulnerable)")
    print(f"  bags                 : {count('SELECT COUNT(*) FROM bags')}")
    print(f"  free seats on options: " + ", ".join(
        f"{r[0]} {r[1]}" for r in conn.execute(
            "SELECT flight_id, COUNT(*) FROM seats WHERE occupant IS NULL GROUP BY flight_id")))
    open_items = count("SELECT COUNT(*) FROM maintenance_log WHERE mandatory=1 AND status<>'SIGNED_OFF'")
    print(f"  open mandatory maint.: {open_items}")
    print(f"  double bookings      : {len(double_bookings(conn))}")
    print(f"  orphan bags          : {len(orphan_bags(conn))}")
