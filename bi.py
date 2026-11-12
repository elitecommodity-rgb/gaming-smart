"""Business-intelligence numbers for the BI board: turnover, hold, payout mix, client concentration and
how much of the data is backed by a photo that agrees with what was entered."""
import datetime

QUALITY = [("match", "Photo agrees with entry"), ("differs", "Photo differs from entry"),
           ("unread", "Photo attached, not readable"), ("manual", "No photo")]
BUCKETS = [("Under R500", 0, 500), ("R500-1k", 500, 1000), ("R1k-2.5k", 1000, 2500), ("R2.5k-5k", 2500, 5000),
           ("R5k-10k", 5000, 10000), ("R10k-25k", 10000, 25000), ("R25k+", 25000, 1e12)]


def pct(a, b):
    return round(a / b * 100, 1) if b else 0.0


def delta(cur, prev):
    return None if not prev else round((cur - prev) / abs(prev) * 100, 1)


def _totals(d, s, e):
    r = d.execute("SELECT COALESCE(SUM(amount_in),0) i, COALESCE(SUM(amount_out),0) o, COUNT(*) n "
                  "FROM entries WHERE entry_date BETWEEN ? AND ?", (s, e)).fetchone()
    return r["i"], r["o"], r["n"]


def build(d, s, e, today):
    ds, de = datetime.date.fromisoformat(s), datetime.date.fromisoformat(e)
    days = (de - ds).days + 1
    ps, pe = (ds - datetime.timedelta(days=days)).isoformat(), (ds - datetime.timedelta(days=1)).isoformat()
    tin, tout, n = _totals(d, s, e)
    pin, pout, _ = _totals(d, ps, pe)
    net, pnet = tin - tout, pin - pout
    big = d.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) v, COALESCE(AVG(amount),0) a FROM payouts "
                    "WHERE status='valid' AND substr(paid_at,1,10) BETWEEN ? AND ?", (s, e)).fetchone()

    # ---- capture quality (every entry classified by how well a photo backs it)
    qrows = d.execute("""SELECT CASE
          WHEN e.photo_check IS NOT NULL THEN e.photo_check
          WHEN p.id IS NOT NULL THEN CASE WHEN COALESCE(p.flags,'')='' THEN 'match'
                WHEN p.flags LIKE 'Entered manually%' THEN 'unread' ELSE 'differs' END
          WHEN EXISTS(SELECT 1 FROM documents x WHERE x.entry_id=e.id) THEN 'unread'
          ELSE 'manual' END k, COUNT(*) n, COALESCE(SUM(e.amount_in+e.amount_out),0) v
        FROM entries e LEFT JOIN payouts p ON p.entry_id=e.id
        WHERE e.entry_date BETWEEN ? AND ? GROUP BY k""", (s, e)).fetchall()
    qd = {r["k"]: (r["n"], r["v"]) for r in qrows}
    qval = sum(v for _, v in qd.values())
    quality = [{"key": k, "label": lab, "n": qd.get(k, (0, 0))[0], "pct": pct(qd.get(k, (0, 0))[0], n),
                "value": qd.get(k, (0, 0))[1], "vpct": pct(qd.get(k, (0, 0))[1], qval)} for k, lab in QUALITY]
    backed = sum(q["n"] for q in quality if q["key"] != "manual")
    agree = qd.get("match", (0, 0))[0]

    # ---- trend
    if days <= 62:
        grp, fmt = "entry_date", None
    else:
        grp = "strftime('%Y-W%W', entry_date)"
    tr = d.execute(f"""SELECT {grp} k, SUM(amount_in) i, SUM(amount_out) o FROM entries
        WHERE entry_date BETWEEN ? AND ? GROUP BY k ORDER BY k""", (s, e)).fetchall()
    trend = {"labels": [r["k"][5:] if days <= 62 else r["k"] for r in tr],
             "in": [round(r["i"], 2) for r in tr], "out": [round(r["o"], 2) for r in tr],
             "net": [round(r["i"] - r["o"], 2) for r in tr],
             "hold": [pct(r["i"] - r["o"], r["i"]) for r in tr]}

    # ---- machines
    mrows = d.execute("""SELECT m.id, m.code, COALESCE(m.location,'') loc,
          COALESCE(SUM(x.amount_in),0) i, COALESCE(SUM(x.amount_out),0) o, COUNT(x.id) n,
          SUM(CASE WHEN x.id IS NOT NULL AND (x.photo_check IS NOT NULL OR EXISTS(SELECT 1 FROM documents dd WHERE dd.entry_id=x.id)) THEN 1 ELSE 0 END) ph
        FROM machines m LEFT JOIN entries x ON x.machine_id=m.id AND x.entry_date BETWEEN ? AND ?
        WHERE m.status='active' OR x.id IS NOT NULL GROUP BY m.id ORDER BY (COALESCE(SUM(x.amount_in),0)-COALESCE(SUM(x.amount_out),0)) DESC""",
                      (s, e)).fetchall()
    bigm = {r["machine_id"]: (r["n"], r["v"]) for r in d.execute(
        "SELECT machine_id, COUNT(*) n, SUM(amount) v FROM payouts WHERE status='valid' AND substr(paid_at,1,10) BETWEEN ? AND ? "
        "GROUP BY machine_id", (s, e))}
    machines = []
    for r in mrows:
        bn, bv = bigm.get(r["id"], (0, 0))
        machines.append({"code": r["code"], "loc": r["loc"], "in": r["i"], "out": r["o"], "net": r["i"] - r["o"],
                         "hold": pct(r["i"] - r["o"], r["i"]), "ratio": pct(r["o"], r["i"]),
                         "share": pct(r["i"], tin), "big_n": bn, "big_v": bv or 0, "n": r["n"],
                         "photo": pct(r["ph"] or 0, r["n"]),
                         "flag": "high payout" if r["i"] and r["o"] / r["i"] > 0.95 else ""})

    # ---- payout size mix
    sizes = []
    for lab, lo, hi in BUCKETS:
        r = d.execute("SELECT COUNT(*) n, COALESCE(SUM(amount_out),0) v FROM entries WHERE entry_date BETWEEN ? AND ? "
                      "AND amount_out>0 AND amount_out>=? AND amount_out<?", (s, e, lo, hi)).fetchone()
        sizes.append({"label": lab, "n": r["n"], "v": r["v"]})
    sz_n = sum(x["n"] for x in sizes)
    for x in sizes:
        x["pct"] = pct(x["n"], sz_n)

    # ---- client concentration (share of all payouts)
    cl = d.execute("""SELECT c.full_name name, SUM(x.amount_out) o, COUNT(*) n FROM entries x JOIN clients c ON c.id=x.client_id
        WHERE x.entry_date BETWEEN ? AND ? AND x.amount_out>0 GROUP BY c.id ORDER BY o DESC LIMIT 8""", (s, e)).fetchall()
    top_sum = sum(r["o"] for r in cl)
    clients = [{"name": r["name"], "out": r["o"], "n": r["n"], "pct": pct(r["o"], tout)} for r in cl]
    known = d.execute("SELECT COALESCE(SUM(amount_out),0) FROM entries WHERE entry_date BETWEEN ? AND ? AND client_id IS NOT NULL",
                      (s, e)).fetchone()[0]
    client_mix = {"labels": [c["name"] for c in clients] + (["Other known clients"] if known - top_sum > 0.5 else []) +
                  (["No client recorded"] if tout - known > 0.5 else []),
                  "data": [round(c["out"], 2) for c in clients] + ([round(known - top_sum, 2)] if known - top_sum > 0.5 else []) +
                  ([round(tout - known, 2)] if tout - known > 0.5 else [])}

    # ---- time of day for large payouts
    hr = {r["h"]: (r["n"], r["v"]) for r in d.execute(
        "SELECT substr(paid_at,12,2) h, COUNT(*) n, SUM(amount) v FROM payouts WHERE status='valid' "
        "AND substr(paid_at,1,10) BETWEEN ? AND ? GROUP BY h", (s, e))}
    hours = {"labels": ["%02d:00" % h for h in range(24)], "n": [hr.get("%02d" % h, (0, 0))[0] for h in range(24)]}

    # ---- coverage by section (each section's photo uptake)
    cl_tot = d.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    cl_id = d.execute("SELECT COUNT(DISTINCT client_id) FROM documents WHERE doc_type='ID document' AND client_id IS NOT NULL").fetchone()[0]
    mach_tot = d.execute("SELECT COUNT(*) FROM machines WHERE status='active'").fetchone()[0]
    wk = (de - datetime.timedelta(days=7)).isoformat()
    mach_meter = d.execute("SELECT COUNT(DISTINCT machine_id) FROM meter_readings WHERE substr(read_at,1,10)>=? AND substr(read_at,1,10)<=?",
                           (wk, e)).fetchone()[0]
    inst_ok = d.execute("SELECT COUNT(*) FROM machine_installs i JOIN machines m ON m.id=i.machine_id "
                        "WHERE m.status='active' AND i.status IN ('complete','review')").fetchone()[0]
    pay_tot = big["n"]
    pay_read = d.execute("SELECT COUNT(*) FROM payouts WHERE status='valid' AND substr(paid_at,1,10) BETWEEN ? AND ? "
                         "AND COALESCE(ocr_text,'')<>''", (s, e)).fetchone()[0]
    doc_tot = d.execute("SELECT COUNT(*) FROM documents WHERE substr(uploaded_at,1,10) BETWEEN ? AND ?", (s, e)).fetchone()[0]
    doc_read = d.execute("SELECT COUNT(*) FROM documents WHERE substr(uploaded_at,1,10) BETWEEN ? AND ? AND COALESCE(ocr_text,'')<>''",
                         (s, e)).fetchone()[0]
    coverage = [
        {"label": "Wins / Losses with a slip photo", "pct": pct(backed, n), "n": backed, "of": n},
        {"label": "Slip photos that agree with the entry", "pct": pct(agree, backed), "n": agree, "of": backed},
        {"label": "Large payouts read from the photo", "pct": pct(pay_read, pay_tot), "n": pay_read, "of": pay_tot},
        {"label": "Clients with an ID photo on file", "pct": pct(cl_id, cl_tot), "n": cl_id, "of": cl_tot},
        {"label": "Machines with a meter photo (last 7 days)", "pct": pct(mach_meter, mach_tot), "n": mach_meter, "of": mach_tot},
        {"label": "Machines installed with a full photo set", "pct": pct(inst_ok, mach_tot), "n": inst_ok, "of": mach_tot},
        {"label": "Documents read from the photo", "pct": pct(doc_read, doc_tot), "n": doc_read, "of": doc_tot},
    ]

    # ---- meter vs captured (machines with 2+ meter photos in the range)
    meter = []
    for m in d.execute("SELECT DISTINCT machine_id FROM meter_readings WHERE substr(read_at,1,10) BETWEEN ? AND ?", (s, e)).fetchall():
        rs = d.execute("SELECT * FROM meter_readings WHERE machine_id=? AND substr(read_at,1,10) BETWEEN ? AND ? ORDER BY read_at, id",
                       (m["machine_id"], s, e)).fetchall()
        if len(rs) < 2:
            continue
        a, b = rs[0], rs[-1]
        code = d.execute("SELECT code FROM machines WHERE id=?", (m["machine_id"],)).fetchone()["code"]
        cap = d.execute("SELECT COALESCE(SUM(amount_in),0) i, COALESCE(SUM(amount_out),0) o FROM entries WHERE machine_id=? "
                        "AND entry_date>substr(?,1,10) AND entry_date<=substr(?,1,10)", (m["machine_id"], a["read_at"], b["read_at"])).fetchone()
        mi = (b["coin_in"] or 0) - (a["coin_in"] or 0) if a["coin_in"] is not None and b["coin_in"] is not None else None
        mo = (b["coin_out"] or 0) - (a["coin_out"] or 0) if a["coin_out"] is not None and b["coin_out"] is not None else None
        meter.append({"code": code, "from": a["read_at"][:10], "to": b["read_at"][:10], "m_in": mi, "m_out": mo,
                      "c_in": cap["i"], "c_out": cap["o"],
                      "var_in": pct(cap["i"] - mi, mi) if mi else None, "var_out": pct(cap["o"] - mo, mo) if mo else None})

    # ---- exceptions for a supervisor to review
    exc = []
    for r in d.execute("""SELECT p.id, p.paid_at, p.amount, p.flags, m.code, c.full_name FROM payouts p
        JOIN machines m ON m.id=p.machine_id JOIN clients c ON c.id=p.client_id
        WHERE p.status='valid' AND substr(p.paid_at,1,10) BETWEEN ? AND ? AND COALESCE(p.flags,'')<>'' AND p.flags NOT LIKE 'Entered manually%'
        ORDER BY p.paid_at DESC LIMIT 10""", (s, e)):
        exc.append({"kind": "Large payout", "when": r["paid_at"][:16], "what": f"#{r['id']} {r['code']} R {r['amount']:,.2f} - {r['full_name']}",
                    "why": r["flags"], "link": "/payouts/%d" % r["id"]})
    for r in d.execute("""SELECT x.id, x.entry_date, x.amount_in, x.amount_out, x.flags, m.code FROM entries x JOIN machines m ON m.id=x.machine_id
        WHERE x.photo_check='differs' AND x.entry_date BETWEEN ? AND ? ORDER BY x.entry_date DESC LIMIT 10""", (s, e)):
        exc.append({"kind": "Win / loss", "when": r["entry_date"], "what": f"{r['code']} in R {r['amount_in']:,.2f} / out R {r['amount_out']:,.2f}",
                    "why": r["flags"], "link": "/entries"})
    for r in d.execute("""SELECT 0, ocr_flags, uploaded_at, doc_type, filename FROM documents WHERE ocr_flags IS NOT NULL AND ocr_flags<>''
        AND ocr_flags NOT LIKE '%not machine-read%' AND ocr_flags NOT LIKE '%matches photo%'
        AND substr(uploaded_at,1,10) BETWEEN ? AND ? ORDER BY id DESC LIMIT 10""", (s, e)):
        exc.append({"kind": "Document", "when": r["uploaded_at"][:16], "what": r["doc_type"], "why": r["ocr_flags"], "link": "/files/" + r["filename"]})

    kpis = {
        "turnover": tin, "turnover_d": delta(tin, pin), "payouts": tout, "payouts_d": delta(tout, pout),
        "net": net, "net_d": delta(net, pnet), "hold": pct(net, tin), "hold_prev": pct(pnet, pin),
        "ratio": pct(tout, tin), "entries": n,
        "big_n": big["n"], "big_v": big["v"], "big_avg": big["a"], "big_share": pct(big["v"], tout),
        "photo_backed": pct(backed, n), "photo_agree": pct(agree, backed),
    }
    return {"kpis": kpis, "quality": quality, "trend": trend, "machines": machines, "sizes": sizes, "clients": clients,
            "client_mix": client_mix, "hours": hours, "coverage": coverage, "meter": meter, "exceptions": exc,
            "days": days, "prev": [ps, pe]}
