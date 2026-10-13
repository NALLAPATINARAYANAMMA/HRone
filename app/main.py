"""Employee Attendance & Analytics API."""

import calendar
import json
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

def create_indexes() -> None:
    """Create idempotent indexes used by uniqueness guarantees and API queries."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_status_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_date_punch",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punch_in",
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(APIModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def distinct_shift_times(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(APIModel):
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_timestamp(self) -> "PunchInIn":
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or a valid epoch-millisecond integer")
        return self


class PunchOutIn(APIModel):
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)

    @model_validator(mode="after")
    def reject_null_timestamp(self) -> "PunchOutIn":
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or a valid epoch-millisecond integer")
        return self


class RegularizeIn(APIModel):
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_null_changes(self) -> "RegularizeIn":
        for field_name in ("status", "punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


def parse_date(value: str) -> date:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Date must be a valid YYYY-MM-DD calendar date") from exc


def require_date(value: str) -> date:
    try:
        return parse_date(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def validate_month(month: str) -> tuple[date, date]:
    if not MONTH_RE.fullmatch(month):
        raise HTTPException(422, "month must be in YYYY-MM format")
    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    return date(year, month_number, 1), date(year, month_number, last_day)


def round_half_up(value: int | float, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def timestamp_datetime(value: int | None) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be epoch milliseconds in the supported range")
    try:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        return (epoch + timedelta(milliseconds=value)).replace(microsecond=0)
    except (OverflowError, OSError, ValueError) as exc:
        raise HTTPException(422, "timestamp is out of range") from exc


def to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def local_datetime(day: date, shift: str, *, next_day: bool = False) -> datetime:
    hour, minute = map(int, shift.split(":"))
    if next_day:
        day += timedelta(days=1)
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, day: date) -> int:
    start = local_datetime(day, shift_start)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 10 * 60 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_end: str, day: date, shift_start: str) -> int:
    end = local_datetime(day, shift_end, next_day=shift_end <= shift_start)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def serial_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(doc.get("created_at"))
    return doc


def serial_attendance(doc: dict) -> dict:
    doc.pop("_id", None)
    doc["punch_in"] = to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = to_epoch_ms(doc.get("punch_out"))
    doc.setdefault("work_hours", None)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    history = doc.setdefault("history", [])
    for entry in history:
        entry["at"] = to_epoch_ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime) or change.get(side) is None:
                    change[side] = to_epoch_ms(change[side])
    return doc


def present_day_expression(
    date_field: str,
    status_field: str = "$status",
    half_day_field: str = "$half_day",
) -> dict:
    parsed = {"$dateFromString": {"dateString": date_field}}
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": [status_field, list(PRESENCE)]},
                    {"$gte": [{"$dayOfWeek": parsed}, 2]},
                    {"$lte": [{"$dayOfWeek": parsed}, 6]},
                ]
            },
            {"$cond": [{"$eq": [half_day_field, True]}, 0.5, 1]},
            0,
        ]
    }


def monthly_employee_pipeline(emp_code: str, month: str) -> list[dict]:
    month_start, month_end = validate_month(month)
    start_str, end_str = month_start.isoformat(), month_end.isoformat()
    days_count = (month_end - month_start).days + 1
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$addFields": {
                "_month_dates": {
                    "$map": {
                        "input": {"$range": [0, days_count]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {"$dateFromString": {"dateString": start_str}},
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                                "format": "%Y-%m-%d",
                            }
                        },
                    }
                }
            }
        },
        {
            "$addFields": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_month_dates",
                            "as": "day",
                            "cond": {
                                "$and": [
                                    {"$gte": ["$$day", "$joined_on"]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$$day"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                        }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_str]},
                                    {"$lte": ["$date", end_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs",
            }
        },
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$emp_code",
                "working_days": {"$first": "$working_days"},
                "present_days": {
                    "$sum": {
                        "$ifNull": [
                            present_day_expression(
                                "$_logs.date", "$_logs.status", "$_logs.half_day"
                            ),
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$_logs.status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$_logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$_logs.late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$_logs.overtime_minutes", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]},
                        None,
                    ]
                },
            }
        },
    ]


def department_summary_pipeline(month: str, department: str | None) -> list[dict]:
    first, last = validate_month(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", first.isoformat()]},
                                    {"$lte": ["$date", last.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs",
            }
        },
        {
            "$addFields": {
                "_person": {
                    "$reduce": {
                        "input": "$_logs",
                        "initialValue": {
                            "present_days": 0,
                            "late_count": 0,
                            "late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                            "work_sum": 0,
                            "work_count": 0,
                        },
                        "in": {
                            "present_days": {
                                "$add": [
                                    "$$value.present_days",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", list(PRESENCE)]},
                                                    {
                                                        "$in": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$$this.date"
                                                                    }
                                                                }
                                                            },
                                                            [2, 3, 4, 5, 6],
                                                        ]
                                                    },
                                                ]
                                            },
                                            {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1]},
                                            0,
                                        ]
                                    },
                                ]
                            },
                            "late_count": {
                                "$add": [
                                    "$$value.late_count",
                                    {"$cond": [{"$gt": ["$$this.late_minutes", 0]}, 1, 0]},
                                ]
                            },
                            "late_minutes": {
                                "$add": ["$$value.late_minutes", {"$ifNull": ["$$this.late_minutes", 0]}]
                            },
                            "leave_count": {
                                "$add": [
                                    "$$value.leave_count",
                                    {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]},
                                ]
                            },
                            "on_duty_count": {
                                "$add": [
                                    "$$value.on_duty_count",
                                    {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]},
                                ]
                            },
                            "work_sum": {
                                "$add": [
                                    "$$value.work_sum",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", list(PRESENCE)]},
                                                    {"$ne": ["$$this.work_hours", None]},
                                                ]
                                            },
                                            "$$this.work_hours",
                                            0,
                                        ]
                                    },
                                ]
                            },
                            "work_count": {
                                "$add": [
                                    "$$value.work_count",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", list(PRESENCE)]},
                                                    {"$ne": ["$$this.work_hours", None]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    },
                                ]
                            },
                        },
                    }
                }
            }
        },
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "people": {"$push": "$_person"}}},
        {"$unwind": "$people"},
        {
            "$group": {
                "_id": "$_id",
                "headcount": {"$first": "$headcount"},
                "present_days": {"$sum": "$people.present_days"},
                "late_count": {"$sum": "$people.late_count"},
                "total_late_minutes": {"$sum": "$people.late_minutes"},
                "leave_count": {"$sum": "$people.leave_count"},
                "on_duty_count": {"$sum": "$people.on_duty_count"},
                "_work_sum": {"$sum": "$people.work_sum"},
                "_work_count": {"$sum": "$people.work_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "_work_sum": 1,
                "_work_count": 1,
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$_work_count", 0]},
                        {"$divide": ["$_work_sum", "$_work_count"]},
                        None,
                    ]
                }
            }
        },
        {"$project": {"_work_sum": 0, "_work_count": 0}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, department: str | None, limit: int) -> list[dict]:
    first, last = validate_month(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend(
        [
            {"$sort": {"total_late_minutes": -1}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$_employee.name",
                    "department": "$_employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def trend_pipeline(department: str, first: date, last: date) -> list[dict]:
    day_count = (last - first).days + 1
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "employees": 1,
                "days": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {"$dateFromString": {"dateString": first.isoformat()}},
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                                "format": "%Y-%m-%d",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$days"},
        {
            "$addFields": {
                "date": "$days",
                "_eligible": {
                    "$filter": {
                        "input": "$employees",
                        "as": "employee",
                        "cond": {"$lte": ["$$employee.joined_on", "$days"]},
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$date",
                    "codes": {
                        "$map": {"input": "$_eligible", "as": "employee", "in": "$$employee.emp_code"}
                    },
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE)]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "_day_logs",
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {"dateString": "$date"}
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "headcount": {"$size": "$_eligible"},
                "present_count": {"$ifNull": [{"$first": "$_day_logs.present_count"}, 0]},
                "late_count": {"$ifNull": [{"$first": "$_day_logs.late_count"}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


def explain_aggregate(collection: str, pipeline: list[dict]) -> dict:
    command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command("explain", command, verbosity="executionStats")
    return json.loads(json_util.dumps(result))


def explain_find(collection: str, query: dict, sort: list[tuple[str, int]], skip: int, limit: int) -> dict:
    command = {
        "find": collection,
        "filter": query,
        "sort": dict(sort),
        "skip": skip,
        "limit": limit,
    }
    result = db.command("explain", command, verbosity="executionStats")
    return json_util.loads(json_util.dumps(result))


@app.get("/health")
def health() -> dict:
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict:
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serial_employee(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    query = {"department": department} if department is not None else {}
    offset = (page - 1) * page_size
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(offset).limit(page_size)
    items = [serial_employee(doc) for doc in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    instant = timestamp_datetime(body.punched_at)
    day = attendance_day(instant, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": instant,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            instant, employee["shift_start"], day
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serial_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    instant = timestamp_datetime(body.punched_at)
    query = {
        "emp_code": body.emp_code,
        "punch_in": {"$lte": instant},
    }
    record = db.attendance_logs.find_one(query, sort=[("punch_in", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")

    elapsed = (instant - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    day = parse_date(record["date"])
    hours = compute_work_hours(record["punch_in"], instant)
    overtime = compute_overtime(instant, employee["shift_end"], day, employee["shift_start"])
    update = {
        "$set": {
            "punch_out": instant,
            "work_hours": hours,
            "overtime_minutes": overtime,
            "half_day": hours < 4.50,
        }
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record was punched out concurrently")
    return serial_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    date_query: dict = {}
    if date_from is not None:
        require_date(date_from)
        date_query["$gte"] = date_from
    if date_to is not None:
        require_date(date_to)
        date_query["$lte"] = date_to
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if date_query:
        query["date"] = date_query
    if status is not None:
        query["status"] = status
    offset = (page - 1) * page_size
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(offset)
        .limit(page_size)
    )
    return {
        "items": [serial_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn) -> dict:
    try:
        path_day = parse_date(date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    requested = body.model_dump(exclude_unset=True)
    requested.pop("reason", None)
    requested.pop("regularized_by", None)
    new_status = requested.get("status", record["status"])
    if new_status in ("ABSENT", "LEAVE") and ("punch_in" in requested or "punch_out" in requested):
        raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = (
            timestamp_datetime(requested["punch_in"])
            if "punch_in" in requested
            else record.get("punch_in")
        )
        new_punch_out = (
            timestamp_datetime(requested["punch_out"])
            if "punch_out" in requested
            else record.get("punch_out")
        )
        if new_punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_day(new_punch_in, employee["shift_start"], employee["shift_end"]) != path_day:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if new_punch_out is not None:
            elapsed = (new_punch_out - new_punch_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if new_status in ("ABSENT", "LEAVE"):
        new_derived = {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        late = compute_late_minutes(
            new_punch_in, employee["shift_start"], path_day
        )
        if new_punch_out is None:
            hours, overtime, half_day = None, 0, False
        else:
            hours = compute_work_hours(new_punch_in, new_punch_out)
            overtime = compute_overtime(
                new_punch_out, employee["shift_end"], path_day, employee["shift_start"]
            )
            half_day = hours < 4.50
        new_derived = {
            "late_minutes": late,
            "work_hours": hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }

    desired = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        **new_derived,
    }
    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    before = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    changes = {
        key: {"from": before[key], "to": desired[key]}
        for key in tracked
        if before[key] != desired[key]
    }
    if not changes:
        raise HTTPException(422, "request does not change the attendance record")

    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    update_query: dict = {"_id": record["_id"]}
    if "history" in record:
        update_query["history"] = record["history"]
    else:
        update_query["history"] = {"$exists": False}
    # Protect concurrent corrections, including legacy documents with missing derived fields.
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if key in record:
            update_query[key] = record[key]
        else:
            update_query[key] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        update_query,
        {"$set": desired, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return serial_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str) -> dict:
    validate_month(month)
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    result = next(db.employees.aggregate(monthly_employee_pipeline(emp_code, month)), None)
    if result is None:
        raise HTTPException(404, "employee not found")
    result["present_days"] = round_half_up(result["present_days"], 2)
    result["attendance_pct"] = (
        round_half_up(result["attendance_pct"], 4)
        if result["attendance_pct"] is not None
        else None
    )
    return result


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None) -> dict:
    validate_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        item["avg_work_hours"] = (
            round_half_up(item["avg_work_hours"], 2)
            if item["avg_work_hours"] is not None
            else None
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
) -> dict:
    validate_month(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, department, limit)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)) -> dict:
    try:
        first, last = parse_date(from_), parse_date(to)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if last < first:
        raise HTTPException(422, "to must not be before from")
    if (last - first).days + 1 > 92:
        raise HTTPException(422, "trend range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(trend_pipeline(department, first, last)))
    for item in items:
        for field in ("attendance_rate", "moving_avg_7d"):
            if item[field] is not None:
                item[field] = round_half_up(item[field], 4)
        item["present_count"] = round_half_up(item["present_count"], 2)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> dict:
    if endpoint == "attendance_list":
        query: dict = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        date_query: dict = {}
        if date_from is not None:
            require_date(date_from)
            date_query["$gte"] = date_from
        if date_to is not None:
            require_date(date_to)
            date_query["$lte"] = date_to
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        if date_query:
            query["date"] = date_query
        if status is not None:
            query["status"] = status
        result = explain_find(
            "attendance_logs",
            query,
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            (page - 1) * page_size,
            page_size,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    if endpoint in ("employee_monthly", "department_summary", "late_leaderboard"):
        if month is None:
            raise HTTPException(422, "month is required")
        validate_month(month)
    if endpoint == "employee_monthly":
        if emp_code is None:
            raise HTTPException(422, "emp_code is required")
        pipeline = monthly_employee_pipeline(emp_code, month)
        collection = "employees"
    elif endpoint == "department_summary":
        pipeline = department_summary_pipeline(month, department)
        collection = "employees"
    elif endpoint == "late_leaderboard":
        pipeline = leaderboard_pipeline(month, department, limit)
        collection = "attendance_logs"
    else:
        if department is None or from_ is None or to is None:
            raise HTTPException(422, "department, from and to are required")
        try:
            first, last = parse_date(from_), parse_date(to)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if last < first or (last - first).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        pipeline = trend_pipeline(department, first, last)
        collection = "employees"
    result = explain_aggregate(collection, pipeline)
    return {"endpoint": endpoint, "collection": collection, "explain": result}
