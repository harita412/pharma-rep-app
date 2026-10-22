# Pharma Rep demo: one link, a separate space for each pharma client

Everyone opens the same website link. Each pharma client signs in with its **own username and password** and sees **only its own space**: its own doctors, visits, notes and AI insights. Nothing is shared between spaces.

```
 Browser (frontend/index.html)  ──  login page, then the app
        │  HTTPS, header "Authorization: Bearer <signed token>"
        ▼
 Backend API (backend/, FastAPI in a Docker container)
        │                         │
        ▼                         ▼
 PostgreSQL (db/schema.sql)    Claude API (Anthropic)
```

**Demo only.** All doctors are fictional. Never type real patient details into the app.

| Space code | Company | Product | Login |
|---|---|---|---|
| `demo1` | Mylin Biotech | Zavetox | `mylin_demo` |
| `demo2` | Eldroga | Nitrotrim | `eldroga_demo` |

---

## How the spaces work, in plain words

- Every doctor, visit and insight in the database is tagged with a space code (`demo1`, `demo2`, ...).
- When someone logs in, the server puts their space code **inside a signed token**. Every later request carries that token, and the server reads the space from it. The page cannot ask for another space, because nothing in a request names one.
- Asking for a doctor or visit that belongs to another space gets a plain "not found".
- **Reset demo** clears only that space's own notes and insights. The sample doctors and sample visits stay.
- Each space starts with its own set of fictional doctors and visits, worded differently, so two companies never see the same people.

**What you hand to each client:** the same website link, plus their username and password. Nothing else.

---

## What each file does

| File | In plain words |
|---|---|
| `frontend/index.html` | The whole app in one page: the login screen, then every screen of your prototype. It holds **no** doctors, visits, keys or passwords. The only setting in it is the backend address (`API_CONFIG.baseUrl`, near the top of the script). After login it loads the signed-in space's doctors and visits from the backend. |
| `backend/main.py` | The API, the "front desk". It checks logins, signs the tokens, checks the token on every request, reads and writes only the signed-in space's data, and enforces the limit of 30 insight refreshes per space per day. |
| `backend/insights.py` | The part that talks to Claude. It packs the doctor's profile, the visit notes and the last insight into one request and turns the answer into a plan, talking points, an interest score and "what changed". |
| `backend/seed.py` | Sets up the database. It creates the tables, loads both spaces, and creates the two logins with random passwords that are printed **once** and never saved. It also adds new spaces and changes passwords (see below). Safe to run twice. |
| `backend/seed_data/demo1.json`, `demo2.json` | The sample doctors and visits for each space. `demo1` is your prototype's data unchanged. `demo2` has four different doctors and its own visits. Visit dates are stored as "days from today", so upcoming and past visits are always right. |
| `backend/requirements.txt` | The Python libraries the backend needs. |
| `backend/requirements-dev.txt` | Extra libraries, only for running the tests. |
| `backend/tests/` | The automatic checks (see "Run the tests"). |
| `backend/Dockerfile` | A recipe that packs the backend into a container, so it runs the same way on your laptop and on AWS. |
| `backend/.env.example` | A template for the settings and secrets. Copy it to `.env` and fill it in. Never share or commit the real `.env`. |
| `backend/.dockerignore` | Keeps `.env` and other local files out of the container. |
| `db/schema.sql` | The database layout (version 2). `seed.py` runs it for you. |
| `db/migrate_v2.sql` | One-time step if you have the **old** version 1 tables. It renames them (nothing is deleted). |

### Settings (environment variables, in `backend/.env`)

| Name | What it is |
|---|---|
| `DATABASE_URL` | Where the database is, for example `postgresql://pharma_app:PASSWORD@HOST:5432/pharma_demo`. On AWS RDS add `?sslmode=require`. |
| `ANTHROPIC_API_KEY` | The key for the Claude API. |
| `SESSION_SECRET` | A long random secret (at least 32 characters) used to sign login tokens. Make one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Changing it signs everybody out. |
| `ALLOWED_ORIGIN` | The single web address allowed to call the API from a browser, for example `https://demo.example.com`. No trailing `/`. |
| `CLAUDE_MODEL` | Optional. The Claude model id. Defaults to `claude-opus-5-5`. |

There is no shared access token any more. Logins replace it.

### API endpoints

Everything except `/health` and `/login` needs the header `Authorization: Bearer <token>`. The space always comes from the token.

