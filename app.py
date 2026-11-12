import os, io, re, json, time, hashlib, sqlite3, uuid, datetime
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, send_from_directory, send_file, flash, abort, jsonify, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps
import reports as rp
import validators as vd
import bi
import importer as imp
import licence
import secrets

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
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=8)
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
MASTER = os.environ.get("MASTER") == "1"          # this deployment is the Gilded Age backend, not a client installation
GRACE_DAYS = int(os.environ.get("GRACE_DAYS", "5"))
RETENTION_YEARS = int(os.environ.get("RETENTION_YEARS", "5"))
INFO_OFFICER = os.environ.get("INFO_OFFICER_NAME", "")
INFO_OFFICER_EMAIL = os.environ.get("INFO_OFFICER_EMAIL", "")
RESPONSIBLE_PARTY = os.environ.get("RESPONSIBLE_PARTY", "the venue operator")
MIN_PW = 10
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
CREATE TABLE IF NOT EXISTS meter_readings(
  id INTEGER PRIMARY KEY, machine_id INTEGER NOT NULL, read_at TEXT NOT NULL, coin_in REAL, coin_out REAL,
  document_id INTEGER, captured_by TEXT);
CREATE INDEX IF NOT EXISTS idx_meter_machine ON meter_readings(machine_id, read_at);
CREATE TABLE IF NOT EXISTS machine_installs(
  id INTEGER PRIMARY KEY, machine_id INTEGER UNIQUE NOT NULL, serial_no TEXT, manufacturer TEXT,
  installed_by TEXT, installed_at TEXT, checks TEXT, notes TEXT, flags TEXT, status TEXT DEFAULT 'incomplete', updated_at TEXT);
CREATE TABLE IF NOT EXISTS install_photos(
  id INTEGER PRIMARY KEY, install_id INTEGER NOT NULL, slot TEXT NOT NULL, filename TEXT NOT NULL,
  ocr_text TEXT, flags TEXT, taken_by TEXT, taken_at TEXT);
CREATE INDEX IF NOT EXISTS idx_install_photos ON install_photos(install_id, slot);
CREATE TABLE IF NOT EXISTS popia_requests(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, client_id INTEGER, received_at TEXT, due_at TEXT,
  status TEXT NOT NULL DEFAULT 'open', notes TEXT, logged_by TEXT, closed_at TEXT, closed_by TEXT);
