"""Define las posiciones de neumáticos y las coordenadas del diagrama
para cada tipo de unidad (tracto, carreta, camión simple).

Configuraciones asumidas (las más comunes en transporte de carga pesada en
Perú):
- TRACTO (cabezal tractor, 6x4): 1 eje de dirección simple (2 llantas) +
  2 ejes de tracción dobles (4 llantas cada uno) = 10 llantas.
- CARRETA (semirremolque): 3 ejes dobles (4 llantas cada uno) = 12 llantas.
- CAMION (unidad simple, sin remolque separado): 1 eje de dirección simple
  (2 llantas) + 1 eje trasero doble (4 llantas) = 6 llantas.

Si tu configuración real es distinta (más o menos ejes), avisa para
ajustar estas listas — están centralizadas aquí, en un solo lugar.
"""

VEHICLE_TYPE_LABELS = {
    "TRACTO": "Tracto camión",
    "CARRETA": "Carreta / semirremolque",
    "CAMION": "Camión (unidad simple)",
}

# Vida útil por defecto (km) sugerida al registrar una llanta nueva — 2 sep,
# pedido de Braulio: "Bueno de 0 a 20,000 km, Regular de 21,000 a 40,000 km,
# Grave de 40,000 a 60,000 km, y a partir de 60,000 km alerta de cambio."
# Estas 4 bandas se calculan como tercios de la vida útil de CADA llanta
# (ver _tire_metrics() en app/routes/neumaticos.py) — con el valor por
# defecto de 60,000 km dan exactamente esos cortes, pero siguen siendo
# ajustables llanta por llanta (algunas marcas/modelos duran más o menos).
DEFAULT_EXPECTED_LIFE_KM = 60000

_TIRE_W = 20
_TIRE_H = 34
_SINGLE_LEFT_X = 34
_SINGLE_RIGHT_X = 146
_DUAL_LEFT_OUTER_X = 8
_DUAL_LEFT_INNER_X = 34
_DUAL_RIGHT_INNER_X = 146
_DUAL_RIGHT_OUTER_X = 172


def _single_axle(axle_num, y):
    return [
        {
            "code": f"EJE{axle_num}_IZQ",
            "label": f"Eje {axle_num} — izquierda (dirección)",
            "x": _SINGLE_LEFT_X,
            "y": y,
        },
        {
            "code": f"EJE{axle_num}_DER",
            "label": f"Eje {axle_num} — derecha (dirección)",
            "x": _SINGLE_RIGHT_X,
            "y": y,
        },
    ]


def _dual_axle(axle_num, y):
    return [
        {
            "code": f"EJE{axle_num}_IZQ_EXT",
            "label": f"Eje {axle_num} — izquierda exterior",
            "x": _DUAL_LEFT_OUTER_X,
            "y": y,
        },
        {
            "code": f"EJE{axle_num}_IZQ_INT",
            "label": f"Eje {axle_num} — izquierda interior",
            "x": _DUAL_LEFT_INNER_X,
            "y": y,
        },
        {
            "code": f"EJE{axle_num}_DER_INT",
            "label": f"Eje {axle_num} — derecha interior",
            "x": _DUAL_RIGHT_INNER_X,
            "y": y,
        },
        {
            "code": f"EJE{axle_num}_DER_EXT",
            "label": f"Eje {axle_num} — derecha exterior",
            "x": _DUAL_RIGHT_OUTER_X,
            "y": y,
        },
    ]


_AXLE_Y_START = 60
_AXLE_Y_STEP = 80

# Para cada tipo de unidad: lista de ejes, donde cada uno es "single" o
# "dual". El orden es de adelante hacia atrás.
_AXLE_LAYOUTS = {
    "TRACTO": ["single", "dual", "dual"],
    "CARRETA": ["dual", "dual", "dual"],
    "CAMION": ["single", "dual"],
}


def get_axle_ys(vehicle_type):
    layout = _AXLE_LAYOUTS.get(vehicle_type, _AXLE_LAYOUTS["CAMION"])
    return [_AXLE_Y_START + i * _AXLE_Y_STEP for i in range(len(layout))]


def get_positions(vehicle_type):
    """Devuelve la lista ordenada de posiciones (dicts con code/label/x/y)
    para el tipo de unidad dado."""
    layout = _AXLE_LAYOUTS.get(vehicle_type, _AXLE_LAYOUTS["CAMION"])
    positions = []
    for i, kind in enumerate(layout):
        axle_num = i + 1
        y = _AXLE_Y_START + i * _AXLE_Y_STEP
        if kind == "single":
            positions.extend(_single_axle(axle_num, y))
        else:
            positions.extend(_dual_axle(axle_num, y))
    return positions


def get_diagram_height(vehicle_type):
    axle_ys = get_axle_ys(vehicle_type)
    return axle_ys[-1] + _TIRE_H + 40 if axle_ys else 160


def get_position_label(vehicle_type, position_code):
    for p in get_positions(vehicle_type):
        if p["code"] == position_code:
            return p["label"]
    return position_code


TIRE_WIDTH = _TIRE_W
TIRE_HEIGHT = _TIRE_H
