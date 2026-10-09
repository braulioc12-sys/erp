"""Quinta categoría (9 oct, pedido de Braulio: "un módulo de quinta categoría,
para poder registrar por trabajador los ingresos que ha recibido meses
anteriores a la implementación y cuánto se le retuvo, y así mismo en el caso
haya gente que esté en otras planillas").

Guarda en `fifth_prior_entries` lo que YA pasó en el año y el sistema no
calculó: por trabajador, año y mes, el ingreso afecto y la retención de

- PREVIO: meses de esta misma empresa antes de usar el sistema, y
- OTRO: otro empleador / otra planilla (nombre y RUC opcionales).

La planilla suma esos montos de los meses anteriores al que calcula (ver
app/payroll_calc.py, fifth_registered_totals()) para proyectar bien el
impuesto anual y restar lo ya retenido. Mismo permiso que RRHH
(`pagos_personal`: ver / editar).
"""
from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from app.audit import log_activity
from app.auth import permission_required, validate_csrf
from app.bulk_import import XLSX_MIME, ImportColumn, build_import_template, read_import_rows
from app.db import execute, query_all, query_one
from app.helpers import parse_float, today_str

bp = Blueprint("quinta", __name__, url_prefix="/quinta-categoria")

MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
SOURCE_LABELS = {"PREVIO": "Antes del sistema (esta empresa)", "OTRO": "Otro empleador / otra planilla"}


def _year_arg():
    year = request.args.get("anio", type=int) or request.form.get("anio", type=int)
    current = int(today_str()[:4])
    return year if year and 2000 <= year <= 2100 else current


def _year_options(year):
    current = int(today_str()[:4])
    return sorted({current - 2, current - 1, current, current + 1, year})


def _money(raw):
    """(valor, error): vacío = 0; negativo o ilegible = error."""
    text = (raw or "").strip()
    if not text:
        return 0.0, None
    value = parse_float(text, None)
    if value is None or value < 0:
        return None, "Los montos deben ser números de 0 en adelante."
    return round(value, 2), None


def _get_staff_or_404(staff_id):
    staff = query_one("SELECT * FROM staff WHERE id = ?", (staff_id,))
    if staff is None:
        abort(404)
    return staff


@bp.route("")
@permission_required("pagos_personal", "view")
def index():
    year = _year_arg()
    q = request.args.get("q", "").strip()
    show_all = request.args.get("todos") == "1"
    sql = """SELECT s.id, s.name, s.document_type, s.document_number, s.company, s.in_payroll,
                    s.fifth_prior_income, s.fifth_prior_withheld
             FROM staff s WHERE s.status = 'ACTIVO'"""
    params = []
    if not show_all:
        sql += " AND s.in_payroll = 1"
    if q:
        sql += " AND (LOWER(s.name) LIKE LOWER(?) OR LOWER(COALESCE(s.document_number, '')) LIKE LOWER(?))"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY s.name"
    people = query_all(sql, params)
    entries = query_all("SELECT staff_id, source, income, withheld FROM fifth_prior_entries WHERE year = ?", (year,))
    agg = {}
    for e in entries:
        a = agg.setdefault(e["staff_id"], {"PREVIO": [0.0, 0.0], "OTRO": [0.0, 0.0]})
        a[e["source"] if e["source"] in a else "PREVIO"][0] += e["income"] or 0
        a[e["source"] if e["source"] in a else "PREVIO"][1] += e["withheld"] or 0
    sys_ret = {
        r["staff_id"]: r["w"]
        for r in query_all(
            """SELECT l.staff_id, SUM(l.fifth_deduction) AS w FROM payroll_lines l JOIN payroll_periods p ON p.id = l.period_id
               WHERE p.period >= ? AND p.period <= ? GROUP BY l.staff_id""",
            (f"{year}-01", f"{year}-12"),
        )
    }
    rows = []
    for p in people:
        a = agg.get(p["id"], {"PREVIO": [0.0, 0.0], "OTRO": [0.0, 0.0]})
        rows.append({"staff": p, "previo": a["PREVIO"], "otro": a["OTRO"], "system_withheld": sys_ret.get(p["id"], 0.0)})
    return render_template(
        "quinta/index.html", rows=rows, year=year, years=_year_options(year), q=q, show_all=show_all,
    )


