"""Report data + PDF / Excel / Word export for Gaming Smart."""
import io, datetime

MIN_ROWS_NOTE = "No data in this period."


def default_range():
    today = datetime.date.today()
    return today.replace(day=1).isoformat(), today.isoformat()


def parse_range(args):
    s0, e0 = default_range()
    s = args.get("start") or s0
    e = args.get("end") or e0
    try:
        datetime.date.fromisoformat(s)
        datetime.date.fromisoformat(e)
    except ValueError:
        s, e = s0, e0
    if s > e:
        s, e = e, s
    return s, e


def _pct(o, i):
    return round(o / i * 100, 1) if i else 0.0


def _totals(rows, label, money_cols, pct_pair=None, int_cols=(), width=0):
    t = [label] + [""] * (width - 1)
    for c in money_cols + list(int_cols):
        t[c] = sum(r[c] for r in rows)
    if pct_pair:
        o, i, p = pct_pair
        t[p] = _pct(t[o], t[i])
    return t


# --------------------------------------------------------------------------
def machine_report(d, s, e):
    q = d.execute("""
        SELECT m.code, COALESCE(m.location,'') loc,
               COALESCE(SUM(x.amount_in),0) i, COALESCE(SUM(x.amount_out),0) o, COUNT(x.id) n
        FROM machines m LEFT JOIN entries x
             ON x.machine_id=m.id AND x.entry_date BETWEEN ? AND ?
        GROUP BY m.id
        ORDER BY (COALESCE(SUM(x.amount_in),0)-COALESCE(SUM(x.amount_out),0)) DESC""",
                  (s, e)).fetchall()
    rows = [[r["code"], r["loc"], r["i"], r["o"], r["i"] - r["o"], r["n"], _pct(r["o"], r["i"])]
            for r in q]
    cols = [("Machine", "text"), ("Location", "text"), ("Total in", "money"),
            ("Payouts", "money"), ("House net", "money"), ("Entries", "int"), ("Payout %", "pct")]
    return {
        "key": "machine", "title": "Machine performance", "columns": cols, "rows": rows,
        "totals": _totals(rows, "Total", [2, 3, 4], (3, 2, 6), [5], 7) if rows else None,
        "chart": {"kind": "bar", "limit": 20, "series": [
            {"name": "Total in", "col": 2, "color": "#2f6fb0"},
            {"name": "Payouts", "col": 3, "color": "#d9822b"}]},
    }


def client_report(d, s, e):
    q = d.execute("""
        SELECT c.full_name, COUNT(x.id) n, COALESCE(SUM(x.amount_in),0) i,
               COALESCE(SUM(x.amount_out),0) o, MAX(x.entry_date) last
        FROM clients c JOIN entries x ON x.client_id=c.id
        WHERE x.entry_date BETWEEN ? AND ?
        GROUP BY c.id ORDER BY SUM(x.amount_in) DESC LIMIT 200""", (s, e)).fetchall()
    rows = [[r["full_name"], r["n"], r["i"], r["o"], r["i"] - r["o"], r["last"] or ""] for r in q]
    cols = [("Client", "text"), ("Visits", "int"), ("Total in", "money"),
            ("Payouts", "money"), ("House net", "money"), ("Last played", "text")]
    return {
        "key": "client", "title": "Client performance", "columns": cols, "rows": rows,
        "totals": _totals(rows, "Total", [2, 3, 4], None, [1], 6) if rows else None,
        "chart": {"kind": "barh", "limit": 15, "sign_colors": True, "series": [
            {"name": "House net", "col": 4, "color": "#2e9e6b"}]},
    }


def business_report(d, s, e):
    days = (datetime.date.fromisoformat(e) - datetime.date.fromisoformat(s)).days + 1
    fmt = "%Y-%m" if days > 92 else "%Y-%m-%d"
    q = d.execute(f"""
        SELECT strftime('{fmt}', entry_date) p, COALESCE(SUM(amount_in),0) i,
               COALESCE(SUM(amount_out),0) o, COUNT(*) n
        FROM entries WHERE entry_date BETWEEN ? AND ? GROUP BY p ORDER BY p""", (s, e)).fetchall()
    rows = [[r["p"], r["i"], r["o"], r["i"] - r["o"], r["n"], _pct(r["o"], r["i"])] for r in q]
    cols = [("Month" if days > 92 else "Date", "text"), ("Total in", "money"), ("Payouts", "money"),
            ("House net", "money"), ("Entries", "int"), ("Payout %", "pct")]
    return {
        "key": "business", "title": "Business performance", "columns": cols, "rows": rows,
        "totals": _totals(rows, "Total", [1, 2, 3], (2, 1, 5), [4], 6) if rows else None,
        "chart": {"kind": "line", "limit": 400, "series": [
            {"name": "Total in", "col": 1, "color": "#2f6fb0"},
            {"name": "Payouts", "col": 2, "color": "#d9822b"},
            {"name": "House net", "col": 3, "color": "#2e9e6b"}]},
    }


