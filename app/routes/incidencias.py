"""Asistencia e incidencias del personal (6 oct, pedido de Braulio: "tardanzas
(por día y por hora), falta injustificada, falta con permiso o sin goce de
haber, licencia maternidad / paternidad / fallecimiento, descanso médico,
incapacidad temporal (si pasa 20 días crear archivo para pedir subsidio a
EsSalud)" y "solo excepciones, pero implementar un Excel que registre
inasistencias también").

Se asume que todos asisten: solo se anotan las EXCEPCIONES, una fila por
evento (rango de fechas; las tardanzas llevan minutos). Se pueden cargar a
mano o importar desde un Excel (plantilla descargable).

Cómo afecta cada una a la planilla (ver `month_summary`, que la planilla
usa para descontar):

- Tardanza: descuento proporcional por los minutos (valor hora = sueldo ÷ 30
  ÷ horas de la jornada).
- Tardanza con descuento de 1 día: se descuenta un día completo.
- Falta injustificada y falta con permiso / sin goce de haber: se descuenta
  el día (sueldo ÷ 30) por cada día.
- Permiso con goce, licencia de paternidad y de fallecimiento: pagadas por el
  empleador, sin descuento.
- Descanso médico / incapacidad temporal: los primeros 20 días de cada AÑO
  CALENDARIO los paga el empleador (los dos tipos se suman en ese tope); del
  día 21 en adelante lo cubre el subsidio de EsSalud. Al pasar de 20 días se
  avisa y se puede generar el archivo Excel para pedir el subsidio.
- Licencia de maternidad: la cubre el subsidio de EsSalud.

Si EsSalud le paga directo al trabajador, esos días se descuentan de la
planilla (parámetro "subsidio_descuenta"). Son reglas generales: conviene
confirmarlas con el contador.
"""
import io
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, Response, abort, flash, g, redirect, render_template, request, send_from_directory, url_for,
)
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from app import storage
from app.audit import log_activity
from app.auth import permission_required, validate_csrf
from app.bulk_import import XLSX_MIME, ImportColumn, build_import_template, read_import_rows
from app.db import execute, query_all, query_one
from app.helpers import parse_date, parse_float, today_str
from app.payroll_params import get_params
from app.routes.viajes import _save_binary_attachment

bp = Blueprint("incidencias", __name__, url_prefix="/incidencias")

# (código, etiqueta, grupo, usa minutos)
KINDS = [
    ("TARDANZA", "Tardanza (descuento por minutos)", "Tardanzas y faltas", True),
    ("TARDANZA_DIA", "Tardanza con descuento de 1 día", "Tardanzas y faltas", False),
    ("FALTA_INJUSTIFICADA", "Falta injustificada", "Tardanzas y faltas", False),
    ("PERMISO_SIN_GOCE", "Falta con permiso / sin goce de haber", "Permisos y licencias", False),
    ("PERMISO_CON_GOCE", "Permiso con goce de haber", "Permisos y licencias", False),
    ("LICENCIA_MATERNIDAD", "Licencia de maternidad", "Permisos y licencias", False),
    ("LICENCIA_PATERNIDAD", "Licencia de paternidad", "Permisos y licencias", False),
    ("LICENCIA_FALLECIMIENTO", "Licencia por fallecimiento de familiar", "Permisos y licencias", False),
    ("DESCANSO_MEDICO", "Descanso médico", "Salud", False),
    ("INCAPACIDAD_TEMPORAL", "Incapacidad temporal (CITT)", "Salud", False),
]
KIND_LABELS = {k: label for k, label, _g, _m in KINDS}
MEDICAL_KINDS = ("DESCANSO_MEDICO", "INCAPACIDAD_TEMPORAL")
ESSALUD_KINDS = MEDICAL_KINDS + ("LICENCIA_MATERNIDAD",)
SUBSIDY_STATUS = {"PENDIENTE": "Por solicitar", "SOLICITADO": "Solicitado", "COBRADO": "Cobrado"}

# Si un mismo día tiene varias incidencias, cuenta solo la de mayor prioridad.
DAY_PRIORITY = [
    "LICENCIA_MATERNIDAD", "INCAPACIDAD_TEMPORAL", "DESCANSO_MEDICO", "LICENCIA_PATERNIDAD",
    "LICENCIA_FALLECIMIENTO", "PERMISO_CON_GOCE", "PERMISO_SIN_GOCE", "FALTA_INJUSTIFICADA", "TARDANZA_DIA",
]
DEDUCT_DAY_KINDS = ("FALTA_INJUSTIFICADA", "PERMISO_SIN_GOCE", "TARDANZA_DIA")