| Endpoint | What it does |
|---|---|
| `GET /health` | Is the service up, and can it reach the database? Needs no login, so AWS can use it as a health check. |
| `POST /login` | Body `{"username","password"}`. Returns a signed token (valid 12 hours) and the space's name, product and theme. A wrong username and a wrong password give the same message. After 5 wrong tries for a username within 15 minutes, that username is locked for 15 minutes. |
| `GET /me` | Who is signed in and which space. |
| `GET /doctors` | This space's doctors, with their full profiles. |
| `GET /visits?doctor_id=` | This space's visits, newest first. `doctor_id` is optional. |
| `GET /visits/{id}` | One visit (404 if it belongs to another space). |
| `POST /visits` | Saves a visit. Body `{"doctor_id","visit_date","rep_notes","outcome"}` (optional `visit_time`, `objective`). |
| `GET /insights/latest?doctor_id=` | The most recent saved insight for a doctor, or `null`. |
| `POST /insights/refresh` | Body `{"doctor_id"}`. Asks Claude for a new insight from the saved notes. 30 per space per day (a failed attempt does not count). |
| `POST /reset` | Deletes this space's saved insights and the visits reps added. Keeps the sample doctors and sample visits. |

---

## Run it on your own computer (Windows)

You need **Python 3.12**, **PostgreSQL 16 or newer** and an Anthropic API key.

**1. Backend packages and settings**

```powershell
cd C:\Users\LENOVO\pharma-demo\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # skip this if .env already exists
notepad .env                # fill in DATABASE_URL, ANTHROPIC_API_KEY and SESSION_SECRET
```

**2. The database** (pick the case that fits)

*A. A brand-new empty database.* In pgAdmin or psql, as `postgres`:

```sql
CREATE DATABASE pharma_demo;
CREATE USER pharma_app WITH PASSWORD 'choose-a-password';
```
Then connect to `pharma_demo` and run `GRANT USAGE, CREATE ON SCHEMA public TO pharma_app;`.

*B. You already have the old version 1 tables* (the earlier demo). In pgAdmin, open a **Query Tool on the `pharma_demo` database** (check the tab title says `pharma_demo`), logged in as `postgres`, open `db/migrate_v2.sql` and run it. It renames the five old tables to `..._v1` (nothing is deleted) and lets `pharma_app` create the new ones.

**3. Create the tables, the spaces and the logins**

```powershell
python seed.py
```

It prints each username and a random password **once**. Write them down now. Running it again changes nothing and shows no passwords.

**4. Start the backend** (leave this window open)

```powershell
uvicorn main:app --env-file .env --port 8000
```
Open http://localhost:8000/health. You should see `{"status":"ok","database":"ok"}`.

**5. Start the page** (a second PowerShell window)

```powershell
cd C:\Users\LENOVO\pharma-demo\frontend
python -m http.server 5500
```
Open http://localhost:5500 and sign in. Use exactly that address: it must match `ALLOWED_ORIGIN`.

---

## Everyday jobs

**Add a third space** (a new client, with its own copy of the sample doctors and visits):

```powershell
python seed.py add-space demo3 "Acme Pharma" "Zentrol" mylin
```
`demo3` is the code, then the company name, the product name and the colour theme (`mylin` is teal, `eldroga` is purple). It creates the login `demo3_demo` and prints its password once. Add `--username NAME` to choose a different login name. The copied doctors are a copy of demo1's; edit `seed_data/` or the database if you want different people.

**Change a password** (also lifts a lock-out):

```powershell
python seed.py set-password mylin_demo
```
It asks for a new password (at least 10 characters). Press Enter to get a random one instead.

**Start a client with a clean slate:** sign in as that client and use **Reset demo** on the Post-Visit Summary screen. It clears only that space.

**Sign everybody out at once:** change `SESSION_SECRET` and restart the backend.

---

## Run the tests

The checks need a database. Use a throwaway one, or the real one: the tests create their own spaces (`test_a`, `test_b`, `test_c`) and delete them afterwards, and they never touch `demo1` or `demo2`.

```powershell
cd C:\Users\LENOVO\pharma-demo\backend
pip install -r requirements-dev.txt
$env:TEST_DATABASE_URL = "postgresql://pharma_app:PASSWORD@localhost:5432/pharma_demo"
python -m pytest tests -v
```

