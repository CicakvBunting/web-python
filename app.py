"""Monthly Budget Tracker — Flask + SQLite backend.

The SQLite database is the single source of truth. Plan line items (income and
expense) are stored per period; summary metrics and the wallet breakdown are
derived from them, matching the original workbook formulas.

Features:
- Password auth, with a master-phrase reset ("open sesame") to set a new password.
- Per-expense budget (= the planned amount) plus an "actual spent" input and a
  salary date, producing a recommended daily spend for the remaining budget.
- Add / delete / reorder both income and expense line items per period.
"""
import os
import sqlite3
import json
import hmac
import hashlib
from datetime import date, datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, g
from messages import SAVINGS_MESSAGES

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BUDGET_DB", os.path.join(BASE, "budget.db"))
SEED_PATH = os.path.join(BASE, "seed_rows.json")
DEFAULT_PASSWORD = os.environ.get("BUDGET_PASSWORD", "changeme")
SECRET = os.environ.get("BUDGET_SECRET", "dev-secret-change-me")
MASTER_PHRASE = os.environ.get("BUDGET_MASTER", "open sesame")

DEFAULT_INCOME = ["gaji", "TA", "TA 2"]
DEFAULT_EXPENSE = [
    "Makan", "Kos + listrik", "Ibu", "kartu kredit", "Laundry",
    "uang free", "Tak terduga", "bensin + service", "ika", "nabung",
]
# Nabung is a fixed, auto-calculated expense: income minus every other expense.
# It is never typed by the user and is always recomputed server-side.
NABUNG_ITEM = "nabung"

# Default auto-classification for the budgeting-rules panel. The user can
# override any item globally (stored in settings under "cat:<item>").
DEFAULT_NEEDS = {"Makan", "Kos + listrik", "Ibu", "kartu kredit", "Laundry",
                 "bensin + service", "Tak terduga"}

# Default wallet list and which expense items feed each (mirrors the original
# workbook). Used to seed a user's editable wallet config on first use.
DEFAULT_WALLETS = ["Seabank", "Masuk Gopay", "Masuk BCA", "Nanovest"]
DEFAULT_WALLET_OF = {
    "uang free": "Seabank", "Tak terduga": "Seabank",
    "Kos + listrik": "Masuk Gopay", "Ibu": "Masuk Gopay", "kartu kredit": "Masuk Gopay",
    "Laundry": "Masuk Gopay", "bensin + service": "Masuk Gopay", "ika": "Masuk Gopay",
    "Makan": "Masuk BCA",
    "nabung": "Nanovest",
}
UNASSIGNED = "Other / unassigned"


def get_wallets(conn, user):
    """The user's wallet list (editable). Falls back to the default set."""
    raw = get_user_setting(conn, user, "wallets")
    if raw:
        try:
            wl = json.loads(raw)
            if isinstance(wl, list) and wl:
                return [str(w) for w in wl]
        except (ValueError, TypeError):
            pass
    return list(DEFAULT_WALLETS)


def wallet_of(conn, user, item, wallets=None):
    """Which wallet an expense item is assigned to. User override, else the
    default mapping. If the resulting wallet isn't in the user's current list
    (e.g. it was renamed or removed), treat the item as unassigned."""
    override = get_user_setting(conn, user, f"wal:{item}")
    w = override if override else DEFAULT_WALLET_OF.get(item, UNASSIGNED)
    if wallets is not None and w != UNASSIGNED and w not in wallets:
        return UNASSIGNED
    return w


def classify(conn, user, item):
    """Return 'needs' | 'wants' | 'savings' for an expense item name.
    Honors a global (per-user) override if present, else the default."""
    if item == NABUNG_ITEM:
        return "savings"
    override = get_user_setting(conn, user, f"cat:{item}")
    if override in ("needs", "wants"):
        return override
    return "needs" if item in DEFAULT_NEEDS else "wants"
# Wallet breakdown is derived from specific expense items (original workbook logic).
# Items added later by the user simply won't map into a wallet bucket.
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

app = Flask(__name__)
app.secret_key = SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return fn(*a, **k)
    return wrapper


def current_user():
    return session.get("user")


ADMIN_USER = "izzul"


