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
