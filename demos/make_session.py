# ruff: noqa: E501  (fixture data: log lines and source files are kept at their natural width)
"""Write demos/session.json: a synthetic Claude Code-style session debugging a booking API.

    python3 demos/make_session.py demos/session.json

The input of demos/session.gif. The story is an ordinary debugging session: the goal, a failing
test run whose `KeyError: 'booking_id'` line matters later, the files read (some more than once),
two fixes, re-runs and a follow-up request. Nothing here is shaped around what lcc removes; the
output is deterministic, so regenerating it reproduces the committed file byte for byte.
"""

import json
import sys

SERIALIZERS_V1 = '''"""Response serializers for the bookings API."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.models import Booking, Guest, Room

ISO = "%Y-%m-%dT%H:%M:%SZ"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime(ISO)


def _money(amount: Decimal, currency: str) -> dict[str, Any]:
    return {"amount": str(amount.quantize(Decimal("0.01"))), "currency": currency}


def serialize_guest(guest: Guest) -> dict[str, Any]:
    return {
        "id": guest.id,
        "name": guest.full_name,
        "email": guest.email,
    }


def serialize_room(room: Room) -> dict[str, Any]:
    return {"id": room.id, "number": room.number, "type": room.room_type}


def serialize_booking(booking: Booking) -> dict[str, Any]:
    payload = booking.to_payload()
    return {
        "booking_id": payload["id"],
        "status": payload["status"],
        "check_in": _iso(booking.check_in),
        "check_out": _iso(booking.check_out),
        "guest": serialize_guest(booking.guest),
        "room": serialize_room(booking.room),
        "total": _money(booking.total, booking.currency),
    }


def serialize_confirmation(booking: Booking) -> dict[str, Any]:
    payload = booking.to_payload()
    return {
        "booking_id": payload["booking_id"],
        "status": "confirmed",
        "confirmed_at": _iso(booking.confirmed_at),
        "confirmation_code": booking.confirmation_code,
        "total": _money(booking.total, booking.currency),
    }


def serialize_cancellation(booking: Booking, refund: Decimal) -> dict[str, Any]:
    payload = booking.to_payload()
    return {
        "booking_id": payload["id"],
        "status": "cancelled",
        "cancelled_at": _iso(booking.cancelled_at),
        "refund": _money(refund, booking.currency),
    }


def serialize_availability(rooms: list[Room], nights: int) -> dict[str, Any]:
    return {
        "nights": nights,
        "rooms": [serialize_room(room) for room in rooms],
        "count": len(rooms),
    }
'''

BOOKINGS_V1 = '''"""HTTP handlers for /bookings."""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException

from app.db import Session, get_session
from app.models import Booking, BookingStatus
from app.payments import capture_deposit, refund_deposit
from app.serializers import (
    serialize_booking,
    serialize_cancellation,
    serialize_confirmation,
)

router = APIRouter(prefix="/bookings", tags=["bookings"])
log = logging.getLogger(__name__)


def _load(session: Session, booking_id: int) -> Booking:
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="booking not found")
    return booking


@router.get("/{booking_id}")
def get_booking(booking_id: int, session: Session = Depends(get_session)):
    return serialize_booking(_load(session, booking_id))


@router.post("", status_code=201)
def create_booking(body: dict, session: Session = Depends(get_session)):
    booking = Booking.from_request(body)
    session.add(booking)
    session.commit()
    log.info("created booking %s", booking.id)
    return serialize_booking(booking)


def _already_confirmed(session: Session, booking: Booking) -> bool:
    existing = session.confirmations.latest_for(booking.id)
    if existing is None:
        return False
    return existing["booking_id"] == booking.id


@router.post("/{booking_id}/confirm")
def confirm_booking(booking_id: int, session: Session = Depends(get_session)):
    booking = _load(session, booking_id)
    if _already_confirmed(session, booking):
        raise HTTPException(status_code=409, detail="booking already confirmed")
    if booking.status is not BookingStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"cannot confirm {booking.status.value}")
    capture_deposit(booking)
    booking.confirm()
    session.confirmations.record(booking.to_payload())
    session.commit()
    log.info("confirmed booking %s", booking.id)
    return serialize_confirmation(booking)


@router.post("/{booking_id}/cancel")
def cancel_booking(booking_id: int, session: Session = Depends(get_session)):
    booking = _load(session, booking_id)
    if booking.status is BookingStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="booking already cancelled")
    refund = refund_deposit(booking) if booking.status is BookingStatus.CONFIRMED else Decimal(0)
    booking.cancel()
    session.commit()
    log.info("cancelled booking %s (refund %s)", booking.id, refund)
    return serialize_cancellation(booking, refund)
'''