def _new_conn():
    # timeout: wait for a lock instead of failing instantly.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets readers and a writer coexist; busy_timeout makes concurrent
    # writes retry rather than erroring with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_db():
    """Return a connection bound to the current request, if there is one, so it
    gets closed automatically when the request ends (see close_db below). This
    prevents leaked connections from holding a write lock. Outside a request
    context (e.g. init_db at startup) it returns a standalone connection that
    the caller closes itself."""
    try:
        if "db" not in g:
            g.db = _new_conn()
        return g.db
    except RuntimeError:
        # No application/request context (startup) — caller manages .close().
        return _new_conn()


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        try:
            if exc is None:
                conn.commit()
        finally:
            conn.close()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# Per-user settings are stored under namespaced keys: "u:<user>:<key>".
def get_user_setting(conn, user, key, default=None):
    return get_setting(conn, f"u:{user}:{key}", default)


def set_user_setting(conn, user, key, value):
    set_setting(conn, f"u:{user}:{key}", value)


def hash_pw(pw):
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def init_db():
    first_time = not os.path.exists(DB_PATH)
    conn = _new_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS budget_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL DEFAULT '',
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            category TEXT NOT NULL,          -- income | expense | meta
            item TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,  -- planned amount (= budget)
            spent REAL NOT NULL DEFAULT 0,   -- actual spent so far
            sort INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user, year, month, category, item)
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            pw_hash TEXT NOT NULL,
            created TEXT
        )
    """)
    # Shared clipboard: transfer text between devices by code. No login required.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            updated TEXT NOT NULL
        )
    """)
    conn.commit()

    # Migrate older single-user DBs.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(budget_lines)")}
    if "spent" not in cols:
        conn.execute("ALTER TABLE budget_lines ADD COLUMN spent REAL NOT NULL DEFAULT 0")
    if "sort" not in cols:
        conn.execute("ALTER TABLE budget_lines ADD COLUMN sort INTEGER NOT NULL DEFAULT 0")
    if "user" not in cols:
        # Old data had no user column; assign it all to the admin account.
        conn.execute("ALTER TABLE budget_lines ADD COLUMN user TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE budget_lines SET user=? WHERE user=''", (ADMIN_USER,))
    conn.commit()

    # Registration token (editable by admin only). Default to the master phrase.
    if get_setting(conn, "reg_token") is None:
        set_setting(conn, "reg_token", MASTER_PHRASE)

    # Ensure the admin user exists. Its password comes from BUDGET_PASSWORD on
    # first creation (so existing deployments keep working), else the default.
    admin = conn.execute("SELECT 1 FROM users WHERE username=?", (ADMIN_USER,)).fetchone()
    if not admin:
        legacy_pw = get_setting(conn, "password", DEFAULT_PASSWORD)
        conn.execute("INSERT INTO users (username, pw_hash, created) VALUES (?,?,?)",
                     (ADMIN_USER, hash_pw(legacy_pw), date.today().isoformat()))
        conn.commit()

    # Seed the sample 10 months into the ADMIN account only, once.
    admin_has_data = conn.execute(
        "SELECT COUNT(*) c FROM budget_lines WHERE user=?", (ADMIN_USER,)).fetchone()["c"] > 0
    if (first_time or not admin_has_data) and os.path.exists(SEED_PATH):
        rows = json.load(open(SEED_PATH))
        order = {}
        seeded = []
        for y, m, cat, item, amt in rows:
            key = (y, m, cat)
            order[key] = order.get(key, 0)
            seeded.append((ADMIN_USER, y, m, cat, item, amt, 0.0, order[key]))
            order[key] += 1
        conn.executemany(
            "INSERT OR IGNORE INTO budget_lines (user, year, month, category, item, amount, spent, sort) "
            "VALUES (?,?,?,?,?,?,?,?)", seeded)
        conn.commit()
    conn.close()


def get_user(conn, username):
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def check_login(conn, username, pw):
    u = get_user(conn, username)
    return u is not None and hmac.compare_digest(u["pw_hash"], hash_pw(pw))


