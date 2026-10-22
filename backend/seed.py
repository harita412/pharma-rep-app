"""Sets up the database and creates the logins. Safe to run twice: it never deletes or overwrites anything.

    python seed.py                                    create tables, load demo1 and demo2, create their logins
    python seed.py add-space CODE NAME PRODUCT THEME  add another space (a copy of demo1's doctors and visits)
    python seed.py add-user USERNAME CODE             add another login to an existing space (e.g. demo2)
    python seed.py set-password USERNAME              change one login's password

Random passwords are printed once on screen and are NOT saved anywhere. Only a hash is stored.
Reads DATABASE_URL from the environment (or from backend/.env).
"""

import argparse
import getpass
import json
import os
import re
import secrets
import string
import sys
from pathlib import Path

import psycopg
from argon2 import PasswordHasher
from psycopg.types.json import Jsonb

HERE = Path(__file__).resolve().parent
SCHEMA_FILE = HERE.parent / "db" / "schema.sql"
SEED_DIR = HERE / "seed_data"
CODE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
THEMES = ("mylin", "eldroga")  # colour schemes the page knows about
MIN_PASSWORD = 10

hasher = PasswordHasher()


def random_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def load_env() -> None:
    env_file = HERE / ".env"
    if env_file.exists() and "DATABASE_URL" not in os.environ:
        try:
            from dotenv import dotenv_values
        except ImportError:
            return
        for k, v in dotenv_values(env_file).items():
            if v is not None:
                os.environ.setdefault(k, v)


def connect() -> psycopg.Connection:
    load_env()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        sys.exit("DATABASE_URL is not set. Put it in backend/.env (see .env.example).")
    return psycopg.connect(url)


# ------------------------------------------------------------------- schema --

def ensure_schema(conn: psycopg.Connection) -> None:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'clients' AND column_name = 'code'")
    has_clients = cur.fetchone() is not None
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'clients' AND column_name = 'product'")
    if has_clients and cur.fetchone() is None:
        sys.exit(
            "Found the old version 1 tables. Run db/migrate_v2.sql once first, as the postgres user "
            "(in pgAdmin, on the pharma_demo database). See the README, then run this again."
        )
    cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()


# --------------------------------------------------------------------- data --

def create_user(conn: psycopg.Connection, username: str, client_code: str, password: str | None = None):
    """Creates a login. Returns the password if it was created now, or None if the login already existed."""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
    if cur.fetchone() is not None:
        return None
    password = password or random_password()
    cur.execute("INSERT INTO users (username, password_hash, client_code) VALUES (%s, %s, %s)",
                (username, hasher.hash(password), client_code))
    return password


def load_space(conn: psycopg.Connection, space: dict, code: str | None = None) -> None:
    """Adds a space's client row, doctors and sample visits. Existing rows are left alone."""
    code = code or space["code"]
    cur = conn.cursor()
    cur.execute("INSERT INTO clients (code, name, product, theme) VALUES (%s, %s, %s, %s) ON CONFLICT (code) DO NOTHING",
                (code, space["name"], space["product"], space["theme"]))
    for d in space["doctors"]:
        cur.execute(
            "INSERT INTO doctors (client_code, id, name, specialty, hospital, city, persona, profile) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (client_code, id) DO NOTHING",
            (code, d["id"], d["name"], d["specialty"], d["hospital"], d["city"], d["persona"], Jsonb(d["profile"])),
        )
    for v in space["visits"]:
        cur.execute(
            "INSERT INTO visits (client_code, doctor_id, visit_date, day_offset, visit_time, objective, status, rep_notes, outcome, extra, is_seed) "
            "VALUES (%s, %s, CURRENT_DATE + %s::int, %s::int, %s::time, %s, %s, %s, %s, %s, true) "
            "ON CONFLICT (client_code, (extra ->> 'key')) WHERE is_seed DO NOTHING",
            (code, v["doctor_id"], v["day_offset"], v["day_offset"], v["visit_time"], v["objective"], v["status"],
             v["rep_notes"], v["outcome"], Jsonb({**v["extra"], "key": v["key"]})),
        )


