"""Datos de ejemplo: usuarios, clientes, flota, conductores y viajes.

Se usa desde dos lugares:
- seed.py (script manual: `python seed.py`)
- app/__init__.py (automático al iniciar, solo si la tabla de usuarios está
  vacía y AUTO_SEED_DEMO=1 — ver README, sección "Desplegar en Render").
"""
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from app.db import execute, get_db, query_one


def _upsert_user(name, email, password, role):
    existing = query_one("SELECT id FROM users WHERE email = ?", (email,))
    if existing:
        return existing["id"], False
    uid = execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
        (name, email, generate_password_hash(password), role),
    )
    return uid, True


# Catálogos por defecto. Los códigos de "expense_type" se mantienen en
# mayúsculas por compatibilidad con instalaciones previas a la versión con
# catálogos editables; "maintenance_type" siempre fue texto libre, así que
# usa nombres directamente legibles.
DEFAULT_CATALOGS = {
    "expense_type": ["COMBUSTIBLE", "PEAJE", "VIATICOS", "MANTENIMIENTO", "OTRO"],
    "maintenance_type": [
        "Cambio de aceite",
        "Revisión general de frenos",
        "Cambio de llantas",
        "Revisión de sistema eléctrico",
        "Otro",
    ],
    "inspection_item": [
        "Llantas y aros",
        "Frenos",
        "Luces y direccionales",
        "Niveles (aceite, refrigerante, freno)",
        "Extintor y botiquín",
        "Cinturones de seguridad",
        "Espejos y limpiaparabrisas",
        "Documentos del vehículo (SOAT, tarjeta de propiedad)",
    ],
}

# Trabajos de mantenimiento con tiempo estimado (minutos), por defecto.
DEFAULT_JOB_TYPES = [
    ("Cambio de aceite", 60),
    ("Cambio de filtro de aire", 30),
    ("Cambio de filtro de aceite", 20),
    ("Revisión y ajuste de frenos", 90),
    ("Cambio de llantas", 45),
    ("Alineamiento y balanceo", 60),
    ("Revisión de sistema eléctrico", 120),
]


def _seed_catalogs():
    db = get_db()
    for category, items in DEFAULT_CATALOGS.items():
        for order, name in enumerate(items):
            db.execute(
                "INSERT OR IGNORE INTO catalog_items (category, name, sort_order) VALUES (?, ?, ?)",
                (category, name, order),
            )
    for order, (name, minutes) in enumerate(DEFAULT_JOB_TYPES):
        db.execute(
            "INSERT OR IGNORE INTO maintenance_job_types (name, estimated_minutes, sort_order) VALUES (?, ?, ?)",
            (name, minutes, order),
        )
    db.commit()


