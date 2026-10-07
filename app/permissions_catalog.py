"""Catálogo de acciones puntuales por módulo, para los permisos por
usuario (Usuarios > editar > "Permisos específicos").

18 sep, pedido de Braulio: "el usuario Gustavo Lopez puede entrar a
neumatico pero no puede agregar llantas al inventario, a pesar de tener
permiso... podemos ser mas especificos a la hora de dar accesos a los
usuarios?". Antes de esto, el acceso a cada módulo salía únicamente del/los
rol(es) del usuario (PERMISSIONS en app/auth.py) y "editar" era todo o
nada: no había forma de darle o quitarle una acción puntual a UNA persona
sin afectar a todos los que comparten su rol.

Ahora, además de su(s) rol(es), cada usuario puede tener excepciones
individuales guardadas en user_permission_overrides (ver app/schema.sql):
para cualquier (módulo, acción) de este catálogo se puede forzar "permitir
siempre" o "bloquear siempre" para esa persona en particular, sin tocar su
rol ni el de nadie más — ver app/auth.py (can()) y
app/routes/usuarios.py (_current_overrides/_save_user_overrides).

Por defecto todos los módulos siguen teniendo solo "view"/"edit" (mismo
comportamiento de siempre, ahora también anulable por usuario). Neumáticos
es, por pedido explícito de Braulio, el primer módulo que se divide en
acciones más finas que "editar":
  - "campo": operar sobre llantas ya instaladas o del día a día en taller
    (registrar inspección/cocada, montar una llanta nueva en una posición,
    reemplazar, retirar, rotar).
  - "inventario": dar de alta o editar los datos de una llanta en el
    inventario general (compras/stock) — lo que Gustavo NO debía poder
    hacer.
Si en el futuro aparece un caso parecido en otro módulo (ej. "alguien debe
poder crear cotizaciones pero no aprobarlas"), se sigue el mismo patrón:
se agrega la acción nueva aquí, se usa en el @permission_required()/can()
correspondiente, y ya queda disponible como excepción por usuario.
"""

MODULE_LABELS = {
    "dashboard": "Panel",
    "clientes": "Clientes",
    "viajes": "Viajes",
    "guias": "Guías",
    "inspecciones": "Inspecciones",
    "flota": "Flota",
    "conductores": "Conductores",
    "rutas": "Rutas",
    "mantenimiento": "Mantenimiento",
    "neumaticos": "Neumáticos",
    "liquidaciones": "Liquidaciones",
    "facturacion": "Facturación",
    "cotizaciones": "Cotizaciones",
    "inventarios": "Inventarios",
    "usuarios": "Usuarios",
    "catalogos": "Catálogos",
    # 22 sep, pedido de Braulio: quería dar acceso a Ubicación GPS a un
    # usuario puntual desde Usuarios > editar > "Permisos específicos", pero
    # no encontraba la opción porque esta fila decía "Integraciones" — un
    # nombre genérico que no coincide con como se ve el módulo en el menú
    # ("Ubicación GPS", ver app/templates/base.html). Es el mismo módulo/
    # blueprint (app/routes/integraciones.py) en ambos lados, solo cambia la
    # etiqueta acá para que sea reconocible en la lista de permisos.
    "integraciones": "Ubicación GPS",
    "rrhh": "RRHH",
    "tarifario": "Tarifario",
    "pagos_personal": "Pagos personal",
    # 22 sep, pedido de Braulio ("el menu 'descansos' hay que renombrarlo
    # por 'jornada laboral'"): mismo módulo/blueprint/permiso "descansos"
    # (app/routes/descansos.py) -- solo cambia la etiqueta, igual que
    # "Ubicación GPS" arriba.
    "descansos": "Jornada laboral",
    # 22 sep, pedido de Braulio ("yo como administrador, pueda saber quien
    # ha hecho cada cosa"): pantalla nueva de Actividad (ver
    # app/routes/actividad.py) -- por defecto es solo de Administrador
    # (PERMISSIONS en app/auth.py le da "actividad": set() al resto de
    # roles), pero queda en este catálogo para poder dársela puntualmente a
    # otro usuario desde Usuarios > Permisos específicos, igual que
    # cualquier otro módulo.
    "actividad": "Actividad",
    # 7 oct, pedido de Braulio ("en usuarios y permisos hay que actualizar los
    # nuevos modulos, como por ejemplo el de reportes para ver que reportes
    # puede ver cada usuario"): Reportes se chequeaba en el código pero no
    # estaba en esta lista -- por eso no aparecía en Usuarios > Permisos
    # específicos. Ver REPORTS más abajo. (El módulo "gastos"/"viaticos" de
    # app/routes/gastos.py y viaticos.py NO va acá a propósito: esos
    # blueprints no están registrados en app/__init__.py, son código sin uso.)
    "reportes": "Reportes",
    # 1 oct: herramienta de borrado definitivo de viajes/facturas de prueba
    # (ver app/routes/admin_reset.py) -- NO lleva fila en PERMISSION_CATALOG
    # de abajo a propósito (nunca debe poder otorgarse como "permiso
    # específico" a alguien que no sea Administrador; el acceso se chequea
    # por ROL directamente en el propio código de la ruta). Esta entrada es
    # solo para que su actividad se vea con un nombre legible en la
    # pantalla de Actividad.
    "admin_reset": "Limpieza selectiva",
}

