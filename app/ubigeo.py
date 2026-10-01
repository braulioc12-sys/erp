"""Catálogo de ubigeos (códigos de departamento/provincia/distrito) del
INEI/SUNAT y su validación (10 sep, patch 0028 — pedido de Braulio tras el
error real que tefacturo.pe devolvió al emitir una guía con un ubigeo de
destino inventado, "080000", que no corresponde a ningún distrito real de
Cusco: tefacturo.pe no valida esto con un mensaje claro, su servidor lanza
un NullPointerException al buscar internamente ese código).

FUENTE Y ALCANCE (importante para saber qué tan completo es este catálogo):

- `DEPARTAMENTOS` (25) y `PROVINCIAS` (196): catálogo COMPLETO y verificado
  de todo el Perú, tomado de fuentes públicas que replican el listado oficial
  del INEI (ubigeo-peru-aumentado, geodir/ubigeo-peru y el anexo de ámbito
  geográfico de SUNAT). Se verificó que el conteo de provincias por
  departamento coincide exactamente con el real (196 provincias en total) y
  que cada código de departamento/provincia es consistente entre sí — no hay
  huecos ni duplicados.

- `DISTRITOS`: 1 oct, pedido de Braulio ("cuando pide codigo ubigeo 2
  digitos en la direccion, hay que cargarlos directamente cuando se
  selecciona" -- ya no quería escribir el número de distrito a mano para
  los 16 departamentos que antes quedaban fuera de
  `DEPARTAMENTOS_CON_DISTRITOS_COMPLETOS`). Catálogo COMPLETO para los 25
  departamentos (1881 distritos en total), tomado de
  https://github.com/jmcastagnetto/ubigeo-peru-aumentado (ubigeo_distrito.csv,
  columna `inei`) -- el mismo repo que ya respaldaba `DEPARTAMENTOS`/
  `PROVINCIAS`. Verificado contra el `PROVINCIAS` ya existente de este mismo
  archivo antes de cargarse: las 902 filas nuevas (de los 16 departamentos
  que faltaban) coincidieron el 100% de las veces con el nombre y código de
  provincia ya guardado acá (cero huecos, cero duplicados, cada una de las
  106 provincias de esos 16 departamentos recibió al menos un distrito y su
  distrito "01" -- la capital provincial, que siempre existe), así que no
  hubo que adivinar ni reconstruir ningún código a mano. `DEPARTAMENTOS_CON_
  DISTRITOS_COMPLETOS` ahora son los 25.

Esto ya alcanza para el caso real que falló: "080000" (Cusco/00/00) queda
rechazado porque la provincia "0800" no existe (las provincias de Cusco son
0801..0813) -- y ahora, para cualquier departamento, también se valida que
el distrito exacto (los últimos 2 dígitos) exista de verdad."""
import json
import os

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "ubigeo_peru.json")

with open(_DATA_PATH, encoding="utf-8") as _f:
    _CATALOGO = json.load(_f)

DEPARTAMENTOS = _CATALOGO["departamentos"]  # {"01": "Amazonas", ...}
PROVINCIAS = _CATALOGO["provincias"]  # {"0101": {"nombre": ..., "departamento": "01"}, ...}
DISTRITOS = _CATALOGO["distritos"]  # {"010101": {"nombre": ..., "provincia": "0101"}, ...}
DEPARTAMENTOS_CON_DISTRITOS_COMPLETOS = set(_CATALOGO["departamentos_completos"])


def validar_ubigeo(codigo, etiqueta="El ubigeo"):
    """Valida un código de ubigeo de 6 dígitos contra el catálogo real de
    departamentos/provincias/distritos. Devuelve None si es válido (o si
    viene vacío — los campos de ubigeo siguen siendo opcionales al guardar
    la guía, igual que antes de este patch), o un mensaje de error en
    español listo para mostrar con flash() si no lo es.

    `etiqueta` identifica el campo en el mensaje de error (p.ej. "El ubigeo
    de partida")."""
    codigo = (codigo or "").strip()
    if not codigo:
        return None
    if len(codigo) != 6 or not codigo.isdigit():
        return f"{etiqueta} debe tener 6 dígitos numéricos (código INEI de distrito)."

    dept_code = codigo[:2]
    prov_code = codigo[:4]

    dept_name = DEPARTAMENTOS.get(dept_code)
    if dept_name is None:
        return f"{etiqueta} '{codigo}' no es válido: no existe el departamento con código '{dept_code}'."

    provincia = PROVINCIAS.get(prov_code)
    if provincia is None:
        return (
            f"{etiqueta} '{codigo}' no es válido: no existe esa provincia dentro de "
            f"{dept_name} (código '{prov_code}'). Revisa el listado de ubigeos del INEI."
        )

    if dept_code in DEPARTAMENTOS_CON_DISTRITOS_COMPLETOS and codigo not in DISTRITOS:
        return (
            f"{etiqueta} '{codigo}' no es válido: no existe ese distrito dentro de "
            f"{provincia['nombre'].title()}, {dept_name} — revisa el listado de ubigeos del INEI."
        )

    return None
