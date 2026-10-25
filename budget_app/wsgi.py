"""WSGI entry point for PythonAnywhere.

In the PythonAnywhere "Web" tab, set this file as your WSGI configuration
(or paste its contents into the auto-generated one). It does two things:

1. Adds this project folder to the import path so `app` can be found.
2. Sets the environment variables the app reads (password, master phrase, a
   persistent secret key, and the database location).

Edit the values below before deploying. Keep this file private — it contains
your password and secret.
"""
import os
import sys

# --- 1. Make the project importable -----------------------------------------
# Change this to the absolute path of the folder that contains app.py.
# On PythonAnywhere it is usually /home/YOURUSERNAME/budget_app
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# --- 2. Configuration (EDIT THESE) -------------------------------------------
# Sign-in password for the app.
os.environ.setdefault("BUDGET_PASSWORD", "change-this-password")

# Master phrase that authorizes a password reset on the login page.
os.environ.setdefault("BUDGET_MASTER", "open sesame")

# A long random string used to sign login cookies. Generate one once and keep
# it the same forever (changing it logs everyone out). For example, run:
#   python -c "import secrets; print(secrets.token_hex(32))"
os.environ.setdefault("BUDGET_SECRET", "PASTE-A-LONG-RANDOM-STRING-HERE")

# Where the SQLite database lives. The default (inside the project folder)
# persists on PythonAnywhere, so you normally do not need to change this.
os.environ.setdefault("BUDGET_DB", os.path.join(PROJECT_DIR, "budget.db"))

# --- 3. Expose the Flask app as `application` --------------------------------
from app import app as application  # noqa: E402
