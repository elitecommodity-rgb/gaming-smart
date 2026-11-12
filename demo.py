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
    ents = []
    os.makedirs(uploads, exist_ok=True)
    sf, st = _photo("SM-001", "Demo slip", 1250.0, "demo")
    open(os.path.join(uploads, "demo_slip.jpg"), "wb").write(sf)
    for back in range(60):
        day = (today - datetime.timedelta(days=back)).isoformat()
        for mid in range(1, 7):
            if rng.random() < 0.8:
                stake = round(rng.uniform(800, 6000) * weights[mid - 1] / 2, 2)
                payout = round(stake * rng.uniform(0.55, 1.05), 2)
                payout = min(payout, 4900.0)
                r = rng.random()
                if back > 45:
                    check, fl = None, ""          # older data was captured before photo capture started
                elif r < 0.40:
                    check, fl = "match", ""
                elif r < 0.46:
                    check, fl = "differs", "Photo reads out R %s" % format(round(payout * 1.08, 2), ",.2f")
                elif r < 0.52:
                    check, fl = "unread", "Photo attached, not machine-read"
                else:
                    check, fl = None, ""
                cl = rng.choice(cids + [None, None])
                cur = conn.execute("INSERT INTO entries(machine_id,client_id,entry_date,amount_in,amount_out,notes,captured_by,created_at,"
                                   "source,photo_check,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                   (mid, cl, day, stake, payout, "", "admin", now_str, "photo" if check else "manual", check, fl))
                ents.append((mid, day, stake, payout))
                if check:
                    conn.execute("INSERT INTO documents(client_id,machine_id,entry_id,doc_type,filename,original_name,uploaded_by,uploaded_at,"
                                 "ocr_text,ocr_flags) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (cl, mid, cur.lastrowid, "Payout slip", "demo_slip.jpg", "slip.jpg", "admin",
                                  day + " 12:00", "" if check == "unread" else "SM-00%d" % mid, fl))
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
                                                              "Machine %s R %.2f" % (code, amt), "" if i < 5 else ("Photo reads R %s" % format(amt + 500, ",.2f") if i == 5 else "Entered manually - photo not machine-read"),
                                                              "Demo data", "admin", now_str))
        conn.execute("UPDATE entries SET notes=? WHERE id=?", ("Large payout #%d" % p.lastrowid, cur.lastrowid))
        ents.append((mid, ws[:10], 0, amt))
        conn.execute("INSERT INTO documents(client_id,machine_id,entry_id,doc_type,filename,original_name,uploaded_by,uploaded_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (cid, mid, cur.lastrowid, "Large payout form", fn, "payout-form.jpg", "admin", now_str))
    for k, cid in enumerate(cids[:9]):
        conn.execute("INSERT INTO documents(client_id,doc_type,filename,original_name,uploaded_by,uploaded_at,ocr_text,ocr_flags) VALUES(?,?,?,?,?,?,?,?)",
                     (cid, "ID document", "demo_slip.jpg", "id.jpg", "admin", (today - datetime.timedelta(days=30 - k)).isoformat() + " 10:00",
                      "ID", "ID matches photo"))
    for mid in range(1, 7):
        st = "review" if mid == 4 else ("incomplete" if mid == 6 else "complete")
        cur = conn.execute("INSERT INTO machine_installs(machine_id,serial_no,manufacturer,installed_by,installed_at,checks,notes,flags,status,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)", (mid, "DEMO-%05d" % (40100 + mid), MACHINES[mid - 1][2], "admin",
                                                          (today - datetime.timedelta(days=90)).isoformat() + " 08:30",
                                                          "power,label,meters,printer,locks,position" if mid != 6 else "power,label",
                                                          "", "Plate photo reads serial DEMO-0410X" if mid == 4 else "", st, now_str))
        for slot in ("front", "plate", "label", "meter", "position")[: 3 if mid == 6 else 5]:
            conn.execute("INSERT INTO install_photos(install_id,slot,filename,ocr_text,flags,taken_by,taken_at) VALUES(?,?,?,?,?,?,?)",
                         (cur.lastrowid, slot, "demo_slip.jpg", "", "", "admin", now_str))
    for mid in range(1, 7):
        for back, fac in ((25, 1.0), (12, rng.uniform(0.97, 1.03)), (2, rng.uniform(0.97, 1.03))):
            cut = (today - datetime.timedelta(days=back)).isoformat()
            ci = 5000000 + sum(x[2] for x in ents if x[0] == mid and x[1] <= cut) * fac
            co = 4200000 + sum(x[3] for x in ents if x[0] == mid and x[1] <= cut) * fac
            conn.execute("INSERT INTO documents(machine_id,doc_type,filename,original_name,uploaded_by,uploaded_at,ocr_text,ocr_flags) VALUES(?,?,?,?,?,?,?,?)",
                         (mid, "Machine meter photo", "demo_slip.jpg", "meter.jpg", "admin", cut + " 09:00", "meter", ""))
            conn.execute("INSERT INTO meter_readings(machine_id,read_at,coin_in,coin_out,document_id,captured_by) VALUES(?,?,?,?,?,?)",
                         (mid, cut + " 09:00", round(ci, 2), round(co, 2), None, "admin"))
    conn.execute("UPDATE clients SET consent_at=created_at, consent_by='admin' WHERE id % 4 <> 0")
    conn.commit()
