# REVIEW.md

Defects found in the starter `app/main.py`. "Fixed" means the fix is in the submitted file; the pure-rule fixes (items 3-7) are covered by `tests/test_rules.py`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | No `tz_aware`, so PyMongo returns naive datetimes; they were then mixed with aware IST values. | `compute_overtime` raises `TypeError` (naive minus aware) -> 500. | `tz_aware=True, tzinfo=UTC`; every conversion goes through `ms_to_dt` / `dt_to_ms`. |
| 2 | startup | No indexes at all, no unique index on `emp_code` or (`emp_code`,`date`). | `explain` shows COLLSCAN; slow at 100k rows; duplicates under parallel requests. | `ensure_indexes()` at startup (lifespan), idempotent. |
| 3 | `compute_late_minutes` | Uses `int(seconds/60) > 10`: 09:40:01 gives 10 whole minutes, not > 10, so 0 instead of 10. Start time built with `replace()` on the punch's own (naive/UTC) clock, not IST; ignores overnight shifts; no second truncation. | Punch 09:40:01 for shift 09:30 returns 0 (R2 says 10). | Compare **seconds** to 600, then floor to minutes, using IST shift start on the attendance date. |
| 4 | `compute_work_hours` | Python `round()` is banker's/float rounding. | 2.025 h returns 2.02, R4 wants 2.03. | `Decimal` with `ROUND_HALF_UP` on whole seconds. |
| 5 | `compute_overtime` | No 30-minute minimum (R3); overnight shift end not moved to the next day; mixes naive/aware datetimes. | Punch-out 5 min after shift end returns 5, expected 0; overnight returns 0 or a huge negative-clamped value. | Floor minutes, apply the >= 30 threshold, add one day for overnight shifts. |
| 6 | `punch_in` | `datetime.fromtimestamp(ms/1000)` = server-local naive time; `date` therefore depends on the server's zone, not IST (R1); overnight rule missing; microseconds kept. | Run the server in UTC: a punch at 01:30 IST gets the previous date. | `ms_to_punch` (UTC, whole seconds) and `attendance_date()` (IST + overnight). |
| 7 | `punch_in` | `if body.punched_at` treats `0` as "missing"; no range check, so seconds-looking values, bools and floats are accepted; `status` is a free string (accepts `ABSENT`, `foo`). | `punched_at=1783312500` (seconds) creates a 1970 record. | `EpochMs` = strict int in [1e11, 4102444800000]; `status` is a `Literal` of the 3 presence values. |
| 8 | `punch_in` | Unknown employee: `emp` is `None` -> `TypeError` -> 500. | POST with an unknown `emp_code`. | Explicit 404. |
| 9 | `punch_in` | Check-then-insert race: two parallel requests both pass `find_one` and both insert. | 20 parallel identical requests give several 201s. | Unique index (`emp_code`,`date`) + catch `DuplicateKeyError` -> 409. |
| 10 | `punch_in` response | Returns `_id` as `id` (must never leave the API) and raw datetimes instead of epoch ms. | Response has `id` and ISO strings. | `serialize_attendance()`. |
| 11 | `create_employee` | No validation (code format, e-mail, equal shifts, bad dates, lengths); check-then-insert race instead of a unique index; `created_at = datetime.now()` is naive local time and returned as a datetime. | Two parallel creates -> two 201s; `shift_start == shift_end` accepted. | Pydantic constraints, unique index + `DuplicateKeyError` -> 409, `created_at` stored as UTC and returned in ms. |
| 12 | `list_employees` | `skip = page * page_size` skips the first page (should be `(page-1)*page_size`); `total` counts the whole collection, ignoring the filter; no sort (spec: `emp_code` asc); `page`/`page_size` unbounded (0, negative, 10^9). | `page=1` returns page 2; `department=X` shows the global total. | Correct skip, `count_documents(q)`, sort, `Query(ge=1)` / `le=100`. |
| 13 | `list_attendance` | Loads the whole collection and sorts in Python (memory/latency at 100k); sorts by `date` only (spec: then `emp_code`); no date validation, no `date_from > date_to` 422; `page=0` gives negative slices; `page_size` unbounded; `status` unvalidated; exposes `_id`. | `/attendance` on 100k rows; `page=0`; `date_from=garbage`. | DB-side filter/sort/skip/limit, supporting indexes, validation, serializer. |
| 14 | `/health` | Always "ok", never touches MongoDB (spec: 503 when Mongo is down). | Stop MongoDB, still 200. | `ping`, 503 on failure. |
| 15 | `PunchInIn` | `Optional[int]` coerces `true` to 1 and accepts floats like `1783312500000.0`. | `punched_at: true`. | `StrictInt`. |

**Examined and left as is:** `load_dotenv()` (does not override real environment variables, as required); the 201 status codes; defaulting shifts to 09:30-18:30; the `{"status": "ok"}` body.

**Decisions on ambiguous points:** a punch-out finds the latest record with `punch_in <= punched_at` and returns 409 if that record is already closed (checked before the 422 time checks); the 92-day trend limit counts days inclusively; the 7-day moving average is taken over the *reported* (rounded) rates.