def read_space(code: str) -> dict:
    return json.loads((SEED_DIR / f"{code}.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------- commands --

def cmd_seed() -> None:
    with connect() as conn:
        ensure_schema(conn)
        created = []
        for code in ("demo1", "demo2"):
            space = read_space(code)
            load_space(conn, space)
            pw = create_user(conn, space["username"], code)
            created.append((space["name"], space["product"], space["username"], pw))
        conn.commit()
    print("\nDatabase ready: 2 spaces loaded (4 doctors and 16 visits each).\n")
    for name, product, username, pw in created:
        if pw:
            print(f"  {name} ({product})\n    username: {username}\n    password: {pw}")
        else:
            print(f"  {name} ({product})\n    username: {username}   (already existed, password unchanged)")
    if any(pw for *_, pw in created):
        print("\nWrite these passwords down NOW. They are shown only once and are not stored anywhere.")
        print("Lost one? Run: python seed.py set-password USERNAME")


def cmd_add_space(args: argparse.Namespace) -> None:
    code = args.code.lower()
    if not CODE_RE.match(code):
        sys.exit("CODE may only contain lowercase letters, digits, - and _ (max 32), for example demo3.")
    if args.theme not in THEMES:
        sys.exit(f"THEME must be one of: {', '.join(THEMES)}.")
    username = (args.username or f"{code}_demo").lower()
    with connect() as conn:
        ensure_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM clients WHERE code = %s", (code,))
        if cur.fetchone() is not None:
            sys.exit(f"A space with code '{code}' already exists.")
        space = read_space("demo1")
        space.update(name=args.name, product=args.product, theme=args.theme)
        load_space(conn, space, code)
        pw = create_user(conn, username, code)
        conn.commit()
    if pw is None:
        print(f"\nSpace '{code}' created, but the username '{username}' already existed, so no login was added.")
        print("Pick another with:  python seed.py add-space ... --username NAME")
        return
    print(f"\nSpace '{code}' created: {args.name} - {args.product}, with a copy of demo1's doctors and visits.")
    print(f"  username: {username}\n  password: {pw}")
    print("\nWrite the password down NOW. It is shown only once and is not stored anywhere.")


def cmd_set_password(args: argparse.Namespace) -> None:
    username = args.username.lower()
    with connect() as conn:
        ensure_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
        if cur.fetchone() is None:
            sys.exit(f"No login called '{username}'.")
        typed = getpass.getpass(f"New password for {username} (press Enter to make a random one): ")
        if typed:
            if len(typed) < MIN_PASSWORD:
                sys.exit(f"Use at least {MIN_PASSWORD} characters.")
            if getpass.getpass("Type it again: ") != typed:
                sys.exit("The two passwords differ. Nothing was changed.")
            pw, shown = typed, False
        else:
            pw, shown = random_password(), True
        cur.execute("UPDATE users SET password_hash = %s WHERE username = %s", (hasher.hash(pw), username))
        cur.execute("UPDATE users SET failed_attempts = 0, window_start = NULL, locked_until = NULL WHERE username = %s", (username,))  # also lifts any lock-out
        conn.commit()
    print(f"\nPassword changed for {username}." + (f"\n  new password: {pw}\nWrite it down NOW. It is shown only once." if shown else ""))


def cmd_add_user(args: argparse.Namespace) -> None:
    username, code = args.username.lower(), args.code.lower()
    if not re.match(r"^[a-z0-9_.@-]{3,64}$", username):
        sys.exit("USERNAME must be 3 to 64 characters: letters, digits and . _ - @ only.")
    with connect() as conn:
        ensure_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT name, product FROM clients WHERE code = %s", (code,))
        space = cur.fetchone()
        if space is None:
            sys.exit(f"No space called '{code}'. Spaces: demo1 (Mylin Biotech), demo2 (Eldroga), or one you added.")
        pw = create_user(conn, username, code)
        if pw is None:
            sys.exit(f"A login called '{username}' already exists. To change its password: python seed.py set-password {username}")
        conn.commit()
    print(f"\nLogin created in {space[0]} - {space[1]} (space {code}).\n  username: {username}\n  password: {pw}")
    print(f"\nWrite the password down NOW. It is shown only once. To choose your own later: python seed.py set-password {username}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")
    a = sub.add_parser("add-space", help="add another space")
    a.add_argument("code")
    a.add_argument("name")
    a.add_argument("product")
    a.add_argument("theme", help="one of: " + ", ".join(THEMES))
    a.add_argument("--username", help="login name (default: CODE_demo)")
    s = sub.add_parser("set-password", help="change a login's password")
    s.add_argument("username")
    u = sub.add_parser("add-user", help="add another login to an existing space")
    u.add_argument("username")
    u.add_argument("code", help="the space's code, for example demo2")
    args = p.parse_args()
    if args.command == "add-space":
        cmd_add_space(args)
    elif args.command == "add-user":
        cmd_add_user(args)
    elif args.command == "set-password":
        cmd_set_password(args)
    else:
        cmd_seed()


if __name__ == "__main__":
    main()
