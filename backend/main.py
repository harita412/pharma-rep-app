"""Pharma Rep demo API: one login per pharma client, each client sees only its own space.

Run locally:  uvicorn main:app --env-file .env --port 8000
All settings come from environment variables (see .env.example).
"""

import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from insights import InsightError, generate_insight

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable {name}. See .env.example.")
    return value


DATABASE_URL = _require_env("DATABASE_URL")
SESSION_SECRET = _require_env("SESSION_SECRET")
ALLOWED_ORIGIN = _require_env("ALLOWED_ORIGIN").rstrip("/")
_require_env("ANTHROPIC_API_KEY")  # read by insights.py; checked here so a missing key fails at start-up
if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET must be at least 32 characters. See .env.example for how to make one.")

DAILY_REFRESH_LIMIT = 30
TOKEN_LIFETIME = timedelta(hours=12)
MAX_FAILED_LOGINS = 5
LOGIN_WINDOW_MINUTES = 15
LOCKOUT_MINUTES = 15
DOCTOR_ID = r"^[A-Za-z0-9_-]{1,32}$"

hasher = PasswordHasher()
# Checked when the username does not exist, so a wrong username takes as long as a wrong password.
DUMMY_HASH = hasher.hash("this-is-not-a-real-password")

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False, kwargs={"row_factory": dict_row})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    pool.open()
    yield
    pool.close()


# The interactive /docs pages are switched off because every route except
# /health and /login needs a token.
app = FastAPI(title="Pharma Rep demo API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


# --------------------------------------------------------------------- auth --

def _make_token(user_id: int, client_code: str) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": str(user_id), "client": client_code, "iat": now, "exp": now + TOKEN_LIFETIME}
    return jwt.encode(claims, SESSION_SECRET, algorithm="HS256")


class Session(BaseModel):
    user_id: int
    username: str
    client: str  # the space this login belongs to. It comes from the token, never from the request.


def current_session(authorization: str | None = Header(default=None)) -> Session:
    """Every route except /health and /login depends on this."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Please sign in.")
    try:
        claims = jwt.decode(token, SESSION_SECRET, algorithms=["HS256"], options={"require": ["exp", "sub", "client"]})
        user_id = int(claims["sub"])
        client = str(claims["client"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Your session has expired. Please sign in again.")
    except (jwt.InvalidTokenError, ValueError):
        raise HTTPException(status_code=401, detail="Please sign in.")
    # The user must still exist and still belong to the same space.
    with pool.connection() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = %s AND client_code = %s", (user_id, client)).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return Session(user_id=user_id, username=row["username"], client=client)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


def _client_info(cur, code: str) -> dict:
    cur.execute("SELECT code, name, product, theme FROM clients WHERE code = %s", (code,))
    return cur.fetchone()


@app.post("/login")
def login(body: LoginIn):
    username = body.username.strip().lower()
    with pool.connection() as conn, conn.cursor() as cur:
        # 1. Locked out? The answer is the same for real and made-up usernames.
        # The failed-attempt counters live on the user's own row in the users table.
        cur.execute("SELECT id, password_hash, client_code, locked_until FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        if user is not None and user["locked_until"] is not None and user["locked_until"] > datetime.now(timezone.utc):
            minutes = max(1, math.ceil((user["locked_until"] - datetime.now(timezone.utc)).total_seconds() / 60))
            raise HTTPException(status_code=429, detail=f"Too many wrong attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.")

        # 2. Check the password. A wrong username and a wrong password give the same error.
        ok = False
        try:
            hasher.verify(user["password_hash"] if user else DUMMY_HASH, body.password)
            ok = user is not None
        except (VerificationError, InvalidHashError):
            ok = False

        if not ok:
            if user is not None:
                cur.execute(
                    "UPDATE users SET "
                    "  failed_attempts = CASE WHEN window_start IS NULL OR window_start < now() - make_interval(mins => %s) THEN 1 ELSE failed_attempts + 1 END, "
                    "  window_start = CASE WHEN window_start IS NULL OR window_start < now() - make_interval(mins => %s) THEN now() ELSE window_start END, "
                    "  locked_until = CASE WHEN (CASE WHEN window_start IS NULL OR window_start < now() - make_interval(mins => %s) THEN 1 ELSE failed_attempts + 1 END) >= %s "
                    "                      THEN now() + make_interval(mins => %s) ELSE locked_until END "
                    "WHERE id = %s",
                    (LOGIN_WINDOW_MINUTES, LOGIN_WINDOW_MINUTES, LOGIN_WINDOW_MINUTES, MAX_FAILED_LOGINS, LOCKOUT_MINUTES, user["id"]),
                )
                conn.commit()
            raise HTTPException(status_code=401, detail="Wrong username or password.")

        cur.execute("UPDATE users SET failed_attempts = 0, window_start = NULL, locked_until = NULL WHERE id = %s", (user["id"],))
        client = _client_info(cur, user["client_code"])
    return {
        "token": _make_token(user["id"], user["client_code"]),
        "token_type": "bearer",
        "expires_in": int(TOKEN_LIFETIME.total_seconds()),
        "username": username,
        "client": client,
    }


api = APIRouter(dependencies=[Depends(current_session)])


# ------------------------------------------------------------------ helpers --

def _load_doctor(cur, client: str, doctor_id: str) -> dict:
    cur.execute(
        "SELECT id, name, specialty, hospital, city, persona, profile FROM doctors WHERE client_code = %s AND id = %s",
        (client, doctor_id),
    )
    doctor = cur.fetchone()
    if doctor is None:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    return doctor


def _profile_notes(doctor: dict) -> str:
    """Turns the doctor's stored profile into the plain text the AI reads."""
    p = doctor.get("profile") or {}
    parts = [f"Persona: {doctor['persona']}.", str(p.get("profile", "")).strip()]
    for label, key in (("Values", "motivators"), ("Concerns", "concerns")):
        if p.get(key):
            parts.append(f"{label}: " + "; ".join(map(str, p[key])) + ".")
    approach = p.get("approach") or {}
    for label, key in (("Do", "do"), ("Avoid", "dont")):
        if approach.get(key):
            parts.append(f"{label}: " + "; ".join(map(str, approach[key])) + ".")
    return " ".join(x for x in parts if x)


# Seed visits store a day offset; the date shown is worked out from today, so they never go stale.
EFFECTIVE_DATE = "CASE WHEN day_offset IS NOT NULL THEN CURRENT_DATE + day_offset ELSE visit_date END"
INSIGHT_COLUMNS = "id, doctor_id, next_visit_plan, talking_points, interest_score, what_changed, created_at"
VISIT_COLUMNS = (f"id, doctor_id, {EFFECTIVE_DATE} AS visit_date, day_offset, visit_time, objective, status, rep_notes, "
                 "outcome, extra, is_seed, created_at")


# ------------------------------------------------------------------- routes --

@app.get("/health")
def health():
    try:
        with pool.connection(timeout=3) as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "database": "ok"}
    except Exception as e:
        log.warning("Health check could not reach the database: %s", type(e).__name__)
        return {"status": "ok", "database": "unreachable"}


