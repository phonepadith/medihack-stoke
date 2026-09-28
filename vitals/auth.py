"""Clinician accounts: registration, login, session.

The capture and scoring endpoints handle health data, so they are not left
open to anyone who can reach the port. Accounts stay deliberately thin --
username, display name, password -- because nothing more is needed to
attribute a measurement to the clinician who took it.

Standard library only: ``sqlite3`` for the store, ``hashlib.scrypt`` for the
password hash. Flask-Login, passlib and an ORM would be three more pinned
versions carrying a table with five columns.
"""
import functools
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

DB_PATH = os.environ.get("VITALS_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vitals.db")

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
MIN_PASSWORD = 8          # NIST SP 800-63B floor; length is the only rule we impose
MAX_PASSWORD = 1024       # scrypt on an unbounded string is a free CPU exhaustion

# scrypt at the parameters RFC 7914 suggests for interactive logins: ~16 MB and
# ~100 ms per hash, which is the point -- it prices a stolen-database guessing
# run far above what a plain SHA-256 would.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_MAXMEM = 64 * 1024 * 1024

# Throttle guessing without a dependency or a shared cache: count recent
# failures per username. This is per-process and resets on restart, which is
# the honest limit of an in-memory counter -- it blunts a script, not a
# distributed campaign.
FAIL_LIMIT = 8
FAIL_WINDOW = 900         # seconds
_fails: dict[str, list[float]] = {}

# Registration is open when REGISTER_CODE is unset, which is right for a local
# run and wrong for anything reachable from the internet: without it, everyone
# who finds the URL can issue themselves an account. Set it to a long random
# string and registration closes to whoever has been given that string.
_INVITE_KEY = "\x00invite"   # cannot collide with a username: the regex forbids \x00


def registration_code():
    return os.environ.get("REGISTER_CODE", "")


def registration_requires_code():
    return bool(registration_code())


# ── store ────────────────────────────────────────────────────────────────
@contextmanager
def _db():
    """One connection per operation.

    ``app.run(threaded=True)`` serves requests on different threads, and a
    sqlite3 connection may not cross threads; a module-level connection would
    need a lock around every call to stay correct.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _db() as conn:
        # WAL lets a reader run while another request writes, so a login does
        # not block behind the last_login_at update of another.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users(
              id            INTEGER PRIMARY KEY,
              username      TEXT NOT NULL UNIQUE,
              full_name     TEXT NOT NULL DEFAULT '',
              password_hash TEXT NOT NULL,
              created_at    TEXT NOT NULL,
              last_login_at TEXT
            )""")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── passwords ────────────────────────────────────────────────────────────
def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, maxmem=_MAXMEM, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${dk}".format(
        salt=salt.hex(), dk=dk.hex(), **_SCRYPT)


def verify_password(password, stored):
    """Constant-time check against a stored ``scrypt$...`` string.

    The parameters are read back from the record rather than assumed, so
    raising the cost later does not lock out existing accounts.
    """
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                            n=int(n), r=int(r), p=int(p),
                            dklen=len(expected) // 2, maxmem=_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), expected)


def _throttled(username):
    recent = [t for t in _fails.get(username, []) if time.time() - t < FAIL_WINDOW]
    _fails[username] = recent
    return len(recent) >= FAIL_LIMIT


def _record_failure(username):
    _fails.setdefault(username, []).append(time.time())


# ── users ────────────────────────────────────────────────────────────────
def create_user(username, password, full_name=""):
    """Insert a clinician. Raises ValueError on a taken username."""
    with _db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users(username, full_name, password_hash, created_at)"
                " VALUES(?,?,?,?)",
                (username, full_name.strip()[:120], hash_password(password), _now()))
        except sqlite3.IntegrityError:
            raise ValueError("username already taken")
        return {"id": cur.lastrowid, "username": username,
                "full_name": full_name.strip()[:120]}


def find_user(username):
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?",
                           (username,)).fetchone()
    return dict(row) if row else None