@bp.route("/<int:staff_id>")
@permission_required("pagos_personal", "view")
def detail(staff_id):
    staff = _get_staff_or_404(staff_id)
    year = _year_arg()
    entries = query_all(
        """SELECT e.*, u.name AS created_by_name FROM fifth_prior_entries e LEFT JOIN users u ON u.id = e.created_by
           WHERE e.staff_id = ? AND e.year = ? ORDER BY e.month, e.source, e.id""",
        (staff_id, year),
    )
    previo = {e["month"]: e for e in entries if e["source"] == "PREVIO"}
    otros = [e for e in entries if e["source"] == "OTRO"]
    in_system = {
        int(r["period"][5:7]): r
        for r in query_all(
            """SELECT p.period, l.fifth_base_income, l.fifth_deduction FROM payroll_lines l JOIN payroll_periods p ON p.id = l.period_id
               WHERE l.staff_id = ? AND p.period >= ? AND p.period <= ? ORDER BY p.period""",
            (staff_id, f"{year}-01", f"{year}-12"),
        )
    }
    tot = {
        "previo_income": sum(e["income"] or 0 for e in previo.values()),
        "previo_withheld": sum(e["withheld"] or 0 for e in previo.values()),
        "otro_income": sum(e["income"] or 0 for e in otros),
        "otro_withheld": sum(e["withheld"] or 0 for e in otros),
        "system_withheld": sum(r["fifth_deduction"] or 0 for r in in_system.values()),
    }
    return render_template(
        "quinta/detail.html", staff=staff, year=year, years=_year_options(year), previo=previo, otros=otros,
        in_system=in_system, tot=tot, months=MONTHS,
    )


@bp.route("/<int:staff_id>/previo", methods=["POST"])
@permission_required("pagos_personal", "edit")
def save_previo(staff_id):
    """Guarda la grilla de 12 meses (ingreso y retención de cada mes antes del
    sistema). Una fila en blanco borra lo que hubiera de ese mes."""
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    year = _year_arg()
    parsed, errors = {}, []
    for m in range(1, 13):
        income, e1 = _money(request.form.get(f"income_{m}"))
        withheld, e2 = _money(request.form.get(f"withheld_{m}"))
        if e1 or e2:
            errors.append(f"{MONTHS[m - 1].capitalize()}: {e1 or e2}")
        parsed[m] = (income, withheld, bool((request.form.get(f"income_{m}") or "").strip() or (request.form.get(f"withheld_{m}") or "").strip()))
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("quinta.detail", staff_id=staff_id, anio=year))
    saved = removed = 0
    for m, (income, withheld, filled) in parsed.items():
        existing = query_one(
            "SELECT id FROM fifth_prior_entries WHERE staff_id = ? AND year = ? AND month = ? AND source = 'PREVIO'",
            (staff_id, year, m),
        )
        if filled:
            if existing:
                execute("UPDATE fifth_prior_entries SET income = ?, withheld = ? WHERE id = ?", (income, withheld, existing["id"]))
            else:
                execute(
                    """INSERT INTO fifth_prior_entries (staff_id, year, month, source, income, withheld, created_by)
                       VALUES (?, ?, ?, 'PREVIO', ?, ?, ?)""",
                    (staff_id, year, m, income, withheld, g.user["id"] if getattr(g, "user", None) else None),
                )
            saved += 1
        elif existing:
            execute("DELETE FROM fifth_prior_entries WHERE id = ?", (existing["id"],))
            removed += 1
    log_activity(
        "pagos_personal", "EDITAR", f"5ta categoría {year} (meses anteriores): {staff['name']}",
        entity_type="empleado", entity_id=staff_id, entity_url=url_for("quinta.detail", staff_id=staff_id, anio=year),
    )
    flash(f"Guardado: {saved} mes(es) registrado(s)" + (f", {removed} quitado(s)" if removed else "") + ". "
          "Recalcula la planilla del mes si ya estaba calculada.", "success")
    return redirect(url_for("quinta.detail", staff_id=staff_id, anio=year))


