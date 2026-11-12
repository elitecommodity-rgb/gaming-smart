"""Optional demo data for client presentations. Enabled only with DEMO_DATA=1 and only when
the database has no machines yet, so it never touches real data."""
import os, io, random, hashlib, datetime
from PIL import Image, ImageDraw
import validators as vd

MACHINES = [("SM-001", "Main floor", "Lucky Dragon"), ("SM-002", "Main floor", "Gold Rush"),
            ("SM-003", "Bar area", "Mega Fortune"), ("SM-004", "Bar area", "Fire Link"),
            ("SM-005", "VIP lounge", "Buffalo Gold"), ("SM-006", "VIP lounge", "Lightning Cash")]
NAMES = ["Thandi Mokoena", "Sipho Dlamini", "Anele Naidoo", "Pieter van Wyk", "Zanele Khumalo",
         "Rajesh Pillay", "Lerato Molefe", "Johan Botha", "Nomsa Zulu", "Ahmed Patel",
         "Busisiwe Ndlovu", "Craig Henderson"]


def _id(rng, year):
    while True:
        base = "%02d%02d%02d%04d08" % (year % 100, rng.randint(1, 12), rng.randint(1, 28), rng.randint(1000, 4999))
        for c in range(10):
            if vd.luhn_ok(base + str(c)):
                return base + str(c)


def _photo(code, name, amount, when):
    im = Image.new("RGB", (900, 1200), (250, 248, 240))
    d = ImageDraw.Draw(im)
    d.rectangle([30, 30, 870, 1170], outline=(60, 60, 60), width=4)
    d.text((60, 60), "DEMO - MANUAL PAYOUT FORM", fill=(20, 20, 20))
    for i, line in enumerate(["Machine: " + code, "Customer: " + name, "Amount paid: R %.2f" % amount,
                              "Date: " + when, "Signature: ______________"]):
        d.text((60, 160 + i * 90), line, fill=(30, 30, 120))
    b = io.BytesIO(); im.save(b, "JPEG", quality=80)
    t = im.copy(); t.thumbnail((360, 360)); tb = io.BytesIO(); t.save(tb, "JPEG", quality=70)
    return b.getvalue(), tb.getvalue()


def seed(conn, uploads, today, now_str):
    if conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0]:
        return
    rng = random.Random(7)
    for c, l, m in MACHINES:
        conn.execute("INSERT INTO machines(code,location,model) VALUES(?,?,?)", (c, l, m))
    cids = []
    for n in NAMES:
        idn = _id(rng, rng.randint(1960, 2002))
        cur = conn.execute("INSERT INTO clients(full_name,id_number,phone,notes,created_at,id_type) VALUES(?,?,?,?,?,?)",
                           (n, idn, "08%d" % rng.randint(10000000, 99999999), "", now_str, "sa_id"))
        cids.append(cur.lastrowid)
    weights = [3, 2, 2, 1, 4, 3]
    for back in range(60):
        day = (today - datetime.timedelta(days=back)).isoformat()
        for mid in range(1, 7):
            if rng.random() < 0.8:
                stake = round(rng.uniform(800, 6000) * weights[mid - 1] / 2, 2)
                payout = round(stake * rng.uniform(0.55, 1.05), 2)
                payout = min(payout, 4900.0)
                conn.execute("INSERT INTO entries(machine_id,client_id,entry_date,amount_in,amount_out,notes,captured_by,created_at) "
                             "VALUES(?,?,?,?,?,?,?,?)", (mid, rng.choice(cids + [None, None]), day, stake, payout, "", "admin", now_str))
    os.makedirs(uploads, exist_ok=True)
    for i in range(7):
        mid = rng.randint(1, 6); cid = rng.choice(cids); amt = float(rng.choice([5200, 6500, 7800, 9000, 12500, 15000, 22000]))
        when = (datetime.datetime.combine(today, datetime.time(14, 30)) - datetime.timedelta(days=i * 4 + 1))
        ws = when.strftime("%Y-%m-%d %H:%M")
        code = MACHINES[mid - 1][0]
        name = conn.execute("SELECT full_name FROM clients WHERE id=?", (cid,)).fetchone()[0]
        full, thumb = _photo(code, name, amt, ws)
        fn = "demo%d.jpg" % i
        open(os.path.join(uploads, fn), "wb").write(full)
        open(os.path.join(uploads, "t_" + fn), "wb").write(thumb)
        cur = conn.execute("INSERT INTO entries(machine_id,client_id,entry_date,amount_in,amount_out,notes,captured_by,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?)", (mid, cid, ws[:10], 0, amt, "Large payout", "admin", now_str))
        p = conn.execute("INSERT INTO payouts(machine_id,client_id,amount,paid_at,entry_id,filename,file_hash,ocr_text,flags,notes,captured_by,created_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (mid, cid, amt, ws, cur.lastrowid, fn, hashlib.sha256(full).hexdigest(),
                                                              "", "Entered manually - photo not machine-read", "Demo data", "admin", now_str))
        conn.execute("UPDATE entries SET notes=? WHERE id=?", ("Large payout #%d" % p.lastrowid, cur.lastrowid))
        conn.execute("INSERT INTO documents(client_id,machine_id,entry_id,doc_type,filename,original_name,uploaded_by,uploaded_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (cid, mid, cur.lastrowid, "Large payout form", fn, "payout-form.jpg", "admin", now_str))
    conn.commit()