TESTS_V1 = '''from decimal import Decimal

import pytest

from app.models import BookingStatus


@pytest.fixture
def pending(client, booking_factory):
    return booking_factory(status=BookingStatus.PENDING, total=Decimal("240.00"))


def test_get_booking(client, pending):
    response = client.get(f"/bookings/{pending.id}")
    assert response.status_code == 200
    assert response.json()["booking_id"] == pending.id


def test_create_booking(client, guest, room):
    body = {"guest_id": guest.id, "room_id": room.id, "check_in": "2026-11-02", "nights": 2}
    response = client.post("/bookings", json=body)
    assert response.status_code == 201
    assert response.json()["status"] == "pending"


def test_confirm_booking(client, pending):
    response = client.post(f"/bookings/{pending.id}/confirm")
    assert response.status_code == 200
    data = response.json()
    assert data["booking_id"] == pending.id
    assert data["status"] == "confirmed"
    assert data["total"] == {"amount": "240.00", "currency": "EUR"}


def test_confirm_twice_returns_409(client, pending):
    assert client.post(f"/bookings/{pending.id}/confirm").status_code == 200
    response = client.post(f"/bookings/{pending.id}/confirm")
    assert response.status_code == 409


def test_cancel_confirmed_refunds(client, pending):
    client.post(f"/bookings/{pending.id}/confirm")
    response = client.post(f"/bookings/{pending.id}/cancel")
    assert response.status_code == 200
    assert response.json()["refund"]["amount"] == "240.00"


def test_cancel_pending_no_refund(client, pending):
    response = client.post(f"/bookings/{pending.id}/cancel")
    assert response.json()["refund"]["amount"] == "0.00"
'''

SERIALIZERS_V2 = SERIALIZERS_V1.replace(
    '        "booking_id": payload["booking_id"],\n        "status": "confirmed",',
    '        "booking_id": payload["id"],\n        "status": "confirmed",',
)
BOOKINGS_V2 = BOOKINGS_V1.replace(
    'return existing["booking_id"] == booking.id', 'return existing["id"] == booking.id'
)
NEW_TEST = '''


def test_confirm_twice_keeps_first_confirmation(client, pending):
    first = client.post(f"/bookings/{pending.id}/confirm").json()
    second = client.post(f"/bookings/{pending.id}/confirm")
    assert second.status_code == 409
    again = client.get(f"/bookings/{pending.id}").json()
    assert again["status"] == "confirmed"
    assert first["confirmation_code"]'''
TESTS_V2 = TESTS_V1.replace(
    "\n\n\ndef test_cancel_confirmed_refunds",
    NEW_TEST + "\n\n\ndef test_cancel_confirmed_refunds",
)
assert SERIALIZERS_V2 != SERIALIZERS_V1 and BOOKINGS_V2 != BOOKINGS_V1 and TESTS_V2 != TESTS_V1


def numbered(text: str) -> str:
    """Claude Code's Read format: right-aligned line number, an arrow, the line."""
    return "\n".join(f"{n:>6}→{line}" for n, line in enumerate(text.rstrip("\n").split("\n"), 1))


def line_of(text: str, needle: str) -> int:
    for n, line in enumerate(text.split("\n"), 1):
        if needle in line:
            return n
    raise SystemExit(f"missing {needle!r}")


SER_LINE = line_of(SERIALIZERS_V1, 'payload["booking_id"]')
BOOK_LINE = line_of(BOOKINGS_V1, 'existing["booking_id"]')