@api.get("/me")
def me(session: Session = Depends(current_session)):
    with pool.connection() as conn, conn.cursor() as cur:
        return {"username": session.username, "client": _client_info(cur, session.client)}


@api.get("/doctors")
def list_doctors(session: Session = Depends(current_session)):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, specialty, hospital, city, persona, profile FROM doctors WHERE client_code = %s ORDER BY id",
            (session.client,),
        )
        # The screens expect one flat record per doctor.
        return [{**{k: d[k] for k in ("id", "name", "specialty", "hospital", "city", "persona")}, **d["profile"]} for d in cur.fetchall()]


class VisitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: str = Field(pattern=DOCTOR_ID)
    visit_date: date
    rep_notes: str = Field(min_length=1, max_length=5000)
    outcome: str = Field(min_length=1, max_length=100)
    visit_time: dtime | None = None
    objective: str = Field(default="Visit notes", max_length=200)


@api.post("/visits", status_code=201)
def create_visit(visit: VisitIn, session: Session = Depends(current_session)):
    notes = visit.rep_notes.strip()
    if not notes:
        raise HTTPException(status_code=422, detail="Visit notes are empty.")
    with pool.connection() as conn, conn.cursor() as cur:
        _load_doctor(cur, session.client, visit.doctor_id)
        cur.execute(
            f"INSERT INTO visits (client_code, doctor_id, visit_date, visit_time, objective, status, rep_notes, outcome, extra) "
            f"VALUES (%s, %s, %s, COALESCE(%s, '09:00'::time), %s, 'completed', %s, %s, %s) RETURNING {VISIT_COLUMNS}",
            (session.client, visit.doctor_id, visit.visit_date, visit.visit_time, visit.objective.strip() or "Visit notes",
             notes, visit.outcome.strip(), Jsonb({"kind": "Visit notes"})),
        )
        return cur.fetchone()