They check that each login sees only its own company and doctors, that a note saved in one space never appears in another, that another space's doctors and visits return 404, that bad tokens and wrong passwords are refused and the 6th wrong attempt is locked out, that Reset touches only its own space and keeps the sample data, that a second login sees the same notes, and that the page source contains no key, password or secret.

---

## Deploy on AWS (RDS + App Runner + Amplify)

**1. Database (Amazon RDS for PostgreSQL).** Create an instance and set *Public access* to No. From a machine inside the same VPC (an EC2 instance, or CloudShell in VPC mode), create the database and the `pharma_app` user as in "A" above. Your `DATABASE_URL` is `postgresql://pharma_app:PASSWORD@<rds-endpoint>:5432/pharma_demo?sslmode=require`.

**2. Secrets.** Store `DATABASE_URL`, `ANTHROPIC_API_KEY` and `SESSION_SECRET` in **AWS Secrets Manager**. Do not put them in plain environment variables.

**3. Load the data.** From a machine that can reach the database, with `DATABASE_URL` set to the RDS address, run `python seed.py` from the `backend` folder. Write down the two passwords it prints.

**4. Push the container image to Amazon ECR** (needs the AWS CLI and Docker):

```powershell
aws ecr create-repository --repository-name pharma-demo-api
aws ecr get-login-password | docker login --username AWS --password-stdin <account-id>.dkr.ecr.<region>.amazonaws.com
docker build -t pharma-demo-api backend
docker tag pharma-demo-api <account-id>.dkr.ecr.<region>.amazonaws.com/pharma-demo-api:latest
docker push <account-id>.dkr.ecr.<region>.amazonaws.com/pharma-demo-api:latest
```

**5. Create the App Runner service.**

- **Source:** the ECR image above. **Port:** `8000`. **Health check:** HTTP, path `/health`.
- **Environment:** add the three secrets from Secrets Manager, and set `ALLOWED_ORIGIN` (the page's address from step 6) and, if you need it, `CLAUDE_MODEL`.
- **Networking:** set outgoing traffic to *Custom VPC* with a **VPC connector** in the database's subnets. In the RDS security group allow port 5432 from the connector's security group. All outgoing traffic then leaves through your VPC, so those subnets need a **NAT gateway** so the backend can reach the Claude API (it has an hourly cost).
- App Runner gives you an `https://….awsapprunner.com` address. Open `/health` there to check it.

If App Runner isn't offered in your account or region, the same container runs on **Amazon ECS with Fargate** with the same settings.

**6. Host the page on AWS Amplify.**

1. In `frontend/index.html`, set `API_CONFIG.baseUrl` to the App Runner address. That is the only edit.
2. Host the file on HTTPS with **Amplify Hosting** (drag and drop), or S3 + CloudFront.
3. Put that exact page address in `ALLOWED_ORIGIN` on App Runner and redeploy.
4. Share the page link and each client's username and password.

---

## Good to know

- **Passwords** are stored only as argon2 hashes. **Tokens** are signed with `SESSION_SECRET`, last 12 hours, and are kept in the browser's session storage, so a refresh keeps you signed in and closing the tab signs you out. Logging in on another device shows the same notes, because notes are stored on the server.
- **Lock-out:** 5 wrong attempts for a username within 15 minutes locks that username for 15 minutes. The counter is in the database, so it survives restarts. A made-up username is locked in the same way, so nobody can tell which usernames exist.
- **Sample records that are not stored per space.** The Compliance flags, follow-up list, scientific questions, opportunities and activity feed are still the prototype's sample records, shown the same way in every space (names in them are filled in from that space's own doctors, and dates move with today's date). Actions you take on them last until the page is refreshed. Doctors, visits, notes and insights are the parts saved in the database.
- **The visit workspace** (pre-visit briefing, concerns, next actions) works on the loaded visits but does not save its edits. The notes saved from the **Post-Visit Summary** screen are the ones stored.
- **Insight quality depends on the notes.** Claude is told to use only the profile and notes, not to invent study results or safety claims, and to suggest correcting claims the rep shouldn't have made. Someone must still check every point before it is used with a doctor.
- **Limits:** visit notes up to 5,000 characters; each refresh sends the 50 most recent notes for that doctor; answers are capped at 800 tokens.
- **"Can't reach the demo server" but `/health` works?** The page's address probably doesn't match `ALLOWED_ORIGIN` exactly. Protocol, domain and port must all match, for example `http://localhost:5500`.
- **`python seed.py` says it found the old version 1 tables?** Run `db/migrate_v2.sql` once first (step 2B above).