def month_totals(conn, user, year, month):
    """Lightweight key totals for one month (income, expense, kebutuhan,
    nabung), used for prev-month comparison and the trend chart. Does not
    build the full payload."""
    rows = conn.execute(
        "SELECT category, item, amount FROM budget_lines "
        "WHERE user=? AND year=? AND month=?", (user, year, month)).fetchall()
    income = sum(r["amount"] for r in rows if r["category"] == "income")
    exp_items = {r["item"]: r["amount"] for r in rows if r["category"] == "expense"}
    other_exp = sum(v for k, v in exp_items.items() if k != NABUNG_ITEM)
    nabung = income - other_exp
    keb_items = ("Makan", "Kos + listrik", "Ibu", "kartu kredit", "Laundry")
    kebutuhan = sum(exp_items.get(k, 0.0) for k in keb_items)
    carry = next((r["amount"] for r in rows
                  if r["category"] == "meta" and r["item"] == "uang_sebelumnya"), 17000000.0)
    return {
        "income": income,
        "expense": other_exp,
        "kebutuhan": kebutuhan,
        "nabung": nabung,
        "carry": carry,
        "tabungan": carry + nabung,  # full projected savings = carry-over + nabung
        "has_data": bool(rows),
    }


def prev_period(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def derive(user, year, month):
    """Build the full dashboard payload for one period from stored line items."""
    conn = get_db()
    rows = conn.execute(
        "SELECT category, item, amount, spent, sort FROM budget_lines "
        "WHERE user=? AND year=? AND month=? ORDER BY category, sort, item",
        (user, year, month)).fetchall()
    salary_day = (get_user_setting(conn, user, f"salary_day:{year}-{month}")
                  or get_user_setting(conn, user, "salary_day"))

    income, expense = [], []
    emap = {}
    uang_sebelumnya = 17000000.0
    for r in rows:
        if r["category"] == "income":
            income.append({"item": r["item"], "amount": r["amount"]})
        elif r["category"] == "expense":
            e = {"item": r["item"], "amount": r["amount"], "spent": r["spent"]}
            expense.append(e)
        elif r["category"] == "meta" and r["item"] == "uang_sebelumnya":
            uang_sebelumnya = r["amount"]

    income_total = sum(i["amount"] for i in income)
    wallets_list = get_wallets(conn, user)

    # Nabung is fixed: it equals income minus every OTHER expense. Recompute it
    # here so it is always correct regardless of what is stored, and mark it
    # locked so the UI shows it read-only.
    other_expense_total = sum(e["amount"] for e in expense if e["item"] != NABUNG_ITEM)
    nabung_value = income_total - other_expense_total
    found_nabung = False
    for e in expense:
        if e["item"] == NABUNG_ITEM:
            e["amount"] = nabung_value
            e["locked"] = True
            e["cat"] = "savings"
            e["wallet"] = wallet_of(conn, user, NABUNG_ITEM, wallets_list)
            found_nabung = True
        else:
            e["locked"] = False
            e["cat"] = classify(conn, user, e["item"])
            e["cat_override"] = get_user_setting(conn, user, f"cat:{e['item']}") in ("needs", "wants")
            e["wallet"] = wallet_of(conn, user, e["item"], wallets_list)
    if not found_nabung:
        expense.append({"item": NABUNG_ITEM, "amount": nabung_value,
                        "spent": 0.0, "locked": True, "cat": "savings",
                        "wallet": wallet_of(conn, user, NABUNG_ITEM, wallets_list)})

    # Nabung always sits at the very bottom of the expense list, regardless of
    # its stored position.
    expense.sort(key=lambda e: 1 if e["item"] == NABUNG_ITEM else 0)

    emap = {e["item"]: e["amount"] for e in expense}
    expense_total = sum(e["amount"] for e in expense)

    def g(k):
        return emap.get(k, 0.0)

    # Wallet breakdown: sum each expense into its assigned wallet. Wallets with
    # no expenses still show (as 0); anything unassigned lands in UNASSIGNED.
    wallet = {w: 0.0 for w in wallets_list}
    for e in expense:
        w = e.get("wallet") or UNASSIGNED
        wallet[w] = wallet.get(w, 0.0) + e["amount"]
    # Only keep the unassigned bucket if it actually has something in it.
    if wallet.get(UNASSIGNED, 0.0) == 0.0:
        wallet.pop(UNASSIGNED, None)

    kebutuhan = g("Makan") + g("Kos + listrik") + g("Ibu") + g("kartu kredit") + g("Laundry")
    total_spent = sum(e["spent"] for e in expense)
    savings_rate = (nabung_value / income_total) if income_total > 0 else 0.0
    summary = {
        "Total Pemasukan": income_total,
        "Total Pengeluaran": other_expense_total,
        "Kebutuhan": kebutuhan,
        "Uang Tabungan Seharusnya": uang_sebelumnya + g("nabung"),
        "Uang Sebelumnya": uang_sebelumnya,
        "Total Terpakai": total_spent,
    }

    # Budgeting-rule comparison (informational). Uses each item's category
    # (auto-classified, with user overrides), treating nabung as savings.
    needs_amt = sum(e["amount"] for e in expense if e.get("cat") == "needs")
    wants_amt = sum(e["amount"] for e in expense if e.get("cat") == "wants")
    savings_amt = max(nabung_value, 0.0)
    base = income_total if income_total > 0 else 1.0
    rules = {
        "actual": {
            "needs": needs_amt / base,
            "wants": wants_amt / base,
            "savings": savings_amt / base,
        },
        "presets": [
            {"name": "50 / 30 / 20", "needs": .50, "wants": .30, "savings": .20,
             "blurb": "Balanced default — half needs, a third wants, a fifth saved."},
            {"name": "70 / 20 / 10", "needs": .70, "wants": .20, "savings": .10,
             "blurb": "Higher cost of living — more room for needs, modest saving."},
            {"name": "80 / 20 (pay yourself first)", "needs": .80, "wants": 0, "savings": .20,
             "blurb": "Save 20% first, spend the rest however you like."},
            {"name": "Aggressive saver 40 / 20 / 40", "needs": .40, "wants": .20, "savings": .40,
             "blurb": "FIRE-style — maximize savings, keep spending lean."},
        ],
    }
    # Connection is request-scoped and closed by teardown; do not close here.

    # Days until the next payday from today. The payday is a day-of-month; if it
    # has already passed this month we roll to next month. A payday beyond a
    # month's length (e.g. 31 in a 30-day month) falls on that month's last day.
    days_left = None
    if salary_day:
        try:
            import calendar
            sd = int(salary_day)
            today = date.today()
            y2, m2 = today.year, today.month
            last_this = calendar.monthrange(y2, m2)[1]
            # Payday lands on min(sd, last day of this month).
            payday_this = min(sd, last_this)
            if today.day >= payday_this:  # already reached/passed -> next month
                m2 += 1
                if m2 > 12:
                    m2 = 1
                    y2 += 1
                last_next = calendar.monthrange(y2, m2)[1]
                target = date(y2, m2, min(sd, last_next))
            else:
                target = date(y2, m2, payday_this)
            days_left = max((target - today).days, 1)
        except (ValueError, TypeError):
            days_left = None

    # Previous-month key totals, for the ↑/↓ comparison indicators on cards.
    py, pm = prev_period(year, month)
    prev = month_totals(conn, user, py, pm)
    prev_summary = {
        "Total Pemasukan": prev["income"],
        "Total Pengeluaran": prev["expense"],
        "Kebutuhan": prev["kebutuhan"],
        "Uang Tabungan Seharusnya": prev["nabung"],  # carry-over excluded; compares the month's own saving
    } if prev["has_data"] else None
    prev_tabungan = prev["tabungan"] if prev["has_data"] else None
    prev_label = MONTHS[pm - 1] if prev["has_data"] else None

    return {
        "year": year, "month": month, "month_name": MONTHS[month - 1],
        "has_data": bool(rows),
        "income": income, "income_total": income_total,
        "expense": expense, "expense_total": expense_total,
        "wallet": wallet, "summary": summary,
        "prev_summary": prev_summary,
        "prev_tabungan": prev_tabungan,
        "prev_label": prev_label,
        "wallets_list": wallets_list,
        "savings_rate": savings_rate, "rules": rules,
        "salary_day": int(salary_day) if salary_day else None,
        "days_left": days_left,
    }


MAX_CLIPS = 15  # keep only the 15 most recently updated clips


@app.route("/api/clip/save", methods=["POST"])
def clip_save():
    """Save text under a code (no login). Overwrites if the code exists, then
    prunes so at most MAX_CLIPS clips are kept (oldest removed)."""
    p = request.get_json(silent=True) or {}
    code = (p.get("code") or "").strip()
    content = p.get("content")
    if not code:
        return jsonify({"error": "Kode tidak boleh kosong."}), 400
    if len(code) > 100:
        return jsonify({"error": "Kode terlalu panjang."}), 400
    if content is None:
        return jsonify({"error": "Teks tidak boleh kosong."}), 400
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    # Delete any existing clip with this code, then insert fresh so it gets the
    # highest id (counts as most-recent for pruning, even within the same second).
    conn.execute("DELETE FROM clips WHERE code=?", (code,))
    conn.execute("INSERT INTO clips (code, content, updated) VALUES (?,?,?)",
                 (code, content, now))
    # Prune: keep only the newest MAX_CLIPS by insertion order (id).
    conn.execute(
        "DELETE FROM clips WHERE id NOT IN "
        "(SELECT id FROM clips ORDER BY id DESC LIMIT ?)", (MAX_CLIPS,))
    conn.commit()
    return jsonify({"ok": True, "code": code})


@app.route("/api/clip/load")
def clip_load():
    """Load the text stored under a code (no login)."""
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "Kode tidak boleh kosong."}), 400
    conn = get_db()
    row = conn.execute("SELECT content, updated FROM clips WHERE code=?", (code,)).fetchone()
    if not row:
        return jsonify({"error": "Kode tidak ditemukan."}), 404
    return jsonify({"content": row["content"], "updated": row["updated"]})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username", "") or "").strip().lower()
        pw = request.form.get("password", "")
        conn = get_db()
        ok = check_login(conn, username, pw)
        if ok:
            session["user"] = username
            session.permanent = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Wrong username or password."), 401
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("login.html", error=None)


