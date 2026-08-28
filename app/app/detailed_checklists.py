"""Definición fija de los "Check List" físicos de Harraso Transport para
unidades tipo TRACTO y CARRETA (basados en los formatos en papel reales
que usa la empresa hoy: "CHECK LIST TRACTO HARRASO" y "CHECK LIST DE
CARRETA"). CAMION sigue usando el checklist genérico configurable desde
Catálogos hasta que Harraso comparta su propio formato para ese tipo de
unidad.

Cada sección tiene sus propias etiquetas de estado tal como aparecen en
el papel (Bien/Mal, Normal/Falta, etc.), pero internamente se normalizan
a OK/FALLA (la primera etiqueta siempre mapea a OK, la segunda a FALLA)
para poder reutilizar el mismo conteo de fallas que ya usa el checklist
genérico (columna `status` de `inspection_items`).

Centralizado aquí, en un solo lugar, igual que `app/tire_positions.py`
— si Harraso actualiza alguno de sus formatos físicos, este es el único
archivo que hay que tocar.
"""

DETAILED_CHECKLIST_TYPES = {"TRACTO", "CARRETA"}

LOCATIONS = ["Pucallpa", "Tarapoto", "Lima"]

CHECKLIST_LABELS = {
    "TRACTO": "Check List de Tracto",
    "CARRETA": "Check List de Carreta",
}

VEHICLE_FIELD_LABELS = {
    "TRACTO": "Tracto (unidad)",
    "CARRETA": "Carreta (unidad)",
}

# La carreta es un semirremolque sin motor: no tiene odómetro propio en el
# formato físico (a diferencia del tracto, que sí lo trae), así que el
# formulario/PDF de carreta no muestra el campo de kilometraje.
HAS_ODOMETER = {
    "TRACTO": True,
    "CARRETA": False,
}

# extra_field: None, o una tupla (clave, etiqueta) para una columna extra
# puntual junto al estado (cantidad de recarga, si se sopleteó, etc.)
TRACTO_CHECKLIST_SECTIONS = [
    {
        "key": "PERSONAL",
        "title": "Personal",
        "status_labels": ("Completo", "Falta"),
        "extra_field": None,
        "checklist_items": ["Equipos de protección personal"],
    },
    {
        "key": "REVISION_NIVELES",
        "title": "Revisión de niveles",
        "status_labels": ("Normal", "Falta"),
        "extra_field": ("recarga_cant", "Recarga (cant.)"),
        "checklist_items": ["Aceite motor", "Aceite transmisión", "Aceite dirección", "Refrigerante"],
    },
    {
        "key": "SISTEMA_ADMISION",
        "title": "Sistema de admisión",
        "status_labels": ("Normal", "Obstruido"),
        "extra_field": ("sopleteado", "Sopleteado"),
        "checklist_items": ["Filtro de aire"],
    },
    {
        "key": "REVISION_GENERAL",
        "title": "Revisión general",
        "status_labels": ("Bien", "Mal"),
        "extra_field": None,
        "checklist_items": [
            "Faros delanteros",
            "Faros posteriores",
            "Espejos retrovisores",
            "Orden y limpieza cabina",
            "Freno, freno de estacionamiento",
            "Dirección",
            "Suspensión, muelles, bolsas de aire",
            "Transmisión, embrague, cardán",
        ],
    },
    {
        "key": "ACTIVIDADES",
        "title": "Actividades",
        "status_labels": ("Completo", None),  # una sola columna (checkbox)
        "extra_field": None,
        "checklist_items": ["Drenado de separador de agua", "Drenado de compresora-tanques de aire"],
    },
    {
        "key": "TABLERO_CONTROL",
        "title": "Tablero de control",
        "status_labels": ("Bien", "Mal"),
        "extra_field": None,
        "checklist_items": [
            "Baterías",
            "Arrancador",
            "Alternador",
            "Manómetros",
            "Limpia parabrisas",
            "Alarma de retroceso",
            "Neblineros",
            "Luz pirata",
        ],
    },
    {
        "key": "ACCESORIOS_SEGURIDAD",
        "title": "Accesorios de seguridad",
        "status_labels": ("Bien", "Mal"),
        "extra_field": None,
        "checklist_items": [
            "Conos y triángulo de seguridad",
            "Extintor",
            "Botiquín",
            "Gata",
            "Estuche de herramientas",
            "Cable de remolque",
            "Cable de batería",
            "Fajas",
            "Tacos",
        ],
    },
]

# El formato físico de carreta es más simple: una sola tabla de
# "Revisión general" (Bien/Mal/Observaciones), sin las secciones propias
# del tracto (Personal, niveles, sistema de admisión, tablero, etc.).
CARRETA_CHECKLIST_SECTIONS = [
    {
        "key": "REVISION_GENERAL",
        "title": "Revisión general",
        "status_labels": ("Bien", "Mal"),
        "extra_field": None,
        "checklist_items": [
            "Muelles, bolsas de aire",
            "Templadores bocina",
            "Ejes alineamiento",
            "Kin pin y plancha",
            "Sistema eléctrico",
            "Bocamasas, grasa, tapas, araña",
            "Mangueras",
            "Chasis: patas de apoyo",
            "Furgón: puertas, techo",
            "Engrase",
            "Soldadura",
            "Freno: zapatas, tambores",
            "Raches",
            "Válvula de freno",
            "Cilindro de freno",
            "Retráctil",
            "Suspensión",
            "Ajustes de pernos de suspensión",
        ],
    },
]

CHECKLIST_SECTIONS = {
    "TRACTO": TRACTO_CHECKLIST_SECTIONS,
    "CARRETA": CARRETA_CHECKLIST_SECTIONS,
}

# Sección especial: código de llanta según posición. No tiene estado
# Bien/Mal — es captura de dato (qué llanta/código está en cada posición
# al momento de la inspección). Las posiciones vienen de
# app/tire_positions.py (mismo layout que usa el módulo de Neumáticos
# para cada tipo de unidad), más una fila aparte para la llanta de
# repuesto. En el formato de carreta el papel también trae una columna
# de "Presión" por posición (junto al código de llanta); en el de tracto
# no aparece esa columna.
TIRE_SECTION_KEY = "REVISION_LLANTAS"
SPARE_TIRE_ITEM = "Llanta de repuesto"

TIRE_SECTION_META = {
    "TRACTO": {
        "title": "Revisión de llantas — código según posición",
        "note": "Especificar en la observación si falta esparrago, tuerca o chapeta (sapo).",
        "has_pressure": False,
    },
    "CARRETA": {
        "title": "Revisión de llantas — presión y código según posición",
        "note": "Especificar en la observación si falta esparrago, tuerca o chapeta (sapo).",
        "has_pressure": True,
    },
}


def sections_for(vehicle_type):
    return CHECKLIST_SECTIONS.get(vehicle_type, [])


def tire_meta_for(vehicle_type):
    return TIRE_SECTION_META.get(vehicle_type, TIRE_SECTION_META["TRACTO"])


def section_by_key(vehicle_type, key):
    for s in sections_for(vehicle_type):
        if s["key"] == key:
            return s
    return None
