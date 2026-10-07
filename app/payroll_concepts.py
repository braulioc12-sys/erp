"""Conceptos de planilla que se siembran la primera vez (7 oct, idea de Buk).

Cada uno: (nombre, tipo, afecto a pensión, afecto a EsSalud, afecto a 5ta).
Se pueden cambiar o desactivar desde Planilla → Conceptos de planilla.
"""

DEFAULT_CONCEPTS = [
    ("Comisión por viajes", "INGRESO", 1, 1, 1),
    ("Bono", "INGRESO", 1, 1, 1),
    ("Movilidad por condiciones de trabajo", "INGRESO", 0, 0, 0),
    ("Descuento varios", "DESCUENTO", 0, 0, 0),
]