CREATE TABLE IF NOT EXISTS popia_incidents(
  id INTEGER PRIMARY KEY, discovered_at TEXT, description TEXT NOT NULL, info_affected TEXT, people_affected TEXT,
  regulator_notified TEXT, subjects_notified TEXT, status TEXT NOT NULL DEFAULT 'open', logged_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY, k TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(k, ts);
CREATE TABLE IF NOT EXISTS tenants(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, contact_name TEXT, contact_email TEXT, plan TEXT, monthly_fee REAL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', suspended_reason TEXT, paid_until TEXT, license_key TEXT UNIQUE NOT NULL,
  instance_url TEXT, notes TEXT, created_at TEXT, last_seen TEXT, last_stats TEXT, version TEXT);
CREATE TABLE IF NOT EXISTS tenant_resets(
  id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, username TEXT NOT NULL, pw_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', requested_by TEXT, created_at TEXT, applied_at TEXT);
CREATE TABLE IF NOT EXISTS tenant_events(
  id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, at TEXT, user TEXT, detail TEXT);
"""


def local_now():
    return datetime.datetime.now(TZ).replace(tzinfo=None)


def local_today():
    return local_now().date()


def now():
    return local_now().strftime("%Y-%m-%d %H:%M")


_ready = False


def _addcol(conn, table, col, ddl):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass


def init_db(conn):
    conn.executescript(SCHEMA)
    for t, c, ddl in (("entries", "source", "TEXT DEFAULT 'manual'"), ("entries", "photo_check", "TEXT"),
                      ("entries", "flags", "TEXT"), ("documents", "ocr_text", "TEXT"),
                      ("documents", "ocr_flags", "TEXT"), ("clients", "consent_at", "TEXT"),
                      ("clients", "consent_by", "TEXT"), ("clients", "anonymised_at", "TEXT")):
        _addcol(conn, t, c, ddl)
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
            "can": lambda need: bool(r) and ROLES[r] >= ROLES[need], "master_mode": MASTER,
            "mask_id": lambda s: (s or "") if (r and ROLES[r] >= ROLES["supervisor"]) or not s else "•" * max(0, len(s) - 4) + s[-4:]}


@app.after_request
def secure_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    if request.endpoint not in ("static", "files"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


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
        d = db()
        uname = request.form["username"].strip()[:60]
        ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
        ku, ki, t0 = "u:" + uname.lower(), "i:" + ip, time.time() - 900
        d.execute("DELETE FROM login_attempts WHERE ts<?", (t0,))
        nu = d.execute("SELECT COUNT(*) FROM login_attempts WHERE k=?", (ku,)).fetchone()[0]
        ni = d.execute("SELECT COUNT(*) FROM login_attempts WHERE k=?", (ki,)).fetchone()[0]
        if nu >= 5 or ni >= 30:
            flash("Too many failed sign-in attempts. Please wait 15 minutes and try again.")
            d.commit()
            return render_template("login.html"), 429
        u = d.execute("SELECT * FROM users WHERE username=?", (uname,)).fetchone()
        if u and u["active"] and check_password_hash(u["pw_hash"], request.form["password"]):
            d.execute("DELETE FROM login_attempts WHERE k=?", (ku,))
            session.clear()
            session.permanent = True
            session["user"] = u["username"]
            log("login")
            d.commit()
            return redirect(url_for("dashboard"))
        d.execute("INSERT INTO login_attempts(k,ts) VALUES(?,?)", (ku, time.time()))
        d.execute("INSERT INTO login_attempts(k,ts) VALUES(?,?)", (ki, time.time()))
        d.commit()
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
    key = request.args.get("p", "mtd")
    if key not in ("mtd", "prior", "90", "ytd"):
        key = "mtd"
    ov = bi.overview(d, key, local_today())
    bigrecent = d.execute("""SELECT p.id, p.paid_at, p.amount, m.code, c.full_name FROM payouts p
        JOIN machines m ON m.id=p.machine_id JOIN clients c ON c.id=p.client_id
        WHERE p.status='valid' ORDER BY p.paid_at DESC, p.id DESC LIMIT 5""").fetchall()
    return render_template("dashboard.html", ov=ov, bigrecent=bigrecent)


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
    installs = {r["machine_id"]: r["status"] for r in d.execute("SELECT machine_id, status FROM machine_installs")}
    meters = {r["machine_id"]: r for r in d.execute(
        "SELECT * FROM meter_readings m1 WHERE id=(SELECT MAX(id) FROM meter_readings m2 WHERE m2.machine_id=m1.machine_id)")}
    return render_template("machines.html", rows=d.execute("SELECT * FROM machines ORDER BY code").fetchall(), meters=meters, installs=installs)


INSTALL_SLOTS = [
    ("front", "Front of the machine (whole cabinet, screen on)"),
    ("plate", "Serial / compliance plate (close up, readable)"),
    ("label", "Machine code label fixed to the cabinet"),
    ("meter", "Meter or accounting screen at install"),
    ("position", "Position on the floor, showing the surroundings"),
]
INSTALL_CHECKS = [
    ("power", "Machine powered up and test-played"),
    ("label", "Machine code label fixed and legible"),
    ("meters", "Opening meter readings recorded"),
    ("printer", "Ticket / cash printer tested"),
    ("locks", "Cabinet locked and keys logged"),
    ("position", "Position recorded on the floor plan"),
]


def _install_state(d, mid):
    inst = d.execute("SELECT * FROM machine_installs WHERE machine_id=?", (mid,)).fetchone()
    photos = {}
    if inst:
        for r in d.execute("SELECT * FROM install_photos WHERE install_id=? ORDER BY id", (inst["id"],)):
            photos[r["slot"]] = r  # latest photo per slot wins
    return inst, photos


@app.route("/machines/<int:mid>/install", methods=["GET", "POST"])
@require("staff")
def machine_install(mid):
    d = db()
    m = d.execute("SELECT * FROM machines WHERE id=?", (mid,)).fetchone() or abort(404)
    inst, photos = _install_state(d, mid)
    if request.method == "POST":
        f = request.form
        serial = " ".join(f.get("serial_no", "").split()).upper()
        back = redirect(url_for("machine_install", mid=mid))
        if serial and d.execute("SELECT 1 FROM machine_installs WHERE serial_no=? AND machine_id<>?", (serial, mid)).fetchone():
            flash("That serial number is already registered to another machine - check the plate photo")
            return back
        newp = {}
        for key, label in INSTALL_SLOTS:
            up = request.files.get("photo_" + key)
            if up and up.filename:
                try:
                    newp[key] = process_image(up.read())[0]
                except ValueError as ex:
                    flash(f"{label}: {ex}")
                    return back

        def num(k):
            raw = re.sub(r"[ ,]", "", f.get(k, ""))
            try:
                v = float(raw)
            except ValueError:
                return None
            return v if v >= 0 else None
        m_in, m_out = num("meter_in"), num("meter_out")
        flags = []
        saved = {}
        for key, full in newp.items():
            ocr = (f.get("ocr_" + key) or "")[:4000]
            pf = []
            if ocr.strip():
                if key == "label":
                    r = vd.read_plate(d, ocr, local_today())
                    if r["machine_id"] and r["machine_id"] != mid:
                        pf.append("Label photo reads " + r["machine_code"])
                elif key == "plate":
                    r = vd.read_plate(d, ocr, local_today())
                    if r["serial"] and serial and r["serial"] != serial:
                        pf.append("Plate photo reads serial " + r["serial"])
                elif key == "meter":
                    r = vd.read_meter(d, ocr, local_today())
                    if r["coin_in"] is not None and m_in is not None and abs(r["coin_in"] - m_in) > 0.5:
                        pf.append("Photo reads coin in %s" % format(r["coin_in"], ",.0f"))
                    if r["coin_out"] is not None and m_out is not None and abs(r["coin_out"] - m_out) > 0.5:
                        pf.append("Photo reads coin out %s" % format(r["coin_out"], ",.0f"))
            elif key in ("plate", "label", "meter"):
                pf.append("Photo not machine-read")
            saved[key] = (ocr, "; ".join(pf))
            flags += [x for x in pf if "not machine-read" not in x]
        checks = [k for k, _ in INSTALL_CHECKS if f.get("chk_" + k)]
        if inst is None:
            cur = d.execute("INSERT INTO machine_installs(machine_id,installed_by,installed_at) VALUES(?,?,?)",
                            (mid, session["user"], now()))
            iid = cur.lastrowid
            first_time = True
        else:
            iid, first_time = inst["id"], False
        os.makedirs(UPLOADS, exist_ok=True)
        meter_doc = None
        for key, full in newp.items():
            fn = uuid.uuid4().hex + ".jpg"
            with open(os.path.join(UPLOADS, fn), "wb") as fh:
                fh.write(full)
            ocr, pf = saved[key]
            d.execute("INSERT INTO install_photos(install_id,slot,filename,ocr_text,flags,taken_by,taken_at) VALUES(?,?,?,?,?,?,?)",
                      (iid, key, fn, ocr, pf, session["user"], now()))
            cur = d.execute("""INSERT INTO documents(client_id,machine_id,entry_id,doc_type,filename,original_name,uploaded_by,uploaded_at,
                ocr_text,ocr_flags) VALUES(?,?,?,?,?,?,?,?,?,?)""", (None, mid, None, "Machine install photo", fn,
                                                               key + ".jpg", session["user"], now(), ocr, pf))
            if key == "meter":
                meter_doc = cur.lastrowid
        if (m_in is not None or m_out is not None) and (first_time or "meter" in newp):
            d.execute("INSERT INTO meter_readings(machine_id,read_at,coin_in,coin_out,document_id,captured_by) VALUES(?,?,?,?,?,?)",
                      (mid, now(), m_in, m_out, meter_doc, session["user"]))
        have = {k for k, _ in INSTALL_SLOTS if k in newp or k in photos}
        old_flags = [x for x in (inst["flags"] or "").split("; ") if x] if inst else []
        # the flags describe the latest photos, so they are replaced whenever any photo is re-taken
        all_flags = flags if newp else old_flags
        complete = serial and len(have) == len(INSTALL_SLOTS) and len(checks) == len(INSTALL_CHECKS)
        status = "incomplete" if not complete else ("review" if all_flags else "complete")
        d.execute("""UPDATE machine_installs SET serial_no=?, manufacturer=?, checks=?, notes=?, flags=?, status=?, updated_at=?
            WHERE id=?""", (serial or None, " ".join(f.get("manufacturer", "").split()), ",".join(checks),
                            f.get("notes", "").strip(), "; ".join(all_flags), status, now(), iid))
        log("machine_install", f"{m['code']} {status}"); d.commit()
        flash({"complete": f"{m['code']} installation is complete and verified.",
               "review": f"{m['code']} installation saved, but the photos show differences a supervisor should review.",
               "incomplete": f"{m['code']} installation saved. Still to do: " + ", ".join(
                   (["serial number"] if not serial else []) +
                   [lab.split(" (")[0].lower() for k, lab in INSTALL_SLOTS if k not in have] +
                   (["checklist"] if len(checks) < len(INSTALL_CHECKS) else []))}[status])
        return back
    return render_template("machine_install.html", m=m, inst=inst, photos=photos, slots=INSTALL_SLOTS, checks_def=INSTALL_CHECKS,
                           done=(inst["checks"] or "").split(",") if inst else [])


@app.route("/manual/install")
@require("staff")
def manual_install():
    return render_template("manual_install.html", slots=INSTALL_SLOTS, checks_def=INSTALL_CHECKS, lo=LARGE_MIN)


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
def _attach_id_photo(d, client_id, idn):
    """Keep the photo of the customer's ID with their record and note whether it agrees with the entered ID number."""
    up = request.files.get("photo")
    if not _ext_ok(up):
        return
    ocr = (request.form.get("ocr_text") or "")[:4000]
    if not ocr.strip():
        flag = "Photo attached, not machine-read"
    else:
        r = vd.read_id(d, ocr, local_today())
        if r["id_number"] and idn:
            flag = "ID matches photo" if r["id_number"] == idn else "ID number differs from photo"
        else:
            flag = "Photo attached, ID number not readable"
    save_upload(up, "ID document", client_id=client_id, ocr_text=ocr, ocr_flags=flag)


@app.route("/capture/suggest", methods=["POST"])
@require("staff")
def capture_suggest():
    j = request.get_json(silent=True) or {}
    fn = {"slip": vd.read_slip, "meter": vd.read_meter, "id": vd.read_id, "doc": vd.read_doc,
          "plate": vd.read_plate}.get(j.get("kind"))
    if not fn:
        abort(400)
    return jsonify(fn(db(), (j.get("text") or "")[:6000], local_today()))


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
        if f.get("notice") != "1":
            flash("Confirm that the client has been given the privacy notice before their details are saved")
            return redirect(url_for("clients"))
        if idn:
            if idn.isdigit() and len(idn) == 13:
                ok, age, msg = vd.sa_id_info(idn, local_today())
                if not ok:
                    flash(msg); return redirect(url_for("clients"))
            old = _find_client_by_id(d, idn)
            if old:
                _attach_id_photo(d, old["id"], idn)
                d.commit()
                flash("A client with that ID number already exists - opened their record")
                return redirect(url_for("client_detail", cid=old["id"]))
        cur = d.execute("INSERT INTO clients(full_name,id_number,phone,notes,created_at,id_type,consent_at,consent_by) VALUES(?,?,?,?,?,?,?,?)",
                        (name, idn, f.get("phone", "").strip(), f.get("notes", "").strip(), now(),
                         "sa_id" if idn.isdigit() and len(idn) == 13 else ("other" if idn else None), now(), session["user"]))
        _attach_id_photo(d, cur.lastrowid, idn)
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
    log("client_view", "%s #%d" % ("client", cid)); d.commit()
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
        up = request.files.get("photo")
        has_photo = _ext_ok(up)
        ocr = (f.get("ocr_text") or "")[:4000]
        if has_photo:
            check, flags = vd.corroborate_slip(d, ocr, mid, amt_in, amt_out, ed.isoformat(), local_today())
        else:
            check, flags = None, []
        cur = d.execute("""INSERT INTO entries(machine_id,client_id,entry_date,amount_in,amount_out,notes,captured_by,created_at,
            source,photo_check,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, cl, ed.isoformat(), amt_in, amt_out, f.get("notes", "").strip(), session["user"], now(),
             "photo" if has_photo else "manual", check, "; ".join(flags)))
        eid = cur.lastrowid
        if has_photo:
            save_upload(up, "Payout slip", entry_id=eid, client_id=cl, machine_id=mid, ocr_text=ocr, ocr_flags="; ".join(flags))
        log("entry_add", f"#{eid} {check or 'manual'}"); d.commit()
        flash("Entry saved" if check in (None, "match", "unread") else
              "Entry saved, but the photo shows different figures (" + "; ".join(flags) + "). A supervisor can review it.")
        return redirect(url_for("entries"))
    rows = d.execute("""SELECT e.*, m.code, c.full_name,
        (SELECT COUNT(*) FROM documents WHERE entry_id=e.id) ndocs
        FROM entries e JOIN machines m ON m.id=e.machine_id
        LEFT JOIN clients c ON c.id=e.client_id ORDER BY entry_date DESC, e.id DESC LIMIT 200""").fetchall()
    return render_template("entries.html", rows=rows,
        machines=d.execute("SELECT * FROM machines WHERE status='active' ORDER BY code").fetchall(),
        clients=d.execute("SELECT id,full_name FROM clients ORDER BY full_name").fetchall(),
        today=local_today().isoformat())


def _ext_ok(f):
    if not f or not f.filename or "." not in f.filename:
        return False
    return f.filename.rsplit(".", 1)[-1].lower() in ALLOWED


def save_upload(f, doc_type, client_id=None, machine_id=None, entry_id=None, ocr_text="", ocr_flags=""):
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED:
        flash("File type not allowed"); return None
    name = f"{uuid.uuid4().hex}.{ext}"
    os.makedirs(UPLOADS, exist_ok=True)
    f.save(os.path.join(UPLOADS, name))
    db().execute("""INSERT INTO documents(client_id,machine_id,entry_id,doc_type,filename,original_name,uploaded_by,uploaded_at,
        ocr_text,ocr_flags) VALUES(?,?,?,?,?,?,?,?,?,?)""", (client_id, machine_id, entry_id, doc_type, name,
                                      secure_filename(f.filename), session.get("user"), now(), ocr_text, ocr_flags))
    return name


@app.route("/documents", methods=["GET", "POST"])
@require("staff")
def documents():
    d = db()
    if request.method == "POST":
        n, first = 0, None
        fm = request.form
        dtype = fm.get("doc_type", "Other")
        if dtype not in DOC_TYPES:
            dtype = "Other"
        cid = fm.get("client_id") or None
        mid = fm.get("machine_id") or None
        ocr = (fm.get("ocr_text") or "")[:4000]
        meter = dtype == "Machine meter photo"
        if meter:
            def num(k):
                raw = re.sub(r"[ ,]", "", fm.get(k, ""))
                try:
                    v = float(raw)
                except ValueError:
                    return None
                return v if v >= 0 else None
            m_in, m_out = num("meter_in"), num("meter_out")
            if not (mid and mid.isdigit() and d.execute("SELECT 1 FROM machines WHERE id=?", (mid,)).fetchone()):
                flash("Select the machine the meter photo belongs to")
                return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/") else url_for("documents"))
            if m_in is None and m_out is None:
                flash("Enter the meter reading (coin in and/or coin out) shown in the photo")
                return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/") else url_for("documents"))
        flags = []
        if ocr.strip():
            rd = vd.read_doc(d, ocr, local_today())
            if rd["doc_type"] and rd["doc_type"] != dtype:
                flags.append("Photo looks like: " + rd["doc_type"])
            if rd["machine_id"] and mid and str(rd["machine_id"]) != str(mid):
                flags.append("Photo reads machine " + rd["machine_code"])
            if rd["client"] and cid and str(rd["client"]["id"]) != str(cid):
                flags.append("Photo matches client " + rd["client"]["name"])
            if meter:
                mr = vd.read_meter(d, ocr, local_today())
                if mr["coin_in"] is not None and m_in is not None and abs(mr["coin_in"] - m_in) > 0.5:
                    flags.append("Photo reads coin in %s" % format(mr["coin_in"], ",.0f"))
                if mr["coin_out"] is not None and m_out is not None and abs(mr["coin_out"] - m_out) > 0.5:
                    flags.append("Photo reads coin out %s" % format(mr["coin_out"], ",.0f"))
        elif request.files.getlist("files"):
            flags.append("Photo attached, not machine-read")
        for f in request.files.getlist("files"):
            nm = save_upload(f, dtype, cid, mid, ocr_text=ocr if first is None else "",
                             ocr_flags="; ".join(flags) if first is None else "")
            if nm:
                n += 1
                first = first or nm
        if meter and first:
            did = d.execute("SELECT id FROM documents WHERE filename=?", (first,)).fetchone()["id"]
            d.execute("INSERT INTO meter_readings(machine_id,read_at,coin_in,coin_out,document_id,captured_by) VALUES(?,?,?,?,?,?)",
                      (mid, now(), m_in, m_out, did, session["user"]))
        log("doc_upload", f"{n} files {dtype}"); d.commit()
        flash(f"{n} file(s) uploaded" + (" - check: " + "; ".join(flags) if flags and n else ""))
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

# -------------------------------------------------------- historical import
def _imp_path(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        abort(404)
    return os.path.join(PENDING, "imp_" + token + ".json")


def _imp_load(token):
    try:
        with open(_imp_path(token)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        abort(404)


@app.route("/import", methods=["GET", "POST"])
@require("supervisor")
def import_history():
    if request.method == "POST":
        up = request.files.get("file")
        if not up or not up.filename:
            flash("Choose a file to import"); return redirect(url_for("import_history"))
        try:
            tables = imp.read_tables(up.filename, up.read())
        except Exception as ex:
            flash("Could not read that file: %s" % (ex if isinstance(ex, ValueError) else "it may be damaged or password protected"))
            return redirect(url_for("import_history"))
        out = []
        for sheet, rows in tables:
            if not rows:
                continue
            kind, mp = imp.classify(rows[0])
            out.append({"sheet": sheet, "kind": kind, "map": mp, "headers": rows[0], "rows": rows[1:]})
        if not out:
            flash("That file has no data"); return redirect(url_for("import_history"))
        token = uuid.uuid4().hex
        os.makedirs(PENDING, exist_ok=True)
        with open(_imp_path(token), "w") as fh:
            json.dump({"filename": secure_filename(up.filename) or "import", "tables": out}, fh)
        return redirect(url_for("import_review", token=token))
    recent = db().execute("SELECT at, user, detail FROM audit WHERE action='import' ORDER BY id DESC LIMIT 8").fetchall()
    return render_template("import.html", pend=None, recent=recent, kinds=imp.KINDS, help=imp.HELP)


@app.route("/import/template/<kind>.csv")
@require("supervisor")
def import_template(kind):
    if kind not in imp.TEMPLATES:
        abort(404)
    return Response(imp.TEMPLATES[kind], mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=gaming-smart-%s-template.csv" % kind})


@app.route("/import/<token>", methods=["GET", "POST"])
@require("supervisor")
def import_review(token):
    pend = _imp_load(token)
    d = db()
    today = local_today()
    create = request.method == "GET" or request.form.get("create") == "1"
    if request.method == "POST":
        done, order = [], {"machines": 0, "clients": 1, "entries": 2}
        for tb in sorted([x for x in pend["tables"] if x["kind"]], key=lambda x: order[x["kind"]]):
            pl = imp.plan(d, tb["kind"], tb["map"], tb["rows"], today, create, LARGE_MIN)
            r = imp.apply(d, pl, session["user"], now(), pend["filename"], create)
            done.append("%s: %d new%s" % (imp.KINDS[tb["kind"]], pl["new"], (", %d already in the app" % pl["dups"]) if pl["dups"] else ""))
            if pl["errors"]:
                done[-1] += ", %d rows skipped with problems" % len(pl["errors"])
        log("import", "%s | %s" % (pend["filename"], "; ".join(done)))
        d.commit()
        try:
            os.remove(_imp_path(token))
        except OSError:
            pass
        flash("Import finished. " + " | ".join(done) if done else "Nothing recognised to import")
        return redirect(url_for("import_history"))
    plans = []
    for tb in pend["tables"]:
        pl = imp.plan(d, tb["kind"], tb["map"], tb["rows"], today, create, LARGE_MIN) if tb["kind"] else None
        plans.append({"tb": tb, "pl": pl, "n": len(tb["rows"])})
    return render_template("import.html", pend=pend, plans=plans, token=token, kinds=imp.KINDS, help=imp.HELP, recent=[])


# ------------------------------------------- licence gate (client installs) and Gilded Age backend
def _lic_stats():
    d = db()
    m = local_today().isoformat()[:7]
    r = d.execute("SELECT COALESCE(SUM(amount_in),0) i, COALESCE(SUM(amount_out),0) o FROM entries WHERE entry_date LIKE ?", (m + "%",)).fetchone()
    return {"machines": d.execute("SELECT COUNT(*) FROM machines WHERE status='active'").fetchone()[0],
            "clients": d.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
            "users": d.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0],
            "month_in": r["i"], "month_out": r["o"],
            "big_n": d.execute("SELECT COUNT(*) FROM payouts WHERE status='valid' AND paid_at LIKE ?", (m + "%",)).fetchone()[0]}


def _lic_apply(cmds):
    done = []
    d = db()
    for c in cmds:
        if c.get("type") == "reset_password" and c.get("username") and c.get("pw_hash"):
            d.execute("UPDATE users SET pw_hash=? WHERE username=?", (c["pw_hash"], c["username"]))
            d.execute("INSERT INTO audit(at,user,action,detail) VALUES(?,?,?,?)", (now(), "backend", "password_reset", c["username"]))
            done.append(c.get("id"))
    d.commit()
    return done


MASTER_OK = ("static", "health", "login", "logout", "users", "user_toggle", "user_role", "user_password", "audit",
             "master", "privacy", "master_tenant", "master_toggle", "master_paid", "master_reset", "master_edit", "license_check", "license_ack")


@app.before_request
def gate():
    ep = request.endpoint or ""
    if MASTER:
        if ep not in MASTER_OK and not ep.startswith("master"):
            return redirect(url_for("master"))
        return None
    if licence.enabled() and ep not in ("static", "health"):
        st = licence.check(_lic_stats, _lic_apply, force=(ep == "login" and request.method == "POST"))
        if not st["active"]:
            session.clear() if ep != "login" else None
            return render_template("suspended.html", msg=st["message"]), 402
    return None


def _months(d0, n):
    y, m = d0.year + (d0.month - 1 + n) // 12, (d0.month - 1 + n) % 12 + 1
    day = min(d0.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return datetime.date(y, m, day)


def _tview(r, today):
    t = dict(r)
    try:
        t["stats"] = json.loads(t.get("last_stats") or "{}")
    except ValueError:
        t["stats"] = {}
    pu = t.get("paid_until")
    over = (today - datetime.date.fromisoformat(pu)).days if pu else 0
    t["overdue"] = max(0, over)
    t["overdue_block"] = bool(pu) and over > GRACE_DAYS
    t["live"] = t["status"] == "active" and not t["overdue_block"]
    t["stale"] = bool(t.get("last_seen")) and t["last_seen"][:10] < (today - datetime.timedelta(days=2)).isoformat()
    return t


def _tevent(tid, detail):
    db().execute("INSERT INTO tenant_events(tenant_id,at,user,detail) VALUES(?,?,?,?)", (tid, now(), session.get("user"), detail))


def _tclean(f):
    pu = f.get("paid_until", "").strip()
    if pu:
        try:
            datetime.date.fromisoformat(pu)
        except ValueError:
            return None, "Paid-until date is not valid"
    try:
        fee = float((f.get("monthly_fee") or "0").replace(",", "").replace(" ", ""))
        if fee < 0 or fee > 10_000_000:
            raise ValueError
    except ValueError:
        return None, "Fee must be a number"
    name = f.get("name", "").strip()
    if len(name) < 2:
        return None, "Client name is required"
    return {"name": name, "contact_name": f.get("contact_name", "").strip(), "contact_email": f.get("contact_email", "").strip(),
            "plan": f.get("plan", "").strip(), "monthly_fee": fee, "paid_until": pu or None,
            "instance_url": f.get("instance_url", "").strip(), "notes": f.get("notes", "").strip()}, None


@app.route("/master", methods=["GET", "POST"])
@require("admin")
def master():
    if not MASTER:
        abort(404)
    d = db()
    if request.method == "POST":
        c, err = _tclean(request.form)
        if err:
            flash(err); return redirect(url_for("master"))
        key = secrets.token_urlsafe(24)
        cur = d.execute("""INSERT INTO tenants(name,contact_name,contact_email,plan,monthly_fee,paid_until,instance_url,notes,license_key,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (c["name"], c["contact_name"], c["contact_email"], c["plan"], c["monthly_fee"], c["paid_until"],
                                             c["instance_url"], c["notes"], key, now()))
        _tevent(cur.lastrowid, "Client created"); log("tenant_add", c["name"]); d.commit()
        flash("Client created. Copy the licence key from its page into the client's settings.")
        return redirect(url_for("master_tenant", tid=cur.lastrowid))
    today = local_today()
    rows = [_tview(r, today) for r in d.execute("SELECT * FROM tenants ORDER BY name COLLATE NOCASE")]
    k = {"total": len(rows), "active": sum(1 for t in rows if t["live"]), "off": sum(1 for t in rows if not t["live"]),
         "overdue": sum(1 for t in rows if t["overdue"] > 0), "mrr": sum(t["monthly_fee"] or 0 for t in rows if t["live"])}
    return render_template("master.html", rows=rows, k=k)


@app.route("/master/<int:tid>")
@require("admin")
def master_tenant(tid):
    if not MASTER:
        abort(404)
    d = db()
    r = d.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
    if not r:
        abort(404)
    t = _tview(r, local_today())
    return render_template("master_tenant.html", t=t, st=t["stats"], master_url=request.host_url.rstrip("/"),
                           resets=d.execute("SELECT * FROM tenant_resets WHERE tenant_id=? ORDER BY id DESC LIMIT 6", (tid,)).fetchall(),
                           events=d.execute("SELECT * FROM tenant_events WHERE tenant_id=? ORDER BY id DESC LIMIT 30", (tid,)).fetchall())


def _tget(tid):
    if not MASTER:
        abort(404)
    r = db().execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
    if not r:
        abort(404)
    return r


@app.route("/master/<int:tid>/edit", methods=["POST"])
@require("admin")
def master_edit(tid):
    _tget(tid)
    c, err = _tclean(request.form)
    if err:
        flash(err); return redirect(url_for("master_tenant", tid=tid))
    db().execute("""UPDATE tenants SET name=?,contact_name=?,contact_email=?,plan=?,monthly_fee=?,paid_until=?,instance_url=?,notes=? WHERE id=?""",
                 (c["name"], c["contact_name"], c["contact_email"], c["plan"], c["monthly_fee"], c["paid_until"], c["instance_url"], c["notes"], tid))
    _tevent(tid, "Details updated"); db().commit()
    flash("Saved")
    return redirect(url_for("master_tenant", tid=tid))


@app.route("/master/<int:tid>/toggle", methods=["POST"])
@require("admin")
def master_toggle(tid):
    t = _tget(tid)
    to = "active" if request.form.get("to") == "active" else "suspended"
    reason = request.form.get("reason", "").strip()[:200] if to == "suspended" else ""
    db().execute("UPDATE tenants SET status=?, suspended_reason=? WHERE id=?", (to, reason, tid))
    _tevent(tid, "Switched ON" if to == "active" else "Switched OFF. " + reason)
    log("tenant_" + to, t["name"]); db().commit()
    flash("%s is now %s" % (t["name"], "on" if to == "active" else "off"))
    return redirect(request.referrer or url_for("master"))


@app.route("/master/<int:tid>/paid", methods=["POST"])
@require("admin")
def master_paid(tid):
    t = _tget(tid)
    try:
        n = max(1, min(24, int(request.form.get("months", "1"))))
    except ValueError:
        n = 1
    today = local_today()
    base = max(today, datetime.date.fromisoformat(t["paid_until"])) if t["paid_until"] else today
    new = _months(base, n).isoformat()
    db().execute("UPDATE tenants SET paid_until=?, status='active', suspended_reason='' WHERE id=?", (new, tid))
    _tevent(tid, "Payment recorded: %d month(s), paid until %s, switched ON" % (n, new)); log("tenant_paid", t["name"]); db().commit()
    flash("Paid until %s. %s is on." % (new, t["name"]))
    return redirect(url_for("master_tenant", tid=tid))


@app.route("/master/<int:tid>/reset", methods=["POST"])
@require("admin")
def master_reset(tid):
    t = _tget(tid)
    user = request.form.get("username", "").strip()[:60]
    if not user:
        flash("Enter the username to reset"); return redirect(url_for("master_tenant", tid=tid))
    temp = secrets.token_urlsafe(9)
    db().execute("INSERT INTO tenant_resets(tenant_id,username,pw_hash,requested_by,created_at) VALUES(?,?,?,?,?)",
                 (tid, user, generate_password_hash(temp), session["user"], now()))
    _tevent(tid, "Password reset requested for %s" % user); log("tenant_reset", "%s / %s" % (t["name"], user)); db().commit()
    flash("Temporary password for %s: %s. It is shown once. It takes effect when the client's app next checks in." % (user, temp))
    return redirect(url_for("master_tenant", tid=tid))


@app.route("/api/license/check", methods=["POST"])
def license_check():
    if not MASTER:
        abort(404)
    j = request.get_json(silent=True) or {}
    d = db()
    r = d.execute("SELECT * FROM tenants WHERE license_key=?", (str(j.get("key", "")),)).fetchone()
    if not r:
        return jsonify({"active": False, "message": "Licence key not recognised."}), 403
    t = _tview(r, local_today())
    st = j.get("stats") if isinstance(j.get("stats"), dict) else {}
    keep = {k: st[k] for k in ("machines", "clients", "users", "month_in", "month_out", "big_n") if isinstance(st.get(k), (int, float))}
    d.execute("UPDATE tenants SET last_seen=?, last_stats=?, version=? WHERE id=?", (now(), json.dumps(keep), str(j.get("version", ""))[:40], t["id"]))
    cmds = [{"id": c["id"], "type": "reset_password", "username": c["username"], "pw_hash": c["pw_hash"]}
            for c in d.execute("SELECT * FROM tenant_resets WHERE tenant_id=? AND status='pending' ORDER BY id", (t["id"],))]
    d.commit()
    msg = ""
    if not t["live"]:
        msg = t["suspended_reason"] if t["status"] != "active" and t["suspended_reason"] else "Account switched off for non-payment."
    return jsonify({"active": t["live"], "message": msg, "paid_until": t["paid_until"] or "", "commands": cmds})


@app.route("/api/license/ack", methods=["POST"])
def license_ack():
    if not MASTER:
        abort(404)
    j = request.get_json(silent=True) or {}
    d = db()
    r = d.execute("SELECT id, name FROM tenants WHERE license_key=?", (str(j.get("key", "")),)).fetchone()
    if not r:
        return jsonify({"ok": False}), 403
    for i in j.get("ids") or []:
        if isinstance(i, int):
            d.execute("UPDATE tenant_resets SET status='applied', applied_at=? WHERE id=? AND tenant_id=? AND status='pending'", (now(), i, r["id"]))
    d.execute("INSERT INTO tenant_events(tenant_id,at,user,detail) VALUES(?,?,?,?)", (r["id"], now(), "client app", "Password reset applied"))
    d.commit()
    return jsonify({"ok": True})


# ------------------------------------------------------------------- POPIA
def _anonymise(d, cid, who):
    c = d.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not c or c["anonymised_at"]:
        return False
    for doc in d.execute("SELECT id, filename FROM documents WHERE client_id=? AND doc_type IN ('ID document','Proof of address')", (cid,)).fetchall():
        try:
            os.remove(os.path.join(UPLOADS, doc["filename"]))
        except OSError:
            pass
        d.execute("DELETE FROM documents WHERE id=?", (doc["id"],))
    d.execute("UPDATE clients SET full_name=?, id_number=NULL, phone=NULL, notes=NULL, id_type=NULL, anonymised_at=? WHERE id=?",
              ("Anonymised client #%d" % cid, now(), cid))
    d.execute("INSERT INTO audit(at,user,action,detail) VALUES(?,?,?,?)", (now(), who, "client_anonymise", "client #%d" % cid))
    return True


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", ro=INFO_OFFICER, ro_email=INFO_OFFICER_EMAIL, party=RESPONSIBLE_PARTY, years=RETENTION_YEARS)


@app.route("/clients/<int:cid>/consent", methods=["POST"])
@require("staff")
def client_consent(cid):
    d = db()
    c = d.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone() or abort(404)
    if request.form.get("notice") == "1" and not c["consent_at"]:
        d.execute("UPDATE clients SET consent_at=?, consent_by=? WHERE id=?", (now(), session["user"], cid))
        log("client_notice", "client #%d" % cid); d.commit()
        flash("Recorded that the client has been given the privacy notice")
    return redirect(url_for("client_detail", cid=cid))


@app.route("/clients/<int:cid>/export")
@require("supervisor")
def client_export(cid):
    d = db()
    c = d.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone() or abort(404)
    data = {"exported_at": now(), "exported_by": session["user"], "client": dict(c),
            "documents": [dict(x) for x in d.execute("SELECT doc_type, original_name, uploaded_at FROM documents WHERE client_id=?", (cid,))],
            "entries": [dict(x) for x in d.execute("SELECT entry_date, machine_id, amount_in, amount_out, notes FROM entries WHERE client_id=? ORDER BY entry_date", (cid,))],
            "large_payouts": [dict(x) for x in d.execute("SELECT paid_at, machine_id, amount, status FROM payouts WHERE client_id=? ORDER BY paid_at", (cid,))]}
    log("client_export", "client #%d" % cid); d.commit()
    return Response(json.dumps(data, indent=2, default=str), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=client-%d-data.json" % cid})


@app.route("/clients/<int:cid>/anonymise", methods=["POST"])
@require("admin")
def client_anonymise(cid):
    d = db()
    if _anonymise(d, cid, session["user"]):
        d.commit(); flash("Personal details removed. Financial records are kept without a name.")
    return redirect(url_for("client_detail", cid=cid))


def _due(days=30):
    return (local_today() + datetime.timedelta(days=days)).isoformat()


@app.route("/compliance", methods=["GET", "POST"])
@require("supervisor")
def compliance():
    d = db()
    f = request.form
    if request.method == "POST":
        act = f.get("act")
        if act == "request" and f.get("kind") in ("access", "correction", "deletion", "objection") and f.get("subject", "").strip():
            d.execute("INSERT INTO popia_requests(kind,subject,received_at,due_at,notes,logged_by) VALUES(?,?,?,?,?,?)",
                      (f["kind"], f["subject"].strip()[:120], local_today().isoformat(), _due(30), f.get("notes", "").strip()[:500], session["user"]))
            log("popia_request", f["kind"]); flash("Request logged. It must be answered within 30 days.")
        elif act == "close" and f.get("id", "").isdigit():
            d.execute("UPDATE popia_requests SET status='closed', closed_at=?, closed_by=? WHERE id=?", (now(), session["user"], int(f["id"])))
            log("popia_request_close", f["id"])
        elif act == "incident" and f.get("description", "").strip():
            d.execute("""INSERT INTO popia_incidents(discovered_at,description,info_affected,people_affected,logged_by,created_at)
                VALUES(?,?,?,?,?,?)""", (f.get("discovered_at") or local_today().isoformat(), f["description"].strip()[:1000],
                                         f.get("info_affected", "").strip()[:300], f.get("people_affected", "").strip()[:100], session["user"], now()))
            log("popia_incident", "logged"); flash("Incident logged. If personal information was accessed or acquired without authority, notify the Information Regulator and the people affected as soon as reasonably possible.")
        elif act == "incident_update" and f.get("id", "").isdigit() and ROLES[g.role] >= ROLES["admin"]:
            d.execute("UPDATE popia_incidents SET regulator_notified=?, subjects_notified=?, status=? WHERE id=?",
                      (f.get("regulator_notified", "")[:40], f.get("subjects_notified", "")[:40], "closed" if f.get("close") else "open", int(f["id"])))
            log("popia_incident_update", f["id"])
        elif act == "retention" and ROLES[g.role] >= ROLES["admin"]:
            cut = (local_today() - datetime.timedelta(days=365 * RETENTION_YEARS)).isoformat()
            ids = [r["id"] for r in d.execute(_STALE_SQL, (cut, cut, cut))]
            n = sum(1 for i in ids if _anonymise(d, i, session["user"]))
            flash("%d inactive clients anonymised" % n)
        d.commit()
        return redirect(url_for("compliance"))
    cut = (local_today() - datetime.timedelta(days=365 * RETENTION_YEARS)).isoformat()
    tot = d.execute("SELECT COUNT(*) FROM clients WHERE anonymised_at IS NULL").fetchone()[0]
    noted = d.execute("SELECT COUNT(*) FROM clients WHERE anonymised_at IS NULL AND consent_at IS NOT NULL").fetchone()[0]
    reqs = d.execute("SELECT * FROM popia_requests ORDER BY status='closed', due_at LIMIT 30").fetchall()
    inc = d.execute("SELECT * FROM popia_incidents ORDER BY id DESC LIMIT 20").fetchall()
    stale = len(d.execute(_STALE_SQL, (cut, cut, cut)).fetchall())
    today = local_today().isoformat()
    st = {"clients": tot, "noted": noted, "pct": round(noted / tot * 100) if tot else 100, "stale": stale,
          "overdue": sum(1 for r in reqs if r["status"] == "open" and r["due_at"] < today),
          "open_inc": sum(1 for r in inc if r["status"] == "open"), "staff": d.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]}
    return render_template("compliance.html", st=st, reqs=reqs, inc=inc, today=today, ro=INFO_OFFICER, ro_email=INFO_OFFICER_EMAIL,
                           years=RETENTION_YEARS, party=RESPONSIBLE_PARTY, licence_on=licence.enabled())


_STALE_SQL = """SELECT c.id FROM clients c WHERE c.anonymised_at IS NULL AND substr(c.created_at,1,10) < ?
    AND NOT EXISTS (SELECT 1 FROM entries e WHERE e.client_id=c.id AND e.entry_date >= ?)
    AND NOT EXISTS (SELECT 1 FROM payouts p WHERE p.client_id=c.id AND substr(p.paid_at,1,10) >= ?)"""


# ------------------------------------------------------------------ BI board
@app.route("/bi")
@require("supervisor")
def bi_board():
    t = local_today()
    s, e = rp.parse_range({"start": request.args.get("start") or (t - datetime.timedelta(days=29)).isoformat(),
                           "end": request.args.get("end") or t.isoformat()})
    return render_template("bi.html", b=bi.build(db(), s, e, t), start=s, end=e)


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
        if not u or role not in ROLES or len(pw) < MIN_PW or pw.lower() == u.lower():
            flash("Username, a valid role and a password of at least %d characters (not the username) are required" % MIN_PW)
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
    if len(pw) < MIN_PW or pw.lower() == u["username"].lower():
        flash("Password must be at least %d characters and not the username" % MIN_PW)
    else:
        db().execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), uid))
        log("user_password", u["username"]); db().commit()
        flash(f"Password updated for {u['username']}")
    return redirect(url_for("users"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
