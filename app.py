import os, io, re, json, time, hashlib, sqlite3, uuid, datetime
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, send_from_directory, send_file, flash, abort, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps
import reports as rp
import validators as vd

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("APP_TZ", "Africa/Johannesburg"))
except Exception:  # no tz database available: South Africa is UTC+2 all year
    TZ = datetime.timezone(datetime.timedelta(hours=2))

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
UPLOADS = os.path.join(DATA, "uploads")
PENDING = os.path.join(UPLOADS, "pending")
DB = os.path.join(DATA, "slotadmin.db")
ALLOWED = {"jpg", "jpeg", "png", "webp", "heic", "pdf"}
DOC_TYPES = ["ID document", "Proof of address", "Payout slip", "Machine meter photo",
             "Cash-up sheet", "Incident report", "Large payout form", "Other"]
# staff = capture only | supervisor = + dashboard, reports, audit, void payouts | admin = + users, machines
ROLES = {"staff": 1, "supervisor": 2, "admin": 3}
LARGE_MIN = float(os.environ.get("LARGE_PAYOUT_MIN", "5000"))
LARGE_MAX = float(os.environ.get("LARGE_PAYOUT_MAX", "1000000"))
PAGE = 25

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # first-run admin; set via env var

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'staff', active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS machines(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, location TEXT, model TEXT,
  status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS clients(
  id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, id_number TEXT, phone TEXT,
  notes TEXT, created_at TEXT, id_type TEXT);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, machine_id INTEGER NOT NULL, client_id INTEGER,
  entry_date TEXT NOT NULL, amount_in REAL DEFAULT 0, amount_out REAL DEFAULT 0,
  notes TEXT, captured_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, client_id INTEGER, machine_id INTEGER, entry_id INTEGER,
  doc_type TEXT, filename TEXT NOT NULL, original_name TEXT, uploaded_by TEXT,
  uploaded_at TEXT);
CREATE TABLE IF NOT EXISTS payouts(
  id INTEGER PRIMARY KEY, machine_id INTEGER NOT NULL, client_id INTEGER NOT NULL,
  amount REAL NOT NULL, paid_at TEXT NOT NULL, entry_id INTEGER, filename TEXT NOT NULL,
  file_hash TEXT UNIQUE NOT NULL, ocr_text TEXT, flags TEXT, notes TEXT,
  captured_by TEXT, created_at TEXT, status TEXT NOT NULL DEFAULT 'valid',
  void_reason TEXT, voided_by TEXT, voided_at TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, at TEXT, user TEXT, action TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date);
