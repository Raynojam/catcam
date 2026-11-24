"""Four-digit PIN access for the family lounge.

Anyone with the address and the PIN can watch. A four-digit PIN is guessable, so
failed attempts share one persistent counter (independent of client IP) and each
lockout is longer than the last: 5 wrong PINs lock the gate for 15 minutes, the
next 5 for 30, and so on up to a day. The count resets 24 hours after it started.
Session identifiers are random, stored hashed, and expire.
"""
from contextlib import closing
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

COOKIE_NAME = "__Host-cat_lounge"
SESSION_DAYS = 30
MAX_FAILURES = 5          # wrong PINs per lockout step
LOCK_SECONDS = 15 * 60    # first lockout; each further step adds the same again
WINDOW_SECONDS = 24 * 3600
MAX_LOCK_SECONDS = 24 * 3600
PIN_ITERATIONS = 310000


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def pin_record(pin):
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4}", pin):
        raise ValueError("Use exactly four digits")
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PIN_ITERATIONS).hex()
    return {"pin_salt": salt, "pin_hash": hashed, "pin_iterations": PIN_ITERATIONS}


def read_auth(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
        raise ValueError("Invalid access configuration")
    for key, length in (("invite_hash", 64), ("generation", 32), ("pin_salt", 32), ("pin_hash", 64)):
        if not isinstance(config.get(key), str) or not re.fullmatch(r"[0-9a-f]{"+str(length)+"}", config[key]):
            raise ValueError("Invalid access configuration")
    if config.get("pin_iterations") != PIN_ITERATIONS:
        raise ValueError("Invalid PIN hash parameters")
    if not isinstance(config.get("origin"), str):
        raise ValueError("Invalid access origin")
    origin = urlsplit(config["origin"])
    local_preview = origin.scheme == "http" and origin.hostname in ("localhost", "127.0.0.1")
    if (origin.scheme != "https" and not local_preview) or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:
        raise ValueError("Access origin must be an HTTPS origin")
    if config["origin"] != origin.scheme+"://"+origin.netloc:
        raise ValueError("Invalid access origin")
    return config


class LoginError(Exception):
    def __init__(self, status, message, retry=0, *, code=None):
        super().__init__(message)
        self.status, self.retry, self.code = status, retry, code


class Access:
    def __init__(self, config, database, clock=time.time):
        self.config_path, self.database, self.clock = Path(config), Path(database), clock
        read_auth(self.config_path)  # refuse startup rather than falling back to an open viewer
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.database, timeout=10)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, generation TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    generation TEXT PRIMARY KEY, failures INTEGER NOT NULL,
                    window_start REAL NOT NULL, locked_until REAL NOT NULL);
            """)
            db.commit()
        self.database.chmod(0o600)

    def settings(self):
        return read_auth(self.config_path)

    def login(self, invite, pin):
        """`invite` is ignored: the lounge is PIN-only. Kept so older gate pages still sign in."""
        config = self.settings()
        now = self.clock()
        with closing(sqlite3.connect(self.database, timeout=10)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT failures,window_start,locked_until FROM attempts WHERE generation=?",
                             (config["generation"],)).fetchone()
            failures, start, locked = row or (0, now, 0)
            if locked > now:
                raise LoginError(429, "Too many tries. Please try again in a little while.", math.ceil(locked-now))
            if now-start >= WINDOW_SECONDS:
                failures, start, locked = 0, now, 0
            valid = False
            if isinstance(pin, str) and re.fullmatch(r"[0-9]{4}", pin):
                candidate = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(config["pin_salt"]), PIN_ITERATIONS).hex()
                valid = hmac.compare_digest(candidate, config["pin_hash"])
            if not valid:
                failures += 1
                step = failures // MAX_FAILURES if failures % MAX_FAILURES == 0 else 0
                lock = min(MAX_LOCK_SECONDS, LOCK_SECONDS * step)
                locked = now+lock if lock else 0
                db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?)",
                           (config["generation"], failures, start, locked))
                db.commit()
                if locked:
                    minutes = math.ceil(lock / 60)
                    wait = f"{minutes} minutes" if minutes < 120 else f"{math.ceil(minutes / 60)} hours"
                    raise LoginError(429, f"Too many tries. Please try again in {wait}.", lock)
                raise LoginError(401, "That PIN did not match. Try again.")
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM attempts WHERE generation=?", (config["generation"],))
            db.execute("DELETE FROM sessions WHERE expires<=? OR generation!=?", (now, config["generation"]))
            # Bound persistent state even if a valid invite/PIN is used by many clients.
            db.execute("DELETE FROM sessions WHERE digest IN (SELECT digest FROM sessions ORDER BY expires DESC LIMIT -1 OFFSET 127)")
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), config["generation"], now+SESSION_DAYS*86400))
            db.commit()
        return token

    @staticmethod
    def token_from_cookie(header):
        if not header or len(header) > 8192:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(header)
            token = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else None
        except (CookieError, KeyError):
            return None
        return token if token and re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else None

    def authenticated(self, header):
        token = self.token_from_cookie(header)
        if not token:
            return False
        config = self.settings()
        with closing(sqlite3.connect(self.database, timeout=10)) as db:
            row = db.execute("SELECT expires,generation FROM sessions WHERE digest=?", (digest(token),)).fetchone()
        return bool(row and row[0] > self.clock() and hmac.compare_digest(row[1], config["generation"]))

    def logout(self, header):
        token = self.token_from_cookie(header)
        if token:
            with closing(sqlite3.connect(self.database, timeout=10)) as db:
                db.execute("DELETE FROM sessions WHERE digest=?", (digest(token),))
                db.commit()

    @staticmethod
    def cookie(token):
        return f"{COOKIE_NAME}={token}; Path=/; Max-Age={SESSION_DAYS*86400}; Secure; HttpOnly; SameSite=Lax"

    @staticmethod
    def clear_cookie():
        return f"{COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax"
