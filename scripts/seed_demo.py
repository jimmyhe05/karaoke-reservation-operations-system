"""Seed a week of mock reservations for demos, starting on a given date.

Usage:
    python scripts/seed_demo.py                      # 7 days starting 2026-10-28
    python scripts/seed_demo.py --start 2026-11-05 --days 7
    python scripts/seed_demo.py --dry-run            # validate and print, write nothing
    python scripts/seed_demo.py --remove             # delete only the demo rows

The target database is whatever services.db resolves: DATABASE_URL (Postgres)
if set, otherwise the SQLite file in DATABASE.

Safety:
- Only rows owned by the demo customers below (fixed @example.com addresses)
  are touched. Re-running replaces the previous demo rows and nothing else.
- Confirmed bookings are checked with the same find_conflict() the API uses,
  so a demo row never overlaps a real confirmed booking. A conflicting demo
  row is skipped and reported.
- One pending request deliberately overlaps a confirmed booking so approving
  it in the staff dashboard shows the 409 conflict path.
- Everything runs in one transaction; --dry-run rolls it back.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from werkzeug.security import generate_password_hash  # noqa: E402

from config import Config  # noqa: E402
from services.db import get_db, is_postgres_connection  # noqa: E402
from services.pricing import calculate_cost  # noqa: E402
from services.validation import find_conflict, normalize_time_range  # noqa: E402

DEFAULT_START = "2026-10-28"

MAYA = "maya.chen@example.com"
DANIEL = "daniel.ortiz@example.com"
PRIYA = "priya.raman@example.com"
KEVIN = "kevin.liu@example.com"
EMMA = "emma.brooks@example.com"
JORDAN = "jordan.smith@example.com"

DEMO_CUSTOMERS = {
    MAYA: ("Maya Chen", "608-555-0101"),
    DANIEL: ("Daniel Ortiz", "608-555-0102"),
    PRIYA: ("Priya Raman", "608-555-0103"),
    KEVIN: ("Kevin Liu", "608-555-0104"),
    EMMA: ("Emma Brooks", "608-555-0105"),
    JORDAN: ("Jordan Smith", "608-555-0106"),
}


@dataclass(frozen=True)
class DemoBooking:
    day: int  # offset from --start
    room_id: int
    start: str  # HH:MM, 24-25h allowed for after midnight
    end: str
    people: int
    email: str
    status: str
    notes: str
    language: str = "en"
    expect_conflict: bool = False  # pending row that intentionally overlaps a confirmed one


B = DemoBooking

# fmt: off
SCHEDULE = [
    # Day 0: a busy weekday with work for staff to do
    B(0, 1, "13:00", "15:00", 4, MAYA, "confirmed", "Lunch break session, ordering appetizers."),
    B(0, 2, "18:00", "20:00", 6, DANIEL, "confirmed", "Team dinner after work. Needs HDMI for slides."),
    B(0, 3, "19:00", "21:30", 8, PRIYA, "confirmed", "Birthday party, bringing a cake."),
    B(0, 1, "20:00", "22:00", 3, KEVIN, "pending", "Wants the Mandarin pop catalog.", "zh"),
    B(0, 2, "19:00", "21:00", 5, EMMA, "pending", "Flexible on room if Room 2 is taken.",
      expect_conflict=True),
    B(0, 3, "22:00", "24:30", 4, JORDAN, "pending", "Late session after a concert."),
    # Day 1
    B(1, 1, "17:00", "19:00", 2, EMMA, "confirmed", "Anniversary, quiet room preferred."),
    B(1, 2, "20:00", "23:00", 7, JORDAN, "confirmed", "College club social."),
    B(1, 3, "15:00", "17:00", 3, MAYA, "cancelled", "Cancelled by customer, schedule change."),
    # Day 2: Friday-style peak with overnight bookings
    B(2, 1, "18:00", "21:00", 8, DANIEL, "confirmed", "Company happy hour."),
    B(2, 1, "21:30", "25:00", 6, PRIYA, "pending", "Weekend late session."),
    B(2, 2, "19:00", "22:00", 6, KEVIN, "confirmed", "Family gathering.", "zh"),
    B(2, 3, "20:00", "22:00", 4, MAYA, "rejected",
      "Asked to bring outside liquor; venue policy does not allow it."),
    B(2, 3, "22:30", "25:00", 5, JORDAN, "confirmed", "Late night after the game."),
    # Day 3: Saturday
    B(3, 1, "14:00", "16:00", 5, EMMA, "confirmed", "Kids costume party, family friendly songs."),
    B(3, 1, "19:00", "22:00", 8, PRIYA, "confirmed", "Halloween costume party."),
    B(3, 2, "18:00", "20:00", 4, DANIEL, "pending", "Friends visiting from Chicago."),
    B(3, 2, "20:30", "23:30", 6, KEVIN, "confirmed", "Cousin's farewell party.", "zh"),
    B(3, 3, "21:00", "24:00", 7, JORDAN, "pending", "Halloween late session."),
    # Days 4 to 6: quieter
    B(4, 2, "12:00", "14:00", 4, MAYA, "confirmed", "Sunday brunch singalong."),
    B(4, 3, "16:00", "18:00", 3, KEVIN, "pending", "Practice for a wedding performance.", "zh"),
    B(5, 1, "19:00", "21:00", 4, EMMA, "pending", "Book club celebration."),
    B(6, 2, "18:30", "20:30", 5, DANIEL, "confirmed", "Team offsite wrap-up."),
]
# fmt: on


def _insert_returning_id(conn, sql, params):
    if is_postgres_connection(conn):
        return conn.execute(sql + " RETURNING id", params).fetchone()["id"]
    return conn.execute(sql, params).lastrowid


def _placeholders(n):
    return ", ".join("?" for _ in range(n))


def remove_demo_rows(conn):
    emails = list(DEMO_CUSTOMERS)
    ph = _placeholders(len(emails))
    ids = [
        r["id"] for r in conn.execute(f"SELECT id FROM reservations WHERE contact_email IN ({ph})", emails).fetchall()
    ]
    if ids:
        id_ph = _placeholders(len(ids))
        conn.execute(f"DELETE FROM notifications WHERE reservation_id IN ({id_ph})", ids)
        conn.execute(f"DELETE FROM reservation_history WHERE reservation_id IN ({id_ph})", ids)
        conn.execute(f"DELETE FROM idle_reservations WHERE reservation_id IN ({id_ph})", ids)
        conn.execute(f"DELETE FROM reservations WHERE id IN ({id_ph})", ids)
    return len(ids)


def ensure_demo_users(conn, password):
    users = {}
    for email, (name, _phone) in DEMO_CUSTOMERS.items():
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            users[email] = row["id"]
            continue
        pw_hash = generate_password_hash(password) if password else None
        users[email] = _insert_returning_id(
            conn,
            "INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)",
            (email, pw_hash, name),
        )
    return users


def seed(conn, start, user_ids, days):
    rooms = {r["id"]: r["capacity"] for r in conn.execute("SELECT id, capacity FROM rooms").fetchall()}
    inserted, skipped = [], []
    tax_rate = getattr(Config, "TAX_RATE", 0.055)

    # Confirmed rows first so pending rows are judged against the full confirmed set.
    for b in sorted(SCHEDULE, key=lambda b: (b.status != "confirmed", b.day, b.room_id, b.start)):
        if b.day >= days:
            continue
        day = (start + timedelta(days=b.day)).isoformat()
        label = f"{day} R{b.room_id} {b.start}-{b.end} {b.status}"

        if b.room_id not in rooms:
            skipped.append((label, f"room {b.room_id} does not exist"))
            continue
        if b.people > rooms[b.room_id]:
            skipped.append((label, f"{b.people} people exceeds capacity {rooms[b.room_id]}"))
            continue
        try:
            n_start, n_end, _, _ = normalize_time_range(day, b.start, b.end)
        except ValueError as exc:
            skipped.append((label, str(exc)))
            continue

        conflict = find_conflict(conn, b.room_id, day, n_start, n_end)
        if b.status == "confirmed" and conflict:
            skipped.append((label, f"overlaps confirmed reservation {conflict['id']}"))
            continue
        if b.expect_conflict and not conflict:
            print(f"  note: {label} was meant to conflict but the slot is free")

        name, phone = DEMO_CUSTOMERS[b.email]
        total = calculate_cost(conn, b.room_id, n_start, n_end, tax_rate)
        requested_at = datetime.now(timezone.utc).replace(tzinfo=None) if b.status == "pending" else None
        cols = (
            "date, start_time, end_time, num_people, contact_name, contact_phone, contact_email, "
            "room_id, total_cost, language, notes, status, user_id, requested_at"
        )
        params = (
            day, n_start, n_end, b.people, name, phone, b.email,
            b.room_id, total, b.language, b.notes, b.status, user_ids[b.email], requested_at,
        )  # fmt: skip
        res_id = _insert_returning_id(conn, f"INSERT INTO reservations ({cols}) VALUES ({_placeholders(14)})", params)
        conn.execute(
            "INSERT INTO reservation_history (reservation_id, action, snapshot) VALUES (?, ?, ?)",
            (res_id, "seeded_demo", json.dumps({"status": b.status, "date": day, "room_id": b.room_id})),
        )
        inserted.append((res_id, label, total, "conflicts on approve" if b.expect_conflict and conflict else ""))
    return inserted, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default=DEFAULT_START, help="first day, YYYY-MM-DD (default %(default)s)")
    parser.add_argument("--days", type=int, default=7, help="number of days to fill, 1 to 7 (default %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="validate and print, then roll back")
    parser.add_argument("--remove", action="store_true", help="delete the demo rows and exit")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    password = os.getenv("DEMO_CUSTOMER_PASSWORD")

    conn = get_db()
    target = "PostgreSQL (DATABASE_URL)" if is_postgres_connection(conn) else f"SQLite ({Config.DATABASE})"
    print(f"Target: {target}")
    try:
        removed = remove_demo_rows(conn)
        print(f"Removed {removed} previous demo reservation(s).")
        if args.remove:
            inserted, skipped = [], []
        else:
            user_ids = ensure_demo_users(conn, password)
            inserted, skipped = seed(conn, start, user_ids, max(1, min(args.days, 7)))

        for res_id, label, total, note in sorted(inserted, key=lambda x: x[1]):
            print(f"  + #{res_id:<5} {label:<42} ${total:>7.2f} {note}")
        for label, reason in skipped:
            print(f"  - skipped {label}: {reason}")

        if args.dry_run:
            conn.rollback()
            print(f"Dry run: {len(inserted)} would be inserted, {len(skipped)} skipped. Nothing written.")
        else:
            conn.commit()
            print(f"Done: {len(inserted)} inserted, {len(skipped)} skipped.")
        if not password and not args.remove:
            print("Demo customers have no password (set DEMO_CUSTOMER_PASSWORD before the first run to allow login).")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