@bp.route("/<int:staff_id>/otro", methods=["POST"])
@permission_required("pagos_personal", "edit")
def add_otro(staff_id):
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    year = _year_arg()
    month = request.form.get("month", type=int)
    employer = request.form.get("employer_name", "").strip()
    income, e1 = _money(request.form.get("income"))
    withheld, e2 = _money(request.form.get("withheld"))
    errors = [e for e in (e1, e2) if e]
    if not month or not 1 <= month <= 12:
        errors.append("Elige el mes.")
    if not employer:
        errors.append("Escribe el nombre del otro empleador o de la otra planilla.")
    if not errors and income == 0 and withheld == 0:
        errors.append("Escribe el ingreso o la retención.")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("quinta.detail", staff_id=staff_id, anio=year))
    execute(
        """INSERT INTO fifth_prior_entries (staff_id, year, month, source, employer_name, employer_ruc, income, withheld, notes, created_by)
           VALUES (?, ?, ?, 'OTRO', ?, ?, ?, ?, ?, ?)""",
        (staff_id, year, month, employer, request.form.get("employer_ruc", "").strip() or None, income, withheld,
         request.form.get("notes", "").strip() or None, g.user["id"] if getattr(g, "user", None) else None),
    )
    log_activity(
        "pagos_personal", "CREAR", f"5ta categoría {year}, otro empleador ({employer}): {staff['name']}",
        entity_type="empleado", entity_id=staff_id, entity_url=url_for("quinta.detail", staff_id=staff_id, anio=year),
    )
    flash("Registrado. Cuenta para la retención de los meses siguientes; recalcula la planilla si ya estaba calculada.", "success")
    return redirect(url_for("quinta.detail", staff_id=staff_id, anio=year))