MODULES = [
    ("tests/test_availability.py", ["test_search_single_night", "test_search_spans_month",
     "test_search_excludes_maintenance", "test_search_respects_capacity",
     "test_search_rejects_past_dates", "test_search_rejects_inverted_range",
     "test_nights_counted_in_hotel_tz", "test_blackout_dates", "test_overlap_detection_edges"]),
    ("tests/test_bookings.py", ["test_get_booking", "test_create_booking",
     "test_confirm_booking", "test_confirm_twice_returns_409",
     "test_cancel_confirmed_refunds", "test_cancel_pending_no_refund"]),
    ("tests/test_guests.py", ["test_create_guest", "test_guest_email_normalised",
     "test_guest_duplicate_email_409", "test_guest_lookup_by_email", "test_guest_gdpr_export",
     "test_guest_gdpr_erase"]),
    ("tests/test_payments.py", ["test_capture_deposit", "test_capture_declined",
     "test_capture_idempotent", "test_refund_full", "test_refund_partial",
     "test_refund_after_checkout_rejected", "test_currency_mismatch_rejected",
     "test_webhook_signature", "test_webhook_replay_ignored"]),
    ("tests/test_pricing.py", ["test_base_rate", "test_weekend_uplift", "test_long_stay_discount",
     "test_child_rate", "test_tax_inclusive_rounding", "test_promo_code_expired",
     "test_promo_code_stacking_refused", "test_currency_eur", "test_currency_gbp",
     "test_currency_usd"]),
    ("tests/test_rooms.py", ["test_list_rooms", "test_room_types", "test_room_maintenance_flag",
     "test_room_capacity", "test_room_amenities"]),
]

WARNINGS = """=============================== warnings summary ===============================
.venv/lib/python3.12/site-packages/pydantic/_internal/_config.py:291
  PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead.

app/models.py:14
  SAWarning: relationship 'Booking.guest' will copy column guests.id to column bookings.guest_id, which conflicts with relationship(s): 'Guest.bookings'

tests/test_payments.py: 9 warnings
tests/test_bookings.py: 6 warnings
  DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC.

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html"""


def pytest_log(modules, failing: dict[str, str], seconds: str) -> str:
    total = sum(len(names) for _, names in modules)
    lines = [
        "============================= test session starts ==============================",
        "platform linux -- Python 3.12.4, pytest-8.3.2, pluggy-1.5.0 -- /srv/booking-api/.venv/bin/python",
        "cachedir: .pytest_cache",
        "rootdir: /srv/booking-api",
        "configfile: pyproject.toml",
        "plugins: anyio-4.4.0, cov-5.0.0, asyncio-0.23.8",
        "asyncio: mode=Mode.STRICT",
        f"collecting ... collected {total} items",
        "",
    ]
    done = 0
    for path, names in modules:
        for name in names:
            done += 1
            outcome = "FAILED" if name in failing else "PASSED"
            pct = f"[{round(100 * done / total):>3}%]"
            lines.append(f"{path}::{name} {outcome}".ljust(72) + pct)
    lines.append("")
    if failing:
        lines.append("=================================== FAILURES ===================================")
        for name, trace in failing.items():
            lines.append(f"_{' ' + name + ' '}".center(80, "_"))
            lines.append(trace)
    lines.append(WARNINGS)
    if failing:
        lines.append("=========================== short test summary info ============================")
        for name, trace in failing.items():
            last = trace.strip().split("\n")[-1].lstrip("E ").strip()
            lines.append(f"FAILED tests/test_bookings.py::{name} - {last}")
    passed = total - len(failing)
    tail = f"{len(failing)} failed, {passed} passed, 17 warnings" if failing else f"{passed} passed, 17 warnings"
    lines.append(f"================== {tail} in {seconds}s ==================")
    return "\n".join(lines)


def trace(test: str, body: str, frames: list[tuple[str, int, str, str]], error: str) -> str:
    out = [
        "",
        "client = <starlette.testclient.TestClient object at 0x7f3a2c1d9e50>",
        "pending = <Booking id=41 status=pending>",
        "",
        f"    def {test}(client, pending):",
        body,
        "",
    ]
    for path, line, func, src in frames:
        out.append(f"{path}:{line}: in {func}")
        out.append(f"    {src}")
    out.append("")
    out.append(f"E   {error}")
    return "\n".join(out)