# module -> [(action, etiqueta), ...] en el orden en que se muestran.
PERMISSION_CATALOG = {
    "dashboard": [("view", "Ver")],
    "clientes": [("view", "Ver"), ("edit", "Crear y editar")],
    # 29 sep, pedido de Braulio ("que solo el administrador pueda borrar
    # viajes, mantenimientos, facturas, jornada laboral, guías de remisión
    # e inspecciones"): se agrega "delete" como acción PROPIA (distinta de
    # "edit") en estos 6 módulos -- ningún rol de PERMISSIONS (app/auth.py)
    # la tiene explícitamente, así que por defecto solo ADMIN puede borrar
    # (su "*" le da cualquier acción, ver can()); queda igual disponible
    # acá para poder dársela puntualmente a otro usuario desde Usuarios >
    # Permisos específicos, si Braulio lo pide más adelante -- mismo
    # patrón ya usado para "campo"/"inventario" en Neumáticos.
    "viajes": [
        ("view", "Ver"),
        ("edit", "Crear y editar"),
        # 7 oct, pedido de Braulio: solo administradores por defecto.
        ("edit_closed", "Editar viajes ya entregados, cancelados o pagados"),
        ("delete", "Eliminar"),
    ],
    "guias": [("view", "Ver"), ("edit", "Crear y editar"), ("delete", "Eliminar")],
    "inspecciones": [("view", "Ver"), ("edit", "Registrar"), ("delete", "Eliminar")],
    "flota": [("view", "Ver"), ("edit", "Crear y editar")],
    "conductores": [("view", "Ver"), ("edit", "Crear y editar")],
    "rutas": [("view", "Ver"), ("edit", "Crear y editar")],
    "mantenimiento": [("view", "Ver"), ("edit", "Crear y editar órdenes"), ("delete", "Eliminar")],
    "neumaticos": [
        ("view", "Ver"),
        ("campo", "Operar en campo (inspección, montar, reemplazar, retirar, rotar)"),
        ("inventario", "Gestionar inventario (agregar/editar llantas en stock)"),
    ],
    "liquidaciones": [("view", "Ver"), ("edit", "Crear y editar")],
    "facturacion": [("view", "Ver"), ("edit", "Crear y editar"), ("delete", "Eliminar")],
    "cotizaciones": [("view", "Ver"), ("edit", "Crear y editar")],
    "inventarios": [("view", "Ver"), ("edit", "Crear y editar")],
    "usuarios": [("view", "Ver"), ("edit", "Crear y editar")],
    "catalogos": [("view", "Ver"), ("edit", "Editar")],
    "integraciones": [("view", "Ver"), ("edit", "Editar")],
    "rrhh": [("view", "Ver")],
    "tarifario": [("view", "Ver"), ("edit", "Editar")],
    "pagos_personal": [("view", "Ver"), ("edit", "Editar y generar archivos")],
    "descansos": [("view", "Ver"), ("edit", "Registrar y editar"), ("delete", "Eliminar")],
    "actividad": [("view", "Ver")],
}

# 7 oct, pedido de Braulio: permiso POR REPORTE. Cada reporte del Centro de
# reportes (app/routes/reportes.py) tiene su propia acción dentro del módulo
# "reportes", para poder dar o quitarle a UNA persona un reporte puntual
# desde Usuarios > Permisos específicos (Permitir siempre / Bloquear siempre).
#
# Sin ninguna excepción guardada, nada cambia respecto a como funcionaba:
# cada reporte se ve si el usuario tiene acceso al área a la que pertenece
# (Facturación, Mantenimiento, etc.) -- y, para los reportes que viven dentro
# del módulo Reportes, además "Reportes > Ver". Ver report_access() en
# app/auth.py.
#
# (clave, nombre, área/módulo al que pertenece, ¿vive dentro del módulo Reportes?)
# Los que NO viven dentro de Reportes (comisiones, GPS, resumen contable,
# historial de gastos, export de pagos) siguen siendo pantallas de su módulo
# de siempre; el Centro solo los enlaza. Para estos el permiso por reporte
# también se respeta en su propia ruta (report_required).
REPORTS = [
    ("viajes_por_cliente", "Viajes por cliente", "viajes", True),
    ("viajes_pendientes_facturar", "Viajes pendientes de facturar", "viajes", True),
    ("comisiones_conductor", "Comisiones por conductor", "viajes", False),
    ("gps_diario", "Reporte diario GPS", "integraciones", False),
    ("cuentas_por_cobrar", "Cuentas por cobrar", "facturacion", True),
    ("resumen_contable", "Resumen contable", "liquidaciones", False),
    ("historial_gastos", "Historial de gastos", "liquidaciones", False),
    ("costos_mantenimiento", "Costos de mantenimiento por unidad", "mantenimiento", True),
    ("pagos_por_persona", "Pagos por persona", "pagos_personal", True),
    ("export_pagos_personal", "Export de pagos personal", "pagos_personal", False),
]
REPORT_LABELS = {key: label for key, label, _area, _inside in REPORTS}
REPORT_AREAS = {key: area for key, _label, area, _inside in REPORTS}
REPORT_INSIDE_MODULE = {key for key, _label, _area, inside in REPORTS if inside}

PERMISSION_CATALOG["reportes"] = [("view", "Entrar a Reportes (Centro de reportes y dashboard)")] + [
    (key, f"Reporte: {label} (área {MODULE_LABELS.get(area, area)})")
    for key, label, area, _inside in REPORTS
]
