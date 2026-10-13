# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index and unique `attendance_logs(emp_code, date)` index enforce identity and one record per employee/day. Attendance date/employee and date/status indexes support listing and monthly filters; employee/punch-in supports selecting the latest punch. Employee department/join-date and joined-date indexes support department summaries and trends. I rejected a standalone status index because status has few values and works better alongside date filters.

2. **Punch-in race.** Concurrent requests may both look up the employee, but inserts are protected by the unique attendance index. MongoDB accepts one; the other raises `DuplicateKeyError`, which the API returns as 409.

3. **Ties.** MongoDB competition ranking gives tied employees the same rank. The API filters on rank after ranking, so every employee tied at the requested cutoff is returned, then orders ties by `emp_code`.

4. **Headcount.** Department summary starts from employees who joined by month-end and looks up logs. Employees with no logs still count; later joiners are excluded.

5. **100x data.** I would benchmark real query plans first, then consider sharding attendance by employee and date or archiving older records, guided by measured bottlenecks.
