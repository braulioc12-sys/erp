"""Planilla del personal (6 oct, pedido de Braulio: AFP, ONP, 5ta categoría,
CTS, gratificación, EsSalud, vacaciones según régimen, asistencia, descuentos,
préstamos, adelantos, reconocimiento de deuda...).

Esta primera parte trae lo que la planilla necesita antes de calcular:

- Parámetros de planilla (UIT, RMV, tasas AFP/ONP/EsSalud...): tabla
  `payroll_params`, corregibles desde pantalla (ver app/payroll_params.py).
- Perfil de planilla de cada persona: si entra en planilla, régimen laboral
  (general / MYPE micro / MYPE pequeña), sistema de pensiones (ONP / AFP y
  cuál), sueldo básico, asignación familiar. Cambiar el régimen ajusta los
  días de vacaciones por año de la persona (30 general, 15 MYPE).
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from app.audit import log_activity
from app.auth import permission_required, validate_csrf
from app.db import execute, query_all, query_one
from app.helpers import parse_float
from app.payroll_params import AFP_NAMES, LABELS, get_param_rows, set_param
from app.routes.legajo import current_contract

bp = Blueprint("planilla", __name__, url_prefix="/planilla")

REGIMES = [
    ("GENERAL", "Régimen general"),
    ("MYPE_MICRO", "MYPE — Microempresa"),
    ("MYPE_PEQUENA", "MYPE — Pequeña empresa"),
]
REGIME_LABELS = dict(REGIMES)
REGIME_VACATION_DAYS = {"GENERAL": 30.0, "MYPE_MICRO": 15.0, "MYPE_PEQUENA": 15.0}
PENSION_SYSTEMS = [("ONP", "ONP"), ("AFP", "AFP"), ("NINGUNO", "Sin descuento de pensión")]
PENSION_LABELS = dict(PENSION_SYSTEMS)
AFP_LABELS = dict(AFP_NAMES)
COMMISSION_TYPES = [("FLUJO", "Comisión sobre la remuneración (flujo)"), ("MIXTA", "Comisión mixta (0% sobre la remuneración)")]


def _get_staff_or_404(staff_id):
    staff = query_one("SELECT * FROM staff WHERE id = ?", (staff_id,))
    if staff is None:
        abort(404)
    return staff


def effective_salary(staff):
    """Sueldo básico de la persona: el del perfil de planilla, o el del
    contrato vigente del legajo si no se escribió uno."""
    if staff["basic_salary"] is not None:
        return float(staff["basic_salary"])
    contract = current_contract(staff["id"])
    if contract and contract["salary"]:
        return float(contract["salary"])
    return 0.0


@bp.route("")
@permission_required("pagos_personal", "view")
def index():
    in_payroll = query_one("SELECT COUNT(*) AS n FROM staff WHERE status = 'ACTIVO' AND in_payroll = 1")["n"]
    return render_template("planilla/index.html", in_payroll=in_payroll)


@bp.route("/parametros", methods=["GET", "POST"])
@permission_required("pagos_personal", "edit")
def params_view():
    if request.method == "POST":
        if not validate_csrf():
            abort(400)
        user = getattr(g, "user", None)
        changed, bad = [], []
        for row in get_param_rows():
            raw = request.form.get(f"p_{row['key']}")
            if raw is None or raw.strip() == "":
                continue
            value = parse_float(raw, None)
            if value is None or value < 0:
                bad.append(row["label"])
                continue
            if abs(value - row["value"]) > 1e-9:
                set_param(row["key"], value, user["id"] if user else None)
                changed.append(f"{row['label']}: {row['value']:g} → {value:g}")
        if bad:
            flash("Valor inválido en: " + "; ".join(bad) + ". No se guardó ese cambio.", "error")
        if changed:
            log_activity("pagos_personal", "EDITAR", "Parámetros de planilla: " + "; ".join(changed))
            flash(f"Parámetros guardados ({len(changed)} cambio(s)).", "success")
        elif not bad:
            flash("No hubo cambios.", "info")
        return redirect(url_for("planilla.params_view"))
    return render_template("planilla/parametros.html", rows=get_param_rows())


@bp.route("/personal")
@permission_required("pagos_personal", "view")
def profiles():
    show = request.args.get("ver", "")
    rows = query_all("SELECT * FROM staff WHERE status = 'ACTIVO' ORDER BY in_payroll DESC, name")
    if show == "planilla":
        rows = [r for r in rows if r["in_payroll"]]
    return render_template(
        "planilla/personal.html", rows=rows, show=show, regime_labels=REGIME_LABELS, pension_labels=PENSION_LABELS,
        afp_labels=AFP_LABELS, salary_of=effective_salary,
    )


@bp.route("/personal/<int:staff_id>")
@permission_required("pagos_personal", "view")
def profile(staff_id):
    staff = _get_staff_or_404(staff_id)
    contract = current_contract(staff_id)
    return render_template(
        "planilla/perfil.html", staff=staff, regimes=REGIMES, pension_systems=PENSION_SYSTEMS, afp_names=AFP_NAMES,
        commission_types=COMMISSION_TYPES, contract=contract, salary=effective_salary(staff),
        regime_vacation_days=REGIME_VACATION_DAYS,
    )


@bp.route("/personal/<int:staff_id>/guardar", methods=["POST"])
@permission_required("pagos_personal", "edit")
def profile_save(staff_id):
    staff = _get_staff_or_404(staff_id)
    if not validate_csrf():
        abort(400)
    f = request.form
    regime = f.get("labor_regime", "GENERAL")
    pension = f.get("pension_system", "ONP")
    afp = f.get("afp_name") or None
    commission = f.get("afp_commission_type", "FLUJO")
    errors = []
    if regime not in REGIME_LABELS:
        errors.append("Elige el régimen laboral.")
    if pension not in PENSION_LABELS:
        errors.append("Elige el sistema de pensiones.")
    if pension == "AFP" and afp not in AFP_LABELS:
        errors.append("Elige la AFP.")
    if commission not in dict(COMMISSION_TYPES):
        commission = "FLUJO"
    salary_raw = (f.get("basic_salary") or "").strip()
    salary = parse_float(salary_raw, None) if salary_raw else None
    if salary_raw and (salary is None or salary < 0):
        errors.append("El sueldo básico no es válido.")
    prior_income = parse_float(f.get("fifth_prior_income"), 0) or 0
    prior_withheld = parse_float(f.get("fifth_prior_withheld"), 0) or 0
    if prior_income < 0 or prior_withheld < 0:
        errors.append("Los montos de 5ta categoría no pueden ser negativos.")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("planilla.profile", staff_id=staff_id))
    vac_days = staff["vacation_days_per_year"]
    if regime != staff["labor_regime"]:
        vac_days = REGIME_VACATION_DAYS[regime]
    execute(
        """UPDATE staff SET in_payroll = ?, labor_regime = ?, pension_system = ?, afp_name = ?,
           afp_commission_type = ?, cuspp = ?, basic_salary = ?, family_allowance = ?, fifth_prior_income = ?,
           fifth_prior_withheld = ?, vacation_days_per_year = ? WHERE id = ?""",
        (
            1 if f.get("in_payroll") else 0, regime, pension, afp if pension == "AFP" else None, commission,
            (f.get("cuspp") or "").strip() or None, salary, 1 if f.get("family_allowance") else 0,
            prior_income, prior_withheld, vac_days, staff_id,
        ),
    )
    log_activity(
        "pagos_personal", "EDITAR", f"Perfil de planilla: {staff['name']} ({REGIME_LABELS[regime]}, {PENSION_LABELS[pension]})",
        entity_type="empleado", entity_id=staff_id, entity_url=url_for("planilla.profile", staff_id=staff_id),
    )
    msg = "Perfil de planilla guardado."
    if regime != staff["labor_regime"]:
        msg += f" Días de vacaciones por año: {vac_days:g}."
    flash(msg, "success")
    return redirect(url_for("planilla.profile", staff_id=staff_id))
