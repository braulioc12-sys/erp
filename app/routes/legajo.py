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
from datetime import datetime, timedelta

from flask import (
    Blueprint,
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
    return render_template(
        "legajo/detail.html", staff=staff, contracts=contracts, documents=documents,
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