CONFIRM_FRAMES = [
    (".venv/lib/python3.12/site-packages/starlette/testclient.py", 633, "post", "return super().post("),
    (".venv/lib/python3.12/site-packages/httpx/_client.py", 1144, "post", "return self.request("),
    (".venv/lib/python3.12/site-packages/starlette/routing.py", 72, "app", "response = await func(request)"),
    (".venv/lib/python3.12/site-packages/fastapi/routing.py", 278, "app", "raw_response = await run_endpoint_function("),
    ("app/routes/bookings.py", line_of(BOOKINGS_V1, "return serialize_confirmation(booking)"), "confirm_booking", "return serialize_confirmation(booking)"),
    ("app/serializers.py", SER_LINE, "serialize_confirmation", '"booking_id": payload["booking_id"],'),
]
FAIL_1 = {
    "test_confirm_booking": trace(
        "test_confirm_booking",
        '        response = client.post(f"/bookings/{pending.id}/confirm")',
        CONFIRM_FRAMES, "KeyError: 'booking_id'"),
    "test_confirm_twice_returns_409": trace(
        "test_confirm_twice_returns_409",
        '>       assert client.post(f"/bookings/{pending.id}/confirm").status_code == 200',
        CONFIRM_FRAMES, "KeyError: 'booking_id'"),
    "test_cancel_confirmed_refunds": trace(
        "test_cancel_confirmed_refunds",
        '        client.post(f"/bookings/{pending.id}/confirm")',
        CONFIRM_FRAMES, "KeyError: 'booking_id'"),
}
FAIL_2 = {
    "test_confirm_twice_returns_409": trace(
        "test_confirm_twice_returns_409",
        '        assert client.post(f"/bookings/{pending.id}/confirm").status_code == 200\n'
        '>       response = client.post(f"/bookings/{pending.id}/confirm")',
        [*CONFIRM_FRAMES[:4],
         ("app/routes/bookings.py", line_of(BOOKINGS_V1, "if _already_confirmed"),
          "confirm_booking", "if _already_confirmed(session, booking):"),
         ("app/routes/bookings.py", BOOK_LINE, "_already_confirmed",
          'return existing["booking_id"] == booking.id')],
        "KeyError: 'booking_id'"),
}

BOOKING_MODULE = [m for m in MODULES if m[0] == "tests/test_bookings.py"]
BOOKING_MODULE_V2 = [("tests/test_bookings.py", [*BOOKING_MODULE[0][1][:4],
                      "test_confirm_twice_keeps_first_confirmation", *BOOKING_MODULE[0][1][4:]])]
MODULES_V2 = [m if m[0] != "tests/test_bookings.py" else BOOKING_MODULE_V2[0] for m in MODULES]

GIT_LOG = """9c41e2a (HEAD -> main, origin/main) serializers: build responses from Booking.to_payload()
4be07d1 payments: retry capture once on 502 from the PSP
e1f9a63 availability: count nights in the hotel's timezone
77d0c58 guests: normalise email before uniqueness check
b3a12f4 pricing: round tax-inclusive totals half-up
0f6e9d2 ci: pin pytest-asyncio to 0.23
5a7c311 rooms: expose amenities on list endpoint
d28be90 bookings: add cancellation refunds"""

GIT_SHOW = """commit 9c41e2a7f0b35d1e8c2a4f6b9d0e1c3a5b7d9f21
Author: Priya Raman <priya@example.com>
Date:   Fri Oct 16 17:42:09 2026 +0100

    serializers: build responses from Booking.to_payload()

    to_payload() is now the single source for booking fields so the API and
    the event bus stop drifting. The payload key is `id`, matching the event
    schema (bookings.v3).

 app/models.py      | 31 +++++++++++++++++++++++--------
 app/serializers.py | 22 +++++++++++-----------
 app/events.py      | 14 ++++----------
 3 files changed, 38 insertions(+), 29 deletions(-)"""

GREP = f"""app/serializers.py:{SER_LINE}:        "booking_id": payload["booking_id"],
app/serializers.py:{line_of(SERIALIZERS_V1, '"booking_id": payload["id"]')}:        "booking_id": payload["id"],
app/serializers.py:{[n for n, line in enumerate(SERIALIZERS_V1.split(chr(10)), 1) if '"booking_id": payload["id"]' in line][1]}:        "booking_id": payload["id"],
app/routes/bookings.py:{BOOK_LINE}:    return existing["booking_id"] == booking.id
app/events.py:22:        "id": payload["id"],
tests/test_bookings.py:15:    assert response.json()["booking_id"] == pending.id
tests/test_bookings.py:28:    assert data["booking_id"] == pending.id
migrations/0007_confirmations.py:18:        sa.Column("booking_id", sa.Integer, sa.ForeignKey("bookings.id")),"""