BUILDERS = {"client": client_report, "machine": machine_report, "business": business_report}


# --------------------------------------------------------------------------
def chart_data(rep):
    """JSON-friendly structure for Chart.js on the web page."""
    c = rep["chart"]
    rows = rep["rows"][:c["limit"]]
    ds = []
    for sr in c["series"]:
        vals = [r[sr["col"]] for r in rows]
        item = {"label": sr["name"], "data": vals, "color": sr["color"]}
        if c.get("sign_colors"):
            item["colors"] = ["#2e9e6b" if v >= 0 else "#e5574f" for v in vals]
        ds.append(item)
    return {"kind": c["kind"], "labels": [str(r[0]) for r in rows], "datasets": ds}


def _fmt(v, t):
    if t == "money":
        return f"R {v:,.2f}"
    if t == "pct":
        return f"{v:.1f}%"
    if t == "int":
        return f"{int(v):,}"
    return "" if v is None else str(v)


def chart_png(rep):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    c = rep["chart"]
    rows = rep["rows"][:c["limit"]]
    kind = c["kind"]
    figsize = (8, max(3.2, 0.4 * len(rows) + 1.2)) if kind == "barh" else (8, 4.2)
    fig, ax = plt.subplots(figsize=figsize, dpi=150)
    if not rows:
        ax.text(0.5, 0.5, MIN_ROWS_NOTE, ha="center", va="center", fontsize=12, color="#666")
        ax.axis("off")
    else:
        labels = [str(r[0]) for r in rows]
        n = len(c["series"])
        if kind == "bar":
            w = 0.8 / n
            for k, sr in enumerate(c["series"]):
                xs = [i + k * w - 0.4 + w / 2 for i in range(len(rows))]
                ax.bar(xs, [r[sr["col"]] for r in rows], w, label=sr["name"], color=sr["color"])
            ax.set_xticks(range(len(rows)))
            ax.set_xticklabels(labels, rotation=30 if len(rows) > 6 else 0, ha="right" if len(rows) > 6 else "center")
        elif kind == "barh":
            sr = c["series"][0]
            vals = [r[sr["col"]] for r in rows]
            ax.barh(range(len(rows)), vals,
                    color=["#2e9e6b" if v >= 0 else "#e5574f" for v in vals])
            ax.set_yticks(range(len(rows)))
            ax.set_yticklabels(labels)
            ax.invert_yaxis()
            ax.axvline(0, color="#999", linewidth=0.8)
        else:
            for sr in c["series"]:
                ax.plot(labels, [r[sr["col"]] for r in rows], label=sr["name"],
                        color=sr["color"], marker="o" if len(rows) <= 31 else None, linewidth=2)
            step = max(1, len(rows) // 12)
            ax.set_xticks(range(0, len(rows), step))
            ax.set_xticklabels(labels[::step], rotation=30, ha="right")
        ax.set_ylabel("Rand (R)") if kind != "barh" else ax.set_xlabel("House net (R)")
        ax.grid(axis="x" if kind == "barh" else "y", alpha=0.25)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        if kind != "barh":
            ax.legend(frameon=False)
    ax.set_title(rep["title"], loc="left", fontsize=13, fontweight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
def to_xlsx(rep, s, e):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = rep["title"][:31]
    ws["A1"] = rep["title"]
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Period: {s} to {e}"
    ws["A3"] = f"Generated {_stamp()}"
    hdr = 5
    cols = rep["columns"]
    for j, (name, _) in enumerate(cols, 1):
        c = ws.cell(row=hdr, column=j, value=name)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F2A44")
        c.alignment = Alignment(horizontal="center")
    nf = {"money": '"R" #,##0.00', "pct": '0.0"%"', "int": "#,##0"}
    for i, row in enumerate(rep["rows"], hdr + 1):
        for j, v in enumerate(row, 1):
            c = ws.cell(row=i, column=j, value=v)
            if cols[j - 1][1] in nf:
                c.number_format = nf[cols[j - 1][1]]
    last = hdr + len(rep["rows"])
    if rep["totals"]:
        r = last + 1
        for j, v in enumerate(rep["totals"], 1):
            c = ws.cell(row=r, column=j, value=v)
            c.font = Font(bold=True)
            if cols[j - 1][1] in nf:
                c.number_format = nf[cols[j - 1][1]]
    for j in range(1, len(cols) + 1):
        width = max(len(str(cols[j - 1][0])), *(len(_fmt(r[j - 1], cols[j - 1][1])) for r in rep["rows"])) if rep["rows"] else len(cols[j - 1][0])
        ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(40, width + 3)

    ch = rep["chart"]
    n = min(len(rep["rows"]), ch["limit"])
    if n:
        if ch["kind"] == "line":
            chart = LineChart()
        else:
            chart = BarChart()
            chart.type = "bar" if ch["kind"] == "barh" else "col"
        chart.title = rep["title"]
        chart.height, chart.width = 9, 20
        for sr in ch["series"]:
            ref = Reference(ws, min_col=sr["col"] + 1, min_row=hdr, max_row=hdr + n)
            chart.add_data(ref, titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=hdr + 1, max_row=hdr + n))
        ws.add_chart(chart, f"A{last + 4}")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def to_docx(rep, s, e):
    from docx import Document
    from docx.shared import Inches, Pt
    doc = Document()
    doc.add_heading(rep["title"], 1)
    doc.add_paragraph(f"Period: {s} to {e}    |    Generated {_stamp()}")
    doc.add_picture(io.BytesIO(chart_png(rep)), width=Inches(6.3))
    cols = rep["columns"]
    if rep["rows"]:
        t = doc.add_table(rows=1, cols=len(cols))
        t.style = "Light Grid Accent 1"
        for j, (name, _) in enumerate(cols):
            t.rows[0].cells[j].text = name
        body = rep["rows"][:200] + ([rep["totals"]] if rep["totals"] else [])
        for k, row in enumerate(body):
            cells = t.add_row().cells
            for j, v in enumerate(row):
                cells[j].text = _fmt(v, cols[j][1])
        for r_i, row in enumerate(t.rows):
            for cell in row.cells:
                for p in cell.paragraphs:
                    for run in p.runs:
                        run.font.size = Pt(8.5)
                        if r_i == 0 or (rep["totals"] and r_i == len(t.rows) - 1):
                            run.bold = True
    else:
        doc.add_paragraph(MIN_ROWS_NOTE)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def to_pdf(rep, s, e):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from PIL import Image as PILImage

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm, title=rep["title"])
    st = getSampleStyleSheet()
    story = [Paragraph(rep["title"], st["Title"]),
             Paragraph(f"Period: {s} to {e} &nbsp;|&nbsp; Generated {_stamp()}", st["Normal"]),
             Spacer(1, 6 * mm)]
    png = chart_png(rep)
    w, h = PILImage.open(io.BytesIO(png)).size
    width = A4[0] - 30 * mm
    story += [Image(io.BytesIO(png), width=width, height=width * h / w), Spacer(1, 6 * mm)]
    cols = rep["columns"]
    if rep["rows"]:
        data = [[c[0] for c in cols]]
        for row in rep["rows"][:200] + ([rep["totals"]] if rep["totals"] else []):
            data.append([_fmt(v, cols[j][1]) for j, v in enumerate(row)])
        tbl = Table(data, repeatRows=1)
        style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2A44")),
                 ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                 ("FONTSIZE", (0, 0), (-1, -1), 8),
                 ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6fa")])]
        for j, (_, t) in enumerate(cols):
            if t != "text":
                style.append(("ALIGN", (j, 0), (j, -1), "RIGHT"))
        if rep["totals"]:
            style += [("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                      ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)]
        tbl.setStyle(TableStyle(style))
        story.append(tbl)
    else:
        story.append(Paragraph(MIN_ROWS_NOTE, st["Normal"]))
    doc.build(story)
    return out.getvalue()


EXPORTERS = {
    "pdf": (to_pdf, "application/pdf"),
    "xlsx": (to_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "docx": (to_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}