@app.route("/register", methods=["POST"])
def register():
    """Anyone with the registration token can create a new empty account."""
    username = (request.form.get("username", "") or "").strip().lower()
    pw = request.form.get("password", "").strip()
    token = request.form.get("token", "")
    conn = get_db()
    expected_token = get_setting(conn, "reg_token", MASTER_PHRASE)
    if not hmac.compare_digest(token, expected_token):
        return render_template("login.html", error="Registration token is incorrect.",
                               show_register=True), 401
    if not username.isalnum() or not (3 <= len(username) <= 20):
        return render_template("login.html",
                               error="Username must be 3–20 letters/numbers, no spaces.",
                               show_register=True), 400
    if len(pw) < 4:
        return render_template("login.html", error="Password must be at least 4 characters.",
                               show_register=True), 400
    if get_user(conn, username):
        return render_template("login.html", error="That username is already taken.",
                               show_register=True), 400
    conn.execute("INSERT INTO users (username, pw_hash, created) VALUES (?,?,?)",
                 (username, hash_pw(pw), date.today().isoformat()))
    conn.commit()
    return render_template("login.html", error=None,
                           notice="Account created. Sign in below.")


@app.route("/reset-password", methods=["POST"])
def reset_password():
    """Master phrase authorizes resetting ANY user's password (admin reset)."""
    username = (request.form.get("username", "") or "").strip().lower()
    phrase = request.form.get("phrase", "")
    new_pw = request.form.get("new_password", "").strip()
    if not hmac.compare_digest(phrase, MASTER_PHRASE):
        return render_template("login.html", error="Master phrase is incorrect.",
                               show_reset=True), 401
    if len(new_pw) < 4:
        return render_template("login.html", error="New password must be at least 4 characters.",
                               show_reset=True), 400
    conn = get_db()
    if not get_user(conn, username):
        return render_template("login.html", error="No account with that username.",
                               show_reset=True), 400
    conn.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_pw(new_pw), username))
    conn.commit()
    return render_template("login.html", error=None, notice="Password updated. Sign in below.")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html", messages=SAVINGS_MESSAGES,
                           username=current_user(), is_admin=(current_user() == ADMIN_USER))