@bp.route("/entrada/<int:entry_id>/eliminar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def delete_entry(entry_id):
    if not validate_csrf():
        abort(400)
    entry = query_one("SELECT * FROM fifth_prior_entries WHERE id = ?", (entry_id,))
    if entry is None:
        abort(404)
    staff = _get_staff_or_404(entry["staff_id"])
    execute("DELETE FROM fifth_prior_entries WHERE id = ?", (entry_id,))
    log_activity(
        "pagos_personal", "ELIMINAR", f"5ta categoría {entry['year']}/{entry['month']:02d}: {staff['name']}",
        entity_type="empleado", entity_id=staff["id"], entity_url=url_for("quinta.detail", staff_id=staff["id"], anio=entry["year"]),
    )
    flash("Registro eliminado.", "success")
    return redirect(url_for("quinta.detail", staff_id=staff["id"], anio=entry["year"]))


# --- Importación masiva desde Excel (misma plantilla para meses anteriores y
# otros empleadores: una fila por trabajador, año, mes y origen). ---

_IMPORT_COLUMNS = [
    ImportColumn("name", "Nombre", kind="text", required=True, width=30,
                 note="Debe ser una persona ya cargada en el Catálogo de Personal (no se crean personas desde aquí)."),
    ImportColumn("document_number", "N° de documento", kind="text", width=16, force_text=True,
                 note="Opcional. Si se llena, se usa para encontrar a la persona en vez del nombre."),
    ImportColumn("year", "Año", kind="float", required=True, width=8),
    ImportColumn("month", "Mes", kind="float", required=True, width=8, note="Número del 1 (enero) al 12 (diciembre)."),
    ImportColumn("source", "Origen", kind="choice", width=18,
                 choices=[("PREVIO", ["ANTES DEL SISTEMA", "PREVIO", "MISMA EMPRESA"]), ("OTRO", ["OTRO EMPLEADOR", "OTRA PLANILLA", "OTRO"])],
                 note="PREVIO = meses de esta empresa antes de usar el sistema; OTRO = otro empleador u otra planilla. Si se deja vacío, PREVIO."),
    ImportColumn("employer_name", "Empleador", kind="text", width=26, note="Obligatorio cuando el origen es OTRO."),
    ImportColumn("employer_ruc", "RUC del empleador", kind="text", width=16, force_text=True),
    ImportColumn("income", "Ingresos afectos", kind="float", required=True, width=16,
                 note="Lo que se pagó ese mes y está afecto a 5ta categoría (incluye gratificaciones y bonos si los hubo)."),
    ImportColumn("withheld", "Retención", kind="float", width=14, note="Renta de 5ta retenida ese mes. Vacío = 0."),
]
_IMPORT_EXAMPLE = {
    "name": "Nombre de Ejemplo", "document_number": "00000000", "year": 2026, "month": 3, "source": "PREVIO",
    "employer_name": "", "employer_ruc": "", "income": 3500, "withheld": 120,
}


@bp.route("/importar/plantilla")
@permission_required("pagos_personal", "edit")
def import_template():
    buffer = build_import_template("Quinta categoría — meses anteriores y otros empleadores", _IMPORT_COLUMNS, _IMPORT_EXAMPLE)
    return Response(
        buffer.getvalue(), mimetype=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="plantilla_quinta_categoria.xlsx"'},
    )


def _apply_import(rows, example_skips):
    from app.routes.legajo import _clean_code, _find_staff_for_import

    created, updated, errors = 0, 0, []
    skipped = [{"row": r, "message": "Fila de ejemplo de la plantilla; se omitió automáticamente."} for r in example_skips]
    user_id = g.user["id"] if getattr(g, "user", None) else None
    for row in rows:
        n = row["_row_number"]
        for warn in row["_warnings"]:
            errors.append({"row": n, "message": warn})
        name = (row.get("name") or "").strip()
        staff = _find_staff_for_import(name, _clean_code(row.get("document_number"))) if name else None
        if staff is None:
            errors.append({"row": n, "message": "No se encontró a la persona en el Catálogo de Personal; la fila no se importó."})
            continue
        year, month = row.get("year"), row.get("month")
        if year is None or month is None or int(year) != year or int(month) != month or not 2000 <= year <= 2100 or not 1 <= month <= 12:
            errors.append({"row": n, "message": "Año o mes no válidos (mes del 1 al 12); la fila no se importó."})
            continue
        year, month = int(year), int(month)
        income, withheld = row.get("income"), row.get("withheld") or 0.0
        if income is None or income < 0 or withheld < 0:
            errors.append({"row": n, "message": "Ingresos y retención deben ser números de 0 en adelante; la fila no se importó."})
            continue
        source = row.get("source") or "PREVIO"
        employer = (row.get("employer_name") or "").strip() or None
        if source == "OTRO" and not employer:
            errors.append({"row": n, "message": "Origen OTRO sin el nombre del empleador; la fila no se importó."})
            continue
        if source == "PREVIO":
            employer = None
        existing = query_one(
            """SELECT id FROM fifth_prior_entries WHERE staff_id = ? AND year = ? AND month = ? AND source = ?
               AND COALESCE(LOWER(employer_name), '') = COALESCE(LOWER(?), '')""",
            (staff["id"], year, month, source, employer),
        )
        ruc = _clean_code(row.get("employer_ruc")) or None
        if existing:
            execute("UPDATE fifth_prior_entries SET income = ?, withheld = ?, employer_ruc = COALESCE(?, employer_ruc) WHERE id = ?",
                    (round(income, 2), round(withheld, 2), ruc, existing["id"]))
            updated += 1
        else:
            execute(
                """INSERT INTO fifth_prior_entries (staff_id, year, month, source, employer_name, employer_ruc, income, withheld, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (staff["id"], year, month, source, employer, ruc, round(income, 2), round(withheld, 2), user_id),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


@bp.route("/importar", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def import_entries():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        rows, file_error, example_skips = read_import_rows(request.files.get("file"), _IMPORT_COLUMNS, _IMPORT_EXAMPLE)
        if file_error:
            flash(file_error, "error")
            return redirect(url_for("quinta.import_entries"))
        result = _apply_import(rows, example_skips)
        if result["created"] or result["updated"]:
            log_activity(
                "pagos_personal", "SUBIR",
                f"Importó 5ta categoría desde Excel ({result['created']} creado(s), {result['updated']} actualizado(s))",
                entity_type="empleado",
            )
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("quinta.index"), retry_url=url_for("quinta.import_entries"),
        )
    return render_template(
        "import_form.html", title="Importar 5ta categoría desde Excel", module_label="la quinta categoría",
        template_url=url_for("quinta.import_template"), upload_url=url_for("quinta.import_entries"),
        back_url=url_for("quinta.index"), columns=_IMPORT_COLUMNS,
        extra_note="Una fila por persona, año y mes. Si ya hay un registro del mismo mes y origen (y mismo empleador), se actualiza en vez de duplicarse.",
    )