CREATE INDEX IF NOT EXISTS idx_entries_machine ON entries(machine_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_entries_client ON entries(client_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_docs_client ON documents(client_id);
CREATE INDEX IF NOT EXISTS idx_clients_idnum ON clients(id_number);
CREATE INDEX IF NOT EXISTS idx_payouts_paid ON payouts(paid_at);
CREATE INDEX IF NOT EXISTS idx_payouts_machine ON payouts(machine_id, amount, paid_at);
CREATE INDEX IF NOT EXISTS idx_payouts_client ON payouts(client_id);
"""


def local_now():
    return datetime.datetime.now(TZ).replace(tzinfo=None)


def local_today():
    return local_now().date()


def now():
    return local_now().strftime("%Y-%m-%d %H:%M")


_ready = False


def init_db(conn):
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(clients)")}
    if "id_type" not in cols:
        try:
            conn.execute("ALTER TABLE clients ADD COLUMN id_type TEXT")
        except sqlite3.OperationalError:
            pass
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute("INSERT OR IGNORE INTO users(username,pw_hash,role,active,created_at) VALUES(?,?,?,?,?)",
                     (ADMIN_USER, generate_password_hash(ADMIN_PASSWORD), "admin", 1, now()))
    conn.commit()
    if os.environ.get("DEMO_DATA") == "1":  # presentation mode: sample data, only into an empty database
        import demo
        demo.seed(conn, UPLOADS, local_today(), now())


def db():
    global _ready
    if "db" not in g:
        os.makedirs(PENDING, exist_ok=True)
        g.db = sqlite3.connect(DB, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA synchronous=NORMAL")
        if not _ready:  # schema, migrations and first admin are set up once per process
            init_db(g.db)
            _ready = True
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()


def log(action, detail=""):
    db().execute("INSERT INTO audit(at,user,action,detail) VALUES(?,?,?,?)",
                 (now(), session.get("user"), action, detail))


def require(role="staff"):
    """Role check is made against the database on every request, so disabling a
    user or changing a role takes effect immediately."""
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            name = session.get("user")
            if not name:
                return redirect(url_for("login"))
            u = db().execute("SELECT role, active FROM users WHERE username=?", (name,)).fetchone()
            if not u or not u["active"]:
                session.clear()
                flash("Your account is disabled")
                return redirect(url_for("login"))
            g.role = u["role"]
            if ROLES[g.role] < ROLES[role]:
                abort(403)
            return f(*a, **k)
        return w
    return deco


@app.context_processor
def inject():
    r = g.get("role")
    return {"role": r, "username": session.get("user"),
            "can": lambda need: bool(r) and ROLES[r] >= ROLES[need]}


@app.errorhandler(403)
def forbidden(_):
    return render_template("forbidden.html"), 403


@app.errorhandler(404)
def notfound(_):
    return render_template("error.html", code=404, msg="That page or record could not be found."), 404


@app.errorhandler(500)
def servererr(_):
    return render_template("error.html", code=500, msg="Something went wrong on our side. Nothing was lost - please try again."), 500


@app.route("/health")
def health():
    return "ok", 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE username=?",
                         (request.form["username"].strip(),)).fetchone()
        if u and u["active"] and check_password_hash(u["pw_hash"], request.form["password"]):
            session.clear()
            session["user"] = u["username"]
            log("login")
            db().commit()
            return redirect(url_for("dashboard"))
        flash("Invalid credentials or account disabled")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _range(args):
    """Report date range, defaulting to this month in local time."""
    t = local_today()
    return rp.parse_range({"start": args.get("start") or t.replace(day=1).isoformat(),
                           "end": args.get("end") or t.isoformat()})


# ---------------------------------------------------------------- dashboard
@app.route("/")
@require("staff")
def dashboard():
    if ROLES[g.role] < ROLES["supervisor"]:
        return redirect(url_for("entries"))
    d = db()
    today = local_today().isoformat()
    month = today[:7]

    def tot(where, args=()):
        r = d.execute(f"SELECT COALESCE(SUM(amount_in),0) i, COALESCE(SUM(amount_out),0) o "
                      f"FROM entries WHERE {where}", args).fetchone()
        return {"in": r["i"], "out": r["o"], "net": r["i"] - r["o"]}
    big = d.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) t FROM payouts "
                    "WHERE status='valid' AND paid_at LIKE ?", (month + "%",)).fetchone()
    stats = {
        "today": tot("entry_date=?", (today,)),
        "month": tot("entry_date LIKE ?", (month + "%",)),
        "machines": d.execute("SELECT COUNT(*) FROM machines WHERE status='active'").fetchone()[0],
        "clients": d.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
        "docs": d.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "big_n": big["n"], "big_total": big["t"],
    }
    per_machine = d.execute("""
        SELECT m.code, m.location, COALESCE(SUM(e.amount_in),0) i,
               COALESCE(SUM(e.amount_out),0) o
        FROM machines m LEFT JOIN entries e ON e.machine_id=m.id AND e.entry_date LIKE ?
        GROUP BY m.id ORDER BY (COALESCE(SUM(e.amount_in),0)-COALESCE(SUM(e.amount_out),0)) DESC
    """, (month + "%",)).fetchall()
    recent = d.execute("""SELECT d.*, c.full_name FROM documents d
        LEFT JOIN clients c ON c.id=d.client_id ORDER BY d.id DESC LIMIT 6""").fetchall()
    bigrecent = d.execute("""SELECT p.id, p.paid_at, p.amount, m.code, c.full_name FROM payouts p
        JOIN machines m ON m.id=p.machine_id JOIN clients c ON c.id=p.client_id
        WHERE p.status='valid' ORDER BY p.paid_at DESC, p.id DESC LIMIT 5""").fetchall()
    return render_template("dashboard.html", s=stats, pm=per_machine, recent=recent, bigrecent=bigrecent)


# ------------------------------------------------------------------ machines
@app.route("/machines", methods=["GET", "POST"])
@require("staff")
def machines():
    d = db()
    if request.method == "POST":
        if ROLES[g.role] < ROLES["admin"]:
            abort(403)
        code = " ".join(request.form.get("code", "").split()).upper()
        if not code:
            flash("Enter a machine code")
            return redirect(url_for("machines"))
        try:
            d.execute("INSERT INTO machines(code,location,model) VALUES(?,?,?)",
                      (code, request.form.get("location", "").strip(), request.form.get("model", "").strip()))
            log("machine_add", code); d.commit()
            flash(f"Machine {code} added")
        except sqlite3.IntegrityError:
            flash("Machine code already exists")
        return redirect(url_for("machines"))
    return render_template("machines.html", rows=d.execute("SELECT * FROM machines ORDER BY code").fetchall())


@app.route("/machines/<int:mid>/toggle", methods=["POST"])
@require("admin")
def machine_toggle(mid):
    d = db()
    m = d.execute("SELECT * FROM machines WHERE id=?", (mid,)).fetchone() or abort(404)
    new = "inactive" if m["status"] == "active" else "active"
    d.execute("UPDATE machines SET status=? WHERE id=?", (new, mid))
    log("machine_status", f"{m['code']} -> {new}"); d.commit()
    flash(f"{m['code']} is now {new}")
    return redirect(url_for("machines"))


# ------------------------------------------------------------------- clients
@app.route("/clients", methods=["GET", "POST"])
@require("staff")
def clients():
    d = db()
    if request.method == "POST":
        f = request.form
        name = " ".join(f.get("full_name", "").split())
        idn = vd.clean_id(f.get("id_number", ""))
        if len(name) < 2:
            flash("Enter the client's full name")
            return redirect(url_for("clients"))
        if idn:
            if idn.isdigit() and len(idn) == 13:
                ok, age, msg = vd.sa_id_info(idn, local_today())
                if not ok:
                    flash(msg); return redirect(url_for("clients"))
            old = _find_client_by_id(d, idn)
            if old:
                flash("A client with that ID number already exists - opened their record")
                return redirect(url_for("client_detail", cid=old["id"]))
        cur = d.execute("INSERT INTO clients(full_name,id_number,phone,notes,created_at,id_type) VALUES(?,?,?,?,?,?)",
                        (name, idn, f.get("phone", "").strip(), f.get("notes", "").strip(), now(),
                         "sa_id" if idn.isdigit() and len(idn) == 13 else ("other" if idn else None)))
        log("client_add", name); d.commit()
        return redirect(url_for("client_detail", cid=cur.lastrowid))
    q = request.args.get("q", "")
    rows = d.execute("SELECT * FROM clients WHERE full_name LIKE ? OR id_number LIKE ? OR phone LIKE ? ORDER BY full_name LIMIT 300",
                     (f"%{q}%",) * 3).fetchall()
    return render_template("clients.html", rows=rows, q=q)


@app.route("/clients/<int:cid>")
@require("staff")
def client_detail(cid):
    d = db()
    c = d.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone() or abort(404)
    docs = d.execute("SELECT * FROM documents WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()
    ents = d.execute("""SELECT e.*, m.code FROM entries e JOIN machines m ON m.id=e.machine_id
                        WHERE client_id=? ORDER BY entry_date DESC, e.id DESC""", (cid,)).fetchall()
    return render_template("client_detail.html", c=c, docs=docs, ents=ents, types=DOC_TYPES)


@app.route("/api/clients")
@require("staff")
def api_clients():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    rows = db().execute("""SELECT id, full_name, id_number FROM clients
        WHERE full_name LIKE ? OR id_number LIKE ? ORDER BY full_name LIMIT 8""",
                        (f"%{q}%", f"%{q}%")).fetchall()
    out = []
    for r in rows:
        n = vd.clean_id(r["id_number"])
        out.append({"id": r["id"], "name": r["full_name"], "has_id": bool(n), "id_last4": n[-4:] if n else ""})
    return jsonify(out)


# ------------------------------------------------------------------- entries
@app.route("/entries", methods=["GET", "POST"])
@require("staff")
def entries():
    d = db()
    if request.method == "POST":
        f = request.form
        errs = []

        def money(key):
            raw = re.sub(r"[ ,]", "", f.get(key, "") or "0")
            try:
                v = round(float(raw), 2)
            except ValueError:
                errs.append("Amounts must be numbers"); return 0.0
            if v < 0 or v > LARGE_MAX:
                errs.append("Amounts must be between 0 and R {:,.0f}".format(LARGE_MAX))
            return v
        amt_in, amt_out = money("amount_in"), money("amount_out")
        if not errs and amt_in == 0 and amt_out == 0:
            errs.append("Enter an amount in or an amount out")
        if amt_out >= LARGE_MIN:
            errs.append("Payouts of R {:,.0f} and above are captured on the Large payouts page, "
                        "with the photo of the signed form".format(LARGE_MIN))
        try:
            ed = datetime.date.fromisoformat(f.get("entry_date", ""))
            if ed > local_today():
                errs.append("The date cannot be in the future")
        except ValueError:
            errs.append("Enter a valid date")
        mid = f.get("machine_id", "")
        if not d.execute("SELECT 1 FROM machines WHERE id=? AND status='active'", (mid if mid.isdigit() else 0,)).fetchone():
            errs.append("Select an active machine")
        cl = f.get("client_id") or None
        if cl and not d.execute("SELECT 1 FROM clients WHERE id=?", (cl if cl.isdigit() else 0,)).fetchone():
            errs.append("The selected client was not found")
        if errs:
            for e in errs:
                flash(e)
            return redirect(url_for("entries"))
        cur = d.execute("""INSERT INTO entries(machine_id,client_id,entry_date,amount_in,amount_out,notes,captured_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (mid, cl, ed.isoformat(), amt_in, amt_out, f.get("notes", "").strip(), session["user"], now()))
        eid = cur.lastrowid
        save_upload(request.files.get("photo"), "Payout slip", entry_id=eid, client_id=cl, machine_id=mid)
        log("entry_add", f"#{eid}"); d.commit()
        flash("Entry saved")
        return redirect(url_for("entries"))
    rows = d.execute("""SELECT e.*, m.code, c.full_name,
        (SELECT COUNT(*) FROM documents WHERE entry_id=e.id) ndocs
        FROM entries e JOIN machines m ON m.id=e.machine_id
        LEFT JOIN clients c ON c.id=e.client_id ORDER BY entry_date DESC, e.id DESC LIMIT 200""").fetchall()
    return render_template("entries.html", rows=rows,
        machines=d.execute("SELECT * FROM machines WHERE status='active' ORDER BY code").fetchall(),
        clients=d.execute("SELECT id,full_name FROM clients ORDER BY full_name").fetchall(),
        today=local_today().isoformat())


def save_upload(f, doc_type, client_id=None, machine_id=None, entry_id=None):
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED:
        flash("File type not allowed"); return None
    name = f"{uuid.uuid4().hex}.{ext}"
    os.makedirs(UPLOADS, exist_ok=True)
    f.save(os.path.join(UPLOADS, name))
    db().execute("""INSERT INTO documents(client_id,machine_id,entry_id,doc_type,filename,original_name,uploaded_by,uploaded_at)
        VALUES(?,?,?,?,?,?,?,?)""", (client_id, machine_id, entry_id, doc_type, name,
                                      secure_filename(f.filename), session.get("user"), now()))
    return name


@app.route("/documents", methods=["GET", "POST"])
@require("staff")
def documents():
    d = db()
    if request.method == "POST":
        n = 0
        for f in request.files.getlist("files"):
            if save_upload(f, request.form["doc_type"], request.form.get("client_id") or None,
                           request.form.get("machine_id") or None): n += 1
        log("doc_upload", f"{n} files"); d.commit()
        flash(f"{n} file(s) uploaded")
        nxt = request.form.get("next") or ""
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("documents"))
    rows = d.execute("""SELECT d.*, c.full_name, m.code FROM documents d
        LEFT JOIN clients c ON c.id=d.client_id LEFT JOIN machines m ON m.id=d.machine_id
        ORDER BY d.id DESC LIMIT 200""").fetchall()
    return render_template("documents.html", rows=rows, types=DOC_TYPES,
        machines=d.execute("SELECT * FROM machines ORDER BY code").fetchall(),
        clients=d.execute("SELECT id,full_name FROM clients ORDER BY full_name").fetchall())


@app.route("/files/<name>")
@require("staff")
def files(name):
    resp = send_from_directory(UPLOADS, name)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.route("/audit")
@require("supervisor")
def audit():
    return render_template("audit.html", rows=db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 300").fetchall())


# ------------------------------------------------------------ large payouts
def process_image(data):
    """Validate the upload is a real photo, fix its rotation, shrink it and make a thumbnail."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise ValueError("That file is not a readable photo - please retake it")
    im = ImageOps.exif_transpose(im).convert("RGB")
    if min(im.size) < 300:
        raise ValueError("The photo is too small to read - please retake it closer to the form")
    im.thumbnail((1800, 1800))
    full = io.BytesIO()
    im.save(full, "JPEG", quality=82, optimize=True)
    th = im.copy()
    th.thumbnail((360, 360))
    t = io.BytesIO()
    th.save(t, "JPEG", quality=70, optimize=True)
    return full.getvalue(), t.getvalue()


def _pending_path(token):
    return os.path.join(PENDING, token + ".jpg")


def _clean_pending():
    cutoff = time.time() - 86400
    try:
        for fn in os.listdir(PENDING):
            p = os.path.join(PENDING, fn)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except OSError:
        pass


def _find_client_by_id(d, id_number):
    return d.execute("SELECT * FROM clients WHERE REPLACE(REPLACE(id_number,' ',''),'-','')=?",
                     (id_number,)).fetchone()


@app.route("/payouts/suggest", methods=["POST"])
@require("staff")
def payout_suggest():
    text = (request.get_json(silent=True) or {}).get("text", "")[:6000]
    return jsonify(vd.match_text(db(), text, LARGE_MIN, LARGE_MAX))


@app.route("/payouts")
@require("staff")
def payouts():
    d = db()
    a = request.args
    where, args = ["1=1"], []
    start, end = a.get("start", ""), a.get("end", "")
    if start:
        where.append("substr(p.paid_at,1,10) >= ?"); args.append(start)
    if end:
        where.append("substr(p.paid_at,1,10) <= ?"); args.append(end)
    if a.get("machine", "").isdigit():
        where.append("p.machine_id = ?"); args.append(int(a["machine"]))
    q = a.get("q", "").strip()
    if q:
        where.append("(c.full_name LIKE ? OR c.id_number LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    if a.get("status") in ("valid", "void"):
        where.append("p.status = ?"); args.append(a["status"])
    cond = " AND ".join(where)
    base = "FROM payouts p JOIN machines m ON m.id=p.machine_id JOIN clients c ON c.id=p.client_id WHERE " + cond
    total = d.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
    page = max(1, int(a["page"])) if a.get("page", "").isdigit() else 1
    rows = d.execute("SELECT p.*, m.code, c.full_name " + base +
                     " ORDER BY p.paid_at DESC, p.id DESC LIMIT ? OFFSET ?",
                     args + [PAGE, (page - 1) * PAGE]).fetchall()
    month = local_today().isoformat()[:7]
    k = d.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) t FROM payouts WHERE status='valid' AND paid_at LIKE ?",
                  (month + "%",)).fetchone()
    pages = max(1, -(-total // PAGE))
    qs = {x: y for x, y in a.items() if x != "page" and y}
    return render_template("payouts.html", rows=rows, total=total, page=page, pages=pages, qs=qs,
                           k=k, lo=LARGE_MIN, a=a,
                           machines=d.execute("SELECT id, code FROM machines ORDER BY code").fetchall())


@app.route("/payouts/new", methods=["GET", "POST"])
@require("staff")
def payout_new():
    d = db()
    machines_ = d.execute("SELECT id, code, location FROM machines WHERE status='active' ORDER BY code").fetchall()
    ctx = dict(machines=machines_, lo=LARGE_MIN, errors=[], token="", show_dup=False, picked=None,
               now_local=local_now().strftime("%Y-%m-%dT%H:%M"))
    if request.method == "GET":
        return render_template("payout_new.html", f={}, **ctx)

    f, errors = request.form, []
    # ---- photo (new upload, or the one kept from a previous failed attempt)
    token = f.get("photo_token", "")
    up = request.files.get("photo")
    data = None
    if up and up.filename:
        data = up.read()
    elif re.fullmatch(r"[0-9a-f]{32}", token) and os.path.exists(_pending_path(token)):
        with open(_pending_path(token), "rb") as fh:
            data = fh.read()
    full = thumb = None
    if not data:
        errors.append("A photo of the signed payout form is required")
    else:
        try:
            full, thumb = process_image(data)
        except ValueError as ex:
            errors.append(str(ex))

    # ---- amount and time
    try:
        amount = round(float(re.sub(r"[ ,]", "", f.get("amount", ""))), 2)
    except ValueError:
        amount = None
    if amount is None:
        errors.append("Enter the payout amount")
    elif amount < LARGE_MIN:
        errors.append(f"Large payouts start at R {LARGE_MIN:,.0f}. Capture smaller wins on the Wins / Losses page")
    elif amount > LARGE_MAX:
        errors.append(f"R {amount:,.2f} looks wrong (limit R {LARGE_MAX:,.0f}) - please check the amount")
    paid = None
    try:
        paid = datetime.datetime.fromisoformat(f.get("paid_at", ""))
    except ValueError:
        errors.append("Enter the date and time of the payout")
    if paid:
        nowl = local_now()
        if paid > nowl + datetime.timedelta(minutes=5):
            errors.append("The payout time cannot be in the future")
        elif (nowl - paid).days > 14 and ROLES[g.role] < ROLES["supervisor"]:
            errors.append("Payouts older than 14 days must be captured by a supervisor")

    # ---- machine
    mid = f.get("machine_id", "")
    m = d.execute("SELECT * FROM machines WHERE id=? AND status='active'", (mid if mid.isdigit() else 0,)).fetchone()
    if not m:
        errors.append("Select the machine that paid out")

    # ---- customer and ID verification
    client = None
    cid = f.get("client_id", "")
    if cid:
        client = d.execute("SELECT * FROM clients WHERE id=?", (cid if cid.isdigit() else 0,)).fetchone()
        if not client:
            errors.append("The selected customer was not found")
    id_type = f.get("id_type", "sa_id")
    id_number = vd.clean_id(f.get("id_number", ""))
    if client is not None and vd.clean_id(client["id_number"]):
        id_number = vd.clean_id(client["id_number"])
        id_type = client["id_type"] or ("sa_id" if id_number.isdigit() and len(id_number) == 13 else "other")
    new_name = " ".join(f.get("new_name", "").split())
    if client is None and len(new_name.split()) < 2:
        errors.append("Enter the customer's full name (first name and surname)")
    if not id_number:
        errors.append("The customer's ID number is required for a large payout")
    elif id_type == "sa_id":
        ok, age, msg = vd.sa_id_info(id_number, local_today())
        if not ok:
            errors.append(msg)
        elif age < 18:
            errors.append(f"The customer is {age} years old - payouts are only for customers aged 18 or older")
    else:
        if len(id_number) < 5:
            errors.append("Enter the passport / ID number as it appears on the document")
        if not f.get("adult_confirm"):
            errors.append("Confirm that you checked the customer is 18 or older")
    if client is None and id_number and not errors:
        client = _find_client_by_id(d, id_number)  # same ID already on file: reuse that customer

    # ---- duplicates
    show_dup = False
    fhash = hashlib.sha256(full).hexdigest() if full else None
    if fhash and not errors:
        prev = d.execute("SELECT id FROM payouts WHERE file_hash=?", (fhash,)).fetchone()
        if prev:
            errors.append(f"This exact photo was already captured as payout #{prev['id']}")
        else:
            dup = d.execute("""SELECT id FROM payouts WHERE status='valid' AND machine_id=? AND amount=?
                AND ABS(julianday(?) - julianday(paid_at)) < 1""",
                            (m["id"], amount, paid.strftime("%Y-%m-%d %H:%M"))).fetchone()
            if dup and not f.get("dup_ok"):
                show_dup = True
                errors.append(f"Payout #{dup['id']} on {m['code']} for the same amount was captured in the last 24 hours. "
                              "If this is a different payout, tick the box below and save again")

    if errors:
        token_out = token if (re.fullmatch(r"[0-9a-f]{32}", token) and os.path.exists(_pending_path(token))) else ""
        if full and not token_out:  # keep the photo so staff do not have to retake it
            token_out = uuid.uuid4().hex
            with open(_pending_path(token_out), "wb") as fh:
                fh.write(full)
        picked = None
        if client is not None:
            n = vd.clean_id(client["id_number"])
            picked = {"id": client["id"], "name": client["full_name"], "has_id": bool(n),
                      "id_last4": n[-4:] if n else ""}
        ctx.update(errors=errors, token=token_out, show_dup=show_dup, picked=picked)
        return render_template("payout_new.html", f=f, **ctx), 400

    # ---- cross-check against what the photo says (information only, not blocking)
    ocr_text = (f.get("ocr_text") or "")[:4000]
    flags = []
    if ocr_text.strip():
        sug = vd.match_text(d, ocr_text, LARGE_MIN, LARGE_MAX)
        if sug["machine_id"] and sug["machine_id"] != m["id"]:
            flags.append(f"Photo reads machine {sug['machine_code']}")
        if sug["amount"] and abs(sug["amount"] - amount) > 0.5:
            flags.append(f"Photo reads R {sug['amount']:,.2f}")
        if sug["id_number"] and sug["id_number"] != id_number:
            flags.append("ID number differs from photo")
        elif sug["client"] and client and sug["client"]["id"] != client["id"]:
            flags.append(f"Photo matches customer {sug['client']['name']}")
    else:
        flags.append("Entered manually - photo not machine-read")

    # ---- save: files first, then one database transaction
    fname = uuid.uuid4().hex + ".jpg"
    os.makedirs(UPLOADS, exist_ok=True)
    with open(os.path.join(UPLOADS, fname), "wb") as fh:
        fh.write(full)
    with open(os.path.join(UPLOADS, "t_" + fname), "wb") as fh:
        fh.write(thumb)
    try:
        if client is None:
            cur = d.execute("INSERT INTO clients(full_name,id_number,phone,notes,created_at,id_type) VALUES(?,?,?,?,?,?)",
                            (new_name, id_number, f.get("phone", "").strip(), "", now(), id_type))
            client_id = cur.lastrowid
        else:
            client_id = client["id"]
            if not vd.clean_id(client["id_number"]):
                d.execute("UPDATE clients SET id_number=?, id_type=? WHERE id=?", (id_number, id_type, client_id))
        cur = d.execute("""INSERT INTO entries(machine_id,client_id,entry_date,amount_in,amount_out,notes,captured_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (m["id"], client_id, paid.date().isoformat(), 0, amount,
                                         "Large payout", session["user"], now()))
        entry_id = cur.lastrowid
        cur = d.execute("""INSERT INTO payouts(machine_id,client_id,amount,paid_at,entry_id,filename,file_hash,ocr_text,
            flags,notes,captured_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (m["id"], client_id, amount, paid.strftime("%Y-%m-%d %H:%M"), entry_id, fname, fhash,
                         ocr_text, "; ".join(flags), f.get("notes", "").strip(), session["user"], now()))
        pid = cur.lastrowid
        d.execute("UPDATE entries SET notes=? WHERE id=?", (f"Large payout #{pid}", entry_id))
        d.execute("""INSERT INTO documents(client_id,machine_id,entry_id,doc_type,filename,original_name,uploaded_by,uploaded_at)
            VALUES(?,?,?,?,?,?,?,?)""", (client_id, m["id"], entry_id, "Large payout form", fname,
                                          "payout-form.jpg", session["user"], now()))
        log("payout_add", f"#{pid} {m['code']} R{amount:,.2f}")
        d.commit()
    except Exception:
        d.rollback()
        for p in (fname, "t_" + fname):
            try:
                os.remove(os.path.join(UPLOADS, p))
            except OSError:
                pass
        raise
    if re.fullmatch(r"[0-9a-f]{32}", token):
        try:
            os.remove(_pending_path(token))
        except OSError:
            pass
    _clean_pending()
    flash(f"Large payout #{pid} saved: R {amount:,.2f} on {m['code']}")
    return redirect(url_for("payout_detail", pid=pid))


@app.route("/payouts/<int:pid>")
@require("staff")
def payout_detail(pid):
    p = db().execute("""SELECT p.*, m.code, m.location, c.full_name, c.id_number, c.phone, c.id_type
        FROM payouts p JOIN machines m ON m.id=p.machine_id JOIN clients c ON c.id=p.client_id
        WHERE p.id=?""", (pid,)).fetchone() or abort(404)
    return render_template("payout_detail.html", p=p)


@app.route("/payouts/<int:pid>/void", methods=["POST"])
@require("supervisor")
def payout_void(pid):
    d = db()
    p = d.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone() or abort(404)
    reason = " ".join(request.form.get("reason", "").split())
    if p["status"] != "valid":
        flash("This payout is already voided")
    elif len(reason) < 5:
        flash("Enter a reason (at least 5 characters) to void a payout")
    else:
        d.execute("UPDATE payouts SET status='void', void_reason=?, voided_by=?, voided_at=? WHERE id=?",
                  (reason, session["user"], now(), pid))
        if p["entry_id"]:
            d.execute("DELETE FROM entries WHERE id=?", (p["entry_id"],))  # takes it out of the reports
        log("payout_void", f"#{pid}: {reason}")
        d.commit()
        flash(f"Payout #{pid} voided and removed from the reports")
    return redirect(url_for("payout_detail", pid=pid))


# ------------------------------------------------------------------- reports
@app.route("/reports")
@require("supervisor")
def reports():
    s, e = _range(request.args)
    d = db()
    reps = [rp.client_report(d, s, e), rp.machine_report(d, s, e), rp.business_report(d, s, e)]
    return render_template("reports.html", reps=reps, charts=[rp.chart_data(r) for r in reps],
                           start=s, end=e, fmtmoney=lambda v: f"R {v:,.2f}")


@app.route("/reports/export/<kind>/<fmt>")
@require("supervisor")
def export_report(kind, fmt):
    if kind not in rp.BUILDERS or fmt not in rp.EXPORTERS:
        abort(404)
    s, e = _range(request.args)
    d = db()
    rep = rp.BUILDERS[kind](d, s, e)
    fn, mime = rp.EXPORTERS[fmt]
    data = fn(rep, s, e)
    log("report_export", f"{kind} {fmt} {s}..{e}"); d.commit()
    return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True,
                     download_name=f"{kind}-performance_{s}_to_{e}.{fmt}")


# --------------------------------------------------------------------- users
@app.route("/users", methods=["GET", "POST"])
@require("admin")
def users():
    d = db()
    if request.method == "POST":
        u = request.form["username"].strip()
        pw = request.form["password"]
        role = request.form["role"]
        if not u or role not in ROLES or len(pw) < 8:
            flash("Username, a valid role and a password of at least 8 characters are required")
        else:
            try:
                d.execute("INSERT INTO users(username,pw_hash,role,active,created_at) VALUES(?,?,?,?,?)",
                          (u, generate_password_hash(pw), role, 1, now()))
                log("user_add", f"{u} ({role})"); d.commit()
            except sqlite3.IntegrityError:
                flash("That username is taken")
        return redirect(url_for("users"))
    return render_template("users.html", rows=d.execute("SELECT * FROM users ORDER BY username").fetchall(),
                           roles=list(ROLES))


def _target(uid):
    u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() or abort(404)
    return u


@app.route("/users/<int:uid>/toggle", methods=["POST"])
@require("admin")
def user_toggle(uid):
    u = _target(uid)
    if u["username"] == session["user"]:
        flash("You cannot disable your own account")
    else:
        db().execute("UPDATE users SET active=? WHERE id=?", (0 if u["active"] else 1, uid))
        log("user_toggle", u["username"]); db().commit()
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/role", methods=["POST"])
@require("admin")
def user_role(uid):
    u = _target(uid)
    role = request.form["role"]
    if u["username"] == session["user"] or role not in ROLES:
        flash("You cannot change your own role")
    else:
        db().execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        log("user_role", f"{u['username']} -> {role}"); db().commit()
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/password", methods=["POST"])
@require("admin")
def user_password(uid):
    u = _target(uid)
    pw = request.form["password"]
    if len(pw) < 8:
        flash("Password must be at least 8 characters")
    else:
        db().execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), uid))
        log("user_password", u["username"]); db().commit()
        flash(f"Password updated for {u['username']}")
    return redirect(url_for("users"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
