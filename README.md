# Library Lending System

**Language:** Python (FastAPI) &nbsp;|&nbsp; **Needs:** Postgres + Redis

This is a **starter**. The application already works. Your job is everything
that gets it building, tested and running in CI.

---

## You do not need Python installed

You will build this into a container, and the container brings its own
Python 3.12. You are not being asked to extend the app — you are being asked
to ship it.

---

## 1. What this app needs

| | |
|---|---|
| **Runtime** | Python 3.12 |
| **Install dependencies** | `pip install -r requirements.txt` |
| **Start the app** | `uvicorn app.main:app --host 0.0.0.0 --port 8080` |
| **Listens on** | port 8080, bound to `0.0.0.0` |
| **Environment variables** | `DATABASE_URL`, `REDIS_URL` |
| **Needs running first** | Postgres, Redis, and the migrations applied |

### What it does

Students borrow and return books. The system tracks due dates, charges a late fine with a grace period and a weekend exemption, caps how many books one person may hold, and refuses to lend to anyone with unpaid fines or an overdue book already out.

### Endpoints

```
GET  /health
GET  /books                          every title with copies / available
GET  /books/{id}/availability        Redis-cached, says whether it was a cache hit
GET  /members/{id}                   loans, fines, and the running fine on open loans
POST /borrow                         {"member_id":1,"book_id":3}
POST /return                         {"loan_id":5}   -> returns the fine charged
POST /loans/{id}/renew               extends the due date, twice at most
POST /members/{id}/fines/pay         clears everything outstanding
```

`/health` reports Postgres and Redis **separately**. If it says
`postgres: false` the app started fine and your compose wiring is wrong —
do not go looking in the application code.

### Migrations

`migrations/` holds `.sql` files applied **in filename order** before the app
starts. They create the tables and insert sample data. A container running
`psql` over them in order is enough; you do not need a migration tool.

---

## 2. What you must write

| File | What it has to do |
|---|---|
| `Dockerfile` | Install dependencies **before** copying source, pin the base image, do not run as root. |
| `docker-compose.yml` | App + Postgres + Redis + a migration step, one `docker compose up`. |
| `.circleci/config.yml` | lint → unit tests → integration tests → secret scan → image build |
| Unit tests | For `app/fines.py`. No database, no network. |
| Integration tests | Against a real Postgres and Redis as CircleCI service containers. |

Then push your image to **your own Docker Hub account**, tagged `:1.0`.

### When it works

```bash
docker compose up --build
curl localhost:8080/health
```

```json
{"status":"ok","postgres":true,"redis":true}
```

---

## Where the marks are

`app/fines.py` is **pure logic** — plain functions over plain data, no
database and no HTTP. Start your tests there. Use pytest:
`pytest --cov=app --cov-report=term-missing`. Minimum 70%.

Start with `chargeable_days`. Pick a Friday, return the book the following Tuesday, and work out on paper what the answer should be before you write the assertion.

## Why Redis is here

Two different jobs, and it is worth keeping them apart in your head. `avail:{book_id}` caches the answer to "has this book got a free copy" for 30 seconds, because the catalogue page asks it constantly. `lock:borrow:{book_id}` is a short SET NX PX lock held only while one borrow is being confirmed, released with a token check so a slow request can never delete a lock that has already expired and been taken by somebody else.

## The hard part

The last copy of a book, requested by two students at once. Exactly one should succeed.

The Redis lock in `app/main.py` is not the answer - it is a convenience. Redis locks expire, and a process can be paused between taking the lock and using it. Find the two things in this codebase that make the guarantee real: the conditional `UPDATE ... WHERE status='available' ... RETURNING` inside a single transaction, and the partial unique index `loans_one_active_per_copy`. Explain why either one alone would still be correct, why the UPDATE must not be split into a SELECT followed by an UPDATE on a second connection, and what the client should see when it loses - a 409, not a 500.

Write your answer in your README. It is worth more marks than the feature.

### Analysis & Solution: Concurrency Guarantees for the Last-Copy Race

#### 1. Why the Redis Lock is Only a Convenience, Not the Guarantee
The Redis lock (`lock:borrow:{book_id}` via `SET NX PX 4000`) in `app/main.py` is an optimistic optimization, not a correctness guarantee:
- **Lock Expiration During Process Stalls:** If a worker process experiences an operating system scheduling stall, Python GIL contention, garbage collection delay, or database latency longer than 4000ms, Redis will automatically expire the key.
- **Lost Mutual Exclusion:** Once expired, a second worker process racing for the same book will successfully acquire the lock. Both workers now operate under the assumption that they have exclusive access to the copy.
- **Distributed Failure Modes:** Network splits, clock drift, or Redis restarts can also compromise client-side lock state.

