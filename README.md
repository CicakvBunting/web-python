# Budget Bulanan — local + hosted web app

A budgeting app where a SQLite database (`budget.db`) is the single source of
truth. The dashboard reads from it and writes back directly: edit any plan
amount, add or delete individual line items, add or delete whole months. No
Excel, no macros. Responsive — works on phone and desktop. Your 10 existing
months (Sept 2025 – June 2026) are pre-loaded on first run.

**Features**
- Per-expense budget (the planned amount) with an "actual spent" input.
- Set your **payday** (day of month); each expense shows a recommended daily
  spend = (budget - spent) / days left until payday. Over-budget items flag in red.
- Add / delete / rename income and expense line items freely.
- Password sign-in, plus a **master-phrase reset**: on the login page, "Forgot
  password?" lets you set a new password by entering the master phrase
  (default `open sesame`, configurable via `BUDGET_MASTER`).
- A public **Gunsen companion** page with a rule-informed battle strength
  calculator, a temporary achievement tracker, and a quick rules reference.
  The calculator sums base, hero, and kept-die stats, estimates kills from
  Blood versus Armor, and applies the documented battle result rules. Since the
  accessible rules summary doesn't provide the complete casualty formula or
  individual hero abilities, those estimates and numeric hero effects must be
  checked against the printed game materials.

---

## Run locally

Python 3.9+:

```bash
cd budget_app
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000**. Default local password is `changeme`
(set `BUDGET_PASSWORD` to change it).

Open **http://127.0.0.1:5000/gunsen** to use the battle calculator, achievement
tracker, and rules reference without signing in. Achievement checks are kept in
the signed session only; they are not stored in the budget database. The
dashboard header also links to the companion when you're signed in.

To open from your **phone on the same Wi-Fi**: the local server already listens
on all interfaces, so find your computer's IP (e.g. `192.168.1.20`) and open
`http://192.168.1.20:5000` on your phone.

---

## Host it free on PythonAnywhere (recommended)

PythonAnywhere is the best free fit for this app: its file storage **persists**
(so `budget.db` survives), and free apps **don't sleep** (no cold-start delay).
No GitHub or command line required.

1. **Sign up** for a free "Beginner" account at pythonanywhere.com.
2. **Upload the app.** In the **Files** tab, create a folder `budget_app` and
   upload every file from this project into it (app.py, wsgi.py, messages.py,
   seed_rows.json, and the `templates` folder with index.html + login.html).
   (Or open a **Bash console** and `git clone` your repo if you prefer.)
3. **Install Flask.** Open a **Bash console** and run:
   `pip install --user flask`
4. **Create the web app.** Go to the **Web** tab → **Add a new web app** →
   **Manual configuration** → pick **Python 3.10** (or newer).
5. **Point it at the WSGI file.** In the Web tab, find the **WSGI configuration
   file** link and open it. Delete the sample contents and paste in the contents
   of this project's `wsgi.py`. Then edit the values near the top:
   - set `BUDGET_PASSWORD` to your own password,
   - set `BUDGET_MASTER` to your own master phrase,
   - set `BUDGET_SECRET` to a long random string (generate one in a Bash console
     with `python -c "import secrets; print(secrets.token_hex(32))"`),
   - make sure `PROJECT_DIR` matches the folder you uploaded to, e.g.
     `/home/YOURUSERNAME/budget_app`.
   Save the file.
6. **Reload** the web app (green **Reload** button in the Web tab).
7. Open `https://YOURUSERNAME.pythonanywhere.com` on any device and sign in.

On first load the app creates `budget.db`, creates the **izzul** admin account
(its first password = whatever you set as `BUDGET_PASSWORD`), and seeds the admin
account with your 10 months. Other users register themselves with the token and
start empty. It stays there across reloads. **Free accounts** have one
web app and limited outbound internet (which this app doesn't need). PythonAnywhere
asks free accounts to log in and click a button every ~3 months to keep the app
running — a quick step, not a data reset.

**Updating later:** re-upload changed files (or `git pull` in a console), then
hit **Reload**. Your `budget.db` is untouched by reloads.

---

## Host it publicly (Render — free tier)

Render also works but is a worse fit for this app: free Render web services
**sleep** after 15 minutes (≈30–60s wake-up) and the free tier has **no
persistent disk**, so plain SQLite would reset. Use this only if you switch the
storage to Render's managed database. The included `render.yaml` assumes a
persistent disk add-on.

1. Put this folder in a GitHub repo (the `.gitignore` keeps `budget.db` out, so
   the host starts fresh and seeds from `seed_rows.json`).
2. Go to **render.com** -> New -> **Blueprint** -> connect your repo. Render reads
   `render.yaml` and configures everything, including a persistent disk
   mounted at `/var/data` where `budget.db` will live.
3. When prompted, set the **`BUDGET_PASSWORD`** environment variable to a strong
   password. (`BUDGET_SECRET` is auto-generated; `BUDGET_DB` is preset to the
   disk path.)
4. Deploy. You get a URL like `https://budget-bulanan.onrender.com`.

### Other hosts
Any host that runs Python with persistent storage works. What matters everywhere:
- **WSGI/entry:** `wsgi.py` (PythonAnywhere) or `gunicorn app:app` (the `Procfile`).
- **Persistent storage** for `budget.db`, or set `BUDGET_DB` to a path that persists.

---

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `BUDGET_PASSWORD` | Sign-in password | `changeme` (change it!) |
| `BUDGET_DB` | Path to the SQLite file | `./budget.db` |
| `BUDGET_SECRET` | Session-cookie signing key | dev fallback (set in prod) |
| `BUDGET_MASTER` | Master phrase that authorizes password reset | `open sesame` |

---

## How it works

- **Edit:** tap any white amount field, type a number, then **Save changes**.
  Totals, percentages, wallet breakdown, and summary cards update live as you
  type; Save commits to the database.
- **Per-day recommendation:** set **Payday** at the top, fill each expense's
  Budget and Spent; the Per-day column shows what's safe to spend daily until payday.
- **Add / delete items:** use **+ Add income** / **+ Add expense**, or the × on any
  row. Rename by editing the name field. Changes commit on **Save changes**.
- **Add a month:** **+ New month** -> pick month/year -> Create (starts at zero).
- **Delete a month:** select it -> **Delete** (asks to confirm).
- **Reset password:** on the login screen, "Forgot password?" -> enter the master
  phrase + a new password. The new password is stored in the database.
- **Derived values** (wallet breakdown, kebutuhan, tabungan seharusnya) are
  computed from your plan items with the same logic as the original workbook.

## Files

- `app.py` — Flask backend, SQLite, auth, API, derive logic.
- `templates/index.html` — dashboard UI. `templates/login.html` — sign-in.
- `seed_rows.json` — initial data from your original xlsx.
- `requirements.txt`, `Procfile`, `render.yaml` — deploy config.
- `budget.db` — created on first run. **This is your data — back it up.**

## Security note
This protects access with a single shared password and a signed session cookie —
fine for personal use. On a public host always use HTTPS (Render provides it
automatically) and a strong password, since it guards your financial data.

**Important:** anyone who knows the master phrase can reset your password, so the
master phrase is effectively a second key. If you deploy publicly, change it from
the default by setting `BUDGET_MASTER` to something only you know.