@api.get("/visits")
def list_visits(doctor_id: str | None = Query(default=None, pattern=DOCTOR_ID), session: Session = Depends(current_session)):
    sql = f"SELECT {VISIT_COLUMNS} FROM visits WHERE client_code = %s"
    params: list = [session.client]
    with pool.connection() as conn, conn.cursor() as cur:
        if doctor_id:
            _load_doctor(cur, session.client, doctor_id)  # 404 if this space has no such doctor
            sql += " AND doctor_id = %s"
            params.append(doctor_id)
        sql += f" ORDER BY {EFFECTIVE_DATE} DESC, id DESC LIMIT 500"
        cur.execute(sql, params)
        return cur.fetchall()


@api.get("/visits/{visit_id}")
def get_visit(visit_id: int, session: Session = Depends(current_session)):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {VISIT_COLUMNS} FROM visits WHERE client_code = %s AND id = %s", (session.client, visit_id))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Visit not found.")
    return row


@api.get("/insights/latest")
def latest_insight(doctor_id: str = Query(pattern=DOCTOR_ID), session: Session = Depends(current_session)):
    """The most recent insight for a doctor, or null if there is none yet."""
    with pool.connection() as conn, conn.cursor() as cur:
        _load_doctor(cur, session.client, doctor_id)
        cur.execute(
            f"SELECT {INSIGHT_COLUMNS} FROM insights WHERE client_code = %s AND doctor_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (session.client, doctor_id),
        )
        return cur.fetchone()


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: str = Field(pattern=DOCTOR_ID)


@api.post("/insights/refresh", status_code=201)
def refresh_insight(body: RefreshIn, session: Session = Depends(current_session)):
    client = session.client
    # 1. Load what Claude needs, and claim one of today's refreshes.
    with pool.connection() as conn, conn.cursor() as cur:
        doctor = _load_doctor(cur, client, body.doctor_id)
        cur.execute(
            f"SELECT {EFFECTIVE_DATE} AS visit_date, outcome, rep_notes FROM visits "
            "WHERE client_code = %s AND doctor_id = %s AND rep_notes <> '' "
            f"ORDER BY {EFFECTIVE_DATE} ASC, id ASC",
            (client, body.doctor_id),
        )
        visits = cur.fetchall()
        cur.execute(
            "SELECT next_visit_plan, talking_points, interest_score, created_at FROM insights "
            "WHERE client_code = %s AND doctor_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (client, body.doctor_id),
        )
        last = cur.fetchone()
        # Adds 1 to today's count, but only while it is below the limit.
        cur.execute(
            "INSERT INTO insight_refresh_usage (client_code, usage_date, refresh_count) VALUES (%s, CURRENT_DATE, 1) "
            "ON CONFLICT (client_code, usage_date) DO UPDATE SET refresh_count = insight_refresh_usage.refresh_count + 1 "
            "WHERE insight_refresh_usage.refresh_count < %s RETURNING refresh_count",
            (client, DAILY_REFRESH_LIMIT),
        )
        claimed = cur.fetchone()
    if claimed is None:
        raise HTTPException(status_code=429, detail=f"This demo space has used all {DAILY_REFRESH_LIMIT} insight refreshes for today. Try again tomorrow.")

    # 2. Ask Claude (outside the database connection, as it can take a while).
    try:
        result = generate_insight({**doctor, "profile_notes": _profile_notes(doctor)}, visits, last)
    except InsightError as e:
        _release_refresh(client)  # a failed attempt does not count
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        _release_refresh(client)
        log.exception("Unexpected error while generating an insight")
        raise HTTPException(status_code=502, detail="Something went wrong while generating the insight. Try again.")

    # 3. Save it.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO insights (client_code, doctor_id, next_visit_plan, talking_points, interest_score, what_changed) "
            f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {INSIGHT_COLUMNS}",
            (client, body.doctor_id, result["next_visit_plan"], Jsonb(result["talking_points"]),
             result["interest_score"], result["what_changed"]),
        )
        saved = cur.fetchone()
    saved["refreshes_left_today"] = DAILY_REFRESH_LIMIT - claimed["refresh_count"]
    return saved


def _release_refresh(client: str) -> None:
    try:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE insight_refresh_usage SET refresh_count = GREATEST(refresh_count - 1, 0) "
                "WHERE client_code = %s AND usage_date = CURRENT_DATE",
                (client,),
            )
    except Exception:
        log.exception("Could not give back an insight refresh")


@api.post("/reset")
def reset_space(session: Session = Depends(current_session)):
    """Deletes this space's insights and the visits reps added. Doctors, sample visits and the daily limit stay."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM insights WHERE client_code = %s", (session.client,))
        insights_deleted = cur.rowcount
        cur.execute("DELETE FROM visits WHERE client_code = %s AND NOT is_seed", (session.client,))
        visits_deleted = cur.rowcount
    return {"visits_deleted": visits_deleted, "insights_deleted": insights_deleted}


app.include_router(api)
