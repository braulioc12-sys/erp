"""Legajo digital del personal (6 oct, pedido de Braulio: "conoces la
plataforma Buk? puedes hacer algo similar?" -- fase 1 de 3: legajo y
contratos; luego asistencia/vacaciones y por último planillas).

Se cuelga del catálogo de Personal que ya existe (tabla `staff`, módulo
"Pagos personal"/RRHH -- mismo permiso `pagos_personal`): cada persona tiene
una ficha con

1. Datos personales y laborales (fecha de ingreso, cese, nacimiento,
   teléfono, correo, dirección, área) -- columnas nuevas de `staff`.
2. Historial de contratos (`staff_contracts`): tipo, inicio, fin, cargo,
   sueldo y el archivo del contrato firmado. El más reciente es el vigente.
3. Documentos (`staff_documents`): DNI, CV, examen médico, SCTR,
   antecedentes, certificados... con vencimiento opcional.

Igual que con las guías de los viajes: subir un contrato o documento nuevo
SIEMPRE agrega uno más, nunca reemplaza al anterior, y se puede eliminar uno
subido por error. La pantalla "Vencimientos" junta lo que vence pronto (o ya
venció): contratos a plazo fijo y documentos con fecha de vencimiento.
"""
import re
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from app import storage
from app.audit import log_activity
from app.auth import permission_required, validate_csrf
from app.bulk_import import XLSX_MIME, ImportColumn, build_import_template, read_import_rows
from app.db import execute, query_all, query_one
from app.helpers import parse_date, parse_float, today_str
from app.routes.viajes import _save_binary_attachment

bp = Blueprint("legajo", __name__, url_prefix="/legajo")

CONTRACT_TYPES = [
    ("INDETERMINADO", "Plazo indeterminado"),
    ("PLAZO_FIJO", "Plazo fijo / sujeto a modalidad"),
    ("TIEMPO_PARCIAL", "Tiempo parcial"),
    ("PRACTICAS", "Prácticas pre/profesionales"),
    ("LOCACION", "Locación de servicios (honorarios)"),
    ("OTRO", "Otro"),
]
CONTRACT_TYPE_LABELS = dict(CONTRACT_TYPES)

DOCUMENT_TYPES = [
    ("DNI", "DNI / documento de identidad"),
    ("CV", "Currículum vitae"),
    ("ANTECEDENTES", "Antecedentes policiales / penales"),
    ("EXAMEN_MEDICO", "Examen médico ocupacional"),
    ("SCTR", "SCTR / seguro"),
    ("LICENCIA", "Licencia de conducir (brevete)"),
    ("CERTIFICADO", "Certificado / estudios / capacitación"),
    ("OTRO", "Otro"),
]
DOCUMENT_TYPE_LABELS = dict(DOCUMENT_TYPES)

# Mismo umbral que los documentos de conductores y unidades (30 días).
ALERT_DAYS = 30


# 6 oct, pedido de Braulio ("en los documentos de RRHH, en el caso de brevete
# y dni no puedes jalar los que ya se subieron en conductores?"): si la
# persona es también conductor, su brevete, DNI y examen médico que ya están
# en Conductores se MUESTRAN en el legajo (no se copian: así hay un solo
# archivo y una sola fecha de vencimiento, y si se actualiza en Conductores el
# legajo ya lo ve). (clave en la URL, columna del archivo en `drivers`, tipo de
# documento del legajo, columna de vencimiento o None).
DRIVER_LEGAJO_DOCS = [
    ("brevete", "license_filename", "LICENCIA", "license_expiry"),
    ("dni", "dni_filename", "DNI", None),
    ("examen-medico", "medical_exam_filename", "EXAMEN_MEDICO", "medical_exam_expiry"),
]
_DRIVER_DOC_BY_KEY = {d[0]: d for d in DRIVER_LEGAJO_DOCS}


def linked_driver(staff):
    """El conductor de esta persona: el enlazado en Personal (driver_id) o, si
    no hay enlace, el que tenga el mismo número de DNI."""
    if staff["driver_id"]:
        row = query_one("SELECT * FROM drivers WHERE id = ?", (staff["driver_id"],))
        if row:
            return row
    number = (staff["document_number"] or "").strip()
    if number and (staff["document_type"] or "DNI") == "DNI":
        return query_one("SELECT * FROM drivers WHERE TRIM(document_number) = ? ORDER BY id LIMIT 1", (number,))
    return None


