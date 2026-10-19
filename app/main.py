"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this single file:
  1. configuration / database handle
  2. pure business rules R1-R5 (no I/O, unit-testable)
  3. serialisation helpers (BSON datetime <-> epoch milliseconds)
  4. request models
  5. index creation
  6. endpoints (system, employees, attendance)
  7. aggregation pipeline builders + analytics endpoints
  8. admin explain endpoint
"""
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True: BSON dates come back as aware UTC datetimes (never naive local time).
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# >>> PURE RULES BEGIN (unit-tested without any third-party package; keep this block free of I/O)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ONE_SECOND = timedelta(seconds=1)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def round_half_up(value, places: int) -> float:
    """Half-up rounding that is immune to binary-float artefacts (R4, R8)."""
    exp = Decimal(1).scaleb(-places)
    return float(Decimal(repr(round(float(value), 9))).quantize(exp, rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, exact to the millisecond."""
    return EPOCH + timedelta(milliseconds=ms)


def ms_to_punch(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def parse_hhmm(value: str) -> tuple:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def at_ist(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    y, mo, d = map(int, date_str.split("-"))
    h, m = parse_hhmm(hhmm)
    return datetime(y, mo, d, h, m, tzinfo=IST) + timedelta(days=plus_days)


def attendance_date(ts: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs to the previous day."""
    local = ts.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if (local.hour, local.minute, local.second) < (eh, em, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole minutes since shift start."""
    start = at_ist(date_str, shift_start)
    secs = (truncate_seconds(punch_in) - start) // ONE_SECOND
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals, half-up (Decimal, not float rounding)."""
    secs = (truncate_seconds(punch_out) - truncate_seconds(punch_in)) // ONE_SECOND
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only when >= 30. Overnight shifts end on the next calendar day."""
    end = at_ist(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    secs = (truncate_seconds(punch_out) - end) // ONE_SECOND
    minutes = secs // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: judged on the already rounded work_hours."""
    return Decimal(repr(work_hours)) < HALF_DAY_LIMIT


def derive_fields(status: str, punch_in, punch_out, date_str: str, shift_start: str, shift_end: str) -> dict:
    """All four derived fields for a record (DATA_MODEL.md, 'For a record your own code writes')."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, date_str, shift_start, shift_end),
        "half_day": compute_half_day(wh),
    }


def month_bounds(month: str) -> tuple:
    """'YYYY-MM' -> (first day, last day) as dates. Raises ValueError on a bad month."""
    if not MONTH_RE.match(month):
        raise ValueError("month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)  # ValueError for year 0
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7). Pure calendar arithmetic over at most 31 days."""
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def parse_iso_date(value: str) -> date:
    if not DATE_RE.match(value):
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(value)
# <<< PURE RULES END


# --------------------------------------------------------------------------- #
# Validation / serialisation helpers
# --------------------------------------------------------------------------- #
def invalid(loc: str, msg: str):
    """Raise a FastAPI-style 422 with the default body shape."""
    raise RequestValidationError([{"type": "value_error", "loc": ["query", loc], "msg": msg, "input": None}])


def need_date(value: str, loc: str) -> date:
    try:
        return parse_iso_date(value)
    except ValueError:
        invalid(loc, "must be a valid calendar date YYYY-MM-DD")


def need_month(value: str, loc: str = "month"):
    try:
        return month_bounds(value)
    except ValueError:
        invalid(loc, "must be a valid month YYYY-MM")


def now_ms() -> int:
    return int(time.time() * 1000)


def history_value(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def serialize_history(entry: dict) -> dict:
    changes = {}
    for k, ch in (entry.get("changes") or {}).items():
        changes[k] = {"from": history_value(ch.get("from")), "to": history_value(ch.get("to"))}
    at = entry.get("at")
    return {"at": dt_to_ms(at) if isinstance(at, datetime) else at, "by": entry.get("by"),
            "reason": entry.get("reason"), "changes": changes}


def serialize_attendance(doc: dict) -> dict:
    """Stored document -> API record. Missing legacy fields get their defaults; _id is never exposed."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


def serialize_employee(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"], "created_at": dt_to_ms(created) if isinstance(created, datetime) else created,
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: StrictStr = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = Field(default="09:30", pattern=HHMM)
    shift_end: StrictStr = Field(default="18:30", pattern=HHMM)
    joined_on: StrictStr

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, v):
        parse_iso_date(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Indexes (idempotent; run at startup)
# --------------------------------------------------------------------------- #
INDEXES_READY = False


def _ensure(coll, keys, name, unique=False):
    try:
        coll.create_index(keys, name=name, unique=unique)
    except OperationFailure:
        if not unique:
            raise
        # Seeded data already violates uniqueness: keep the app up with a plain index and shout about it.
        log.error("could not build unique index %s; falling back to a non-unique index", name)
        coll.create_index(keys, name=name + "_nonunique")


def ensure_indexes():
    global INDEXES_READY
    emp, att = db.employees, db.attendance_logs
    _ensure(emp, [("emp_code", 1)], "uq_emp_code", unique=True)  # identity + race-safe create
    _ensure(emp, [("department", 1), ("joined_on", 1)], "dept_joined")  # summary / trend / list by dept
    _ensure(emp, [("joined_on", 1)], "joined")  # summary without department filter
    _ensure(att, [("emp_code", 1), ("date", 1)], "uq_emp_date", unique=True)  # one record per day; employee lookups
    _ensure(att, [("date", -1), ("emp_code", 1)], "date_emp")  # list sort order, month scans
    _ensure(att, [("status", 1), ("date", -1), ("emp_code", 1)], "status_date_emp")  # status-filtered list
    _ensure(att, [("emp_code", 1), ("punch_in", -1)], "emp_punchin")  # punch-out: latest record at/before a time
    INDEXES_READY = True


@asynccontextmanager
async def lifespan(_app):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep serving /health (503) so the runner can see the cause
        log.error("index creation failed at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not INDEXES_READY:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = ms_to_dt(now_ms())
    try:
        db.employees.insert_one(doc)  # unique index decides the winner of a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_punch(body.punched_at) if body.punched_at is not None else ms_to_punch(now_ms())
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None,
        **derive_fields(body.status, ts, None, day, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_punch(body.punched_at) if body.punched_at is not None else ms_to_punch(now_ms())
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = truncate_seconds(rec["punch_in"])
    if ts <= pin:
        invalid_body("punched_at", "must be after punch_in")
    if ts - pin > MAX_SHIFT:
        invalid_body("punched_at", "more than 24 hours after punch_in")
    derived = derive_fields(rec["status"], pin, ts, rec["date"], emp["shift_start"], emp["shift_end"])
    # Late minutes were fixed at punch-in (or by a correction): keep the stored value.
    updates = {"punch_out": ts, "work_hours": derived["work_hours"],
               "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}
    res = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None}, {"$set": updates})
    if res.matched_count == 0:  # lost the race against another punch-out
        raise HTTPException(409, "already punched out")
    rec.update(updates)
    return serialize_attendance(rec)


def invalid_body(loc: str, msg: str):
    raise RequestValidationError([{"type": "value_error", "loc": ["body", loc], "msg": msg, "input": None}])


ATT_SORT = [("date", -1), ("emp_code", 1)]


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    rng = {}
    if date_from is not None:
        rng["$gte"] = need_date(date_from, "date_from").isoformat()
    if date_to is not None:
        rng["$lte"] = need_date(date_to, "date_to").isoformat()
    if date_from is not None and date_to is not None and rng["$gte"] > rng["$lte"]:
        invalid("date_from", "date_from must not be after date_to")
    if rng:
        q["date"] = rng
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(ATT_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date_path(date)
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
    old = {
        "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    new_status = body.status or old["status"]
    if new_status in PRESENCE:
        new_in = ms_to_punch(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = ms_to_punch(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            invalid_body("punch_in", "a presence status requires a punch_in")
        new_in = truncate_seconds(new_in)
        if new_out is not None:
            new_out = truncate_seconds(new_out)
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            invalid_body("punch_in", "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT):
            invalid_body("punch_out", "punch_out must be after punch_in and within 24 hours")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            invalid_body("status", "ABSENT/LEAVE clear the punch times; do not supply punch_in/punch_out")
        new_in = new_out = None
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(new_status, new_in, new_out, day, emp["shift_start"], emp["shift_end"])}

    def same(a, b):
        return dt_to_ms(a) == dt_to_ms(b) if isinstance(a, datetime) or isinstance(b, datetime) else a == b

    changes = {k: {"from": old[k], "to": new[k]} for k in old if not same(old[k], new[k])}
    if not changes:
        invalid_body("body", "the request changes nothing")
    entry = {"at": ms_to_dt(now_ms()), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history = rec.get("history") or []
    # Optimistic concurrency: only apply if the record is exactly as we read it.
    guard = {"_id": rec["_id"], "status": old["status"], "punch_in": old["punch_in"],
             "punch_out": old["punch_out"],
             "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(history)]}}
    res = db.attendance_logs.update_one(guard, {"$set": new, "$push": {"history": entry}})
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently; retry")
    rec.update(new)
    rec["history"] = history + [entry]
    return serialize_attendance(rec)


def parse_date_path(value: str) -> str:
    try:
        return parse_iso_date(value).isoformat()
    except ValueError:
        raise RequestValidationError([{"type": "value_error", "loc": ["path", "date"],
                                       "msg": "must be a valid calendar date YYYY-MM-DD", "input": value}])


# --------------------------------------------------------------------------- #
# Aggregation pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
# $dayOfWeek: 1 = Sunday ... 7 = Saturday.  A "YYYY-MM-DD" string parses as UTC midnight, so the weekday is exact.
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "onError": None, "onNull": None}}}
_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_PRESENT_VALUE = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}  # R5
_PRESENT_WEEKDAY = {"$cond": [{"$and": [_IS_PRESENT, {"$in": [_DOW, [2, 3, 4, 5, 6]]}]}, _PRESENT_VALUE, 0]}  # R6+R7
_PRESENT_ANY_DAY = {"$cond": [_IS_PRESENT, _PRESENT_VALUE, 0]}
_LATE = {"$ifNull": ["$late_minutes", 0]}
_IS_LATE = {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _round4_expr(x):
    """Half-up to 4 decimals inside MongoDB ($round is banker's); the epsilon absorbs binary-float error."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [x, 10000]}, 0.5, 1e-9]}}, 10000]}


def employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _PRESENT_WEEKDAY},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _IS_LATE},
            "total_late_minutes": {"$sum": _LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        match["department"] = department
    per_employee = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _PRESENT_WEEKDAY},
            "work_sum": {"$sum": {"$cond": [_HAS_HOURS, "$work_hours", 0]}},
            "work_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
            "late_count": {"$sum": _IS_LATE},
            "late_minutes": {"$sum": _LATE},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    s = lambda f: {"$sum": {"$ifNull": ["$s." + f, 0]}}  # noqa: E731
    return [
        {"$match": match},
        # Start from employees so people with zero logs still count; join their month of logs by emp_code.
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "s"}},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1}, "present_days": s("present_days"),
            "work_sum": s("work_sum"), "work_n": s("work_n"), "late_count": s("late_count"),
            "total_late_minutes": s("late_minutes"), "leave_count": s("leave_count"),
            "on_duty_count": s("on_duty_count"),
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipe: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipe += [
        # $rank = standard competition ranking (1, 2, 2, 4); never use the array position.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


def trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    n = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    s, e = d_from.isoformat(), d_to.isoformat()
    day = {"$add": [start, {"$multiply": ["$$i", 86400000]}]}
    row = {"$let": {"vars": {"d": day}, "in": {"$let": {
        "vars": {"ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$d"}}, "dow": {"$dayOfWeek": "$$d"}},
        "in": {"$let": {
            "vars": {
                "hc": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$$ds"]}}}},
                "st": {"$ifNull": [{"$arrayElemAt": [
                    {"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$$ds"]}}}, 0]}, {}]},
            },
            "in": {"$let": {
                "vars": {"work": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]},
                         "pc": {"$ifNull": ["$$st.present", 0]}},
                "in": {
                    "date": "$$ds", "is_working_day": "$$work", "headcount": "$$hc", "present_count": "$$pc",
                    "late_count": {"$ifNull": ["$$st.late", 0]},
                    "attendance_rate": {"$cond": [{"$and": ["$$work", {"$gt": ["$$hc", 0]}]},
                                                  _round4_expr({"$divide": ["$$pc", "$$hc"]}), None]},
                },
            }},
        }},
    }}}}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        # one pass over the department's logs in range, collapsed to one small row per day
        {"$lookup": {"from": "attendance_logs", "localField": "codes", "foreignField": "emp_code",
                     "pipeline": [
                         {"$match": {"date": {"$gte": s, "$lte": e}}},
                         {"$group": {"_id": "$date", "present": {"$sum": _PRESENT_ANY_DAY},
                                     "late": {"$sum": _IS_LATE}}},
                     ], "as": "daily"}},
        # gap filling: exactly one row per calendar day, generated in the database
        {"$project": {"_id": 0, "rows": {"$map": {"input": {"$range": [0, n]}, "as": "i", "in": row}}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"ma": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$in": [{"$type": "$ma"}, ["null", "missing"]]}, None,
                                        _round4_expr("$ma")]},
        }},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")):
    first, last = need_month(month)
    emp = get_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    r = rows[0] if rows else {}
    start = max(first, parse_iso_date(emp["joined_on"]))
    working_days = count_weekdays(start, last) if start <= last else 0
    present = float(r.get("present_days", 0))
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": round_half_up(present, 2), "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)), "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(Decimal(repr(present)) / working_days * 100, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
                       department: Optional[str] = None):
    first, last = need_month(month)
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        n = r["work_n"]
        items.append({
            "department": r["_id"], "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(Decimal(repr(round(float(r["work_sum"]), 6))) / n, 2) if n else None,
            "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = need_month(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(
        leaderboard_pipeline(first, last, limit, department)))}


