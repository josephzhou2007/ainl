"""
Remove typed credentials from Anything Analyzer's local SQLite.

The analyzer's interaction recorder stores every keystroke's input_value in
plaintext, including password fields. This script blanks input_value for
input events on login/SSO/account pages and nothing else. Requests, hooks,
storage snapshots and all other interaction data are left intact.

Run with the analyzer CLOSED (it holds the DB open).
    python scripts/purge-analyzer-secrets.py          # dry run: shows what would change
    python scripts/purge-analyzer-secrets.py --apply  # actually blank the values
"""
import os
import shutil
import sqlite3
import sys
import time

DB = os.path.join(os.environ["APPDATA"], "anything-analyzer", "data", "anything-register.db")
AUTH_HOSTS = ["login.wisc.edu", "accounts.google.com", "duosecurity.com",
              "sso.", "/login", "signin", "auth"]

apply = "--apply" in sys.argv
con = sqlite3.connect(DB)
cur = con.cursor()

where = " OR ".join("url LIKE ?" for _ in AUTH_HOSTS)
params = [f"%{h}%" for h in AUTH_HOSTS]
rows = cur.execute(
    "SELECT id, url, length(input_value) FROM interaction_events "
    "WHERE type='input' AND input_value IS NOT NULL AND input_value<>'' "
    f"AND ({where})", params
).fetchall()

print(f"{len(rows)} input events on auth pages hold typed values:")
for _id, url, ln in rows:
    print(f"  {ln:>3} chars  {url.split('/')[2]}")

if not apply:
    print("\nDry run. Re-run with --apply to blank them.")
    sys.exit(0)

backup = DB + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(DB, backup)
cur.execute(
    "UPDATE interaction_events SET input_value='[redacted]' "
    f"WHERE type='input' AND ({where})", params
)
con.commit()
print(f"\nBlanked {cur.rowcount} values.")
print(f"Backup still contains the secrets; delete it once verified: {backup}")