def driver_documents_for(staff):
    """(conductor, [documentos]) con los archivos que el conductor ya tiene
    subidos en Conductores y que el legajo puede mostrar."""
    driver = linked_driver(staff)
    docs = []
    if driver:
        for key, column, doc_type, expiry_col in DRIVER_LEGAJO_DOCS:
            if driver[column]:
                docs.append({
                    "key": key, "doc_type": doc_type, "label": DOCUMENT_TYPE_LABELS[doc_type],
                    "expiry": driver[expiry_col] if expiry_col else None,
                })
    return driver, docs


def _get_staff_or_404(staff_id):
    staff = query_one("SELECT * FROM staff WHERE id = ?", (staff_id,))
    if staff is None:
        abort(404)
    return staff


def _save_staff_file(file_storage):
    return _save_binary_attachment(file_storage, storage.save_staff_document)


def _alert_limit():
    return (datetime.now() + timedelta(days=ALERT_DAYS)).strftime("%Y-%m-%d")


def _expiry_state(expiry):
    """None si no vence / está lejos, 'VENCIDO' o 'POR_VENCER'."""
    if not expiry:
        return None
    if expiry < today_str():
        return "VENCIDO"
    if expiry <= _alert_limit():
        return "POR_VENCER"
    return None


def current_contract(staff_id):
    """Contrato vigente = el de inicio más reciente."""
    return query_one(
        "SELECT * FROM staff_contracts WHERE staff_id = ? ORDER BY start_date DESC, id DESC LIMIT 1", (staff_id,)
    )


def legajo_alerts():
    """Contratos (el vigente de cada persona activa) y documentos que
    vencen dentro de ALERT_DAYS días o ya vencieron. Una lista de
    {staff_id, name, kind, label, expiry, overdue}, la más urgente primero."""
    limit = _alert_limit()
    today = today_str()
    alerts = []
    rows = query_all(
        """SELECT s.id AS staff_id, s.name, c.contract_type, c.end_date AS expiry
           FROM staff s
           JOIN staff_contracts c ON c.id = (
                SELECT c2.id FROM staff_contracts c2 WHERE c2.staff_id = s.id
                ORDER BY c2.start_date DESC, c2.id DESC LIMIT 1)
           WHERE s.status = 'ACTIVO' AND COALESCE(c.end_date, '') != '' AND c.end_date <= ?""",
        (limit,),
    )
    for r in rows:
        alerts.append({
            "staff_id": r["staff_id"], "name": r["name"], "kind": "Contrato",
            "label": CONTRACT_TYPE_LABELS.get(r["contract_type"], r["contract_type"]),
            "expiry": r["expiry"], "overdue": r["expiry"] < today,
        })
    rows = query_all(
        """SELECT s.id AS staff_id, s.name, d.doc_type, d.title, d.expiry_date AS expiry
           FROM staff_documents d JOIN staff s ON s.id = d.staff_id
           WHERE s.status = 'ACTIVO' AND COALESCE(d.expiry_date, '') != '' AND d.expiry_date <= ?""",
        (limit,),
    )
    for r in rows:
        label = DOCUMENT_TYPE_LABELS.get(r["doc_type"], r["doc_type"])
        if r["title"]:
            label = f"{label} — {r['title']}"
        alerts.append({
            "staff_id": r["staff_id"], "name": r["name"], "kind": "Documento", "label": label,
            "expiry": r["expiry"], "overdue": r["expiry"] < today,
        })
    alerts.sort(key=lambda a: (a["expiry"], a["name"]))
    return alerts


