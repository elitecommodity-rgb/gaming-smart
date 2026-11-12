"""Historical data import: read CSV / Excel / text PDF, recognise which section each table belongs to,
check every row, and report what would be created before anything is saved."""
import csv, datetime, io, re
from collections import Counter

import validators as vd

SYN = {
    "date": {"date", "entry date", "day", "play date", "transaction date", "paid date", "date paid", "trading date"},
    "machine": {"machine", "machine code", "machine no", "machine number", "machine id", "slot", "slot machine", "code", "terminal"},
    "amount_in": {"in", "amount in", "coin in", "total in", "turnover", "stake", "stakes", "bets", "wagered", "money in"},
    "amount_out": {"out", "amount out", "coin out", "total out", "payout", "payouts", "pay out", "win", "wins", "paid", "paid out", "money out"},
    "client": {"client", "client name", "customer", "customer name", "player", "player name", "paid to"},
    "name": {"name", "full name", "client", "client name", "customer", "customer name", "player", "player name"},
    "id_number": {"id", "id number", "id no", "id num", "identity number", "passport", "passport number", "id passport"},
    "phone": {"phone", "cell", "cellphone", "mobile", "tel", "telephone", "contact", "contact number", "phone number"},
    "location": {"location", "site", "area", "floor", "position"},
    "model": {"model", "game", "manufacturer", "type"},
    "notes": {"notes", "note", "comment", "comments", "remarks", "description"},
}
KINDS = {"entries": "Wins / Losses", "clients": "Clients", "machines": "Machines"}
HELP = {
    "entries": "Date, Machine, Amount in, Amount out (optional: Client, Notes)",
    "clients": "Name, ID number (optional: Phone, Notes)",
    "machines": "Machine code (optional: Location, Model)",
}
MAX_ROWS = 20000
MAX_AMOUNT = 10_000_000


def norm(h):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(h or "").lower())).strip()


# --------------------------------------------------------------------------- reading
def _csv(data):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]


def _xlsx(data):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            cells = ["" if c is None else (c.date().isoformat() if isinstance(c, datetime.datetime) else
                                           c.isoformat() if isinstance(c, datetime.date) else str(c).strip()) for c in r]
            if any(cells):
                rows.append(cells)
            if len(rows) > MAX_ROWS:
                break
        out.append((ws.title, rows))
    return out


SPLITTERS = [lambda s: [c.strip() for c in re.split(r"\t|\s{2,}|\||;", s.strip()) if c.strip()],
             lambda s: [c.strip() for c in s.split(",")],
             lambda s: s.split()]


def _pdf(data):
    from pypdf import PdfReader
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        raise ValueError("No text found in this PDF. It looks like a scan or photo: use the photo capture on the Documents page instead.")
    for sp in SPLITTERS:
        for i, l in enumerate(lines[:40]):
            cells = sp(l)
            if len(cells) >= 3 and classify(cells)[0]:
                n = len(cells)
                rows = [cells] + [c for c in (sp(x) for x in lines[i + 1:]) if len(c) == n]
                return [("PDF", rows)]
    raise ValueError("Could not find a table with recognisable column headings in this PDF.")


