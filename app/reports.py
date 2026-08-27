"""Generación de reportes en Excel (.xlsx) usando openpyxl."""
import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.helpers import pretty_label

# Paleta acorde a la app (ver app/static/css/style.css)
COLOR_PRIMARY = "1D4ED8"
COLOR_PRIMARY_SOFT = "E8EDFC"
COLOR_HEADER_TEXT = "FFFFFF"
COLOR_GRAY = "667085"
COLOR_TOTAL_FILL = "111827"

CURRENCY_FORMAT = '"S/" #,##0.00'
COLUMN_WIDTHS = [13, 16, 12, 12, 44, 16]  # Fecha, Tipo, Viaje, Unidad, Descripción, Monto
COLUMNS = ["Fecha", "Tipo", "Viaje", "Unidad", "Descripción", "Monto"]


def _thin_border(sides="bottom"):
    side = Side(style="thin", color="D0D5DD")
    kwargs = {s: side for s in ("left", "right", "top", "bottom") if s in sides or sides == "all"}
    return Border(**kwargs)


def build_expenses_workbook(expenses, company_name, filter_description, known_type_order=None):
    """Construye un workbook con el reporte de gastos agrupado por tipo,
    con subtotales y total general. `expenses` es una lista de filas
    (sqlite3.Row) con: expense_date, type, trip_code, vehicle_plate,
    description, amount. `known_type_order` (opcional) es la lista de tipos
    en el orden del catálogo — los tipos que aparezcan en los gastos pero no
    en esta lista (por ejemplo un concepto ya desactivado) se agregan al
    final, en orden alfabético."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Gastos"

    last_col_letter = get_column_letter(len(COLUMNS))

    # --- Título y subtítulo ---
    ws.merge_cells(f"A1:{last_col_letter}1")
    ws["A1"] = f"{company_name} — Reporte de Gastos"
    ws["A1"].font = Font(bold=True, size=14, color=COLOR_PRIMARY)

    ws.merge_cells(f"A2:{last_col_letter}2")
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    ws["A2"] = f"Generado el {generated}  ·  {filter_description}"
    ws["A2"].font = Font(italic=True, size=10, color=COLOR_GRAY)

    header_row = 4

    # --- Encabezados de columna ---
    for idx, title in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=header_row, column=idx, value=title)
        cell.font = Font(bold=True, color=COLOR_HEADER_TEXT)
        cell.fill = PatternFill("solid", fgColor=COLOR_PRIMARY)
        cell.alignment = Alignment(horizontal="right" if title == "Monto" else "left", vertical="center")
        cell.border = _thin_border("all")

    ws.freeze_panes = f"A{header_row + 1}"

    # --- Agrupar gastos por tipo. El orden de los grupos sigue el orden en
    # que aparecen en el catálogo (app/routes/catalogos.py); si un gasto usa
    # un tipo que ya no está en el catálogo (por ejemplo fue desactivado),
    # igual se muestra, al final. ---
    by_type = {}
    for e in expenses:
        by_type.setdefault(e["type"], []).append(e)

    known_type_order = known_type_order or []
    type_order = [t for t in known_type_order if t in by_type]
    extra_types = sorted(t for t in by_type if t not in known_type_order)
    type_order += extra_types

    row = header_row + 1
    grand_total = 0.0

    for type_key in type_order:
        rows = by_type.get(type_key) or []
        if not rows:
            continue
        rows = sorted(rows, key=lambda r: r["expense_date"])

        # Subencabezado de grupo
        ws.merge_cells(f"A{row}:{last_col_letter}{row}")
        group_cell = ws.cell(row=row, column=1, value=pretty_label(type_key))
        group_cell.font = Font(bold=True, color=COLOR_PRIMARY)
        group_cell.fill = PatternFill("solid", fgColor=COLOR_PRIMARY_SOFT)
        row += 1

        subtotal = 0.0
        for e in rows:
            ws.cell(row=row, column=1, value=e["expense_date"])
            ws.cell(row=row, column=2, value=pretty_label(e["type"]))
            ws.cell(row=row, column=3, value=e["trip_code"] or "—")
            ws.cell(row=row, column=4, value=e["vehicle_plate"] or "—")
            ws.cell(row=row, column=5, value=e["description"] or "")
            amount_cell = ws.cell(row=row, column=6, value=float(e["amount"]))
            amount_cell.number_format = CURRENCY_FORMAT
            amount_cell.alignment = Alignment(horizontal="right")
            for col in range(1, len(COLUMNS) + 1):
                ws.cell(row=row, column=col).border = _thin_border("bottom")
            subtotal += float(e["amount"])
            row += 1

        # Fila de subtotal del grupo
        ws.merge_cells(f"A{row}:E{row}")
        subtotal_label = ws.cell(row=row, column=1, value=f"Subtotal {pretty_label(type_key)}")
        subtotal_label.font = Font(bold=True)
        subtotal_label.alignment = Alignment(horizontal="right")
        subtotal_cell = ws.cell(row=row, column=6, value=subtotal)
        subtotal_cell.number_format = CURRENCY_FORMAT
        subtotal_cell.font = Font(bold=True)
        subtotal_cell.border = Border(top=Side(style="thin", color="98A2B3"))
        subtotal_label.border = Border(top=Side(style="thin", color="98A2B3"))
        grand_total += subtotal
        row += 2  # fila en blanco entre grupos

    # --- Total general ---
    row += 1
    ws.merge_cells(f"A{row}:E{row}")
    total_label = ws.cell(row=row, column=1, value="TOTAL GENERAL")
    total_label.font = Font(bold=True, size=12, color=COLOR_HEADER_TEXT)
    total_label.fill = PatternFill("solid", fgColor=COLOR_TOTAL_FILL)
    total_label.alignment = Alignment(horizontal="right", vertical="center")

    total_cell = ws.cell(row=row, column=6, value=grand_total)
    total_cell.number_format = CURRENCY_FORMAT
    total_cell.font = Font(bold=True, size=12, color=COLOR_HEADER_TEXT)
    total_cell.fill = PatternFill("solid", fgColor=COLOR_TOTAL_FILL)
    total_cell.alignment = Alignment(horizontal="right", vertical="center")

    if not any(by_type.values()):
        ws.cell(row=header_row + 1, column=1, value="No hay gastos para los filtros seleccionados.").font = Font(italic=True, color=COLOR_GRAY)

    # --- Anchos de columna ---
    for idx, width in enumerate(COLUMN_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width

    ws.sheet_view.showGridLines = False

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