@bp.route("")
@permission_required("pagos_personal", "view")
def index():
    """Lista de colaboradores (activos por defecto) con su contrato vigente,
    cantidad de documentos y si algo suyo vence pronto."""
    show_inactive = request.args.get("ver") == "inactivos"
    q = request.args.get("q", "").strip()
    sql = "SELECT s.* FROM staff s WHERE s.status = ?"
    params = ["INACTIVO" if show_inactive else "ACTIVO"]
    if q:
        sql += " AND (LOWER(s.name) LIKE LOWER(?) OR LOWER(COALESCE(s.document_number, '')) LIKE LOWER(?))"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY s.name"
    people = query_all(sql, params)
    alerts = legajo_alerts()
    alerts_by_staff = {}
    for a in alerts:
        alerts_by_staff.setdefault(a["staff_id"], []).append(a)
    rows = []
    for p in people:
        contract = current_contract(p["id"])
        n_docs = query_one("SELECT COUNT(*) AS n FROM staff_documents WHERE staff_id = ?", (p["id"],))["n"]
        rows.append({
            "staff": p, "contract": contract, "n_docs": n_docs,
            "alerts": alerts_by_staff.get(p["id"], []),
        })
    return render_template(
        "legajo/index.html", rows=rows, q=q, show_inactive=show_inactive, alerts=alerts,
        contract_type_labels=CONTRACT_TYPE_LABELS,
    )


# --- Importación masiva del personal desde Excel (7 oct, pedido de Braulio:
# "crear la opción de importar masivamente a través de un Excel el personal, y
# que haya una columna Empresa donde se registre la empresa que lo contrata y
# a la que pertenece la planilla"). Mismo motor que Flota/Conductores/Rutas
# (app/bulk_import.py). Crea a la persona o, si ya existe (mismo documento, o
# mismo nombre si la que está cargada no tiene documento), la ACTUALIZA: solo
# se pisan los campos que vienen llenos en el Excel -- una celda vacía nunca
# borra un dato ya cargado. ---

_YES_NO_CHOICES = [("SI", ["SÍ", "SI", "S", "1", "X", "YES", "Y"]), ("NO", ["NO", "N", "0"])]

_STAFF_IMPORT_EXAMPLE = {
    "name": "Nombre de Ejemplo", "document_type": "DNI", "document_number": "00000000", "company": "",
    "position": "Asistente administrativo", "area": "Administración", "hire_date": "2026-01-15",
    "birth_date": "1990-05-20", "phone": "999999999", "email": "ejemplo@correo.com", "address": "Av. Ejemplo 123, Lima",
    "in_payroll": "SI", "labor_regime": "GENERAL", "basic_salary": 1500, "family_allowance": "NO",
    "pension_system": "AFP", "afp_name": "PRIMA", "cuspp": "", "bank_name": "BCP",
    "account_number": "1931234567890", "cci": "",
}


def _company_choices():
    """Empresas activas del sistema como opciones de la columna Empresa: se
    acepta el nombre, la razón social o el RUC."""
    choices = []
    for c in query_all("SELECT name, legal_name, ruc FROM companies WHERE active = 1 ORDER BY name"):
        aliases = [a for a in (c["legal_name"], c["ruc"]) if a and a.strip()]
        choices.append((c["name"], aliases))
    return choices


def staff_import_columns():
    from app.payroll_params import AFP_NAMES
    from app.routes.planilla import PENSION_SYSTEMS, REGIMES

    companies = _company_choices()
    names = ", ".join(c for c, _ in companies) or "(aún no hay empresas creadas)"
    return [
        ImportColumn("name", "Nombre", kind="text", required=True, width=30,
                     note="Apellidos y nombres. Se usa para emparejar con una persona ya cargada cuando no se da N° de documento."),
        ImportColumn("document_type", "Tipo de documento", kind="choice", width=16,
                     choices=[("DNI", ["DNI"]), ("CE", ["CE", "CARNET DE EXTRANJERIA", "CARNÉ DE EXTRANJERÍA"]), ("RUC", ["RUC"])],
                     note="Si se deja vacío, se usa DNI."),
        ImportColumn("document_number", "N° de documento", kind="text", width=16, force_text=True,
                     note="Si ya existe una persona con este documento, se actualizan sus datos en vez de crear otra."),
        ImportColumn("company", "Empresa", kind="choice", width=26, choices=companies,
                     note="Empresa que contrata a la persona y a cuya planilla pertenece (se acepta el nombre, la razón social "
                          "o el RUC). Debe estar creada en el sistema. Empresas disponibles: " + names + "."),
        ImportColumn("position", "Cargo", kind="text", width=24),
        ImportColumn("area", "Área", kind="text", width=20),
        ImportColumn("hire_date", "Fecha de ingreso", kind="date", width=16),
        ImportColumn("birth_date", "Fecha de nacimiento", kind="date", width=18),
        ImportColumn("phone", "Teléfono", kind="text", width=14, force_text=True),
        ImportColumn("email", "Correo", kind="text", width=26),
        ImportColumn("address", "Dirección", kind="text", width=32),
        ImportColumn("in_payroll", "En planilla", kind="choice", width=12, choices=_YES_NO_CHOICES,
                     note="SI = entra en la planilla mensual (requiere Empresa). NO = no entra (ej. recibo por honorarios). "
                          "Si se deja vacío, no se cambia."),
        ImportColumn("labor_regime", "Régimen laboral", kind="choice", width=22,
                     choices=[(code, [label]) for code, label in REGIMES],
                     note="Define vacaciones, CTS y gratificación. Se puede escribir el código o el nombre."),
        ImportColumn("basic_salary", "Sueldo básico", kind="float", width=14),
        ImportColumn("family_allowance", "Asignación familiar", kind="choice", width=18, choices=_YES_NO_CHOICES),
        ImportColumn("pension_system", "Sistema de pensiones", kind="choice", width=20,
                     choices=[(code, [label]) for code, label in PENSION_SYSTEMS],
                     note="Si se llena la AFP y esta columna está vacía, se toma como AFP."),
        ImportColumn("afp_name", "AFP", kind="choice", width=16,
                     choices=[(code, [label]) for code, label in AFP_NAMES],
                     note="Solo si el sistema de pensiones es AFP."),
        ImportColumn("cuspp", "CUSPP", kind="text", width=16),
        ImportColumn("bank_name", "Banco", kind="text", width=16),
        ImportColumn("account_number", "N° de cuenta", kind="text", width=22, force_text=True),
        ImportColumn("cci", "CCI (cuenta interbancaria)", kind="text", width=24, force_text=True),
    ]