ALERT_NEAR_DAYS = 5  # avisar cuando faltan 5 días o menos para el tope del empleador


def _d(value):
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _dates(start, end):
    cur, last = _d(start), _d(end)
    while cur <= last:
        yield cur
        cur += timedelta(days=1)


def employer_days_limit(params=None):
    return int((params or get_params())["incap_dias_empleador"])


def classify_dates(staff_id, year, params=None):
    """Clasifica cada día del año con incidencia: dict 'YYYY-MM-DD' ->
    {"kind", "payer", "incident_id"}. payer: EMPLEADOR (pagado), ESSALUD
    (lo cubre el subsidio) o SIN_PAGO (se descuenta)."""
    params = params or get_params()
    limit = employer_days_limit(params)
    first, last = f"{year}-01-01", f"{year}-12-31"
    rows = query_all(
        """SELECT id, kind, start_date, end_date FROM staff_incidents
           WHERE staff_id = ? AND end_date >= ? AND start_date <= ? AND kind != 'TARDANZA'
           ORDER BY start_date, id""",
        (staff_id, first, last),
    )
    best = {}  # fecha -> (prioridad, kind, incident_id)
    for r in rows:
        if r["kind"] not in DAY_PRIORITY:
            continue
        pr = DAY_PRIORITY.index(r["kind"])
        for day in _dates(r["start_date"], r["end_date"]):
            if day.year != year:
                continue
            key = day.strftime("%Y-%m-%d")
            if key not in best or pr < best[key][0]:
                best[key] = (pr, r["kind"], r["id"])
    out = {}
    medical_count = 0
    for key in sorted(best):
        _pr, kind, inc_id = best[key]
        if kind in MEDICAL_KINDS:
            medical_count += 1
            payer = "EMPLEADOR" if medical_count <= limit else "ESSALUD"
        elif kind == "LICENCIA_MATERNIDAD":
            payer = "ESSALUD"
        elif kind in DEDUCT_DAY_KINDS:
            payer = "SIN_PAGO"
        else:
            payer = "EMPLEADOR"
        out[key] = {"kind": kind, "payer": payer, "incident_id": inc_id}
    return out


def month_summary(staff_id, year, month, params=None):
    """Resumen de incidencias de una persona en un mes, para la planilla.
    Los días se cuentan en base 30 (el día 31 no cuenta)."""
    params = params or get_params()
    cls = classify_dates(staff_id, year, params)
    prefix = f"{year}-{month:02d}-"
    out = {
        "falta_dias": 0, "sin_goce_dias": 0, "tardanza_dia_dias": 0, "essalud_dias": 0,
        "pagados_dias": 0, "tardanza_min": 0.0,
    }
    for key, info in cls.items():
        if not key.startswith(prefix) or int(key[8:10]) > 30:
            continue
        kind = info["kind"]
        if kind == "FALTA_INJUSTIFICADA":
            out["falta_dias"] += 1
        elif kind == "PERMISO_SIN_GOCE":
            out["sin_goce_dias"] += 1
        elif kind == "TARDANZA_DIA":
            out["tardanza_dia_dias"] += 1
        elif info["payer"] == "ESSALUD":
            out["essalud_dias"] += 1
        else:
            out["pagados_dias"] += 1
    mrow = query_one(
        """SELECT COALESCE(SUM(minutes), 0) AS m FROM staff_incidents
           WHERE staff_id = ? AND kind = 'TARDANZA' AND start_date >= ? AND start_date <= ?""",
        (staff_id, f"{year}-{month:02d}-01", f"{year}-{month:02d}-31"),
    )
    out["tardanza_min"] = float(mrow["m"] or 0)
    out["descuento_dias"] = out["falta_dias"] + out["sin_goce_dias"] + out["tardanza_dia_dias"]
    return out