def read_tables(filename, data):
    """Returns [(sheet_name, rows)] where rows[0] is the header."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("csv", "txt", "tsv"):
        return [("File", _csv(data))]
    if ext in ("xlsx", "xlsm"):
        return _xlsx(data)
    if ext == "pdf":
        return _pdf(data)
    raise ValueError("Use a .csv, .xlsx or text .pdf file.")


# ------------------------------------------------------------------- recognising
def colmap(headers, want):
    """Map wanted fields to column indexes, first match wins and no column is used twice."""
    hs = [norm(h) for h in headers]
    used, out = set(), {}
    for f in want:
        for i, h in enumerate(hs):
            if i not in used and h in SYN[f]:
                out[f] = i; used.add(i); break
    return out


def classify(headers):
    """(kind, mapping) for a header row, or (None, {})."""
    e = colmap(headers, ["date", "machine", "amount_in", "amount_out", "client", "notes"])
    if "date" in e and "machine" in e and ("amount_in" in e or "amount_out" in e):
        return "entries", e
    c = colmap(headers, ["name", "id_number", "phone", "notes"])
    if "name" in c and ("id_number" in c or "phone" in c) and "date" not in e:
        return "clients", c
    m = colmap(headers, ["machine", "location", "model"])
    if "machine" in m and ("location" in m or "model" in m) and "date" not in e and "amount_in" not in e:
        return "machines", m
    return None, {}


# ----------------------------------------------------------------------- values
def parse_date(s, today):
    s = str(s or "").strip()
    if not s:
        return None, "date missing"
    s = s.split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y"):
        try:
            d = datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if d > today:
            return None, "date is in the future"
        if d.year < 2000:
            return None, "date before 2000"
        return d, None
    return None, "date not understood: %s" % s[:20]


def parse_amount(s):
    s = str(s or "").strip()
    if s in ("", "-", "–"):
        return 0.0, None
    t = re.sub(r"(?i)\bzar\b|r(?=\s*\d)", "", s).replace("R", "").replace(" ", "").replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+,\d{1,2}", t):
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d+,\d{1,2}", t):
        t = t.replace(",", ".")
    else:
        t = t.replace(",", "")
    try:
        v = round(float(t), 2)
    except ValueError:
        return None, "amount not understood: %s" % s[:15]
    if v < 0:
        return None, "negative amount"
    if v > MAX_AMOUNT:
        return None, "amount over R %s" % format(MAX_AMOUNT, ",")
    return v, None


def _cell(row, idx):
    return row[idx].strip() if idx is not None and idx < len(row) else ""


# ------------------------------------------------------------------------- plan
def plan(d, kind, mapping, rows, today, create_missing=True, large_min=5000.0):
    """Check every data row against the database. Nothing is written. Returns dict with items and counts."""
    items, errors = [], []
    seen_new_machines, seen_new_clients = set(), set()
    if kind == "entries":
        machines = {r["code"].upper(): r["id"] for r in d.execute("SELECT id, code FROM machines")}
        clients = {}
        for r in d.execute("SELECT id, full_name FROM clients"):
            clients.setdefault(r["full_name"].strip().lower(), r["id"])
        have = Counter()
        for r in d.execute("SELECT machine_id, client_id, entry_date, amount_in, amount_out FROM entries"):
            have[(r["machine_id"], r["client_id"], r["entry_date"], round(r["amount_in"], 2), round(r["amount_out"], 2))] += 1
    for n, row in enumerate(rows, start=2):
        if len(items) + len(errors) >= MAX_ROWS:
            errors.append((n, "Stopped at %d rows. Split the file and import in parts." % MAX_ROWS)); break
        g = lambda f: _cell(row, mapping.get(f))
        if kind == "entries":
            ed, err = parse_date(g("date"), today)
            ai, e2 = parse_amount(g("amount_in")); ao, e3 = parse_amount(g("amount_out"))
            code = re.sub(r"\s+", "", g("machine")).upper()
            bad = err or e2 or e3 or (None if code else "machine missing") or (None if (ai or ao) else "no amounts")
            if bad:
                errors.append((n, bad)); continue
            mid = machines.get(code)
            if mid is None and not create_missing:
                errors.append((n, "machine %s not in the app" % code)); continue
            cname = g("client")
            cid = clients.get(cname.lower()) if cname else None
            key = (mid, cid, ed.isoformat(), ai, ao)
            dup = bool(mid and have[key] > 0)
            if dup:
                have[key] -= 1
            if mid is None and code not in seen_new_machines:
                seen_new_machines.add(code)
            if cname and cid is None and create_missing:
                seen_new_clients.add(cname.lower())
            items.append({"row": n, "dup": dup, "machine": code, "machine_id": mid, "client": cname, "client_id": cid, "date": ed.isoformat(),
                          "in": ai, "out": ao, "notes": g("notes"), "large": ao >= large_min})
        elif kind == "clients":
            name = re.sub(r"\s+", " ", g("name"))
            idn = vd.clean_id(g("id_number"))
            if len(name) < 2:
                errors.append((n, "name missing")); continue
            if idn and not re.fullmatch(r"[A-Za-z0-9]{5,20}", idn):
                errors.append((n, "ID number looks wrong: %s" % idn[:15])); continue
            phone = g("phone")
            if idn:
                dup = d.execute("SELECT 1 FROM clients WHERE id_number=?", (idn,)).fetchone() is not None
            else:
                dup = d.execute("SELECT 1 FROM clients WHERE lower(full_name)=? AND COALESCE(phone,'')=?", (name.lower(), phone)).fetchone() is not None
            key = idn or (name.lower(), phone)
            if not dup and key in seen_new_clients:
                dup = True
            seen_new_clients.add(key)
            items.append({"row": n, "dup": dup, "name": name, "id_number": idn, "phone": phone, "notes": g("notes")})
        else:
            code = re.sub(r"\s+", "", g("machine")).upper()
            if not code:
                errors.append((n, "machine code missing")); continue
            dup = d.execute("SELECT 1 FROM machines WHERE upper(code)=?", (code,)).fetchone() is not None or code in seen_new_machines
            seen_new_machines.add(code)
            items.append({"row": n, "dup": dup, "code": code, "location": g("location"), "model": g("model")})
    new = [i for i in items if not i["dup"]]
    return {"kind": kind, "items": items, "errors": errors, "new": len(new), "dups": len(items) - len(new),
            "new_machines": sorted(seen_new_machines) if kind == "entries" else [],
            "new_clients": len(seen_new_clients) if kind == "entries" else 0,
            "large": sum(1 for i in new if i.get("large")) if kind == "entries" else 0}


def apply(d, pl, user, now, filename, create_missing=True):
    """Write the new rows. Caller commits. Returns counts."""
    made = {"machines": 0, "clients": 0, "entries": 0, "skipped": 0}
    kind = pl["kind"]
    mcache = {r["code"].upper(): r["id"] for r in d.execute("SELECT id, code FROM machines")}
    ccache = {}
    for r in d.execute("SELECT id, full_name FROM clients"):
        ccache.setdefault(r["full_name"].strip().lower(), r["id"])
    for it in pl["items"]:
        if it["dup"]:
            made["skipped"] += 1; continue
        if kind == "machines":
            d.execute("INSERT INTO machines(code,location,model) VALUES(?,?,?)", (it["code"], it["location"], it["model"]))
            made["machines"] += 1
        elif kind == "clients":
            d.execute("INSERT INTO clients(full_name,id_number,phone,notes,created_at,id_type) VALUES(?,?,?,?,?,?)",
                      (it["name"], it["id_number"], it["phone"], it["notes"] or ("Imported: " + filename), now,
                       "sa_id" if it["id_number"].isdigit() and len(it["id_number"]) == 13 else ("other" if it["id_number"] else None)))
            made["clients"] += 1
        else:
            mid = mcache.get(it["machine"])
            if mid is None:
                mid = d.execute("INSERT INTO machines(code,location,model) VALUES(?,?,?)", (it["machine"], "", "")).lastrowid
                mcache[it["machine"]] = mid; made["machines"] += 1
            cid = it["client_id"] or (ccache.get(it["client"].lower()) if it["client"] else None)
            if it["client"] and cid is None and create_missing:
                cid = d.execute("INSERT INTO clients(full_name,notes,created_at) VALUES(?,?,?)", (it["client"], "Imported: " + filename, now)).lastrowid
                ccache[it["client"].lower()] = cid; made["clients"] += 1
            note = "Imported history" + (" (large payout, no payout form)" if it["large"] else "")
            if it["notes"]:
                note += ": " + it["notes"]
            d.execute("""INSERT INTO entries(machine_id,client_id,entry_date,amount_in,amount_out,notes,captured_by,created_at,source,photo_check,flags)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (mid, cid, it["date"], it["in"], it["out"], note, user, now, "import", None, ""))
            made["entries"] += 1
    return made


TEMPLATES = {
    "entries": "Date,Machine,Amount in,Amount out,Client,Notes\n2026-01-05,SM-001,1500.00,1200.00,,\n2026-01-05,SM-002,980.50,0,Thandi Mokoena,\n",
    "clients": "Name,ID number,Phone,Notes\nThandi Mokoena,8001015009087,0821234567,\n",
    "machines": "Machine code,Location,Model\nSM-001,Main floor,Model X\n",
}