GIT_DIFF = f"""diff --git a/app/routes/bookings.py b/app/routes/bookings.py
index 3d1a9e0..8b72c4f 100644
--- a/app/routes/bookings.py
+++ b/app/routes/bookings.py
@@ -{BOOK_LINE - 4},7 +{BOOK_LINE - 4},7 @@ def _already_confirmed(session: Session, booking: Booking) -> bool:
     existing = session.confirmations.latest_for(booking.id)
     if existing is None:
         return False
-    return existing["booking_id"] == booking.id
+    return existing["id"] == booking.id


 @router.post("/{{booking_id}}/confirm")
diff --git a/app/serializers.py b/app/serializers.py
index a40c2be..f19d7a3 100644
--- a/app/serializers.py
+++ b/app/serializers.py
@@ -{SER_LINE - 3},7 +{SER_LINE - 3},7 @@ def serialize_booking(booking: Booking) -> dict[str, Any]:
 def serialize_confirmation(booking: Booking) -> dict[str, Any]:
     payload = booking.to_payload()
     return {{
-        "booking_id": payload["booking_id"],
+        "booking_id": payload["id"],
         "status": "confirmed",
         "confirmed_at": _iso(booking.confirmed_at),
         "confirmation_code": booking.confirmation_code,"""

MODELS = '''"""Booking domain models."""

from __future__ import annotations

import enum
import secrets
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class BookingStatus(enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"


class Guest(Base):
    __tablename__ = "guests"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(sa.String(200))
    email: Mapped[str] = mapped_column(sa.String(320), unique=True, index=True)
    bookings: Mapped[list["Booking"]] = relationship(back_populates="guest")


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(sa.String(10), unique=True)
    room_type: Mapped[str] = mapped_column(sa.String(40))
    capacity: Mapped[int] = mapped_column(default=2)
    in_maintenance: Mapped[bool] = mapped_column(default=False)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    guest_id: Mapped[int] = mapped_column(sa.ForeignKey("guests.id"))
    room_id: Mapped[int] = mapped_column(sa.ForeignKey("rooms.id"))
    status: Mapped[BookingStatus] = mapped_column(sa.Enum(BookingStatus))
    check_in: Mapped[datetime]
    check_out: Mapped[datetime]
    total: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))
    currency: Mapped[str] = mapped_column(sa.String(3), default="EUR")
    confirmed_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    confirmation_code: Mapped[str | None] = mapped_column(sa.String(12))

    guest: Mapped[Guest] = relationship(back_populates="bookings")
    room: Mapped[Room] = relationship()

    @classmethod
    def from_request(cls, body: dict[str, Any]) -> "Booking":
        check_in = date.fromisoformat(body["check_in"])
        nights = int(body["nights"])
        return cls(
            guest_id=body["guest_id"],
            room_id=body["room_id"],
            status=BookingStatus.PENDING,
            check_in=datetime.combine(check_in, datetime.min.time(), timezone.utc),
            check_out=datetime.combine(check_in, datetime.min.time(), timezone.utc)
            + nights * (datetime(2000, 1, 2) - datetime(2000, 1, 1)),
            total=Decimal(body.get("total", "0")),
        )

    def to_payload(self) -> dict[str, Any]:
        """Single source for booking fields (API responses and bookings.v3 events)."""
        return {
            "id": self.id,
            "guest_id": self.guest_id,
            "room_id": self.room_id,
            "status": self.status.value,
            "total": str(self.total),
            "currency": self.currency,
        }

    def confirm(self) -> None:
        self.status = BookingStatus.CONFIRMED
        self.confirmed_at = datetime.now(timezone.utc)
        self.confirmation_code = secrets.token_hex(6).upper()

    def cancel(self) -> None:
        self.status = BookingStatus.CANCELLED
        self.cancelled_at = datetime.now(timezone.utc)
'''

EVENTS = '''"""Publish booking events to the bus (schema bookings.v3)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.bus import publish
from app.models import Booking

log = logging.getLogger(__name__)
TOPIC = "bookings.v3"


def booking_event(kind: str, booking: Booking) -> dict[str, Any]:
    payload = booking.to_payload()
    return {
        "type": f"booking.{kind}",
        "schema": TOPIC,
        "data": {
            "id": payload["id"],
            "status": payload["status"],
            "total": payload["total"],
            "currency": payload["currency"],
        },
    }


def emit(kind: str, booking: Booking) -> None:
    event = booking_event(kind, booking)
    publish(TOPIC, json.dumps(event, separators=(",", ":")))
    log.debug("emitted %s for booking %s", event["type"], booking.id)
'''

