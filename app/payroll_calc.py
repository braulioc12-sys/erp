"""Cálculo de la planilla mensual (6 oct, pedido de Braulio, fase 3b).

Todo en soles, redondeado a 2 decimales por concepto. Mes comercial de 30
días (el valor del día es sueldo ÷ 30). Las reglas son las generales de la
ley peruana; los parámetros (UIT, RMV, tasas) salen de `payroll_params` y se
corrigen desde pantalla. LÍMITES conocidos (confirmar con el contador):
no promedia comisiones variables en la gratificación ni en la CTS, no
descuenta faltas de la gratificación, no calcula liquidaciones por cese
(trunca) ni utilidades, y la 5ta categoría no considera otras deducciones
(solo las 7 UIT).
"""
import calendar
from datetime import date, datetime, timedelta

from app.db import query_all, query_one
from app.routes.incidencias import month_summary

# Gratificación y CTS según régimen (fracción del sueldo): general = 1,
# pequeña empresa = 1/2, microempresa = no tiene.
REGIME_GRATI_FACTOR = {"GENERAL": 1.0, "MYPE_PEQUENA": 0.5, "MYPE_MICRO": 0.0}
REGIME_CTS_FACTOR = {"GENERAL": 1.0, "MYPE_PEQUENA": 0.5, "MYPE_MICRO": 0.0}

# Tramos de 5ta categoría: (hasta cuántas UIT acumuladas, tasa)
FIFTH_BRACKETS = [(5, 0.08), (20, 0.14), (35, 0.17), (45, 0.20), (None, 0.30)]
# Divisor mensual de la retención (meses que faltan para cerrar el año):
FIFTH_DIVISOR = {1: 12, 2: 12, 3: 12, 4: 9, 5: 8, 6: 8, 7: 8, 8: 5, 9: 4, 10: 4, 11: 4, 12: 1}


def r2(value):
    return round(float(value) + 1e-9, 2)


def _d(value):
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def month_bounds(period):
    y, m = int(period[:4]), int(period[5:7])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def worked_days(staff, period):
    """Días del mes (base 30) en que la persona estuvo en la empresa."""
    first, last = month_bounds(period)
    hire = _d(staff["hire_date"]) if staff["hire_date"] else None
    term = _d(staff["termination_date"]) if staff["termination_date"] else None
    if hire and hire > last:
        return 0
    if term and term < first:
        return 0
    start_day = min(hire.day, 30) if hire and hire >= first else 1
    end_day = min(term.day, 30) if term and term <= last else 30
    return max(end_day - start_day + 1, 0)


def fifth_tax(annual_net_taxable, uit):
    """Impuesto anual de 5ta categoría sobre la renta neta (ya sin las 7 UIT)."""
    if annual_net_taxable <= 0:
        return 0.0
    tax, prev = 0.0, 0.0
    for upto, rate in FIFTH_BRACKETS:
        limit = upto * uit if upto is not None else None
        top = annual_net_taxable if limit is None else min(annual_net_taxable, limit)
        if top > prev:
            tax += (top - prev) * rate
        if limit is None or annual_net_taxable <= limit:
            break
        prev = limit
    return tax


def grati_months(staff, period):
    """Meses completos trabajados en el semestre que paga la gratificación
    del mes (julio: ene–jun; diciembre: jul–dic). 0 si el mes no paga."""
    y, m = int(period[:4]), int(period[5:7])
    if m == 7:
        months = range(1, 7)
    elif m == 12:
        months = range(7, 13)
    else:
        return 0
    hire = _d(staff["hire_date"]) if staff["hire_date"] else None
    term = _d(staff["termination_date"]) if staff["termination_date"] else None
    count = 0
    for mm in months:
        first, last = date(y, mm, 1), date(y, mm, calendar.monthrange(y, mm)[1])
        if hire and hire > first:
            continue
        if term and term < last:
            continue
        count += 1
    return count


def loan_remaining(loan_id, amount):
    paid = query_one("SELECT COALESCE(SUM(amount), 0) AS s FROM staff_loan_payments WHERE loan_id = ?", (loan_id,))["s"]
    return r2(amount - paid), r2(paid)


def loan_installments(staff_id, period, exclude_period_id=None):
    """Cuotas de préstamos / adelantos / reconocimientos de deuda que tocan
    descontar en este periodo: lista de (loan_row, monto)."""
    out = []
    for loan in query_all(
        "SELECT * FROM staff_loans WHERE staff_id = ? AND status = 'ACTIVO' AND start_period <= ? ORDER BY id",
        (staff_id, period),
    ):
        sql = "SELECT COALESCE(SUM(amount), 0) AS s FROM staff_loan_payments WHERE loan_id = ?"
        params = [loan["id"]]
        if exclude_period_id:
            sql += " AND period_id != ?"
            params.append(exclude_period_id)
        paid = query_one(sql, params)["s"]
        remaining = r2(loan["amount"] - paid)
        if remaining <= 0.005:
            continue
        per = loan["installment_amount"] or (loan["amount"] / max(loan["installments"] or 1, 1))
        out.append((loan, r2(min(per, remaining))))
    return out


