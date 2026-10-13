# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # MONGO_URI / MONGO_DB (real environment variables take priority)
python sample_seed.py           # optional: load the nine sample documents
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (`$setWindowFields`, `$rank`, `$lookup` with `localField` + `pipeline`). Indexes are created at startup.

## Tests
* `python -m unittest tests.test_rules tests.test_handlers_sim -v`: stdlib only, no database. 31 rule tests (R1-R5, rounding,
  calendar) + 14 simulation tests that run the real handlers against an in-memory fake of the collections.
  The simulation does **not** prove anything about MongoDB itself.
* `pytest tests/test_api.py`: integration tests, **need a running MongoDB** (throw-away DB `attendance_test`).
* `python -m tests.check_scale`: loads ~100k records and reports latency and IXSCAN/COLLSCAN per endpoint (needs MongoDB).
* Windows + Atlas walkthrough: `TESTING_WINDOWS_ATLAS.md`.

## Status
All endpoints in `openapi.yaml` are implemented. The author's environment had no MongoDB and no network, so the HTTP layer,
pydantic validation, the aggregation pipelines, the unique-index race handling and the explain plans have **not** been
executed against a real database. Run `tests/test_api.py` and `tests/check_scale.py` before relying on them.