@app.route("/api/periods")
@login_required
def periods():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT year, month FROM budget_lines WHERE user=? ORDER BY year, month",
        (current_user(),)).fetchall()
    return jsonify([{"year": r["year"], "month": r["month"],
                     "month_name": MONTHS[r["month"] - 1]} for r in rows])


@app.route("/api/data")
@login_required
def data():
    return jsonify(derive(current_user(), int(request.args["year"]), int(request.args["month"])))


@app.route("/api/trend")
@login_required
def trend():
    """Per-month key totals across all of the user's months, for the trend chart."""
    user = current_user()
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT year, month FROM budget_lines WHERE user=? ORDER BY year, month",
        (user,)).fetchall()
    out = []
    for r in rows:
        t = month_totals(conn, user, r["year"], r["month"])
        out.append({"year": r["year"], "month": r["month"],
                    "label": MONTHS[r["month"] - 1][:3] + " " + str(r["year"])[2:],
                    "income": t["income"], "expense": t["expense"],
                    "nabung": t["nabung"]})
    return jsonify(out)


@app.route("/api/save", methods=["POST"])
@login_required
def save():
    """Replace all line items for a period. Body:
    {year, month, income:[{item,amount}], expense:[{item,amount,spent}],
     uang_sebelumnya, salary_day}"""
    p = request.get_json()
    year, month = int(p["year"]), int(p["month"])
    user = current_user()
    conn = get_db()
    # Wipe income/expense for this period, then re-insert in given order (handles add/delete/reorder).
    conn.execute("DELETE FROM budget_lines WHERE user=? AND year=? AND month=? AND category IN ('income','expense')",
                 (user, year, month))
    for i, it in enumerate(p.get("income", [])):
        name = (it.get("item") or "").strip()
        if name:
            conn.execute("INSERT OR REPLACE INTO budget_lines (user,year,month,category,item,amount,spent,sort) "
                         "VALUES (?,?,?,?,?,?,?,?)", (user, year, month, "income", name, float(it.get("amount") or 0), 0.0, i))
    for i, it in enumerate(p.get("expense", [])):
        name = (it.get("item") or "").strip()
        if name:
            # Nabung is auto-calculated in derive(); store 0 so a stale typed
            # value never lingers. Its spent value is still preserved.
            amt = 0.0 if name == NABUNG_ITEM else float(it.get("amount") or 0)
            conn.execute("INSERT OR REPLACE INTO budget_lines (user,year,month,category,item,amount,spent,sort) "
                         "VALUES (?,?,?,?,?,?,?,?)", (user, year, month, "expense", name,
                          amt, float(it.get("spent") or 0), i))
    # meta + settings
    conn.execute("INSERT INTO budget_lines (user,year,month,category,item,amount,spent,sort) VALUES (?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(user,year,month,category,item) DO UPDATE SET amount=excluded.amount",
                 (user, year, month, "meta", "uang_sebelumnya", float(p.get("uang_sebelumnya") or 0), 0.0, 0))
    if p.get("salary_day") not in (None, ""):
        set_user_setting(conn, user, f"salary_day:{year}-{month}", int(p["salary_day"]))
        set_user_setting(conn, user, "salary_day", int(p["salary_day"]))  # per-user default
    conn.commit()
    return jsonify(derive(user, year, month))


