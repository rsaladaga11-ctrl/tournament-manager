"""
Tournament Manager - Flask web app.

Admins / Tournament Coordinators create tournaments (Swiss Bo1 or Bo3, optional
Bo3 single-elimination Top Cut). Players log in, join, see pairings and report
their own match results.

Run:  python app.py   then open http://localhost:5000
The first account registered becomes the Admin.
"""
import os
import random
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.environ.get("TOURNAMENT_DB", os.path.join(INSTANCE_DIR, "tournament.db"))

app = Flask(__name__)
os.makedirs(INSTANCE_DIR, exist_ok=True)


def _secret_key():
    path = os.path.join(INSTANCE_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.config["SECRET_KEY"] = _secret_key()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    game TEXT,
    event_date TEXT,
    location TEXT,
    swiss_format TEXT NOT NULL CHECK (swiss_format IN ('bo1','bo3')),
    swiss_rounds INTEGER NOT NULL DEFAULT 0,      -- 0 = automatic at start
    topcut_size INTEGER NOT NULL DEFAULT 0,       -- 0 = no top cut
    max_players INTEGER NOT NULL DEFAULT 0,       -- 0 = unlimited
    status TEXT NOT NULL DEFAULT 'registration',  -- registration|swiss|topcut|finished
    current_round INTEGER NOT NULL DEFAULT 0,
    champion_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    dropped INTEGER NOT NULL DEFAULT 0,
    dropped_round INTEGER,
    seed INTEGER,                                 -- top cut seed
    UNIQUE (tournament_id, user_id)
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,                          -- swiss|topcut
    round INTEGER NOT NULL,
    table_no INTEGER,                             -- table (swiss) or bracket slot (topcut)
    p1_id INTEGER NOT NULL,
    p2_id INTEGER,                                -- NULL = bye
    p1_wins INTEGER NOT NULL DEFAULT 0,
    p2_wins INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',       -- pending|reported|confirmed
    reported_by INTEGER,
    updated_at TEXT
);
"""


# ---------------------------------------------------------------- db helpers

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def q(sql, args=(), one=False):
    cur = db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- auth / csrf

def current_user():
    uid = session.get("uid")
    if uid is None:
        return None
    if "user" not in g:
        g.user = q("SELECT * FROM users WHERE id = ?", (uid,), one=True)
    return g.user


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))
        if not u["is_admin"]:
            abort(403)
        return f(*a, **kw)
    return wrapper


@app.before_request
def _csrf_protect():
    if request.method == "POST":
        token = session.get("csrf")
        if not token or token != request.form.get("csrf"):
            abort(400, "Form expired - please go back, refresh and try again.")


@app.context_processor
def _inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"me": current_user(), "csrf": session["csrf"], "engine": engine}


@app.template_filter("pct")
def _pct(v):
    return f"{v * 100:.2f}%"


# ---------------------------------------------------------------- tournament helpers

def get_tournament(tid):
    t = q("SELECT * FROM tournaments WHERE id = ?", (tid,), one=True)
    if not t:
        abort(404)
    return t


def entries_for(tid):
    return q("""SELECT e.*, u.display_name, u.username FROM entries e
                JOIN users u ON u.id = e.user_id
                WHERE e.tournament_id = ? ORDER BY u.display_name COLLATE NOCASE""", (tid,))


def match_dict(m):
    return {"id": m["id"], "p1": m["p1_id"], "p2": m["p2_id"], "p1_wins": m["p1_wins"],
            "p2_wins": m["p2_wins"], "draws": m["draws"], "slot": m["table_no"],
            "has_result": m["status"] != "pending"}


def swiss_matches(tid):
    return [match_dict(m) for m in
            q("SELECT * FROM matches WHERE tournament_id = ? AND stage = 'swiss'", (tid,))]


def standings_for(t):
    ents = entries_for(t["id"])
    names = {e["user_id"]: e["display_name"] for e in ents}
    rows = engine.compute_standings([e["user_id"] for e in ents], swiss_matches(t["id"]),
                                    t["swiss_format"], names)
    dropped = {e["user_id"]: e["dropped"] for e in ents}
    for r in rows:
        r["dropped"] = bool(dropped.get(r["player"]))
    return rows


def round_matches(tid, stage, rnd):
    return q("SELECT * FROM matches WHERE tournament_id = ? AND stage = ? AND round = ? "
             "ORDER BY table_no IS NULL, table_no", (tid, stage, rnd))


def outstanding_count(t):
    if t["status"] not in ("swiss", "topcut"):
        return 0
    row = q("SELECT COUNT(*) c FROM matches WHERE tournament_id = ? AND stage = ? AND round = ? "
            "AND status = 'pending'", (t["id"], t["status"], t["current_round"]), one=True)
    return row["c"]


def create_swiss_round(t, rnd):
    tid = t["id"]
    ranked = [r["player"] for r in standings_for(t) if not r["dropped"]]
    pairs, bye = engine.pair_swiss_round(ranked, swiss_matches(tid), random.Random(), rnd)
    stamp = now()
    for table, (a, b) in enumerate(pairs, 1):
        db().execute("INSERT INTO matches (tournament_id, stage, round, table_no, p1_id, p2_id, updated_at) "
                     "VALUES (?, 'swiss', ?, ?, ?, ?, ?)", (tid, rnd, table, a, b, stamp))
    if bye is not None:
        bye_games = 2 if t["swiss_format"] == "bo3" else 1
        db().execute("INSERT INTO matches (tournament_id, stage, round, table_no, p1_id, p2_id, p1_wins, "
                     "status, updated_at) VALUES (?, 'swiss', ?, NULL, ?, NULL, ?, 'confirmed', ?)",
                     (tid, rnd, bye, bye_games, stamp))
    db().execute("UPDATE tournaments SET status = 'swiss', current_round = ? WHERE id = ?", (rnd, tid))


def insert_topcut_round(tid, rnd, slots):
    stamp = now()
    for slot, a, b in slots:
        db().execute("INSERT INTO matches (tournament_id, stage, round, table_no, p1_id, p2_id, updated_at) "
                     "VALUES (?, 'topcut', ?, ?, ?, ?, ?)", (tid, rnd, slot, a, b, stamp))
    db().execute("UPDATE tournaments SET status = 'topcut', current_round = ? WHERE id = ?", (rnd, tid))


def result_choices(t, m):
    if m["stage"] == "topcut":
        codes = engine.RESULTS_TOPCUT
    else:
        codes = engine.RESULTS_BO3 if t["swiss_format"] == "bo3" else engine.RESULTS_BO1
    return codes


def result_label(code, p1, p2, bo1=False):
    a, b, d = engine.parse_result(code)
    if bo1:
        return {"1-0-0": f"{p1} wins", "0-1-0": f"{p2} wins", "0-0-1": "Draw"}[code]
    if a == b:
        return "Intentional draw (0-0)" if a == 0 else f"Draw {a}-{b}"
    winner = p1 if a > b else p2
    return f"{winner} wins {max(a, b)}-{min(a, b)}"


def format_score(m, t):
    if m["p2_id"] is None:
        return "BYE"
    if m["status"] == "pending":
        return "–"
    if m["stage"] == "swiss" and t["swiss_format"] == "bo1":
        if m["draws"]:
            return "Draw"
        return "1-0" if m["p1_wins"] else "0-1"
    s = f"{m['p1_wins']}-{m['p2_wins']}"
    if m["draws"]:
        s += f"-{m['draws']}"
    return s


app.jinja_env.globals.update(result_choices=result_choices, result_label=result_label,
                             format_score=format_score)


# ---------------------------------------------------------------- auth routes

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        name = request.form.get("display_name", "").strip() or username
        pw = request.form.get("password", "")
        if len(username) < 3 or len(pw) < 4:
            flash("Username must be at least 3 characters and password at least 4.", "error")
        elif q("SELECT 1 FROM users WHERE username = ?", (username,), one=True):
            flash("That username is taken.", "error")
        else:
            first = q("SELECT COUNT(*) c FROM users", one=True)["c"] == 0
            cur = db().execute("INSERT INTO users (username, display_name, password_hash, is_admin, created_at) "
                               "VALUES (?, ?, ?, ?, ?)",
                               (username, name, generate_password_hash(pw), 1 if first else 0, now()))
            db().commit()
            session["uid"] = cur.lastrowid
            flash("Account created. You are the Admin (first account)." if first else "Welcome!", "ok")
            return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE username = ?", (request.form.get("username", "").strip(),), one=True)
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session["uid"] = u["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        flash("Wrong username or password.", "error")
    no_users = q("SELECT COUNT(*) c FROM users", one=True)["c"] == 0
    return render_template("login.html", no_users=no_users)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("uid", None)
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    me = current_user()
    if request.method == "POST":
        name = request.form.get("display_name", "").strip()
        new_pw = request.form.get("new_password", "")
        if not check_password_hash(me["password_hash"], request.form.get("password", "")):
            flash("Current password is wrong.", "error")
        else:
            if name:
                db().execute("UPDATE users SET display_name = ? WHERE id = ?", (name, me["id"]))
            if new_pw:
                if len(new_pw) < 4:
                    flash("New password must be at least 4 characters.", "error")
                    return redirect(url_for("account"))
                db().execute("UPDATE users SET password_hash = ? WHERE id = ?",
                             (generate_password_hash(new_pw), me["id"]))
            db().commit()
            flash("Account updated.", "ok")
        return redirect(url_for("account"))
    return render_template("account.html")


# ---------------------------------------------------------------- admin: users

@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            username = request.form.get("username", "").strip()
            name = request.form.get("display_name", "").strip() or username
            pw = request.form.get("password", "")
            if len(username) < 3 or len(pw) < 4:
                flash("Username must be at least 3 characters and password at least 4.", "error")
            elif q("SELECT 1 FROM users WHERE username = ?", (username,), one=True):
                flash("That username is taken.", "error")
            else:
                db().execute("INSERT INTO users (username, display_name, password_hash, is_admin, created_at) "
                             "VALUES (?, ?, ?, ?, ?)", (username, name, generate_password_hash(pw),
                                                        1 if request.form.get("is_admin") else 0, now()))
                db().commit()
                flash(f"Account '{username}' created.", "ok")
        else:
            uid = int(request.form.get("user_id", 0))
            if uid == current_user()["id"] and action == "toggle_admin":
                flash("You can't remove your own Admin role.", "error")
            elif action == "toggle_admin":
                db().execute("UPDATE users SET is_admin = 1 - is_admin WHERE id = ?", (uid,))
                db().commit()
                flash("Role updated.", "ok")
            elif action == "reset_password":
                pw = request.form.get("password", "")
                if len(pw) < 4:
                    flash("Password must be at least 4 characters.", "error")
                else:
                    db().execute("UPDATE users SET password_hash = ? WHERE id = ?",
                                 (generate_password_hash(pw), uid))
                    db().commit()
                    flash("Password reset.", "ok")
        return redirect(url_for("users"))
    rows = q("SELECT * FROM users ORDER BY is_admin DESC, display_name COLLATE NOCASE")
    return render_template("users.html", users=rows)


# ---------------------------------------------------------------- tournaments

@app.route("/")
@login_required
def index():
    ts = q("""SELECT t.*, (SELECT COUNT(*) FROM entries e WHERE e.tournament_id = t.id) AS players,
                     (SELECT 1 FROM entries e WHERE e.tournament_id = t.id AND e.user_id = ?) AS joined,
                     u.display_name AS champion_name
              FROM tournaments t LEFT JOIN users u ON u.id = t.champion_id
              ORDER BY CASE t.status WHEN 'finished' THEN 1 ELSE 0 END, t.event_date DESC, t.id DESC""",
           (current_user()["id"],))
    return render_template("index.html", tournaments=ts)


def _read_tournament_form():
    f = request.form
    data = {
        "name": f.get("name", "").strip(),
        "game": f.get("game", "").strip(),
        "event_date": f.get("event_date", "").strip(),
        "location": f.get("location", "").strip(),
        "swiss_format": f.get("swiss_format", "bo1"),
        "swiss_rounds": int(f.get("swiss_rounds") or 0),
        "topcut_size": int(f.get("topcut_size") or 0),
        "max_players": int(f.get("max_players") or 0),
    }
    errors = []
    if not data["name"]:
        errors.append("Tournament name is required.")
    if data["swiss_format"] not in ("bo1", "bo3"):
        errors.append("Choose Bo1 or Bo3 for Swiss.")
    if data["topcut_size"] not in engine.TOPCUT_SIZES:
        errors.append("Invalid Top Cut size.")
    if not 0 <= data["swiss_rounds"] <= 20:
        errors.append("Swiss rounds must be between 1 and 20 (or blank for automatic).")
    return data, errors


@app.route("/t/new", methods=["GET", "POST"])
@admin_required
def tournament_new():
    if request.method == "POST":
        data, errors = _read_tournament_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("tournament_form.html", t=data, editing=False)
        cur = db().execute(
            "INSERT INTO tournaments (name, game, event_date, location, swiss_format, swiss_rounds, topcut_size, "
            "max_players, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data["name"], data["game"], data["event_date"], data["location"], data["swiss_format"],
             data["swiss_rounds"], data["topcut_size"], data["max_players"], current_user()["id"], now()))
        db().commit()
        flash("Tournament created. Players can now join.", "ok")
        return redirect(url_for("tournament", tid=cur.lastrowid, tab="players"))
    blank = {"name": "", "game": "", "event_date": datetime.now().strftime("%Y-%m-%d"), "location": "",
             "swiss_format": "bo1", "swiss_rounds": 0, "topcut_size": 0, "max_players": 0}
    return render_template("tournament_form.html", t=blank, editing=False)


@app.route("/t/<int:tid>/edit", methods=["GET", "POST"])
@admin_required
def tournament_edit(tid):
    t = get_tournament(tid)
    if t["status"] != "registration":
        flash("Settings are locked once the tournament has started.", "error")
        return redirect(url_for("tournament", tid=tid))
    if request.method == "POST":
        data, errors = _read_tournament_form()
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("tournament_form.html", t={**dict(t), **data}, editing=True)
        db().execute("UPDATE tournaments SET name=?, game=?, event_date=?, location=?, swiss_format=?, "
                     "swiss_rounds=?, topcut_size=?, max_players=? WHERE id=?",
                     (data["name"], data["game"], data["event_date"], data["location"], data["swiss_format"],
                      data["swiss_rounds"], data["topcut_size"], data["max_players"], tid))
        db().commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("tournament", tid=tid))
    return render_template("tournament_form.html", t=t, editing=True)


@app.route("/t/<int:tid>/delete", methods=["POST"])
@admin_required
def tournament_delete(tid):
    get_tournament(tid)
    db().execute("DELETE FROM matches WHERE tournament_id = ?", (tid,))
    db().execute("DELETE FROM entries WHERE tournament_id = ?", (tid,))
    db().execute("DELETE FROM tournaments WHERE id = ?", (tid,))
    db().commit()
    flash("Tournament deleted.", "ok")
    return redirect(url_for("index"))


@app.route("/t/<int:tid>")
@login_required
def tournament(tid):
    t = get_tournament(tid)
    me = current_user()
    ents = entries_for(tid)
    names = {e["user_id"]: e["display_name"] for e in ents}
    my_entry = next((e for e in ents if e["user_id"] == me["id"]), None)

    default_tab = {"registration": "players", "swiss": "pairings", "topcut": "bracket",
                   "finished": "bracket" if t["topcut_size"] else "standings"}[t["status"]]
    tab = request.args.get("tab", default_tab)

    # Pairings tab: which round to show
    swiss_rounds_played = q("SELECT COALESCE(MAX(round), 0) r FROM matches WHERE tournament_id = ? "
                            "AND stage = 'swiss'", (tid,), one=True)["r"]
    view_round = request.args.get("round", type=int) or (
        t["current_round"] if t["status"] == "swiss" else swiss_rounds_played)
    pairings = round_matches(tid, "swiss", view_round) if view_round else []

    # Bracket
    bracket = []
    tc_rounds = q("SELECT DISTINCT round FROM matches WHERE tournament_id = ? AND stage = 'topcut' "
                  "ORDER BY round", (tid,))
    seeds = {e["user_id"]: e["seed"] for e in ents if e["seed"]}
    size = t["topcut_size"]
    for r in tc_rounds:
        ms = round_matches(tid, "topcut", r["round"])
        bracket.append({"round": r["round"], "name": engine.topcut_round_name(len(ms) * 2), "matches": ms})
    # placeholder rounds for the rest of the bracket
    if size and t["status"] in ("topcut", "swiss", "registration"):
        n = size // (2 ** len(bracket))
        while n >= 2:
            bracket.append({"round": None, "name": engine.topcut_round_name(n), "matches": [None] * (n // 2)})
            n //= 2

    # The logged-in player's current match
    my_match = None
    if my_entry and t["status"] in ("swiss", "topcut"):
        my_match = q("SELECT * FROM matches WHERE tournament_id = ? AND stage = ? AND round = ? "
                     "AND (p1_id = ? OR p2_id = ?)", (tid, t["status"], t["current_round"], me["id"], me["id"]),
                     one=True)

    all_users = q("SELECT id, display_name, username FROM users WHERE id NOT IN "
                  "(SELECT user_id FROM entries WHERE tournament_id = ?) ORDER BY display_name COLLATE NOCASE",
                  (tid,)) if me["is_admin"] else []

    return render_template(
        "tournament.html", t=t, tab=tab, entries=ents, names=names, my_entry=my_entry,
        standings=standings_for(t) if tab == "standings" or t["status"] == "finished" else [],
        pairings=pairings, view_round=view_round, swiss_rounds_played=swiss_rounds_played,
        bracket=bracket, seeds=seeds, my_match=my_match, outstanding=outstanding_count(t),
        all_users=all_users, active_count=sum(1 for e in ents if not e["dropped"]),
        recommended_rounds=engine.recommended_swiss_rounds(len(ents)))


# ---------------------------------------------------------------- registration

def _can_register(t):
    if t["status"] != "registration":
        return "Registration is closed."
    if t["max_players"]:
        count = q("SELECT COUNT(*) c FROM entries WHERE tournament_id = ?", (t["id"],), one=True)["c"]
        if count >= t["max_players"]:
            return "The tournament is full."
    return None


@app.route("/t/<int:tid>/join", methods=["POST"])
@login_required
def join(tid):
    t = get_tournament(tid)
    err = _can_register(t)
    if err:
        flash(err, "error")
    else:
        db().execute("INSERT OR IGNORE INTO entries (tournament_id, user_id) VALUES (?, ?)",
                     (tid, current_user()["id"]))
        db().commit()
        flash("You're registered. Good luck!", "ok")
    return redirect(url_for("tournament", tid=tid))


@app.route("/t/<int:tid>/leave", methods=["POST"])
@login_required
def leave(tid):
    t = get_tournament(tid)
    if t["status"] == "registration":
        db().execute("DELETE FROM entries WHERE tournament_id = ? AND user_id = ?", (tid, current_user()["id"]))
        db().commit()
        flash("You left the tournament.", "ok")
    return redirect(url_for("tournament", tid=tid))


@app.route("/t/<int:tid>/drop", methods=["POST"])
@login_required
def drop(tid):
    """Player drops themselves (or Admin drops/undrops a player)."""
    t = get_tournament(tid)
    me = current_user()
    uid = int(request.form.get("user_id", me["id"]))
    if uid != me["id"] and not me["is_admin"]:
        abort(403)
    if t["status"] not in ("swiss", "topcut"):
        flash("Drops only apply once the tournament is running.", "error")
        return redirect(url_for("tournament", tid=tid))
    e = q("SELECT * FROM entries WHERE tournament_id = ? AND user_id = ?", (tid, uid), one=True)
    if not e:
        abort(404)
    if e["dropped"]:
        if not me["is_admin"]:
            abort(403)
        db().execute("UPDATE entries SET dropped = 0, dropped_round = NULL WHERE id = ?", (e["id"],))
        flash("Player reinstated for the next round.", "ok")
    else:
        db().execute("UPDATE entries SET dropped = 1, dropped_round = ? WHERE id = ?", (t["current_round"], e["id"]))
        flash("Dropped. You won't be paired in future rounds." if uid == me["id"] else "Player dropped.", "ok")
    db().commit()
    return redirect(url_for("tournament", tid=tid, tab="players"))


@app.route("/t/<int:tid>/players/add", methods=["POST"])
@admin_required
def player_add(tid):
    t = get_tournament(tid)
    if t["status"] != "registration":
        flash("Registration is closed.", "error")
        return redirect(url_for("tournament", tid=tid, tab="players"))
    ids = request.form.getlist("user_id")
    for uid in ids:
        db().execute("INSERT OR IGNORE INTO entries (tournament_id, user_id) VALUES (?, ?)", (tid, int(uid)))
    db().commit()
    flash(f"{len(ids)} player(s) added.", "ok")
    return redirect(url_for("tournament", tid=tid, tab="players"))


@app.route("/t/<int:tid>/players/<int:uid>/remove", methods=["POST"])
@admin_required
def player_remove(tid, uid):
    t = get_tournament(tid)
    if t["status"] != "registration":
        flash("Use Drop once the tournament has started.", "error")
    else:
        db().execute("DELETE FROM entries WHERE tournament_id = ? AND user_id = ?", (tid, uid))
        db().commit()
        flash("Player removed.", "ok")
    return redirect(url_for("tournament", tid=tid, tab="players"))


# ---------------------------------------------------------------- running the event

@app.route("/t/<int:tid>/start", methods=["POST"])
@admin_required
def start(tid):
    t = get_tournament(tid)
    if t["status"] != "registration":
        abort(400)
    n = q("SELECT COUNT(*) c FROM entries WHERE tournament_id = ?", (tid,), one=True)["c"]
    if n < 2:
        flash("You need at least 2 players to start.", "error")
        return redirect(url_for("tournament", tid=tid, tab="players"))
    if t["topcut_size"] and n < t["topcut_size"]:
        flash(f"Top {t['topcut_size']} needs at least {t['topcut_size']} players (you have {n}). "
              "Lower the Top Cut in Settings or add players.", "error")
        return redirect(url_for("tournament", tid=tid, tab="players"))
    if not t["swiss_rounds"]:
        db().execute("UPDATE tournaments SET swiss_rounds = ? WHERE id = ?",
                     (engine.recommended_swiss_rounds(n), tid))
    create_swiss_round(get_tournament(tid), 1)
    db().commit()
    flash("Round 1 is paired. Good luck everyone!", "ok")
    return redirect(url_for("tournament", tid=tid, tab="pairings"))


@app.route("/t/<int:tid>/next", methods=["POST"])
@admin_required
def advance(tid):
    t = get_tournament(tid)
    if t["status"] not in ("swiss", "topcut"):
        abort(400)
    left = outstanding_count(t)
    if left:
        flash(f"{left} match(es) still have no result.", "error")
        return redirect(url_for("tournament", tid=tid))

    if t["status"] == "swiss":
        if t["current_round"] < t["swiss_rounds"]:
            nxt = t["current_round"] + 1
            create_swiss_round(t, nxt)
            db().commit()
            flash(f"Round {nxt} is paired.", "ok")
            return redirect(url_for("tournament", tid=tid, tab="pairings"))
        rows = [r for r in standings_for(t) if not r["dropped"]]
        if t["topcut_size"]:
            size = t["topcut_size"]
            if len(rows) < size:
                flash(f"Only {len(rows)} active players - not enough for Top {size}. "
                      "Reinstate dropped players or finish without a cut.", "error")
                return redirect(url_for("tournament", tid=tid))
            seeded = [r["player"] for r in rows[:size]]
            for seed, uid in enumerate(seeded, 1):
                db().execute("UPDATE entries SET seed = ? WHERE tournament_id = ? AND user_id = ?", (seed, tid, uid))
            insert_topcut_round(tid, 1, engine.topcut_first_round(seeded))
            db().commit()
            flash(f"Swiss is over - Top {size} bracket is ready (Bo3).", "ok")
            return redirect(url_for("tournament", tid=tid, tab="bracket"))
        champ = rows[0]["player"] if rows else None
        db().execute("UPDATE tournaments SET status = 'finished', champion_id = ? WHERE id = ?", (champ, tid))
        db().commit()
        flash("Tournament finished!", "ok")
        return redirect(url_for("tournament", tid=tid, tab="standings"))

    # top cut
    ms = [match_dict(m) for m in round_matches(tid, "topcut", t["current_round"])]
    if len(ms) == 1:
        champ = engine.match_winner(ms[0])
        db().execute("UPDATE tournaments SET status = 'finished', champion_id = ? WHERE id = ?", (champ, tid))
        db().commit()
        flash("We have a champion!", "ok")
        return redirect(url_for("tournament", tid=tid, tab="bracket"))
    seed_of = {e["user_id"]: e["seed"] for e in entries_for(tid) if e["seed"]}
    insert_topcut_round(tid, t["current_round"] + 1, engine.topcut_next_round(ms, seed_of))
    db().commit()
    flash("Next bracket round is ready.", "ok")
    return redirect(url_for("tournament", tid=tid, tab="bracket"))


@app.route("/t/<int:tid>/repair", methods=["POST"])
@admin_required
def repair(tid):
    """Re-pair the current Swiss round (e.g. after a late drop), only if no results are in yet."""
    t = get_tournament(tid)
    if t["status"] != "swiss":
        abort(400)
    reported = q("SELECT COUNT(*) c FROM matches WHERE tournament_id = ? AND stage = 'swiss' AND round = ? "
                 "AND status != 'pending' AND p2_id IS NOT NULL", (tid, t["current_round"]), one=True)["c"]
    if reported:
        flash("Results have already been entered for this round - it can't be re-paired.", "error")
        return redirect(url_for("tournament", tid=tid))
    db().execute("DELETE FROM matches WHERE tournament_id = ? AND stage = 'swiss' AND round = ?",
                 (tid, t["current_round"]))
    create_swiss_round(t, t["current_round"])
    db().commit()
    flash(f"Round {t['current_round']} re-paired.", "ok")
    return redirect(url_for("tournament", tid=tid, tab="pairings"))


# ---------------------------------------------------------------- results

def _load_match(tid, mid):
    m = q("SELECT * FROM matches WHERE id = ? AND tournament_id = ?", (mid, tid), one=True)
    if not m:
        abort(404)
    return m


def _back(tid):
    return redirect(request.form.get("back") or url_for("tournament", tid=tid))


@app.route("/t/<int:tid>/match/<int:mid>/report", methods=["POST"])
@login_required
def report(tid, mid):
    t = get_tournament(tid)
    m = _load_match(tid, mid)
    me = current_user()
    is_player = me["id"] in (m["p1_id"], m["p2_id"])
    if not (is_player or me["is_admin"]):
        abort(403)
    if m["p2_id"] is None:
        abort(400)
    is_current = (t["status"] == m["stage"] and t["current_round"] == m["round"])
    if not is_current and not me["is_admin"]:
        flash("This round is closed. Ask the Tournament Coordinator to correct it.", "error")
        return _back(tid)
    if not is_current and m["stage"] == "topcut":
        flash("Bracket results can't be changed after the next round has started.", "error")
        return _back(tid)
    if m["status"] == "confirmed" and not me["is_admin"]:
        flash("This result is already confirmed. Ask the Tournament Coordinator to change it.", "error")
        return _back(tid)
    code = request.form.get("result", "")
    if code not in result_choices(t, m):
        flash("Pick a valid result.", "error")
        return _back(tid)
    a, b, d = engine.parse_result(code)
    status = "confirmed" if me["is_admin"] else "reported"
    db().execute("UPDATE matches SET p1_wins = ?, p2_wins = ?, draws = ?, status = ?, reported_by = ?, "
                 "updated_at = ? WHERE id = ?", (a, b, d, status, me["id"], now(), mid))
    db().commit()
    flash("Result saved." if me["is_admin"] else "Result reported - waiting for your opponent to confirm.", "ok")
    return _back(tid)


@app.route("/t/<int:tid>/match/<int:mid>/confirm", methods=["POST"])
@login_required
def confirm(tid, mid):
    get_tournament(tid)
    m = _load_match(tid, mid)
    me = current_user()
    if m["status"] != "reported":
        return _back(tid)
    if me["id"] not in (m["p1_id"], m["p2_id"]) and not me["is_admin"]:
        abort(403)
    if me["id"] == m["reported_by"] and not me["is_admin"]:
        flash("Your opponent needs to confirm the result you reported.", "error")
        return _back(tid)
    if request.form.get("action") == "dispute":
        db().execute("UPDATE matches SET status = 'pending', p1_wins = 0, p2_wins = 0, draws = 0, "
                     "reported_by = NULL, updated_at = ? WHERE id = ?", (now(), mid))
        flash("Result disputed and cleared. Please re-report it, or call a judge.", "ok")
    else:
        db().execute("UPDATE matches SET status = 'confirmed', updated_at = ? WHERE id = ?", (now(), mid))
        flash("Result confirmed.", "ok")
    db().commit()
    return _back(tid)


# ---------------------------------------------------------------- errors

@app.errorhandler(403)
def _forbidden(e):
    return render_template("error.html", code=403, message="Only the Tournament Coordinator / Admin can do that."), 403


@app.errorhandler(404)
def _not_found(e):
    return render_template("error.html", code=404, message="That page doesn't exist."), 404


@app.errorhandler(400)
def _bad(e):
    return render_template("error.html", code=400, message=getattr(e, "description", "Bad request.")), 400


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Tournament Manager running on http://localhost:{port}")
    print("  Other devices on your network can use http://<this-PC's-IP>:%d\n" % port)
    app.run(host="0.0.0.0", port=port, debug=False)