Thus, the Redis lock serves only as a politeness mechanism to reduce unnecessary load on PostgreSQL by rejecting concurrent requests before they hit the database. It cannot provide ACID serializability.

#### 2. The Two Real Guarantees in this Codebase
The actual safety guarantees are enforced at the database level by PostgreSQL:

1. **The Atomic Conditional UPDATE with Row-Level Locking:**
   ```sql
   UPDATE copies SET status='on_loan' WHERE id = (
     SELECT id FROM copies WHERE book_id=%s AND status='available'
     ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
   ) RETURNING id, barcode
   ```
   *(Found in `app/main.py`, lines 132–136)*
   This query runs inside a single database transaction. The subquery selects an available copy while locking the row exclusively (`FOR UPDATE SKIP LOCKED`). When two workers execute this concurrently:
   - Worker 1 locks the row and updates its status to `'on_loan'`.
   - Worker 2 skips the locked row (`SKIP LOCKED`). Because no other copy has `status='available'`, the subquery returns no rows, and the outer UPDATE updates 0 rows.
   - `cur.fetchone()` returns `None`, and the code raises `HTTPException(409, "no_copies_available")`.

2. **The Partial Unique Index `loans_one_active_per_copy`:**
   ```sql
   CREATE UNIQUE INDEX IF NOT EXISTS loans_one_active_per_copy
       ON loans (copy_id) WHERE returned_on IS NULL;
   ```
   *(Found in `migrations/001_schema.sql`, lines 35–36)*
   This index enforces invariant uniqueness at the storage engine level. In PostgreSQL's B-tree index, there can only ever be at most one row for a given `copy_id` where `returned_on IS NULL`. Even if the conditional UPDATE did not exist and two connections simultaneously issued `INSERT INTO loans` for the same copy, PostgreSQL would reject the second transaction with a unique constraint violation (`23505`).

#### 3. Why Either One Alone Would Still Be Correct
- **Conditional UPDATE alone (without the partial unique index):**
  PostgreSQL ensures row-level mutual exclusion during updates. Only one transaction can successfully match `status='available'` and mutate the status to `'on_loan'`. The losing transaction updates 0 rows, receives `None`, and aborts before issuing an `INSERT INTO loans`. Double-lending is impossible.
- **Partial Unique Index alone (without the conditional UPDATE):**
  If two transactions concurrently attempt to insert an active loan for the same copy, PostgreSQL's index uniqueness constraint guarantees that one transaction commits and the other is rejected with a unique constraint violation. Two active loans for the same physical copy can never coexist in the database.

#### 4. Why the UPDATE Must NOT Be Split into a SELECT Followed by an UPDATE
Splitting the operation into a `SELECT` followed by an `UPDATE` on a separate query/connection introduces a classic **Time-of-Check to Time-of-Use (TOCTOU)** race condition:
1. Connection 1 queries `SELECT id FROM copies WHERE book_id=3 AND status='available'` and sees copy 6 available.
2. Connection 2 queries `SELECT id FROM copies WHERE book_id=3 AND status='available'` concurrently. Under standard `READ COMMITTED` transaction isolation, Connection 2 also sees copy 6 available.
3. Connection 1 executes `UPDATE copies SET status='on_loan' WHERE id=6` and succeeds.
4. Connection 2 executes `UPDATE copies SET status='on_loan' WHERE id=6` on its connection. If unconditional, it overwrites the state. Even if conditional, Connection 2 believed the copy was available and proceeded to insert a second active loan, causing a double-borrow or duplicate loan records.

By keeping the selection and mutation combined into a single atomic statement inside one connection and one transaction, the row lock is acquired and the status updated in one indivisible operation.

#### 5. What the Client Should See When It Loses: A 409, Not a 500
- **500 Internal Server Error:** Indicates an unhandled exception, syntax failure, server crash, or infrastructure breakdown (e.g., database unavailable).
- **409 Conflict:** RFC 9110 specifies that 409 indicates the request could not be completed due to a conflict with the current state of the resource.
- When two students race for the last copy of a book, losing the race is a normal, expected business outcome. The application did not fail; rather, the requested resource is in conflict with another transaction that claimed it first. The losing client should see `409 Conflict` (with payload `{"detail": "no_copies_available"}`), allowing client applications to gracefully notify the user that the book was just taken by someone else.

---

## Getting unstuck

| Symptom | Almost always |
|---|---|
| `/health` says `postgres: false` | Wrong hostname. In compose the host is the **service name**, not `localhost`. |
| Page will not load, logs fine | No `ports:` mapping, or bound to `127.0.0.1` not `0.0.0.0`. |
| `relation "..." does not exist` | Migrations did not run, or the app started before they finished. |
| Build takes minutes each time | `COPY . .` is above your dependency install. |
| CI cannot reach the database | In CircleCI service containers the host **is** `localhost` — opposite of compose. |