@app.route("/api/set_category", methods=["POST"])
@login_required
def set_category():
    """Globally (per-user) override an item's needs/wants category, or clear to 'auto'."""
    p = request.get_json()
    item = (p.get("item") or "").strip()
    cat = p.get("cat")
    user = current_user()
    if not item:
        return jsonify({"error": "missing item"}), 400
    conn = get_db()
    if cat == "auto":
        conn.execute("DELETE FROM settings WHERE key=?", (f"u:{user}:cat:{item}",))
    elif cat in ("needs", "wants"):
        set_user_setting(conn, user, f"cat:{item}", cat)
    else:
        return jsonify({"error": "bad category"}), 400
    conn.commit()
    return jsonify(derive(user, int(p["year"]), int(p["month"])))


@app.route("/api/set_wallet", methods=["POST"])
@login_required
def set_wallet():
    """Assign an expense item to a wallet (global per-user). 'auto' clears it."""
    p = request.get_json()
    item = (p.get("item") or "").strip()
    w = (p.get("wallet") or "").strip()
    user = current_user()
    if not item:
        return jsonify({"error": "missing item"}), 400
    conn = get_db()
    if w in ("", "auto"):
        conn.execute("DELETE FROM settings WHERE key=?", (f"u:{user}:wal:{item}",))
    else:
        set_user_setting(conn, user, f"wal:{item}", w)
    conn.commit()
    return jsonify(derive(user, int(p["year"]), int(p["month"])))