CONFTEST = """import os
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("BOOKING_DB_URL", "sqlite+pysqlite:///:memory:")

from app.db import Base, engine, get_session, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Booking, BookingStatus, Guest, Room  # noqa: E402


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def guest(session):
    g = Guest(full_name="Ada Lovelace", email="ada@example.com")
    session.add(g)
    session.commit()
    return g


@pytest.fixture
def room(session):
    r = Room(number="204", room_type="double")
    session.add(r)
    session.commit()
    return r


@pytest.fixture
def booking_factory(session, guest, room):
    def make(status=BookingStatus.PENDING, total=Decimal("100.00")):
        b = Booking.from_request(
            {"guest_id": guest.id, "room_id": room.id, "check_in": "2026-11-02", "nights": 2}
        )
        b.status, b.total = status, total
        session.add(b)
        session.commit()
        return b

    return make
"""

COVERAGE = """
---------- coverage: platform linux, python 3.12.4-final-0 -----------
Name                         Stmts   Miss  Cover
------------------------------------------------
app/__init__.py                  0      0   100%
app/bus.py                      18      9    50%
app/db.py                       21      2    90%
app/events.py                   17      3    82%
app/main.py                     14      0   100%
app/models.py                   58      4    93%
app/payments.py                 63      7    89%
app/pricing.py                  71      5    93%
app/routes/__init__.py           0      0   100%
app/routes/availability.py      39      2    95%
app/routes/bookings.py          52      1    98%
app/routes/guests.py            44      3    93%
app/routes/rooms.py             22      0   100%
app/serializers.py              33      0   100%
------------------------------------------------
TOTAL                          452     36    92%
"""

msgs: list[dict] = []
n = 0


def say(role: str, text: str) -> None:
    msgs.append({"role": role, "text": text})


def call(text: str, tool: str, tool_input: dict, output: str, is_error: bool = False) -> None:
    global n
    n += 1
    cid = f"toolu_01{n:02d}"
    msgs.append({"role": "assistant", "text": text,
                 "toolUses": [{"tool_use_id": cid, "tool": tool, "input": tool_input}]})
    result = {"tool_use_id": cid, "text": output}
    if is_error:
        result["is_error"] = True
    msgs.append({"role": "user", "toolResults": [result]})


say("user", "POST /bookings/{id}/confirm started returning 500 after Friday's serializer "
    "refactor. Find the cause and fix it so the suite is green. Don't touch migrations/.")
call("I'll reproduce it first.", "Bash",
     {"command": "pytest tests/ -v", "description": "Run the full test suite"},
     pytest_log(MODULES, FAIL_1, "4.81"), is_error=True)
call("Three failures, one cause: `KeyError: 'booking_id'` raised at app/serializers.py:"
     f"{SER_LINE} in `serialize_confirmation`. Checking what Friday's refactor changed.",
     "Bash", {"command": "git log --oneline -8", "description": "Recent commits"}, GIT_LOG)
call("", "Bash", {"command": "git show --stat 9c41e2a", "description": "Inspect the refactor"},
     GIT_SHOW)
call("The refactor routes responses through `to_payload()`. Reading the model.", "Read",
     {"file_path": "/srv/booking-api/app/models.py"}, numbered(MODELS))
call("`to_payload()` emits `id`, not `booking_id`. Checking the event side the commit "
     "mentions.", "Read", {"file_path": "/srv/booking-api/app/events.py"}, numbered(EVENTS))
call("Events already read `payload[\"id\"]`. Reading the serializer.", "Read", {"file_path": "/srv/booking-api/app/serializers.py"},
     numbered(SERIALIZERS_V1))
call(f"Line {SER_LINE} still reads `payload[\"booking_id\"]`; the other serializers already "
     "use `payload[\"id\"]`. Looking for other places that read the old key.", "Grep",
     {"pattern": "booking_id", "path": "/srv/booking-api", "output_mode": "content", "-n": True},
     GREP)