def medical_summary(staff_id, year, params=None):
    """Días de descanso médico / incapacidad del año y cuántos pasan del tope
    que paga el empleador."""
    params = params or get_params()
    limit = employer_days_limit(params)
    cls = classify_dates(staff_id, year, params)
    employer = sum(1 for i in cls.values() if i["kind"] in MEDICAL_KINDS and i["payer"] == "EMPLEADOR")
    essalud_days = sorted(k for k, i in cls.items() if i["kind"] in MEDICAL_KINDS and i["payer"] == "ESSALUD")
    total = employer + len(essalud_days)
    return {
        "total": total, "employer_days": employer, "essalud_days": len(essalud_days), "limit": limit,
        "first_essalud_date": essalud_days[0] if essalud_days else None,
        "remaining": max(limit - total, 0),
    }


def medical_alerts(year=None, params=None):
    """Personas activas cuyo descanso médico del año ya pasó (o está por
    pasar) el tope del empleador. Más urgentes primero."""
    year = year or date.today().year
    params = params or get_params()
    limit = employer_days_limit(params)
    out = []
    ids = query_all(
        """SELECT DISTINCT s.id, s.name FROM staff_incidents i JOIN staff s ON s.id = i.staff_id
           WHERE i.kind IN ('DESCANSO_MEDICO', 'INCAPACIDAD_TEMPORAL') AND s.status = 'ACTIVO'
           AND i.end_date >= ? AND i.start_date <= ?""",
        (f"{year}-01-01", f"{year}-12-31"),
    )
    for r in ids:
        m = medical_summary(r["id"], year, params)
        if m["total"] > limit:
            out.append({"staff_id": r["id"], "name": r["name"], "level": "EXCEDIDO", **m})
        elif m["total"] >= limit - ALERT_NEAR_DAYS:
            out.append({"staff_id": r["id"], "name": r["name"], "level": "CERCA", **m})
    out.sort(key=lambda a: (a["level"] != "EXCEDIDO", -a["total"]))
    return out


def last_12_months_remuneration(staff_id):
    """Remuneraciones de los últimos 12 meses (para el promedio del
    subsidio). None si todavía no hay planilla calculada de esta persona."""
    return None


def _days_between(start, end):
    return (_d(end) - _d(start)).days + 1


def _staff_or_404(staff_id):
    staff = query_one("SELECT * FROM staff WHERE id = ?", (staff_id,))
    if staff is None:
        abort(404)
    return staff


def _month_bounds(mes):
    try:
        y, m = int(mes[:4]), int(mes[5:7])
        first = date(y, m, 1)
    except (TypeError, ValueError):
        first = date.today().replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def _incident_summary_text(row):
    if row["kind"] == "TARDANZA":
        return f"{row['minutes']:g} min"
    return f"{_days_between(row['start_date'], row['end_date'])} día(s)"


