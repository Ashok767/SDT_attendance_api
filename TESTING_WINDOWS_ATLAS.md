# Running the integration tests on Windows with MongoDB Atlas

The tests `tests/test_api.py` and `tests/check_scale.py` need a real MongoDB 6.0+ (Atlas M0 free tier runs a new enough version).
**Both drop their own throw-away databases (`attendance_test`, `attendance_scale`). Never point them at a cluster that holds real data.**

## 1. Create the Atlas cluster
1. Sign in at https://cloud.mongodb.com and create a free **M0** cluster (any region).
2. **Database Access -> Add New Database User**: password authentication, e.g. user `attendance`, a generated password.
   Role: *Atlas admin* (simplest) or *readWriteAnyDatabase*. (The tests create indexes and drop two databases.)
3. **Network Access -> Add IP Address**: "Add current IP address" (or `0.0.0.0/0` for a short-lived test cluster).
4. **Clusters -> Connect -> Drivers**: copy the string `mongodb+srv://attendance:<password>@cluster0.xxxxx.mongodb.net/`.
   If the password contains `@ : / ? # %`, URL-encode it (e.g. `@` -> `%40`).

## 2. Set up Python (PowerShell)
Install Python 3.11+ from python.org (tick "Add python.exe to PATH"), then in the project folder:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -r requirements.txt pytest httpx
```

## 3. Point the app at Atlas
```powershell
$env:MONGO_URI = "mongodb+srv://attendance:YOUR_PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority"
$env:MONGO_DB  = "attendance_db"      # the tests override this with their own throw-away DB names
```
(Variables last for this PowerShell window only. Do not commit them or a `.env` file.)

## 4. Run, in this order
```powershell
python -m unittest tests.test_rules tests.test_handlers_sim -v   # no database needed, expect 45 OK
python -m pytest tests/test_api.py -v                              # integration tests against Atlas
python -m tests.check_scale                                        # ~100k records, latency + IXSCAN/COLLSCAN report
```
Optional manual run: `uvicorn app.main:app --port 8000`, then open http://localhost:8000/docs and
`Invoke-RestMethod http://localhost:8000/health` (expect `status: ok`).

## 5. Reading the results
* A first failure in `test_api.py` is most likely a connection problem (IP not allow-listed, wrong password, SRV/DNS blocked by a VPN).
  `/health` returning 503 confirms it.
* `check_scale` prints one `OK`/`BAD` line per endpoint and per explain. `BAD ... COLLSCAN=True` means an index is not being used.
* Paste any failing output back so the code or test can be fixed.