def current_user():
    """The logged-in clinician, or None.

    Re-read per request rather than trusted from the cookie: a deleted account
    must stop working immediately, and the signed cookie only proves that *we*
    issued it, not that the row still exists.
    """
    uid = session.get("uid")
    if uid is None:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT id, username, full_name, created_at, last_login_at"
            " FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        session.clear()
        return None
    return dict(row)


def login_required(view):
    """Reject anonymous callers with 401 JSON, never an HTML redirect.

    Every protected route here is consumed by fetch(); a redirect to a login
    page would arrive at the frontend as a 200 full of HTML and fail while
    parsing JSON, which is a much worse error to debug.
    """
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify({"error": "authentication required"}), 401
        request.user = user
        return view(*args, **kwargs)
    return wrapped


# ── routes ───────────────────────────────────────────────────────────────
def _payload():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


def _public(user):
    return {"username": user["username"], "full_name": user.get("full_name", "")}


@bp.post("/register")
def register():
    data = _payload()

    # Checked before anything else: someone without the code should not be able
    # to probe which usernames are taken, or spend our scrypt budget, by reading
    # the validation errors below.
    code = registration_code()
    if code:
        if _throttled(_INVITE_KEY):
            return jsonify({"error": "too many failed attempts, try again later"}), 429
        supplied = str(data.get("invite") or "")
        if not hmac.compare_digest(supplied.encode(), code.encode()):
            _record_failure(_INVITE_KEY)
            return jsonify({"error": "invalid or missing invite code"}), 403
        _fails.pop(_INVITE_KEY, None)

    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    full_name = data.get("full_name") or ""

    if not USERNAME_RE.match(username):
        return jsonify({"error": "username must be 3-32 characters, letters "
                                 "digits dot dash underscore"}), 400
    if len(password) < MIN_PASSWORD:
        return jsonify({"error": f"password must be at least {MIN_PASSWORD} "
                                 f"characters"}), 400
    if len(password) > MAX_PASSWORD:
        return jsonify({"error": "password too long"}), 400

    try:
        user = create_user(username, password, full_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    session.clear()
    session["uid"] = user["id"]
    session.permanent = True
    return jsonify({"user": _public(user)}), 201


@bp.post("/login")
def login():
    data = _payload()
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    if _throttled(username):
        return jsonify({"error": "too many failed attempts, try again later"}), 429

    user = find_user(username)
    # Verify even when the user does not exist, against a throwaway hash, so the
    # response time does not reveal which usernames are registered.
    stored = user["password_hash"] if user else hash_password(secrets.token_hex(8))
    if not verify_password(password, stored) or user is None:
        _record_failure(username)
        return jsonify({"error": "wrong username or password"}), 401

    _fails.pop(username, None)
    with _db() as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                     (_now(), user["id"]))
    session.clear()
    session["uid"] = user["id"]
    session.permanent = True
    return jsonify({"user": _public(user)})


@bp.post("/logout")
def logout():
    session.clear()
    return jsonify({"status": "ok"})


@bp.get("/me")
def me():
    user = current_user()
    if user is None:
        return jsonify({"error": "authentication required"}), 401
    return jsonify({"user": _public(user)})


# ── wiring ───────────────────────────────────────────────────────────────
def _secret_key():
    """Stable signing key, or every restart silently logs everyone out.

    Generated once next to the database and kept 0600 when SECRET_KEY is not
    supplied, so a local run needs no setup and a deployment can still inject
    the key through the environment.
    """
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), ".flask-secret")
    try:
        with open(path) as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


def init_app(app):
    app.secret_key = _secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Set COOKIE_SECURE=1 behind TLS. Not the default: the demo is served
        # over plain http on a LAN, where a secure cookie is never sent at all
        # and the login would appear to succeed and then do nothing.
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "") in ("1", "true", "yes"),
        PERMANENT_SESSION_LIFETIME=int(os.environ.get("SESSION_DAYS", "7")) * 86400,
    )
    init_db()
    app.register_blueprint(bp)
