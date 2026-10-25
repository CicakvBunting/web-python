"""One-off database converter for Budget Bulanan.

Fixes the "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE
constraint" error by rebuilding the budget_lines table with the current
schema — UNIQUE(user, year, month, category, item) — and copying existing
rows into it. Rows without a user are assigned to the admin account 'izzul'.

Safe to run more than once: if the table is already on the new schema it does
nothing. Makes a timestamped backup copy of the database first.

Usage (in a PythonAnywhere Bash console, from the folder with budget.db):
    python convert_db.py
or point it at a specific file:
    python convert_db.py /home/youruser/mysite/budget.db
"""
import os
import sys
import shutil
import sqlite3
from datetime import datetime

ADMIN_USER = "izzul"


def needs_migration(conn):
    """True if budget_lines exists but lacks the 5-column UNIQUE(user,...)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='budget_lines'"
    ).fetchone()
    if not row or not row[0]:
        return False  # table doesn't exist yet; app will create it correctly
    sql = row[0].lower().replace(" ", "")
    # The current schema's constraint, normalised.
    return "unique(user,year,month,category,item)" not in sql


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "budget.db"
    if not os.path.exists(db):
        print(f"Database not found: {db}")
        sys.exit(1)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    if not needs_migration(conn):
        print("Already on the current schema — nothing to do.")
        conn.close()
        return

    # Backup first.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{db}.backup_{stamp}"
    shutil.copy2(db, backup)
    print(f"Backup created: {backup}")

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(budget_lines)")}
    has_user = "user" in cols
    has_spent = "spent" in cols
    has_sort = "sort" in cols

    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE budget_lines RENAME TO budget_lines_old")
        conn.execute("""
            CREATE TABLE budget_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL DEFAULT '',
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                category TEXT NOT NULL,
                item TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                spent REAL NOT NULL DEFAULT 0,
                sort INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user, year, month, category, item)
            )
        """)
        # Build the SELECT defensively, supplying defaults for columns the old
        # table may not have.
        user_expr = ("CASE WHEN user IS NULL OR user='' THEN '%s' ELSE user END"
                     % ADMIN_USER) if has_user else "'%s'" % ADMIN_USER
        spent_expr = "COALESCE(spent, 0)" if has_spent else "0"
        sort_expr = "COALESCE(sort, 0)" if has_sort else "0"
        conn.execute(f"""
            INSERT INTO budget_lines (user, year, month, category, item, amount, spent, sort)
            SELECT {user_expr}, year, month, category, item, amount, {spent_expr}, {sort_expr}
            FROM budget_lines_old
        """)
        moved = conn.execute("SELECT COUNT(*) FROM budget_lines").fetchone()[0]
        conn.execute("DROP TABLE budget_lines_old")
        conn.commit()
        print(f"Migrated {moved} rows. budget_lines now uses the correct constraint.")
    except Exception as e:
        conn.rollback()
        print(f"Migration failed, no changes committed: {e}")
        print(f"Your data is untouched. Backup is at: {backup}")
        conn.close()
        sys.exit(1)

    # Quick verification.
    users = [r[0] for r in conn.execute(
        "SELECT DISTINCT user FROM budget_lines").fetchall()]
    print("Distinct users now:", users)
    conn.close()
    print("Done. Reload your web app.")


if __name__ == "__main__":
    main()
