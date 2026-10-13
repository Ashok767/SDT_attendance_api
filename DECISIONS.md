# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, race-safe create), `(department, joined_on)` and `(joined_on)` (headcount, trend, summary). `attendance_logs`: unique `(emp_code, date)` (one record per day, punch-in race, per-employee reads), `(date -1, emp_code 1)` (list order, month scans), `(status, date -1, emp_code 1)` (status filter), `(emp_code, punch_in -1)` (punch-out lookup). Rejected: an index on `late_minutes`; the month scan is already narrow.
2. **Punch-in race.** Both requests compute the same IST date and call `insert_one`. The unique `(emp_code, date)` index lets exactly one insert win; the loser gets `DuplicateKeyError`, which I turn into 409. There is no read before the write.
3. **Ties.** Ranking uses `$rank` after sorting by total late minutes, so ties share a rank and the next is skipped. `limit` filters on `rank <= limit`, so a tie at the cutoff returns every tied employee, more than `limit` rows. Ties are ordered by `emp_code`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and left-joins that month's logs, collapsed to one document per employee. Employees without logs still produce a row and add 1 to headcount.
5. **At 100x.** I would pre-aggregate daily per-department counters (written at punch time) instead of scanning a month of logs per request.