def seed_demo_data(log=print):
    """Crea usuarios, catálogos y datos de ejemplo si todavía no existen. Es
    seguro llamarla varias veces: no duplica nada. Debe llamarse dentro de un
    app_context (con la base de datos ya inicializada)."""
    log("Usuarios:")
    _, created = _upsert_user("Administrador", "admin@erp.local", "admin1234", "ADMIN")
    log(f"  {'creado' if created else 'ya existe'}: admin@erp.local / admin1234 (ADMIN)")
    _, created = _upsert_user("Operador Demo", "operador@erp.local", "operador1234", "OPERADOR")
    log(f"  {'creado' if created else 'ya existe'}: operador@erp.local / operador1234 (OPERADOR)")

    log("Catálogos (tipos de gasto, conceptos de mantenimiento, trabajos, ítems de inspección)...")
    _seed_catalogs()

    log("Rutas frecuentes con viáticos y comisión de conductor predeterminados...")
    # (origen, destino, viáticos, comisión del conductor)
    routes = [
        ("Lima", "Trujillo", 350.0, 80.0),
        ("Arequipa", "Lima", 400.0, 90.0),
        ("Trujillo", "Piura", 250.0, 60.0),
        ("Lima", "Ica", 200.0, 50.0),
    ]
    for origin, destination, amount, commission in routes:
        execute(
            "INSERT OR IGNORE INTO routes (origin, destination, default_expense_amount, default_commission_amount) VALUES (?, ?, ?, ?)",
            (origin, destination, amount, commission),
        )

    if query_one("SELECT COUNT(*) n FROM clients")["n"] > 0:
        log("Ya existen clientes; se omiten datos de ejemplo adicionales.")
        return

    log("Clientes, flota, conductores y viajes de ejemplo...")
    clients = [
        ("Comercial Andina S.A.C.", "20123456789", "01-555-1010", "contacto@andina.pe", "Av. Argentina 1200, Lima"),
        ("Distribuidora del Sur E.I.R.L.", "20456789123", "01-555-2020", "ventas@delsur.pe", "Av. Los Incas 500, Arequipa"),
        ("Agroindustrias Norte S.A.", "20789123456", "044-555-3030", "logistica@agronorte.pe", "Panamericana Norte km 12, Trujillo"),
    ]
    client_ids = [
        execute("INSERT INTO clients (name, ruc, phone, email, address) VALUES (?, ?, ?, ?, ?)", c)
        for c in clients
    ]

    def _d(days):
        return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    # (placa, marca, modelo, capacidad_kg, estado, tipo_unidad, kilometraje_actual,
    #  vencimiento_soat, vencimiento_revision_tecnica)
    vehicles = [
        # SOAT por vencer pronto, para ver la alerta funcionando en la demo.
        ("ABC-123", "Volvo", "FH 460", 28000, "ACTIVO", "TRACTO", 118500, _d(15), _d(200)),
        # Revisión técnica ya vencida, para ver esa alerta también.
        ("XYZ-789", "Scania", "R450", 25000, "ACTIVO", "TRACTO", 76200, _d(200), _d(-5)),
        ("DEF-456", "Mercedes-Benz", "Actros", 30000, "MANTENIMIENTO", "TRACTO", 142300, _d(180), _d(180)),
        # Carreta (semirremolque) de ejemplo, para mostrar el diagrama de
        # neumáticos de 3 ejes en la demo.
        ("TRL-321", "Randon", "Semirremolque 3 ejes", 32000, "ACTIVO", "CARRETA", 95000, _d(10), _d(200)),
    ]
    vehicle_ids = [
        execute(
            """INSERT INTO vehicles (plate, brand, model, capacity_kg, status, vehicle_type, current_km,
               current_km_updated_at, soat_expiry, technical_review_expiry)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*v[:7], datetime.now().strftime("%Y-%m-%d"), v[7], v[8]),
        )
        for v in vehicles
    ]

    # (nombre, dni, n_licencia, vence_brevete, telefono,
    #  fecha_examen_medico, vence_examen_medico,
    #  fecha_examen_manejo_backus, vence_examen_manejo_backus,
    #  fecha_capacitacion_backus, vence_capacitacion_backus,
    #  fecha_dds, vence_dds)
    drivers = [
        # Examen médico y DDS por vencer pronto, para ver ambas alertas en la demo.
        ("Carlos Ramírez", "45678912", "Q12345678", _d(200), "987654321",
         _d(-350), _d(10), _d(-100), _d(260), _d(-60), _d(300), _d(-335), _d(25)),
        # Brevete por vencer pronto (ya existía en la demo) y examen de
        # manejo Backus ya vencido, para ver ambas alertas.
        ("Luis Fernández", "41234567", "Q87654321", _d(20), "987123456",
         _d(-300), _d(60), _d(-370), _d(-5), _d(-30), _d(330), _d(-10), _d(350)),
        # Todo al día.
        ("Jorge Quispe", "47891234", "Q11223344", _d(400), "912345678",
         _d(-30), _d(330), _d(-60), _d(300), _d(-15), _d(345), _d(-5), _d(355)),
    ]
    driver_ids = [
        execute(
            """INSERT INTO drivers (name, document_number, license_number, license_expiry, phone,
               medical_exam_date, medical_exam_expiry,
               backus_driving_exam_date, backus_driving_exam_expiry,
               backus_training_date, backus_training_expiry,
               dds_date, dds_expiry)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            d,
        )
        for d in drivers
    ]

    today = datetime.now()
    # (cliente, unidad, conductor, origen, destino, carga, peso, día relativo, estado, tarifa, comisión conductor)
    trips = [
        (client_ids[0], vehicle_ids[0], driver_ids[0], "Lima", "Trujillo", "Electrodomésticos", 8000, -5, "ENTREGADO", 2500, 80.0),
        (client_ids[1], vehicle_ids[1], driver_ids[1], "Arequipa", "Lima", "Textiles", 5000, -2, "ENTREGADO", 1800, 90.0),
        (client_ids[2], vehicle_ids[0], driver_ids[2], "Trujillo", "Piura", "Productos agrícolas", 12000, 1, "EN_CURSO", 2200, 60.0),
        (client_ids[0], vehicle_ids[1], driver_ids[0], "Lima", "Ica", "Materiales de construcción", 9000, 3, "PENDIENTE", 1500, 50.0),
        # Segundo viaje del mismo conductor por la misma ruta, para que el
        # reporte mensual de comisiones muestre más de un viaje agrupado.
        (client_ids[1], vehicle_ids[0], driver_ids[0], "Lima", "Trujillo", "Repuestos", 6000, -10, "ENTREGADO", 2400, 80.0),
    ]
    for i, (cid, vid, did, origin, dest, cargo, weight, day_offset, status, rate, commission) in enumerate(trips, start=1):
        scheduled = (today + timedelta(days=min(day_offset, 0))).strftime("%Y-%m-%d")
        delivered = scheduled if status == "ENTREGADO" else None
        code = f"V-{i:04d}"
        execute(
            """INSERT INTO trips (code, client_id, vehicle_id, driver_id, origin, destination,
               cargo_description, cargo_weight_kg, scheduled_date, delivered_date, status, rate, driver_commission)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, cid, vid, did, origin, dest, cargo, weight, scheduled, delivered, status, rate, commission),
        )

    execute(
        """INSERT INTO maintenance_records (vehicle_id, type, maintenance_date, cost, description, odometer_km, next_due_date, next_due_km)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            vehicle_ids[2],
            "Revisión general de frenos",
            (today - timedelta(days=3)).strftime("%Y-%m-%d"),
            850.0,
            "Cambio de pastillas y revisión de sistema hidráulico",
            142300,
            (today + timedelta(days=10)).strftime("%Y-%m-%d"),
            143000,
        ),
    )
    # Unidad ABC-123: próxima a su mantenimiento por kilometraje (para ver
    # la alerta funcionando de una vez en la demo).
    execute(
        """INSERT INTO maintenance_records (vehicle_id, type, maintenance_date, cost, description, odometer_km, next_due_km)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            vehicle_ids[0],
            "Cambio de aceite",
            (today - timedelta(days=60)).strftime("%Y-%m-%d"),
            320.0,
            "Cambio de aceite y filtros",
            113000,
            119000,
        ),
    )
    # Neumáticos de ejemplo: unidad ABC-123 (tracto, 10 posiciones) con casi
    # todas sus llantas registradas y con distintos niveles de desgaste
    # (para ver las tres alertas de color en la demo), y la carreta TRL-321
    # con solo dos ejes cargados, dejando el tercero vacío a propósito para
    # mostrar el flujo de "+ Agregar" en una posición libre.
    tracto_id = vehicle_ids[0]  # ABC-123, current_km = 118500
    carreta_id = vehicle_ids[3]  # TRL-321, current_km = 95000
    install_date = (today - timedelta(days=200)).strftime("%Y-%m-%d")
    tire_seed = [
        # (vehicle_id, position_code, brand, km_at_install, expected_life_km) -> ~11% de vida útil (OK)
        (tracto_id, "EJE1_IZQ", "Michelin", 110000, 80000),
        (tracto_id, "EJE1_DER", "Michelin", 110000, 80000),
        # ~98% de vida útil (a reemplazar)
        (tracto_id, "EJE2_IZQ_EXT", "Bridgestone", 40000, 80000),
        (tracto_id, "EJE2_IZQ_INT", "Bridgestone", 40000, 80000),
        (tracto_id, "EJE2_DER_INT", "Bridgestone", 40000, 80000),
        (tracto_id, "EJE2_DER_EXT", "Bridgestone", 40000, 80000),
        # ~67% de vida útil (por vencer)
        (tracto_id, "EJE3_IZQ_EXT", "Goodyear", 65000, 80000),
        (tracto_id, "EJE3_IZQ_INT", "Goodyear", 65000, 80000),
        # eje 3 derecho se deja sin registrar a propósito (posición vacía en la demo)
        # Carreta: eje 1 casi nuevo, eje 2 por vencer, eje 3 vacío.
        (carreta_id, "EJE1_IZQ_EXT", "Michelin", 90000, 80000),
        (carreta_id, "EJE1_IZQ_INT", "Michelin", 90000, 80000),
        (carreta_id, "EJE1_DER_INT", "Michelin", 90000, 80000),
        (carreta_id, "EJE1_DER_EXT", "Michelin", 90000, 80000),
        (carreta_id, "EJE2_IZQ_EXT", "Bridgestone", 30000, 80000),
        (carreta_id, "EJE2_IZQ_INT", "Bridgestone", 30000, 80000),
        (carreta_id, "EJE2_DER_INT", "Bridgestone", 30000, 80000),
        (carreta_id, "EJE2_DER_EXT", "Bridgestone", 30000, 80000),
    ]
    for vid, code, brand, km_install, life_km in tire_seed:
        execute(
            """INSERT INTO tires (vehicle_id, position_code, brand, install_date, km_at_install, expected_life_km)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (vid, code, brand, install_date, km_install, life_km),
        )

    log("Check List de Tracto y de Carreta de ejemplo (ABC-123 / TRL-321)...")
    from app.detailed_checklists import SPARE_TIRE_ITEM, TIRE_SECTION_KEY, sections_for
    from app.tire_positions import get_positions

    checklist_inspection_id = execute(
        """INSERT INTO inspections (vehicle_id, driver_id, type, inspection_date, notes,
           checklist_code, location, odometer_km)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tracto_id,
            driver_ids[0],
            "PRE",
            (today - timedelta(days=1)).strftime("%Y-%m-%d"),
            "Checklist de ejemplo — todo en orden salvo el foco delantero izquierdo.",
            "CL-0001",
            "Pucallpa",
            118500,
        ),
    )
    for section in sections_for("TRACTO"):
        for idx, item_name in enumerate(section["checklist_items"]):
            # Un solo ítem de ejemplo con falla, para ver la alerta en la lista.
            is_falla = section["key"] == "REVISION_GENERAL" and idx == 0
            status = "FALLA" if is_falla else "OK"
            observation = "Foco quemado, se repuso en el mismo día." if is_falla else ""
            execute(
                """INSERT INTO inspection_items (inspection_id, item_name, status, observation, section, extra_value)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (checklist_inspection_id, item_name, status, observation, section["key"], None),
            )
    tracto_positions = get_positions("TRACTO")
    for i, p in enumerate(tracto_positions):
        execute(
            """INSERT INTO inspection_items (inspection_id, item_name, status, observation, section, extra_value)
               VALUES (?, ?, 'NA', ?, ?, ?)""",
            (checklist_inspection_id, p["label"], "", TIRE_SECTION_KEY, f"LL-{i + 1:03d}"),
        )
    execute(
        """INSERT INTO inspection_items (inspection_id, item_name, status, observation, section, extra_value)
           VALUES (?, ?, 'NA', ?, ?, ?)""",
        (checklist_inspection_id, SPARE_TIRE_ITEM, "", TIRE_SECTION_KEY, f"LL-{len(tracto_positions) + 1:03d}"),
    )

    carreta_checklist_id = execute(
        """INSERT INTO inspections (vehicle_id, driver_id, type, inspection_date, notes,
           checklist_code, location, odometer_km)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            carreta_id,
            driver_ids[1] if len(driver_ids) > 1 else driver_ids[0],
            "PRE",
            (today - timedelta(days=2)).strftime("%Y-%m-%d"),
            "Checklist de ejemplo de carreta — muelles a revisar en el próximo mantenimiento.",
            "CL-0002",
            "Tarapoto",
            None,
        ),
    )
    for section in sections_for("CARRETA"):
        for idx, item_name in enumerate(section["checklist_items"]):
            # Un ítem de ejemplo con falla, para ver la alerta en la lista.
            is_falla = idx == 0
            status = "FALLA" if is_falla else "OK"
            observation = "Muelle con desgaste visible, agendar revisión." if is_falla else ""
            execute(
                """INSERT INTO inspection_items (inspection_id, item_name, status, observation, section, extra_value)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (carreta_checklist_id, item_name, status, observation, section["key"], None),
            )
    for i, p in enumerate(get_positions("CARRETA")):
        # Las dos primeras posiciones muestran la presión de ejemplo, tal
        # como se anotaría en el formato físico de carreta.
        observation = "Presión: 100 psi." if i < 2 else ""
        execute(
            """INSERT INTO inspection_items (inspection_id, item_name, status, observation, section, extra_value)
               VALUES (?, ?, 'NA', ?, ?, ?)""",
            (carreta_checklist_id, p["label"], observation, TIRE_SECTION_KEY, f"LL-{i + 21:03d}"),
        )
    execute(
        """INSERT INTO inspection_items (inspection_id, item_name, status, observation, section, extra_value)
           VALUES (?, ?, 'NA', ?, ?, ?)""",
        (carreta_checklist_id, SPARE_TIRE_ITEM, "", TIRE_SECTION_KEY, "LL-033"),
    )

    log("Listo. Inicia sesión con:")
    log("  admin@erp.local / admin1234  (Administrador)")
    log("  operador@erp.local / operador1234  (Operador)")