def check_trend_range(d_from: str, d_to: str) -> tuple:
    a, b = need_date(d_from, "from"), need_date(d_to, "to")
    if b < a:
        invalid("to", "'to' must not be before 'from'")
    if (b - a).days + 1 > 92:
        invalid("to", "range must not exceed 92 days")
    return a, b


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    a, b = check_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": list(db.employees.aggregate(trend_pipeline(department, a, b)))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def run_explain(command: dict) -> dict:
    out = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))


def require(**params):
    for k, v in params.items():
        if v is None:
            invalid(k, "field required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None, month: Optional[str] = Query(None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    def aggregate(coll, pipeline):
        return coll, run_explain({"aggregate": coll, "pipeline": pipeline, "cursor": {}})

    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        coll, result = "attendance_logs", run_explain({
            "find": "attendance_logs", "filter": q, "sort": dict(ATT_SORT),
            "skip": (page - 1) * page_size, "limit": page_size})
    elif endpoint == "employee_monthly":
        require(emp_code=emp_code, month=month)
        first, last = need_month(month)
        coll, result = aggregate("attendance_logs", employee_monthly_pipeline(emp_code, first, last))
    elif endpoint == "department_summary":
        require(month=month)
        first, last = need_month(month)
        coll, result = aggregate("employees", department_summary_pipeline(first, last, department))
    elif endpoint == "late_leaderboard":
        require(month=month)
        first, last = need_month(month)
        coll, result = aggregate("attendance_logs", leaderboard_pipeline(first, last, limit, department))
    else:  # department_trend
        require(department=department, **{"from": from_, "to": to})
        a, b = check_trend_range(from_, to)
        coll, result = aggregate("employees", trend_pipeline(department, a, b))
    return JSONResponse({"endpoint": endpoint, "collection": coll, "explain": result})
