import os, sqlite3, uuid, datetime
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, send_from_directory, flash, abort)
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
UPLOADS = os.path.join(DATA, "uploads")
DB = os.path.join(DATA, "slotadmin.db")
ALLOWED = {"jpg", "jpeg", "png", "webp", "heic", "pdf"}
DOC_TYPES = ["ID document", "Proof of address", "Payout slip", "Machine meter photo",
             "Cash-up sheet", "Incident report", "Other"]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # CHANGE via env var

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, location TEXT, model TEXT,
  status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS clients(
  id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, id_number TEXT, phone TEXT,
  notes TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, machine_id INTEGER NOT NULL, client_id INTEGER,
  entry_date TEXT NOT NULL, amount_in REAL DEFAULT 0, amount_out REAL DEFAULT 0,
  notes TEXT, captured_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, client_id INTEGER, machine_id INTEGER, entry_id INTEGER,
  doc_type TEXT, filename TEXT NOT NULL, original_name TEXT, uploaded_by TEXT,
  uploaded_at TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, at TEXT, user TEXT, action TEXT, detail TEXT);
"""

def db():
    if "db" not in g:
        os.makedirs(UPLOADS, exist_ok=True)
        g.db = sqlite3.connect(DB, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.executescript(SCHEMA)
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def now(): return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

def log(action, detail=""):
    db().execute("INSERT INTO audit(at,user,action,detail) VALUES(?,?,?,?)",
                 (now(), session.get("user"), action, detail))

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

@app.route("/health")
def health():
    return "ok", 200

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if (request.form["username"] == ADMIN_USER and
                request.form["password"] == ADMIN_PASSWORD):
            session["user"] = ADMIN_USER
            return redirect(url_for("dashboard"))
        flash("Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    d = db()
    today = datetime.date.today().isoformat()
    month = today[:7]
    def tot(where, args=()):
        r = d.execute(f"SELECT COALESCE(SUM(amount_in),0) i, COALESCE(SUM(amount_out),0) o "
                      f"FROM entries WHERE {where}", args).fetchone()
        return {"in": r["i"], "out": r["o"], "net": r["i"] - r["o"]}
    stats = {
        "today": tot("entry_date=?", (today,)),
        "month": tot("entry_date LIKE ?", (month + "%",)),
        "machines": d.execute("SELECT COUNT(*) FROM machines WHERE status='active'").fetchone()[0],
        "clients": d.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
        "docs": d.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
    }
    per_machine = d.execute("""
        SELECT m.code, m.location, COALESCE(SUM(e.amount_in),0) i,
               COALESCE(SUM(e.amount_out),0) o
        FROM machines m LEFT JOIN entries e ON e.machine_id=m.id AND e.entry_date LIKE ?
        GROUP BY m.id ORDER BY (COALESCE(SUM(e.amount_in),0)-COALESCE(SUM(e.amount_out),0)) DESC
    """, (month + "%",)).fetchall()
    recent = d.execute("""SELECT d.*, c.full_name FROM documents d
        LEFT JOIN clients c ON c.id=d.client_id ORDER BY d.id DESC LIMIT 6""").fetchall()
    return render_template("dashboard.html", s=stats, pm=per_machine, recent=recent)

@app.route("/machines", methods=["GET", "POST"])
@login_required
def machines():
    d = db()
    if request.method == "POST":
        try:
            d.execute("INSERT INTO machines(code,location,model) VALUES(?,?,?)",
                      (request.form["code"].strip(), request.form["location"],
                       request.form["model"]))
            log("machine_add", request.form["code"]); d.commit()
        except sqlite3.IntegrityError:
            flash("Machine code already exists")
        return redirect(url_for("machines"))
    return render_template("machines.html", rows=d.execute("SELECT * FROM machines ORDER BY code").fetchall())

@app.route("/clients", methods=["GET", "POST"])
@login_required
def clients():
    d = db()
    if request.method == "POST":
        cur = d.execute("INSERT INTO clients(full_name,id_number,phone,notes,created_at) VALUES(?,?,?,?,?)",
                        (request.form["full_name"], request.form["id_number"],
                         request.form["phone"], request.form["notes"], now()))
        log("client_add", request.form["full_name"]); d.commit()
        return redirect(url_for("client_detail", cid=cur.lastrowid))
    q = request.args.get("q", "")
    rows = d.execute("SELECT * FROM clients WHERE full_name LIKE ? OR id_number LIKE ? OR phone LIKE ? ORDER BY full_name",
                     (f"%{q}%",) * 3).fetchall()
    return render_template("clients.html", rows=rows, q=q)

@app.route("/clients/<int:cid>")
@login_required
def client_detail(cid):
    d = db()
    c = d.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone() or abort(404)
    docs = d.execute("SELECT * FROM documents WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()
    ents = d.execute("""SELECT e.*, m.code FROM entries e JOIN machines m ON m.id=e.machine_id
                        WHERE client_id=? ORDER BY entry_date DESC, e.id DESC""", (cid,)).fetchall()
    return render_template("client_detail.html", c=c, docs=docs, ents=ents, types=DOC_TYPES)

@app.route("/entries", methods=["GET", "POST"])
@login_required
def entries():
    d = db()
    if request.method == "POST":
        cur = d.execute("""INSERT INTO entries(machine_id,client_id,entry_date,amount_in,amount_out,notes,captured_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (request.form["machine_id"], request.form.get("client_id") or None,
             request.form["entry_date"], float(request.form.get("amount_in") or 0),
             float(request.form.get("amount_out") or 0), request.form["notes"],
             session["user"], now()))
        eid = cur.lastrowid
        save_upload(request.files.get("photo"), "Payout slip", entry_id=eid,
                    client_id=request.form.get("client_id") or None,
                    machine_id=request.form["machine_id"])
        log("entry_add", f"#{eid}"); d.commit()
        return redirect(url_for("entries"))
    rows = d.execute("""SELECT e.*, m.code, c.full_name,
        (SELECT COUNT(*) FROM documents WHERE entry_id=e.id) ndocs
        FROM entries e JOIN machines m ON m.id=e.machine_id
        LEFT JOIN clients c ON c.id=e.client_id ORDER BY entry_date DESC, e.id DESC LIMIT 200""").fetchall()
    return render_template("entries.html", rows=rows,
        machines=d.execute("SELECT * FROM machines WHERE status='active' ORDER BY code").fetchall(),
        clients=d.execute("SELECT id,full_name FROM clients ORDER BY full_name").fetchall(),
        today=datetime.date.today().isoformat())

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
@login_required
def documents():
    d = db()
    if request.method == "POST":
        n = 0
        for f in request.files.getlist("files"):
            if save_upload(f, request.form["doc_type"], request.form.get("client_id") or None,
                           request.form.get("machine_id") or None): n += 1
        log("doc_upload", f"{n} files"); d.commit()
        flash(f"{n} file(s) uploaded")
        return redirect(request.form.get("next") or url_for("documents"))
    rows = d.execute("""SELECT d.*, c.full_name, m.code FROM documents d
        LEFT JOIN clients c ON c.id=d.client_id LEFT JOIN machines m ON m.id=d.machine_id
        ORDER BY d.id DESC LIMIT 200""").fetchall()
    return render_template("documents.html", rows=rows, types=DOC_TYPES,
        machines=d.execute("SELECT * FROM machines ORDER BY code").fetchall(),
        clients=d.execute("SELECT id,full_name FROM clients ORDER BY full_name").fetchall())

@app.route("/files/<name>")
@login_required
def files(name):
    return send_from_directory(UPLOADS, name)

@app.route("/audit")
@login_required
def audit():
    return render_template("audit.html", rows=db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 300").fetchall())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
