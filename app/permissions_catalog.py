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
    "integraciones": "Integraciones",
    "rrhh": "RRHH",
    "tarifario": "Tarifario",
    "pagos_personal": "Pagos personal",
    "descansos": "Descansos laborales",
}

# module -> [(action, etiqueta), ...] en el orden en que se muestran.
PERMISSION_CATALOG = {
    "dashboard": [("view", "Ver")],
    "clientes": [("view", "Ver"), ("edit", "Crear y editar")],
    "viajes": [("view", "Ver"), ("edit", "Crear y editar")],
    "guias": [("view", "Ver"), ("edit", "Crear y editar")],
    "inspecciones": [("view", "Ver"), ("edit", "Registrar")],
    "flota": [("view", "Ver"), ("edit", "Crear y editar")],
    "conductores": [("view", "Ver"), ("edit", "Crear y editar")],
    "rutas": [("view", "Ver"), ("edit", "Crear y editar")],
    "mantenimiento": [("view", "Ver"), ("edit", "Crear y editar órdenes")],
    "neumaticos": [
        ("view", "Ver"),
        ("campo", "Operar en campo (inspección, montar, reemplazar, retirar, rotar)"),
        ("inventario", "Gestionar inventario (agregar/editar llantas en stock)"),
    ],
    "liquidaciones": [("view", "Ver"), ("edit", "Crear y editar")],
    "facturacion": [("view", "Ver"), ("edit", "Crear y editar")],
    "cotizaciones": [("view", "Ver"), ("edit", "Crear y editar")],
    "inventarios": [("view", "Ver"), ("edit", "Crear y editar")],
    "usuarios": [("view", "Ver"), ("edit", "Crear y editar")],
    "catalogos": [("view", "Ver"), ("edit", "Editar")],
    "integraciones": [("view", "Ver"), ("edit", "Editar")],
    "rrhh": [("view", "Ver")],
    "tarifario": [("view", "Ver"), ("edit", "Editar")],
    "pagos_personal": [("view", "Ver"), ("edit", "Editar y generar archivos")],
    "descansos": [("view", "Ver"), ("edit", "Registrar y editar")],
}