def compute_line(staff, period, params, items, salary_fn, period_id=None):
    """Calcula la boleta de una persona para el periodo 'YYYY-MM'. `items` =
    filas de payroll_items de esa persona y periodo. Devuelve un dict con las
    columnas de payroll_lines (o None si no trabajó ese mes)."""
    days = worked_days(staff, period)
    if days <= 0:
        return None
    year, month = int(period[:4]), int(period[5:7])
    dias_mes = params["dias_mes"] or 30
    basic = salary_fn(staff)
    regime = staff["labor_regime"] or "GENERAL"
    daily = basic / dias_mes
    warnings = []
    if basic <= 0:
        warnings.append("Sin sueldo básico: escríbelo en el perfil de planilla o carga el contrato en el legajo.")

    summ = month_summary(staff["id"], year, month, params)
    salary_earned = basic * days / dias_mes
    absence = min(summ["descuento_dias"], days) * daily
    tard = (summ["tardanza_min"] / 60.0) * daily / (params["horas_dia"] or 8)
    sub_ded = 0.0
    if params["subsidio_descuenta"] >= 0.5:
        sub_ded = min(summ["essalud_dias"], max(days - summ["descuento_dias"], 0)) * daily
    family_full = params["rmv"] * params["asig_fam_pct"] / 100.0 if staff["family_allowance"] else 0.0
    family = family_full * days / dias_mes

    other_taxable = sum(i["amount"] for i in items if i["kind"] == "INGRESO" and i["taxable"])
    other_nontax = sum(i["amount"] for i in items if i["kind"] == "INGRESO" and not i["taxable"])
    other_ded = sum(i["amount"] for i in items if i["kind"] == "DESCUENTO")

    # Gratificación (julio / diciembre) y bonificación extraordinaria
    grati = bonif = 0.0
    gm = grati_months(staff, period)
    factor = REGIME_GRATI_FACTOR.get(regime, 1.0)
    if gm and factor:
        grati = (basic + family_full) * factor * gm / 6.0
        bonif = grati * params["grati_bonif_pct"] / 100.0

    pension_base = max(salary_earned - absence - tard - sub_ded + family + other_taxable, 0.0)
    pension = 0.0
    pension_detail = ""
    system = staff["pension_system"] or "ONP"
    if system == "ONP":
        pension = pension_base * params["onp_pct"] / 100.0
        pension_detail = f"ONP {params['onp_pct']:g}%"
    elif system == "AFP":
        afp = staff["afp_name"]
        if not afp:
            warnings.append("Tiene AFP pero no se eligió cuál: no se calculó el descuento de pensión.")
        else:
            aporte = pension_base * params["afp_aporte_pct"] / 100.0
            seguro = min(pension_base, params["afp_rma"]) * params["afp_seguro_pct"] / 100.0
            flujo_pct = params.get(f"afp_flujo_{afp}", 0.0) if staff["afp_commission_type"] != "MIXTA" else 0.0
            comision = pension_base * flujo_pct / 100.0
            pension = aporte + seguro + comision
            pension_detail = (
                f"AFP {afp.title()}: aporte {params['afp_aporte_pct']:g}% {r2(aporte):.2f} + seguro {params['afp_seguro_pct']:g}% "
                f"{r2(seguro):.2f} + comisión {flujo_pct:g}% {r2(comision):.2f}"
            )

    # --- Renta de 5ta categoría (retención mensual) ---
    this_month_taxable = salary_earned - absence - tard - sub_ded + family + other_taxable + grati + bonif
    fixed_monthly = basic + family_full
    prior = query_all(
        """SELECT l.fifth_base_income, l.fifth_deduction FROM payroll_lines l JOIN payroll_periods p ON p.id = l.period_id
           WHERE l.staff_id = ? AND p.period >= ? AND p.period < ?""",
        (staff["id"], f"{year}-01", period),
    )
    prior_income = sum(r["fifth_base_income"] or 0 for r in prior) + (staff["fifth_prior_income"] or 0)
    prior_withheld = sum(r["fifth_deduction"] or 0 for r in prior) + (staff["fifth_prior_withheld"] or 0)
    estimated = False
    if not prior and not (staff["fifth_prior_income"] or 0):
        hire = _d(staff["hire_date"]) if staff["hire_date"] else None
        start_m = 1 if not hire or hire.year < year else hire.month + (0 if hire.day == 1 else 1)
        before = max(month - max(start_m, 1), 0)
        if before > 0 and fixed_monthly > 0:
            prior_income += fixed_monthly * before
            if month > 7 and factor:
                prior_income += fixed_monthly * factor * (1 + params["grati_bonif_pct"] / 100.0)
            estimated = True
    future = fixed_monthly * (12 - month)
    if factor:
        n_future_grati = 2 if month < 7 else (1 if month < 12 else 0)
        future += fixed_monthly * factor * (1 + params["grati_bonif_pct"] / 100.0) * n_future_grati
    annual_total = prior_income + this_month_taxable + future
    uit = params["uit"]
    net_taxable = annual_total - params["quinta_deduccion_uit"] * uit
    annual_tax = fifth_tax(net_taxable, uit)
    fifth = 0.0
    if annual_tax > 0:
        fifth = max((annual_tax - prior_withheld) / FIFTH_DIVISOR[month], 0.0)
    if estimated and fifth > 0:
        warnings.append("5ta: no había ingresos previos del año registrados; se estimaron con el sueldo actual (puedes corregirlo en el perfil).")

    # --- Préstamos / adelantos / deuda reconocida ---
    loan_d = adv_d = debt_d = 0.0
    for loan, amount in loan_installments(staff["id"], period, exclude_period_id=period_id):
        if loan["kind"] == "ADELANTO":
            adv_d += amount
        elif loan["kind"] == "RECONOCIMIENTO_DEUDA":
            debt_d += amount
        else:
            loan_d += amount

    gross = salary_earned + family + other_taxable + other_nontax + grati + bonif
    total_ded = absence + tard + sub_ded + pension + fifth + loan_d + adv_d + debt_d + other_ded
    net = gross - total_ded
    if net < -0.005:
        warnings.append(f"El neto sale negativo ({r2(net):.2f}): revisa descuentos, préstamos o adelantos.")

    # --- Aporte del empleador (EsSalud / SIS) ---
    if regime == "MYPE_MICRO":
        essalud = params["sis_micro_monto"]
    else:
        essalud = max(pension_base, params["rmv"]) * params["essalud_pct"] / 100.0
    paid_gross = gross - absence - tard - sub_ded
    return {
        "regime": regime, "pension_system": system, "afp_name": staff["afp_name"],
        "basic_salary": r2(basic), "days_worked": float(days), "salary_earned": r2(salary_earned),
        "family_allowance": r2(family), "other_income": r2(other_taxable), "other_income_nontaxable": r2(other_nontax),
        "gratification": r2(grati), "gratification_bonus": r2(bonif), "absence_deduction": r2(absence),
        "tardiness_deduction": r2(tard), "subsidy_deduction": r2(sub_ded), "gross_total": r2(gross),
        "pension_base": r2(pension_base), "pension_deduction": r2(pension), "pension_detail": pension_detail,
        "fifth_base_income": r2(this_month_taxable), "fifth_deduction": r2(fifth), "loan_deduction": r2(loan_d),
        "advance_deduction": r2(adv_d), "debt_deduction": r2(debt_d), "other_deduction": r2(other_ded),
        "total_deductions": r2(total_ded), "net_pay": r2(net), "essalud_employer": r2(essalud),
        "employer_cost": r2(paid_gross + essalud), "warnings": " | ".join(warnings) or None,
    }