@bp.route("")
@permission_required("pagos_personal", "view")
def index():
    first, last = _month_bounds(request.args.get("mes") or today_str()[:7])
    mes = first.strftime("%Y-%m")
    persona = request.args.get("persona", type=int)
    sql = """SELECT i.*, s.name AS staff_name FROM staff_incidents i JOIN staff s ON s.id = i.staff_id
             WHERE i.end_date >= ? AND i.start_date <= ?"""
    params = [first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")]
    if persona:
        sql += " AND i.staff_id = ?"
        params.append(persona)
    sql += " ORDER BY i.start_date DESC, i.id DESC"
    incidents = query_all(sql, params)
    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = (last + timedelta(days=1)).strftime("%Y-%m")
    people = query_all("SELECT id, name FROM staff WHERE status = 'ACTIVO' ORDER BY name")
    return render_template(
        "incidencias/index.html", incidents=incidents, mes=mes, prev_m=prev_m, next_m=next_m, people=people,
        persona=persona, kinds=KINDS, kind_labels=KIND_LABELS, essalud_kinds=ESSALUD_KINDS,
        subsidy_status=SUBSIDY_STATUS, alerts=medical_alerts(first.year), summary_text=_incident_summary_text,
        today=today_str(), year=first.year,
    )


@bp.route("/nueva", methods=["POST"])
@permission_required("pagos_personal", "edit")
def add():
    if not validate_csrf():
        abort(400)
    form = request.form
    staff = query_one("SELECT * FROM staff WHERE id = ?", (form.get("staff_id", type=int),))
    kind = form.get("kind", "")
    start = parse_date(form.get("start_date"))
    end = parse_date(form.get("end_date")) or start
    minutes = parse_float(form.get("minutes"), None)
    back = url_for("incidencias.index", mes=(start or today_str())[:7])
    error = None
    if staff is None:
        error = "Elige a la persona."
    elif kind not in KIND_LABELS:
        error = "Elige el tipo de incidencia."
    elif not start:
        error = "Escribe la fecha (o la fecha de inicio)."
    elif end < start:
        error = "La fecha de fin no puede ser anterior a la de inicio."
    elif kind == "TARDANZA":
        end = start
        if not minutes or minutes <= 0:
            error = "Escribe los minutos de tardanza."
    else:
        minutes = None
    filename = None
    file_storage = request.files.get("certificate_file")
    if not error and file_storage and file_storage.filename:
        filename = _save_binary_attachment(file_storage, storage.save_staff_document)
        if not filename:
            error = "El archivo no es válido: sube una foto o un PDF."
    if error:
        flash(error, "error")
        return redirect(back)
    user = getattr(g, "user", None)
    subsidy = "PENDIENTE" if kind in ESSALUD_KINDS else None
    inc_id = execute(
        """INSERT INTO staff_incidents (staff_id, kind, start_date, end_date, minutes, certificate_number, notes,
           filename, subsidy_status, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            staff["id"], kind, start, end, minutes, form.get("certificate_number", "").strip() or None,
            form.get("notes", "").strip() or None, filename, subsidy, user["id"] if user else None,
        ),
    )
    log_activity(
        "pagos_personal", "CREAR", f"Incidencia — {KIND_LABELS[kind]} ({start}): {staff['name']}",
        entity_type="empleado", entity_id=staff["id"], entity_url=url_for("incidencias.index", mes=start[:7], persona=staff["id"]),
    )
    flash("Incidencia registrada.", "success")
    if kind in MEDICAL_KINDS:
        m = medical_summary(staff["id"], _d(start).year)
        if m["total"] > m["limit"]:
            flash(
                f"{staff['name']} ya lleva {m['total']} días de descanso médico este año (el empleador paga {m['limit']}): "
                f"{m['essalud_days']} día(s) los cubre EsSalud. Genera el archivo para pedir el subsidio.",
                "info",
            )
    return redirect(back)


@bp.route("/<int:incident_id>/eliminar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def delete(incident_id):
    if not validate_csrf():
        abort(400)
    row = query_one(
        "SELECT i.*, s.name AS staff_name FROM staff_incidents i JOIN staff s ON s.id = i.staff_id WHERE i.id = ?",
        (incident_id,),
    )
    if row is None:
        abort(404)
    execute("DELETE FROM staff_incidents WHERE id = ?", (incident_id,))
    log_activity(
        "pagos_personal", "ELIMINAR", f"Incidencia — {KIND_LABELS.get(row['kind'], row['kind'])} ({row['start_date']}): {row['staff_name']}: eliminada",
        entity_type="empleado", entity_id=row["staff_id"],
    )
    flash("Incidencia eliminada.", "success")
    return redirect(url_for("incidencias.index", mes=row["start_date"][:7]))


@bp.route("/<int:incident_id>/archivo")
@permission_required("pagos_personal", "view")
def certificate_file(incident_id):
    row = query_one("SELECT filename FROM staff_incidents WHERE id = ?", (incident_id,))
    if row is None or not row["filename"]:
        abort(404)
    if storage.using_s3():
        return redirect(storage.staff_document_url(row["filename"]))
    return send_from_directory(storage.local_staff_documents_dir(), row["filename"])


@bp.route("/<int:incident_id>/subsidio", methods=["POST"])
@permission_required("pagos_personal", "edit")
def set_subsidy(incident_id):
    if not validate_csrf():
        abort(400)
    row = query_one("SELECT * FROM staff_incidents WHERE id = ?", (incident_id,))
    if row is None or row["kind"] not in ESSALUD_KINDS:
        abort(404)
    status = request.form.get("subsidy_status")
    if status not in SUBSIDY_STATUS:
        abort(400)
    execute("UPDATE staff_incidents SET subsidy_status = ? WHERE id = ?", (status, incident_id))
    flash(f"Subsidio marcado como: {SUBSIDY_STATUS[status].lower()}.", "success")
    return redirect(url_for("incidencias.index", mes=row["start_date"][:7]))


# --- Archivo para pedir el subsidio a EsSalud -------------------------------

def build_subsidy_workbook(staff, year):
    """Excel de trabajo con todo lo necesario para pedir el subsidio: datos
    del trabajador, las incidencias cubiertas por EsSalud (días y fechas),
    el promedio de remuneración y el subsidio estimado. No es el formato
    oficial de EsSalud: sirve para llenar su solicitud sin buscar datos."""
    params = get_params()
    cls = classify_dates(staff["id"], year, params)
    rows = query_all(
        """SELECT * FROM staff_incidents WHERE staff_id = ? AND kind IN ('DESCANSO_MEDICO', 'INCAPACIDAD_TEMPORAL', 'LICENCIA_MATERNIDAD')
           AND end_date >= ? AND start_date <= ? ORDER BY start_date, id""",
        (staff["id"], f"{year}-01-01", f"{year}-12-31"),
    )
    wb = Workbook()
    ws = wb.active
    ws.title = "Solicitud"
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="1D4ED8")
    white = Font(bold=True, color="FFFFFF")
    ws["A1"] = f"Solicitud de subsidio a EsSalud — {year}"
    ws["A1"].font = Font(bold=True, size=14, color="1D4ED8")
    ws["A2"] = "Archivo de trabajo para llenar la solicitud en EsSalud. Verifica los datos con tu agencia de EsSalud."
    ws["A2"].font = Font(italic=True, color="667085")
    info = [
        ("Empleador", staff["company"] or ""),
        ("Trabajador", staff["name"]),
        (staff["document_type"] or "Documento", staff["document_number"] or ""),
        ("Cargo", staff["position"] or ""),
        ("CUSPP", staff["cuspp"] or ""),
        ("Fecha de ingreso", staff["hire_date"] or ""),
        ("Régimen laboral", staff["labor_regime"] or ""),
    ]
    r = 4
    for label, value in info:
        ws.cell(row=r, column=1, value=label).font = bold
        ws.cell(row=r, column=2, value=value)
        r += 1
    r += 1
    heads = ["Tipo", "N° certificado (CITT)", "Desde", "Hasta", "Días totales", "Días que paga el empleador", "Días a cargo de EsSalud", "Primer día EsSalud", "Estado del subsidio"]
    for c, h in enumerate(heads, start=1):
        cell = ws.cell(row=r, column=c, value=h)
        cell.font, cell.fill = white, head_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    r += 1
    total_essalud = 0
    for inc in rows:
        mine = [(k, i) for k, i in cls.items() if i["incident_id"] == inc["id"]]
        ess = sorted(k for k, i in mine if i["payer"] == "ESSALUD")
        total_essalud += len(ess)
        vals = [
            KIND_LABELS.get(inc["kind"], inc["kind"]), inc["certificate_number"] or "", inc["start_date"], inc["end_date"],
            _days_between(inc["start_date"], inc["end_date"]), len(mine) - len(ess), len(ess), ess[0] if ess else "",
            SUBSIDY_STATUS.get(inc["subsidy_status"] or "", ""),
        ]
        for c, v in enumerate(vals, start=1):
            ws.cell(row=r, column=c, value=v)
        r += 1
    r += 1
    med = medical_summary(staff["id"], year, params)
    ws.cell(row=r, column=1, value=f"Descanso médico acumulado en {year}").font = bold
    ws.cell(row=r, column=2, value=f"{med['total']} días (el empleador paga hasta {med['limit']}; EsSalud desde el día {med['limit'] + 1}"
            + (f", que cae el {med['first_essalud_date']}" if med["first_essalud_date"] else "") + ")")
    r += 1
    ws.cell(row=r, column=1, value="Días a cargo de EsSalud (total)").font = bold
    ws.cell(row=r, column=2, value=total_essalud)
    r += 2
    ws.cell(row=r, column=1, value="Remuneración para el cálculo").font = bold
    r += 1
    history = last_12_months_remuneration(staff["id"])
    if history:
        total = sum(history)
        daily = total / 360
        ws.cell(row=r, column=1, value="Suma de los últimos 12 meses"); ws.cell(row=r, column=2, value=round(total, 2)); r += 1
    else:
        from app.routes.planilla import effective_salary

        base = effective_salary(staff)
        total, daily = base * 12, base * 12 / 360
        ws.cell(row=r, column=1, value="Aún no hay planilla calculada: se usa el sueldo básico x 12 como referencia")
        ws.cell(row=r, column=2, value=round(total, 2)); r += 1
    ws.cell(row=r, column=1, value="Promedio diario (÷ 360)"); ws.cell(row=r, column=2, value=round(daily, 2)); r += 1
    ws.cell(row=r, column=1, value="Subsidio estimado (días EsSalud x promedio diario)").font = bold
    ws.cell(row=r, column=2, value=round(daily * total_essalud, 2))
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 34
    for col in "CDEFGHI":
        ws.column_dimensions[col].width = 18

    ws2 = wb.create_sheet("Documentos")
    ws2["A1"] = "Documentos que suele pedir EsSalud (confirma la lista con tu agencia)"
    ws2["A1"].font = Font(bold=True, size=12, color="1D4ED8")
    docs = [
        "Certificado de incapacidad temporal para el trabajo (CITT) o certificado de descanso médico",
        "Copia del DNI del trabajador",
        "Solicitud de subsidio (formulario de EsSalud, se llena con los datos de la hoja anterior)",
        "Constancia o boletas de pago de los últimos meses (de la planilla del sistema)",
        "Para maternidad: certificado de descanso pre y post natal y, luego, copia del acta de nacimiento",
    ]
    for i, d in enumerate(docs, start=3):
        ws2.cell(row=i, column=1, value=f"☐ {d}")
    ws2.column_dimensions["A"].width = 110
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@bp.route("/subsidio/<int:staff_id>/<int:year>.xlsx")
@permission_required("pagos_personal", "view")
def subsidy_file(staff_id, year):
    staff = _staff_or_404(staff_id)
    if year < 2000 or year > 2100:
        abort(404)
    data = build_subsidy_workbook(staff, year)
    safe = "".join(c if c.isalnum() else "_" for c in staff["name"])[:40]
    log_activity(
        "pagos_personal", "EXPORTAR", f"Archivo de subsidio EsSalud {year}: {staff['name']}",
        entity_type="empleado", entity_id=staff_id,
    )
    return Response(
        data, mimetype=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="subsidio_essalud_{year}_{safe}.xlsx"'},
    )


# --- Importar desde Excel --------------------------------------------------

_KIND_ALIASES = {
    "TARDANZA": ["tardanza", "tardanzas", "tarde"],
    "TARDANZA_DIA": ["tardanza dia", "tardanza 1 dia", "tardanza por dia"],
    "FALTA_INJUSTIFICADA": ["falta", "inasistencia", "falta injustificada", "inasistencia injustificada"],
    "PERMISO_SIN_GOCE": ["permiso sin goce", "sin goce", "falta con permiso", "permiso"],
    "PERMISO_CON_GOCE": ["permiso con goce", "con goce"],
    "LICENCIA_MATERNIDAD": ["maternidad", "licencia maternidad"],
    "LICENCIA_PATERNIDAD": ["paternidad", "licencia paternidad"],
    "LICENCIA_FALLECIMIENTO": ["fallecimiento", "licencia fallecimiento", "duelo"],
    "DESCANSO_MEDICO": ["descanso medico", "descanso", "dm"],
    "INCAPACIDAD_TEMPORAL": ["incapacidad", "incapacidad temporal", "citt"],
}
INCIDENT_COLUMNS = [
    ImportColumn("document", "DNI", kind="text", required=True, width=14,
                 note="DNI (o documento) de la persona, como figura en el Catálogo de Personal."),
    ImportColumn("name", "Nombre", kind="text", width=28, note="Opcional, solo para que lo leas tú; se usa el DNI."),
    ImportColumn("kind", "Tipo", kind="choice", required=True, width=26,
                 choices=[(k, _KIND_ALIASES[k]) for k, _l, _g, _m in KINDS],
                 note="Elige de la lista: " + ", ".join(k for k, _l, _g, _m in KINDS) + "."),
    ImportColumn("start_date", "Fecha de inicio", kind="date", required=True, width=16, note="AAAA-MM-DD o DD/MM/AAAA. Para una tardanza, el día."),
    ImportColumn("end_date", "Fecha de fin", kind="date", width=16, note="Opcional: si se deja vacía, es de un solo día."),
    ImportColumn("minutes", "Minutos", kind="float", width=12, note="Solo para TARDANZA: minutos de retraso."),
    ImportColumn("certificate", "N° certificado", kind="text", width=18, note="Opcional (CITT / certificado médico)."),
    ImportColumn("notes", "Observaciones", kind="text", width=30),
]
INCIDENT_EXAMPLE = {
    "document": "00000000", "name": "Nombre de Ejemplo", "kind": "FALTA_INJUSTIFICADA", "start_date": "2026-10-01",
    "end_date": "2026-10-01", "minutes": "", "certificate": "", "notes": "Fila de ejemplo — bórrala o sobrescríbela",
}


@bp.route("/importar/plantilla")
@permission_required("pagos_personal", "edit")
def import_template():
    buf = build_import_template("Incidencias de asistencia", INCIDENT_COLUMNS, INCIDENT_EXAMPLE)
    return Response(
        buf.getvalue(), mimetype=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="plantilla_incidencias.xlsx"'},
    )


def _apply_incident_import(rows, example_skips):
    created, errors = 0, []
    skipped = [{"row": r, "message": "Fila de ejemplo de la plantilla; se omitió automáticamente."} for r in example_skips]
    seen = set()
    user = getattr(g, "user", None)
    for row in rows:
        n = row["_row_number"]
        for warn in row["_warnings"]:
            errors.append({"row": n, "message": warn})
        doc = (row.get("document") or "").strip()
        staff = query_one("SELECT * FROM staff WHERE document_number = ?", (doc,)) if doc else None
        if staff is None:
            errors.append({"row": n, "message": f"No se encontró ninguna persona con DNI \"{doc or '(vacío)'}\"; la fila no se importó."})
            continue
        kind, start = row.get("kind"), row.get("start_date")
        end = row.get("end_date") or start
        if not kind:
            errors.append({"row": n, "message": "Falta el tipo de incidencia (o no se reconoció); la fila no se importó."})
            continue
        if not start:
            errors.append({"row": n, "message": "Falta la fecha de inicio; la fila no se importó."})
            continue
        if end < start:
            errors.append({"row": n, "message": "La fecha de fin es anterior a la de inicio; la fila no se importó."})
            continue
        minutes = row.get("minutes")
        if kind == "TARDANZA":
            end = start
            if not minutes or minutes <= 0:
                errors.append({"row": n, "message": "Una tardanza necesita los minutos; la fila no se importó."})
                continue
        else:
            minutes = None
        key = (staff["id"], kind, start, end, minutes)
        if key in seen or query_one(
            "SELECT id FROM staff_incidents WHERE staff_id = ? AND kind = ? AND start_date = ? AND end_date = ?",
            (staff["id"], kind, start, end),
        ):
            skipped.append({"row": n, "message": f"{staff['name']} ya tiene esa incidencia ({KIND_LABELS[kind]}, {start}); no se duplicó."})
            continue
        seen.add(key)
        inc_id = execute(
            """INSERT INTO staff_incidents (staff_id, kind, start_date, end_date, minutes, certificate_number, notes,
               subsidy_status, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (staff["id"], kind, start, end, minutes, (row.get("certificate") or "").strip() or None,
             (row.get("notes") or "").strip() or None, "PENDIENTE" if kind in ESSALUD_KINDS else None,
             user["id"] if user else None),
        )
        log_activity(
            "pagos_personal", "CREAR", f"Incidencia — {KIND_LABELS[kind]} ({start}): {staff['name']} — importada desde Excel",
            entity_type="empleado", entity_id=staff["id"],
        )
        created += 1
    return {"created": created, "updated": 0, "skipped": skipped, "errors": errors}


@bp.route("/importar", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def import_incidents():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        rows, file_error, example_skips = read_import_rows(request.files.get("file"), INCIDENT_COLUMNS, INCIDENT_EXAMPLE)
        if file_error:
            flash(file_error, "error")
            return redirect(url_for("incidencias.import_incidents"))
        result = _apply_incident_import(rows, example_skips)
        return render_template(
            "import_result.html", result=result, back_url=url_for("incidencias.index"),
            retry_url=url_for("incidencias.import_incidents"),
        )
    return render_template(
        "import_form.html", title="Importar incidencias de asistencia", module_label="las inasistencias, tardanzas y licencias",
        template_url=url_for("incidencias.import_template"), upload_url=url_for("incidencias.import_incidents"),
        back_url=url_for("incidencias.index"), columns=INCIDENT_COLUMNS,
    )
