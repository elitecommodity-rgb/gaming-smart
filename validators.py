"""Verification helpers for large payouts: SA ID checks and reading the payout form photo text."""
import re
import datetime


def clean_id(s):
    return re.sub(r"[\s\-]", "", s or "")


def luhn_ok(s):
    total = 0
    for i, ch in enumerate(reversed(s)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def sa_id_info(s, today):
    """Validate a South African ID number. Returns (ok, age, message)."""
    s = clean_id(s)
    if not (s.isdigit() and len(s) == 13):
        return False, None, "A South African ID number has 13 digits"
    yy, mm, dd = int(s[0:2]), int(s[2:4]), int(s[4:6])
    born = None
    for year in (2000 + yy, 1900 + yy):  # most recent year that is not in the future
        try:
            d = datetime.date(year, mm, dd)
        except ValueError:
            continue
        if d <= today:
            born = d
            break
    if born is None:
        return False, None, "The ID number has an invalid date of birth"
    if s[10] not in "01":
        return False, None, "The ID number is not valid - please re-check it"
    if not luhn_ok(s):
        return False, None, "The ID number check digit is wrong - please re-check it"
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return True, age, ""


_AMOUNT = re.compile(r"(?<![\d.,])(R\s*)?(\d{1,3}(?:[ ,]\d{3})+|\d{4,9})(?:[.,]\d{2})?(?![\d])", re.I)


def find_amount(text, lo, hi):
    """Best payout amount in the text: prefer values written with an R, otherwise the largest."""
    best_r, best_any = None, None
    for m in _AMOUNT.finditer(text or ""):
        try:
            v = float(re.sub(r"[ ,]", "", m.group(2)))
        except ValueError:
            continue
        if not (lo <= v <= hi):
            continue
        if m.group(1) and (best_r is None or v > best_r):
            best_r = v
        if best_any is None or v > best_any:
            best_any = v
    return best_r if best_r is not None else best_any


def match_text(d, text, lo, hi):
    """Suggest machine, customer and amount from text read off the payout form photo."""
    t = text or ""
    out = {"machine_id": None, "machine_code": None, "client": None,
           "id_number": None, "amount": find_amount(t, lo, hi)}
    best = None
    for m in d.execute("SELECT id, code FROM machines WHERE status='active'").fetchall():
        toks = re.findall(r"[A-Za-z]+|\d+", m["code"])
        if not toks:
            continue
        pat = r"(?<![A-Za-z0-9])" + r"[\s\-_.]*".join(re.escape(x) for x in toks) + r"(?![A-Za-z0-9])"
        if re.search(pat, t, re.I) and (best is None or len(m["code"]) > len(best["code"])):
            best = m
    if best:
        out["machine_id"], out["machine_code"] = best["id"], best["code"]

    today = datetime.date.today()
    for mo in re.finditer(r"(?:\d[ \-]?){13}", t):
        digits = re.sub(r"\D", "", mo.group())[:13]
        if sa_id_info(digits, today)[0]:
            out["id_number"] = digits
            break

    def pack(c):
        n = clean_id(c["id_number"])
        return {"id": c["id"], "name": c["full_name"], "has_id": bool(n), "id_last4": n[-4:] if n else ""}

    if out["id_number"]:
        c = d.execute("SELECT * FROM clients WHERE REPLACE(REPLACE(id_number,' ',''),'-','')=?",
                      (out["id_number"],)).fetchone()
        if c:
            out["client"] = pack(c)
    if out["client"] is None and t:
        low = t.lower()
        best_c, best_n = None, 0
        for c in d.execute("SELECT id, full_name, id_number FROM clients").fetchall():
            words = [w for w in re.findall(r"[a-z']+", c["full_name"].lower()) if len(w) >= 3]
            if len(words) >= 2 and all(w in low for w in words) and len(words) > best_n:
                best_c, best_n = c, len(words)
        if best_c:
            out["client"] = pack(best_c)
    return out


# ---------------------------------------------------------------- photo readers
_DATE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b|\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b")
_NUM = re.compile(r"(?<![\w.,])(?:R\s*)?(\d{1,3}(?:[ ,]\d{3})+|\d+)(?:[.,](\d{2}))?(?![\d])", re.I)
_OUT = re.compile(r"\b(out|paid|payout|pay-out|win|won|collect|collected|jackpot)\b", re.I)
_IN = re.compile(r"\b(in|stake|bet|insert|inserted|deposit)\b", re.I)


def find_date(text, today=None):
    """First plausible date (not in the future) as YYYY-MM-DD, or None."""
    today = today or datetime.date.today()
    for m in _DATE.finditer(text or ""):
        try:
            if m.group(1):
                d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                d = datetime.date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
        except ValueError:
            continue
        if d <= today + datetime.timedelta(days=1):
            return d.isoformat()
    return None


def labelled_amounts(text):
    """Amounts written next to 'in' / 'out' style labels, line by line. Returns {'in': x|None, 'out': y|None}."""
    res = {"in": None, "out": None}
    for line in (text or "").splitlines():
        low = line.lower()
        if "id" in low.split() or re.search(r"\d{10,}", line):
            continue
        key = "out" if _OUT.search(low) else ("in" if _IN.search(low) else None)
        if not key or res[key] is not None:
            continue
        nums = []
        for m in _NUM.finditer(line):
            try:
                v = float(re.sub(r"[ ,]", "", m.group(1)) + ("." + m.group(2) if m.group(2) else ""))
            except ValueError:
                continue
            nums.append(v)
        if nums:
            res[key] = nums[-1]
    return res


def find_machine(d, text, active_only=True):
    best = None
    q = "SELECT id, code FROM machines" + (" WHERE status='active'" if active_only else "")
    for m in d.execute(q).fetchall():
        toks = re.findall(r"[A-Za-z]+|\d+", m["code"])
        if not toks:
            continue
        pat = r"(?<![A-Za-z0-9])" + r"[\s\-_.]*".join(re.escape(x) for x in toks) + r"(?![A-Za-z0-9])"
        if re.search(pat, text or "", re.I) and (best is None or len(m["code"]) > len(best["code"])):
            best = m
    return best


def read_slip(d, text, today=None):
    m = find_machine(d, text)
    a = labelled_amounts(text)
    out = {"machine_id": m["id"] if m else None, "machine_code": m["code"] if m else None,
           "amount_in": a["in"], "amount_out": a["out"], "date": find_date(text, today)}
    out["read"] = any(out[k] for k in ("machine_id", "amount_in", "amount_out", "date"))
    return out


def read_meter(d, text, today=None):
    m = find_machine(d, text)
    low = (text or "").lower()
    ci = co = None
    for line in (text or "").splitlines():
        l = line.lower()
        nums = [float(re.sub(r"[ ,]", "", x.group(1))) for x in _NUM.finditer(line)
                if not re.search(r"\d{10,}", x.group(1))]
        if not nums:
            continue
        if ci is None and re.search(r"\b(coin|cash|total|credits?)\s*[- ]?\s*in\b|\bturnover\b|\bbets?\b", l):
            ci = nums[-1]
        elif co is None and re.search(r"\b(coin|cash|total|credits?)\s*[- ]?\s*out\b|\bpaid\b|\bwins?\b", l):
            co = nums[-1]
    return {"machine_id": m["id"] if m else None, "machine_code": m["code"] if m else None,
            "coin_in": ci, "coin_out": co, "date": find_date(text, today),
            "read": bool(m or ci or co)}


_NOT_NAME = re.compile(r"republic|identity|south|africa|document|card|passport|driver|licen[cs]e|"
                       r"surname|names?|date|birth|nationality|sex|status|citizen|country|"
                       r"id\b|number|signature|department|affairs|home", re.I)


def read_id(d, text, today=None):
    today = today or datetime.date.today()
    out = {"id_number": None, "full_name": None, "age": None, "client": None, "read": False}
    for mo in re.finditer(r"(?:\d[ \-]?){13}", text or ""):
        digits = re.sub(r"\D", "", mo.group())[:13]
        ok, age, _ = sa_id_info(digits, today)
        if ok:
            out["id_number"], out["age"] = digits, age
            break
    surname = given = None
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for i, l in enumerate(lines):
        low = l.lower()
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if re.match(r"^(surname|last name)\b", low):
            v = re.sub(r"^(surname|last name)[:\s]*", "", l, flags=re.I).strip() or nxt
            surname = surname or v
        elif re.match(r"^(names?|first names?|given names?|forenames?)\b", low):
            v = re.sub(r"^(first names?|given names?|forenames?|names?)[:\s]*", "", l, flags=re.I).strip() or nxt
            given = given or v
    name = " ".join(x for x in (given, surname) if x)
    if not name:
        for l in lines:
            words = re.findall(r"[A-Za-z][A-Za-z'\-]+", l)
            if 2 <= len(words) <= 4 and not _NOT_NAME.search(l) and not re.search(r"\d", l):
                name = " ".join(w.capitalize() if w.isupper() else w for w in words)
                break
    name = re.sub(r"[^A-Za-z' \-]", "", name or "").strip()
    out["full_name"] = name or None
    if out["id_number"]:
        c = d.execute("SELECT * FROM clients WHERE REPLACE(REPLACE(id_number,' ',''),'-','')=?",
                      (out["id_number"],)).fetchone()
        if c:
            out["client"] = {"id": c["id"], "name": c["full_name"]}
    out["read"] = bool(out["id_number"] or out["full_name"])
    return out


_DOCTYPES = [
    ("Large payout form", r"manual payout|payout form|large payout"),
    ("ID document", r"identity|id number|republic of south africa|passport|smart id|green id"),
    ("Proof of address", r"municipal|electricity|water account|rates|proof of (residence|address)|utility|telkom|vodacom|statement of account"),
    ("Machine meter photo", r"meter|coin[- ]?in|coin[- ]?out|credits|turnover"),
    ("Cash-up sheet", r"cash[- ]?up|float|reconciliation|till"),
    ("Incident report", r"incident|complaint|dispute|security"),
    ("Payout slip", r"payout|paid out|jackpot|ticket|voucher|slip"),
]


def read_doc(d, text, today=None):
    low = text or ""
    dtype = None
    for name, pat in _DOCTYPES:
        if re.search(pat, low, re.I):
            dtype = name
            break
    m = find_machine(d, text, active_only=False)
    ident = read_id(d, text, today)
    return {"doc_type": dtype, "machine_id": m["id"] if m else None, "machine_code": m["code"] if m else None,
            "client": ident["client"], "id_number": ident["id_number"],
            "read": bool(dtype or m or ident["client"] or ident["id_number"])}


def corroborate_slip(d, text, machine_id, amt_in, amt_out, entry_date, today=None):
    """Compare what staff entered with what the slip photo says. Returns (status, [flags])."""
    if not (text or "").strip():
        return "unread", ["Photo attached, not machine-read"]
    r = read_slip(d, text, today)
    if not r["read"]:
        return "unread", ["Photo attached, not machine-read"]
    bad, flags = False, []
    if r["machine_id"] and str(r["machine_id"]) != str(machine_id):
        bad = True; flags.append("Photo reads machine " + r["machine_code"])
    if r["amount_in"] is not None and abs(r["amount_in"] - amt_in) > 0.5:
        bad = True; flags.append("Photo reads in R %s" % format(r["amount_in"], ",.2f"))
    if r["amount_out"] is not None and abs(r["amount_out"] - amt_out) > 0.5:
        bad = True; flags.append("Photo reads out R %s" % format(r["amount_out"], ",.2f"))
    if r["date"] and r["date"] != entry_date:
        bad = True; flags.append("Photo reads date " + r["date"])
    return ("differs" if bad else "match"), flags


def read_plate(d, text, today=None):
    """Serial number from a machine's plate photo, and the machine code if it is printed on it."""
    serial = None
    for line in (text or "").splitlines():
        m = re.search(r"(?:serial(?:\s*(?:no|number|num|#))?|s/?n)\b[\s:.#\-]*([A-Z0-9][A-Z0-9\-/]{4,})", line, re.I)
        if m:
            serial = m.group(1).upper()
            break
    mach = find_machine(d, text, active_only=False)
    return {"serial": serial, "machine_id": mach["id"] if mach else None,
            "machine_code": mach["code"] if mach else None, "read": bool(serial or mach)}