@app.route("/api/wallets", methods=["POST"])
@login_required
def edit_wallets():
    """Replace the user's wallet list. Optionally remap items from a renamed or
    removed wallet. Body: {wallets:[...], rename:{old:new}, year, month}."""
    p = request.get_json()
    user = current_user()
    new_list = [str(w).strip() for w in (p.get("wallets") or []) if str(w).strip()]
    if not new_list:
        return jsonify({"error": "need at least one wallet"}), 400
    conn = get_db()
    # If a wallet was renamed, move any per-item assignments to the new name.
    rename = p.get("rename") or {}
    if rename:
        prefix = f"u:{user}:wal:"
        rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE ?",
                            (prefix + "%",)).fetchall()
        for r in rows:
            if r["value"] in rename:
                conn.execute("UPDATE settings SET value=? WHERE key=?",
                            (rename[r["value"]], r["key"]))
        # Items that relied on the DEFAULT mapping (no stored override) won't be
        # caught above — pin them to the new name so the rename is complete.
        for item, dwal in DEFAULT_WALLET_OF.items():
            if dwal in rename:
                key = f"u:{user}:wal:{item}"
                existing = conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone()
                if not existing:
                    set_user_setting(conn, user, f"wal:{item}", rename[dwal])
    set_user_setting(conn, user, "wallets", json.dumps(new_list))
    conn.commit()
    return jsonify(derive(user, int(p["year"]), int(p["month"])))


@app.route("/api/add_period", methods=["POST"])
@login_required
def add_period():
    p = request.get_json()
    year, month = int(p["year"]), int(p["month"])
    user = current_user()
    conn = get_db()
    if conn.execute("SELECT 1 FROM budget_lines WHERE user=? AND year=? AND month=? LIMIT 1",
                    (user, year, month)).fetchone():
        return jsonify({"error": "Period already exists"}), 400

    # Optionally copy budgets (not spent) from the user's most recent period.
    if p.get("duplicate"):
        last = conn.execute(
            "SELECT DISTINCT year, month FROM budget_lines WHERE user=? "
            "ORDER BY year DESC, month DESC LIMIT 1", (user,)).fetchone()
        if last:
            src = conn.execute(
                "SELECT category, item, amount, sort FROM budget_lines "
                "WHERE user=? AND year=? AND month=?",
                (user, last["year"], last["month"])).fetchall()
            rows = [(user, year, month, r["category"], r["item"],
                     r["amount"], 0.0, r["sort"]) for r in src]
            conn.executemany("INSERT INTO budget_lines "
                             "(user,year,month,category,item,amount,spent,sort) VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            return jsonify(derive(user, year, month))

    rows = [(user, year, month, "income", n, 0.0, 0.0, i) for i, n in enumerate(DEFAULT_INCOME)]
    rows += [(user, year, month, "expense", n, 0.0, 0.0, i) for i, n in enumerate(DEFAULT_EXPENSE)]
    rows.append((user, year, month, "meta", "uang_sebelumnya", 17000000.0, 0.0, 0))
    conn.executemany("INSERT INTO budget_lines (user,year,month,category,item,amount,spent,sort) "
                     "VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return jsonify(derive(user, year, month))


@app.route("/api/delete_period", methods=["POST"])
@login_required
def delete_period():
    p = request.get_json()
    conn = get_db()
    conn.execute("DELETE FROM budget_lines WHERE user=? AND year=? AND month=?",
                 (current_user(), int(p["year"]), int(p["month"])))
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/set_reg_token", methods=["POST"])
@login_required
def set_reg_token():
    """Admin-only: change the registration token."""
    if current_user() != ADMIN_USER:
        return jsonify({"error": "admin only"}), 403
    p = request.get_json()
    new_token = (p.get("token") or "").strip()
    if len(new_token) < 3:
        return jsonify({"error": "token too short"}), 400
    conn = get_db()
    set_setting(conn, "reg_token", new_token)
    conn.commit()
    return jsonify({"ok": True, "token": new_token})


@app.route("/api/reg_token")
@login_required
def reg_token_view():
    """Admin-only: read the current registration token."""
    if current_user() != ADMIN_USER:
        return jsonify({"error": "admin only"}), 403
    conn = get_db()
    tok = get_setting(conn, "reg_token", MASTER_PHRASE)
    return jsonify({"token": tok})


init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