def _clean_code(value):
    """Documento/teléfono/cuenta como texto limpio: un número que Excel
    guardó como 45270106.0 vuelve a 45270106."""
    text = (value or "").strip() if isinstance(value, str) else ("" if value is None else str(value).strip())
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".")[0]
    return text


def _find_staff_for_import(name, document_number):
    """Por documento si viene uno; si no (o no está), por nombre exacto SOLO
    si la persona cargada no tiene documento (así dos personas distintas con
    el mismo nombre y distinto DNI nunca se confunden)."""
    if document_number:
        row = query_one("SELECT * FROM staff WHERE TRIM(document_number) = ?", (document_number,))
        if row:
            return row
    row = query_one(
        "SELECT * FROM staff WHERE LOWER(TRIM(name)) = LOWER(?) AND COALESCE(TRIM(document_number), '') = '' ORDER BY id LIMIT 1",
        (name.strip(),),
    )
    if row:
        return row
    if not document_number:
        return query_one("SELECT * FROM staff WHERE LOWER(TRIM(name)) = LOWER(?) ORDER BY id LIMIT 1", (name.strip(),))
    return None


def _apply_staff_import(rows, example_skips):
    from app.routes.pagos_personal import _has_valid_document_number
    from app.routes.planilla import REGIME_VACATION_DAYS

    created, updated, errors = 0, 0, []
    skipped = [
        {"row": r, "message": "Fila de ejemplo de la plantilla; se omitió automáticamente."} for r in example_skips
    ]
    for row in rows:
        n = row["_row_number"]
        for warn in row["_warnings"]:
            errors.append({"row": n, "message": warn})
        name = (row.get("name") or "").strip()
        if not name:
            errors.append({"row": n, "message": "Falta el nombre; la fila no se importó."})
            continue
        doc_type = row.get("document_type") or None
        doc_number = _clean_code(row.get("document_number"))
        existing = _find_staff_for_import(name, doc_number)
        if doc_number and not _has_valid_document_number(doc_type or (existing["document_type"] if existing else "DNI"), doc_number):
            errors.append({"row": n, "message": "El DNI debe tener 8 dígitos; se importó igual, pero corrígelo antes de pagar por Telecrédito."})

        vals = {}
        if doc_type:
            vals["document_type"] = doc_type
        if doc_number:
            vals["document_number"] = doc_number
        for key in ("position", "area", "email", "address", "bank_name", "cuspp", "hire_date", "birth_date"):
            if row.get(key):
                vals[key] = row[key]
        for key in ("phone", "account_number", "cci"):
            clean = _clean_code(row.get(key))
            if clean:
                vals[key] = clean

        company = None
        if row.get("company"):
            company = query_one("SELECT id, name FROM companies WHERE active = 1 AND LOWER(name) = LOWER(?)", (row["company"],))
            if company:
                vals["company_id"], vals["company"] = company["id"], company["name"]
        has_company = bool(company) or bool(existing and existing["company_id"])

        regime = row.get("labor_regime")
        if regime:
            vals["labor_regime"] = regime
            if regime in REGIME_VACATION_DAYS and (existing is None or existing["labor_regime"] != regime):
                vals["vacation_days_per_year"] = REGIME_VACATION_DAYS[regime]

        salary = row.get("basic_salary")
        if salary is not None:
            if salary < 0:
                errors.append({"row": n, "message": "El sueldo básico no puede ser negativo; se dejó sin cambiar."})
            else:
                vals["basic_salary"] = salary

        if row.get("family_allowance"):
            vals["family_allowance"] = 1 if row["family_allowance"] == "SI" else 0

        pension = row.get("pension_system") or ("AFP" if row.get("afp_name") else None)
        if pension == "AFP":
            afp = row.get("afp_name") or (existing["afp_name"] if existing else None)
            if afp:
                vals["pension_system"], vals["afp_name"] = "AFP", afp
            else:
                errors.append({"row": n, "message": "Sistema de pensiones AFP sin indicar cuál AFP; no se cambió el sistema de pensiones."})
        elif pension:
            vals["pension_system"], vals["afp_name"] = pension, None

        if row.get("in_payroll") == "SI":
            if has_company:
                vals["in_payroll"] = 1
            else:
                errors.append({"row": n, "message": "Para entrar en planilla necesita Empresa; no se marcó \"En planilla\"."})
        elif row.get("in_payroll") == "NO":
            vals["in_payroll"] = 0

        if existing:
            if vals:
                sets = ", ".join(f"{k} = ?" for k in vals)
                execute(f"UPDATE staff SET {sets} WHERE id = ?", list(vals.values()) + [existing["id"]])
            updated += 1
        else:
            cols = ["name"] + list(vals)
            execute(
                f"INSERT INTO staff ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [name] + list(vals.values()),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


@bp.route("/importar/plantilla")
@permission_required("pagos_personal", "edit")
def import_template():
    buffer = build_import_template("Personal", staff_import_columns(), _staff_import_example())
    return Response(
        buffer.getvalue(), mimetype=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="plantilla_personal.xlsx"'},
    )


def _staff_import_example():
    example = dict(_STAFF_IMPORT_EXAMPLE)
    first = query_one("SELECT name FROM companies WHERE active = 1 ORDER BY name LIMIT 1")
    example["company"] = first["name"] if first else ""
    return example


@bp.route("/importar", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def import_staff():
    columns = staff_import_columns()
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        rows, file_error, example_skips = read_import_rows(request.files.get("file"), columns, _staff_import_example())
        if file_error:
            flash(file_error, "error")
            return redirect(url_for("legajo.import_staff"))
        result = _apply_staff_import(rows, example_skips)
        if result["created"] or result["updated"]:
            log_activity(
                "pagos_personal", "SUBIR",
                f"Importó personal desde Excel ({result['created']} creado(s), {result['updated']} actualizado(s))",
                entity_type="empleado",
            )
        return render_template(
            "import_result.html", result=result,
            back_url=url_for("legajo.index"), retry_url=url_for("legajo.import_staff"),
        )
    return render_template(
        "import_form.html", title="Importar personal desde Excel", module_label="el personal",
        template_url=url_for("legajo.import_template"), upload_url=url_for("legajo.import_staff"),
        back_url=url_for("legajo.index"), columns=columns,
        extra_note="Si la persona ya existe (mismo N° de documento), se actualizan solo los datos que traiga el Excel; "
                   "las celdas vacías no borran nada. La columna Empresa asigna la empresa que contrata a la persona y "
                   "a cuya planilla pertenece.",
    )


@bp.route("/<int:staff_id>")
@permission_required("pagos_personal", "view")
def detail(staff_id):
    staff = _get_staff_or_404(staff_id)
    contracts = query_all(
        """SELECT c.*, u.name AS created_by_name FROM staff_contracts c LEFT JOIN users u ON u.id = c.created_by
           WHERE c.staff_id = ? ORDER BY c.start_date DESC, c.id DESC""",
        (staff_id,),
    )
    documents = query_all(
        """SELECT d.*, u.name AS created_by_name FROM staff_documents d LEFT JOIN users u ON u.id = d.created_by
           WHERE d.staff_id = ? ORDER BY d.doc_type, d.id""",
        (staff_id,),
    )
    driver, driver_docs = driver_documents_for(staff)
    return render_template(
        "legajo/detail.html", staff=staff, contracts=contracts, documents=documents, driver=driver, driver_docs=driver_docs,
        contract_types=CONTRACT_TYPES, contract_type_labels=CONTRACT_TYPE_LABELS,
        document_types=DOCUMENT_TYPES, document_type_labels=DOCUMENT_TYPE_LABELS,
        expiry_state=_expiry_state, current_contract_id=(contracts[0]["id"] if contracts else None),
        today=today_str(),
    )


@bp.route("/<int:staff_id>/datos", methods=["POST"])
@permission_required("pagos_personal", "edit")
def save_data(staff_id):
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    form = request.form
    execute(
        """UPDATE staff SET hire_date = ?, termination_date = ?, birth_date = ?, phone = ?, email = ?,
           address = ?, area = ? WHERE id = ?""",
        (
            parse_date(form.get("hire_date")), parse_date(form.get("termination_date")),
            parse_date(form.get("birth_date")), form.get("phone", "").strip() or None,
            form.get("email", "").strip() or None, form.get("address", "").strip() or None,
            form.get("area", "").strip() or None, staff_id,
        ),
    )
    log_activity(
        "pagos_personal", "EDITAR", f"Legajo (datos laborales) de {staff['name']}",
        entity_type="empleado", entity_id=staff_id, entity_url=url_for("legajo.detail", staff_id=staff_id),
    )
    flash("Datos del legajo guardados.", "success")
    return redirect(url_for("legajo.detail", staff_id=staff_id))


# --- Contratos ---------------------------------------------------------------

@bp.route("/<int:staff_id>/contratos", methods=["POST"])
@permission_required("pagos_personal", "edit")
def add_contract(staff_id):
    """AGREGA un contrato al historial (nunca reemplaza a los anteriores)."""
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    form = request.form
    contract_type = form.get("contract_type", "")
    start_date = parse_date(form.get("start_date"))
    end_date = parse_date(form.get("end_date"))
    if contract_type not in CONTRACT_TYPE_LABELS:
        flash("Elige el tipo de contrato.", "error")
    elif not start_date:
        flash("La fecha de inicio del contrato es obligatoria.", "error")
    elif end_date and end_date < start_date:
        flash("La fecha de fin no puede ser anterior a la de inicio.", "error")
    else:
        filename = _save_staff_file(request.files.get("contract_file"))
        user = getattr(g, "user", None)
        execute(
            """INSERT INTO staff_contracts (staff_id, contract_type, start_date, end_date, position, salary, notes,
               filename, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                staff_id, contract_type, start_date, end_date, form.get("position", "").strip() or None,
                parse_float(form.get("salary"), None), form.get("notes", "").strip() or None, filename,
                user["id"] if user else None,
            ),
        )
        log_activity(
            "pagos_personal", "SUBIR",
            f"Contrato ({CONTRACT_TYPE_LABELS[contract_type]}, desde {start_date}) de {staff['name']}",
            entity_type="empleado", entity_id=staff_id, entity_url=url_for("legajo.detail", staff_id=staff_id),
        )
        flash("Contrato agregado al legajo.", "success")
    return redirect(url_for("legajo.detail", staff_id=staff_id))


@bp.route("/<int:staff_id>/contratos/<int:contract_id>/archivo")
@permission_required("pagos_personal", "view")
def contract_file(staff_id, contract_id):
    row = query_one(
        "SELECT filename FROM staff_contracts WHERE id = ? AND staff_id = ?", (contract_id, staff_id)
    )
    return _serve(row)


@bp.route("/<int:staff_id>/contratos/<int:contract_id>/eliminar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def delete_contract(staff_id, contract_id):
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    row = query_one("SELECT * FROM staff_contracts WHERE id = ? AND staff_id = ?", (contract_id, staff_id))
    if row is None:
        abort(404)
    execute("DELETE FROM staff_contracts WHERE id = ?", (contract_id,))
    log_activity(
        "pagos_personal", "ELIMINAR",
        f"Contrato ({CONTRACT_TYPE_LABELS.get(row['contract_type'], row['contract_type'])}, desde "
        f"{row['start_date']}) de {staff['name']}: eliminado",
        entity_type="empleado", entity_id=staff_id, entity_url=url_for("legajo.detail", staff_id=staff_id),
    )
    flash("Contrato eliminado.", "success")
    return redirect(url_for("legajo.detail", staff_id=staff_id))


# --- Documentos --------------------------------------------------------------

@bp.route("/<int:staff_id>/documentos", methods=["POST"])
@permission_required("pagos_personal", "edit")
def add_document(staff_id):
    """AGREGA un documento al legajo (nunca reemplaza a los anteriores)."""
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    form = request.form
    doc_type = form.get("doc_type", "")
    title = form.get("title", "").strip() or None
    issue_date = parse_date(form.get("issue_date"))
    expiry_date = parse_date(form.get("expiry_date"))
    file_storage = request.files.get("document_file")
    if doc_type not in DOCUMENT_TYPE_LABELS:
        flash("Elige el tipo de documento.", "error")
    elif not (file_storage and file_storage.filename):
        flash("Adjunta el archivo del documento (foto o PDF).", "error")
    else:
        filename = _save_staff_file(file_storage)
        if not filename:
            flash("El archivo no es válido: sube una foto o un PDF.", "error")
        else:
            user = getattr(g, "user", None)
            execute(
                """INSERT INTO staff_documents (staff_id, doc_type, title, issue_date, expiry_date, filename, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (staff_id, doc_type, title, issue_date, expiry_date, filename, user["id"] if user else None),
            )
            log_activity(
                "pagos_personal", "SUBIR",
                f"Documento ({DOCUMENT_TYPE_LABELS[doc_type]}) de {staff['name']}",
                entity_type="empleado", entity_id=staff_id, entity_url=url_for("legajo.detail", staff_id=staff_id),
            )
            flash("Documento agregado al legajo.", "success")
    return redirect(url_for("legajo.detail", staff_id=staff_id))


@bp.route("/<int:staff_id>/documentos/<int:document_id>/archivo")
@permission_required("pagos_personal", "view")
def document_file(staff_id, document_id):
    row = query_one(
        "SELECT filename FROM staff_documents WHERE id = ? AND staff_id = ?", (document_id, staff_id)
    )
    return _serve(row)


@bp.route("/<int:staff_id>/documentos/conductor/<key>")
@permission_required("pagos_personal", "view")
def driver_document_file(staff_id, key):
    """Sirve el archivo (brevete / DNI / examen médico) que el conductor
    ligado a esta persona ya subió en Conductores — con el permiso de RRHH,
    para que no haga falta tener acceso al módulo Conductores."""
    staff = _get_staff_or_404(staff_id)
    spec = _DRIVER_DOC_BY_KEY.get(key)
    driver = linked_driver(staff)
    if spec is None or driver is None or not driver[spec[1]]:
        abort(404)
    filename = driver[spec[1]]
    if storage.using_s3():
        return redirect(storage.driver_document_url(filename))
    return send_from_directory(storage.local_driver_documents_dir(), filename)


@bp.route("/<int:staff_id>/documentos/<int:document_id>/eliminar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def delete_document(staff_id, document_id):
    if not validate_csrf():
        abort(400)
    staff = _get_staff_or_404(staff_id)
    row = query_one("SELECT * FROM staff_documents WHERE id = ? AND staff_id = ?", (document_id, staff_id))
    if row is None:
        abort(404)
    execute("DELETE FROM staff_documents WHERE id = ?", (document_id,))
    log_activity(
        "pagos_personal", "ELIMINAR",
        f"Documento ({DOCUMENT_TYPE_LABELS.get(row['doc_type'], row['doc_type'])}) de {staff['name']}: eliminado",
        entity_type="empleado", entity_id=staff_id, entity_url=url_for("legajo.detail", staff_id=staff_id),
    )
    flash("Documento eliminado.", "success")
    return redirect(url_for("legajo.detail", staff_id=staff_id))


def _serve(row):
    if row is None or not row["filename"]:
        abort(404)
    if storage.using_s3():
        return redirect(storage.staff_document_url(row["filename"]))
    return send_from_directory(storage.local_staff_documents_dir(), row["filename"])
