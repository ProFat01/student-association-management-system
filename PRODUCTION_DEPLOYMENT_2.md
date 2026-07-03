# SAMS — Production Deployment (Part 2)

## SQLite limitations and when to move to PostgreSQL

SQLite works for SAMS's initial deployment. WAL mode is already enabled
specifically for concurrent-write safety during elections. Move to PostgreSQL
when **any** of these trigger conditions apply:

1. A single election expects more than ~50 simultaneous voters in any
   5-minute window.
2. A second association is onboarded (also: `application_number` format
   `APP-YYYY-NNNNN` currently omits the association code — two associations
   could independently mint `APP-2026-00001` in the same year, which would
   fail the global uniqueness constraint; fix this separately before
   multi-tenancy goes live regardless of which database backend is in use).
3. PythonAnywhere starts logging "database is locked" errors in
   `logs/sams.log`.

**The migration when that time comes:**

```bash
# 1. Set the new DSN — this is the ONLY required change
#    Add to PythonAnywhere env vars:
DATABASE_URL=postgres://user:password@host:5432/sams

# 2. Uncomment psycopg in requirements.txt and reinstall
pip install -r requirements.txt

# 3. Run the same migrations against the new DB
python manage.py migrate

# 4. Transfer existing data
python manage.py dumpdata --natural-foreign --natural-primary > /tmp/sams_data.json
# (then point DATABASE_URL at the new DB and:)
python manage.py loaddata /tmp/sams_data.json
```

No application code changes are required — `DATABASE_URL` is now actually
wired up in both settings files (it wasn't before the audit; this was the
"documented but silent" bug described in Part 1 item 6).

---

## Backup and restore procedure

### Taking a backup

```bash
workon sams && cd ~/sams

# Full backup (DB + media)
python manage.py backup_db

# DB only (faster, use before/after deploys)
python manage.py backup_db --no-media

# Retain more than the default 7 backups
python manage.py backup_db --keep 30
```

Backups are written to `~/sams/backups/<YYYYMMDD_HHMMSS>/`:
- `db.sqlite3` — a consistent snapshot via SQLite's backup API
- `media.tar.gz` — all uploaded files (passport photos, receipts, candidate photos)

`backups/` is in `.gitignore` — it will not be accidentally committed.

### Scheduling automatic backups (PythonAnywhere Tasks tab, paid tier)

Create a daily scheduled task:
```
cd ~/sams && workon sams && python manage.py backup_db
```

Run it at low-traffic hours (e.g. 03:00 WAT). Also run it **manually
before and after every election event** and before every deployment.

### Restore procedure

```bash
# 1. Put the site into maintenance (Web tab → Reload with an intentionally
#    broken ALLOWED_HOSTS, or just accept brief downtime)

# 2. Restore the database
cp ~/sams/backups/<timestamp>/db.sqlite3 ~/sams/db.sqlite3

# 3. Restore media files
tar -xzf ~/sams/backups/<timestamp>/media.tar.gz -C ~/sams/

# 4. Verify
cd ~/sams
workon sams
python manage.py check
python -c "
import sqlite3
conn = sqlite3.connect('db.sqlite3')
cur = conn.cursor()
cur.execute('PRAGMA integrity_check')
print(cur.fetchone())   # should print ('ok',)
conn.close()
"

# 5. Reload the web app (Web tab → Reload)
```

**IMPORTANT: Test a restore before you need one.** Run the restore procedure
on a spare account or a local clone *now*, while it's not an emergency. A
backup that has never been test-restored is an optimistic claim, not a
recovery plan.

---

## Reverse proxy / HTTPS (why `SECURE_PROXY_SSL_HEADER` is not optional)

PythonAnywhere's architecture in one diagram:

```
Browser ──[HTTPS]──► PythonAnywhere reverse proxy ──[HTTP]──► Your Django process
                                                      │
                                          X-Forwarded-Proto: https
```

Django's `request.is_secure()` checks `wsgi.url_scheme` in the WSGI environ,
which is determined by the *actual transport* — plain HTTP in this case, even
for genuine HTTPS visitors.

Without `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`:

| Setting | Consequence |
|---|---|
| `SECURE_SSL_REDIRECT = True` | Infinite redirect loop — site completely inaccessible |
| `SESSION_COOKIE_SECURE = True` | Session cookies never set on "insecure" responses — no one can stay logged in |
| `CSRF_COOKIE_SECURE = True` | CSRF cookies never set — every POST returns 403 |

This is the most severe class of bug in the codebase and the easiest to miss,
because:
- `manage.py check --deploy` cannot know what proxy setup you're deploying behind
- It never manifests in any test run (no proxy in the test environment)
- The symptom (redirect loop) looks like a misconfigured URL or server error,
  not a missing single line in settings

This is now fixed and the fix has been verified (see Part 1 item 1).

---

## Deploying an update

```bash
cd ~/sams
workon sams
git pull origin main          # or upload the new zip

# Back up before touching anything
python manage.py backup_db --no-media

# Apply any new migrations
python manage.py migrate

# Re-run setup_roles if permissions.py changed
python manage.py setup_roles

# Rebuild the static manifest (always needed when static files change)
python manage.py collectstatic --noinput

# PythonAnywhere Web tab → Reload
```

---

## Quick-reference: everything that changed in this audit

| File | What changed |
|---|---|
| `config/settings/production.py` | `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL` wiring, fixed CSP (`font-src`, `fonts.googleapis.com`), production `LOGGING` with rotating file + `mail_admins` |
| `config/settings/development.py` | `DATABASE_URL` wiring (matches production) |
| `config/settings/base.py` | Base `LOGGING` config (console handler for all envs) |
| `requirements.txt` | Exact version pins instead of wildcards |
| `.env.example` | Complete rewrite — every variable actually referenced, with explanatory comments, `DATABASE_URL` correctly commented-out-by-default |
| `.gitignore` | Added `/logs/` and `/backups/` |
| `templates/base.html` | Inline `<script>` → `<script src="{% static 'js/site.js' %}">`, inline `style=""` → utility class |
| `static/js/site.js` | New file — nav toggle + vote-bar-width setter (CSP-safe) |
| `static/css/base.css` | New utility classes replacing the removed inline styles |
| `static/css/components.css` | `vote-bar-fill` width now set by JS instead of inline style |
| 12 other template files | `style="..."` → CSS utility classes, `data-percentage` on vote bars |
| `apps/core/management/commands/backup_db.py` | New — the entire backup mechanism |
| `apps/core/tests/test_backup_command.py` | 6 new tests for backup_db |
| `deploy/pythonanywhere_wsgi.py` | New WSGI configuration file |