def cts_for(staff, deposit_month, year, params, salary_fn):
    """CTS de un semestre. deposit_month = 5 (nov–abr, se deposita el 15 de
    mayo) o 11 (may–oct, se deposita el 15 de noviembre). Devuelve None si el
    régimen no tiene CTS o no trabajó en el semestre."""
    regime = staff["labor_regime"] or "GENERAL"
    factor = REGIME_CTS_FACTOR.get(regime, 1.0)
    if not factor:
        return None
    if deposit_month == 5:
        start, end = date(year - 1, 11, 1), date(year, 4, 30)
    else:
        start, end = date(year, 5, 1), date(year, 10, 31)
    hire = _d(staff["hire_date"]) if staff["hire_date"] else None
    term = _d(staff["termination_date"]) if staff["termination_date"] else None
    s = max(start, hire) if hire else start
    e = min(end, term) if term else end
    if s > e:
        return None
    # días 30/360 entre s y e (inclusive)
    d1, d2 = min(s.day, 30), min(e.day, 30)
    total_days = (e.year - s.year) * 360 + (e.month - s.month) * 30 + (d2 - d1) + 1
    total_days = max(min(total_days, 180), 0)
    months, days = divmod(total_days, 30)
    basic = salary_fn(staff)
    family = params["rmv"] * params["asig_fam_pct"] / 100.0 if staff["family_allowance"] else 0.0
    last_grati_period = f"{year - 1}-12" if deposit_month == 5 else f"{year}-07"
    row = query_one(
        """SELECT l.gratification FROM payroll_lines l JOIN payroll_periods p ON p.id = l.period_id
           WHERE l.staff_id = ? AND p.period = ?""",
        (staff["id"], last_grati_period),
    )
    grati_real = row["gratification"] if row else None
    grati = grati_real if grati_real is not None else (basic + family) * REGIME_GRATI_FACTOR.get(regime, 1.0)
    computable = basic + family + grati / 6.0
    cts = (computable / 12.0 * months + computable / 360.0 * days) * factor
    return {
        "months": months, "days": days, "basic": r2(basic), "family": r2(family), "grati": r2(grati),
        "grati_is_estimate": grati_real is None, "computable": r2(computable), "cts": r2(cts), "regime": regime,
        "start": s.strftime("%Y-%m-%d"), "end": e.strftime("%Y-%m-%d"),
    }