call("`app/routes/bookings.py` reads it too, in the idempotency check. Reading the route.",
     "Read", {"file_path": "/srv/booking-api/app/routes/bookings.py"}, numbered(BOOKINGS_V1))
call("", "Read", {"file_path": "/srv/booking-api/tests/test_bookings.py"}, numbered(TESTS_V1))
call("The tests assert the public field `booking_id`, so the response shape stays; only the "
     "lookup into the payload is wrong. Re-reading the serializer before editing.", "Read",
     {"file_path": "/srv/booking-api/app/serializers.py"}, numbered(SERIALIZERS_V1))
call("", "Edit", {"file_path": "/srv/booking-api/app/serializers.py",
                  "old_string": '        "booking_id": payload["booking_id"],\n        "status": "confirmed",',
                  "new_string": '        "booking_id": payload["id"],\n        "status": "confirmed",'},
     "The file /srv/booking-api/app/serializers.py has been updated successfully.")
call("Running the booking tests.", "Bash",
     {"command": "pytest tests/test_bookings.py -v", "description": "Run booking tests"},
     pytest_log(BOOKING_MODULE, FAIL_2, "1.12"), is_error=True)
call("Same KeyError, now from the idempotency check at app/routes/bookings.py:"
     f"{BOOK_LINE}: the second confirm reads `existing[\"booking_id\"]` from the stored payload. "
     "Re-reading the route.", "Read", {"file_path": "/srv/booking-api/app/routes/bookings.py"},
     numbered(BOOKINGS_V1))
call("", "Edit", {"file_path": "/srv/booking-api/app/routes/bookings.py",
                  "old_string": 'return existing["booking_id"] == booking.id',
                  "new_string": 'return existing["id"] == booking.id'},
     "The file /srv/booking-api/app/routes/bookings.py has been updated successfully.")
call("Checking the serializer edit landed as intended.", "Read",
     {"file_path": "/srv/booking-api/app/serializers.py"}, numbered(SERIALIZERS_V2))
call("Full suite, with coverage.", "Bash",
     {"command": "pytest tests/ -v --cov=app", "description": "Run the full test suite"},
     "{0}{2}{1}".format(*pytest_log(MODULES, {}, "6.37").rsplit("\n", 1), COVERAGE))
call("", "Bash", {"command": "ruff check app/", "description": "Lint"}, "All checks passed!")
call("", "Bash", {"command": "git diff", "description": "Review the change"}, GIT_DIFF)
say("assistant", "Fixed. Root cause: commit 9c41e2a renamed the payload key to `id`, but two "
    f"readers still used `booking_id`: app/serializers.py:{SER_LINE} (the 500 on confirm) and "
    f"app/routes/bookings.py:{BOOK_LINE} (the idempotency check, which would have turned the "
    "409 on a double confirm into a 500). The response field stays `booking_id`; migrations/ "
    "untouched. 45 passed.")
say("user", "Nice. Add a regression test that a second confirm keeps the first confirmation.")
call("Checking the fixtures first.", "Read", {"file_path": "/srv/booking-api/tests/conftest.py"},
     numbered(CONFTEST))
call("", "Read", {"file_path": "/srv/booking-api/tests/test_bookings.py"}, numbered(TESTS_V1))
call("", "Edit", {"file_path": "/srv/booking-api/tests/test_bookings.py",
                  "old_string": "\n\n\ndef test_cancel_confirmed_refunds",
                  "new_string": NEW_TEST + "\n\n\ndef test_cancel_confirmed_refunds"},
     "The file /srv/booking-api/tests/test_bookings.py has been updated successfully.")
call("", "Bash", {"command": "pytest tests/test_bookings.py -v", "description": "Run booking tests"},
     pytest_log(BOOKING_MODULE_V2, {}, "1.08"))
call("And the whole suite once more.", "Bash",
     {"command": "pytest tests/ -q", "description": "Run the full test suite"},
     "." * 45 + "                                    [100%]\n" + WARNINGS + "\n46 passed, 17 warnings in 4.93s")
say("assistant", "Added `test_confirm_twice_keeps_first_confirmation`: the second confirm "
    "returns 409 and the booking stays confirmed with its original code. 46 passed.")

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"messages": msgs}, handle, indent=2, ensure_ascii=False)
sys.stdout.write(f"{len(msgs)} messages, {n} tool calls\n")
