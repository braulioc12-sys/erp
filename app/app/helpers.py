"""Funciones auxiliares compartidas por las rutas."""
from datetime import datetime

from app.db import query_one


def next_code(prefix, table, code_column="code"):
    """Genera un código correlativo tipo V-0001, F-0001, etc."""
    row = query_one(f"SELECT COUNT(*) as n FROM {table}")
    n = (row["n"] if row else 0) + 1
    return f"{prefix}-{n:04d}"


def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_date(value):
    """Valida que la fecha venga en formato YYYY-MM-DD; si no, devuelve None."""
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def money(value):
    try:
        return f"S/ {float(value):,.2f}"
    except (TypeError, ValueError):
        return "S/ 0.00"


def pretty_label(value):
    """Convierte códigos tipo 'COMBUSTIBLE' o 'en_curso' en texto legible
    ('Combustible', 'En Curso'). Si el valor ya viene con formato humano
    (por ejemplo un concepto agregado desde Catálogos), lo deja tal cual."""
    if not value:
        return ""
    return value.replace("_", " ").title()
