import os
from pathlib import Path

BASE_DIR = Path(__file__).parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")
    DATABASE_PATH = os.environ.get(
        "DATABASE_PATH", str(BASE_DIR / "instance" / "erp.db")
    )
    # Si se define, la app usa PostgreSQL (ej. Amazon RDS) en vez de SQLite
    # — ver app/db.py y README, sección "Base de datos persistente en AWS
    # (RDS + S3)". Vacío por defecto: sigue usando DATABASE_PATH como
    # siempre.
    DATABASE_URL = os.environ.get("DATABASE_URL", "")

    # Si se define, los comprobantes de gastos (Liquidaciones) se guardan en
    # este bucket de Amazon S3 en vez de en disco local — ver app/storage.py
    # y el mismo apartado del README. Las credenciales (AWS_ACCESS_KEY_ID /
    # AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION) no se leen aquí: boto3 las
    # toma solo, directo de esas variables de entorno estándar.
    AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "")
    AWS_S3_PREFIX = os.environ.get("AWS_S3_PREFIX", "comprobantes")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # En producción detrás de HTTPS, activa esto en tu entorno:
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "Harraso Transport")

    # Integración con Frotcom (GPS). Ver README, sección "Integración con
    # Frotcom (GPS)" para cómo obtener estas credenciales.
    FROTCOM_BASE_URL = os.environ.get("FROTCOM_BASE_URL", "")
    FROTCOM_USERNAME = os.environ.get("FROTCOM_USERNAME", "")
    FROTCOM_PASSWORD = os.environ.get("FROTCOM_PASSWORD", "")
    # Cada cuántos segundos se sincroniza solo con Frotcom en segundo plano
    # (31 ago, pedido de Braulio: la página de Ubicación GPS se actualiza
    # sola cada 2 minutos). 0 desactiva la sincronización automática (queda
    # solo el botón manual "Sincronizar") — ver app/scheduler.py.
    FROTCOM_AUTO_SYNC_SECONDS = int(os.environ.get("FROTCOM_AUTO_SYNC_SECONDS", "120"))

    # Facturación electrónica SUNAT vía tefacturo.pe (7 sep, pedido de
    # Braulio: "hagamos el enlace de tefacturo.pe para las guias y
    # facturas"). Primer intento (mismo día, antes de esta ronda) se basó en
    # un PDF suelto de 2019, sin autenticación documentada ni endpoint de
    # guía de remisión — Braulio compartió después el portal REAL de
    # documentación técnica (https://api.tefacturo.pe/doc/integracion/),
    # que sí confirma todo esto:
    # - Login: POST https://jarvis.tefacturo.pe/tokenapi/secure/v2/login/token
    #   con {ruc, mail, clave} — un usuario/clave POR RUC, no una "ruta" +
    #   "token" genéricos como se había asumido antes. Harraso y BRMS son
    #   dos empresas con RUC propio, así que cada una necesita su propia
    #   cuenta (usuario/clave) en tefacturo.pe — créala en
    #   https://api.tefacturo.pe/doc/integracion/registro/ (cuenta de
    #   pruebas) o pídesela a tefacturo.pe para producción.
    # - El RUC de cada empresa YA está en COMPANY_RUC/BRMS_RUC más abajo —
    #   no hace falta repetirlo aquí.
    # Ver README, sección "Facturación electrónica (SUNAT)".
    TEFACTURO_BASE_URL = os.environ.get("TEFACTURO_BASE_URL", "https://jarvis.tefacturo.pe")
    HARRASO_TEFACTURO_EMAIL = os.environ.get("HARRASO_TEFACTURO_EMAIL", "")
    HARRASO_TEFACTURO_PASSWORD = os.environ.get("HARRASO_TEFACTURO_PASSWORD", "")
    BRMS_TEFACTURO_EMAIL = os.environ.get("BRMS_TEFACTURO_EMAIL", "")
    BRMS_TEFACTURO_PASSWORD = os.environ.get("BRMS_TEFACTURO_PASSWORD", "")
    # Guía de remisión — TRANSPORTISTA (Harraso/BRMS es el transportista,
    # no el remitente — ver app/integrations/sunat_ose.py): además de
    # RUC/usuario/clave, SUNAT exige el número de registro ante el MTC
    # (Ministerio de Transportes y Comunicaciones) de la empresa que
    # transporta la carga. Vacío por defecto — AJUSTAR con el registro MTC
    # real de Harraso/BRMS antes de poder emitir guías electrónicas.
    HARRASO_MTC_REGISTRATION = os.environ.get("HARRASO_MTC_REGISTRATION", "")
    BRMS_MTC_REGISTRATION = os.environ.get("BRMS_MTC_REGISTRATION", "")
    # Código de almacén ante tefacturo.pe (8 sep, confirmado contra el JSON
    # de ejemplo real de la guía transportista — "codigoAlmacen" es
    # obligatorio y no estaba en la tabla de campos documentada, solo en el
    # ejemplo real). No hay ningún dato parecido en el sistema — probable
    # que lo asigne tefacturo.pe al dar de alta la cuenta/almacén de cada
    # empresa. AJUSTAR con el código real de Harraso/BRMS antes de poder
    # emitir una guía electrónica de verdad — mientras tanto queda vacío y
    # el envío se rechaza con un mensaje claro (ver build_waybill_payload).
    HARRASO_WAREHOUSE_CODE = os.environ.get("HARRASO_WAREHOUSE_CODE", "")
    BRMS_WAREHOUSE_CODE = os.environ.get("BRMS_WAREHOUSE_CODE", "")
    # Datos reales de Harraso Transport S.A.C. (tomados de una cotización
    # real que Braulio compartió, 1 sep) — se usan como default porque
    # antes quedaban vacíos; se pueden sobreescribir por variable de
    # entorno si cambian.
    COMPANY_RUC = os.environ.get("COMPANY_RUC", "20610357726")
    COMPANY_ADDRESS = os.environ.get(
        "COMPANY_ADDRESS", "AV. MANUEL DEL VALLE LT. 15 MZ. X - LIMA LIMA LURIN"
    )
    COMPANY_EMAIL = os.environ.get("COMPANY_EMAIL", "contacto@harraso.com")
    COMPANY_PHONE = os.environ.get("COMPANY_PHONE", "994185119")
    INVOICE_SERIES = os.environ.get("INVOICE_SERIES", "F001")
    # tefacturo.pe exige que la serie de una guía de remisión - transportista
    # empiece con "V" (confirmado en su documentación real, 7 sep) — antes
    # se usaba "T001" por suposición propia. Si ya diste de alta otra serie
    # ante SUNAT/tefacturo.pe para tus guías, ponla aquí por variable de
    # entorno.
    WAYBILL_SERIES = os.environ.get("WAYBILL_SERIES", "V001")
    # Número inicial de Cotizaciones (1 sep) — Braulio pidió seguir la
    # numeración real de sus cotizaciones anteriores (la última que mandó
    # como referencia fue la N° 111), así que el módulo arranca en 112.
    QUOTATION_START_NUMBER = int(os.environ.get("QUOTATION_START_NUMBER", "112"))
    # Datos bancarios para la sección "Datos para la Transferencia" del PDF
    # de Cotizaciones (tomados de la misma cotización real de referencia) —
    # AJUSTAR aquí si las cuentas cambian.
    COMPANY_BANK_NACION_ACCOUNT = os.environ.get(
        "COMPANY_BANK_NACION_ACCOUNT", "00003351882"
    )
    COMPANY_BANK_NACION_CCI = os.environ.get(
        "COMPANY_BANK_NACION_CCI", "01800300000335188240"
    )
    COMPANY_BANK_BCP_SAVINGS_ACCOUNT = os.environ.get(
        "COMPANY_BANK_BCP_SAVINGS_ACCOUNT", "19477004008025"
    )
    COMPANY_BANK_BCP_SAVINGS_CCI = os.environ.get(
        "COMPANY_BANK_BCP_SAVINGS_CCI", "00219417700400802598"
    )
    COMPANY_BANK_BCP_CHECKING_ACCOUNT = os.environ.get(
        "COMPANY_BANK_BCP_CHECKING_ACCOUNT", "1949949117029"
    )

    # BRMS como segunda empresa que puede emitir una Cotización (1 sep,
    # pedido de Braulio: "la cotizacion debes poder elegir entre Harraso o
    # BRMS ... ya que son las 2"). El correo/teléfono de contacto se
    # comparten con Harraso (COMPANY_EMAIL/COMPANY_PHONE) — Braulio
    # confirmó que son los mismos. El RUC y la dirección de BRMS SÍ son
    # propios y todavía no se confirmaron — quedan vacíos a propósito
    # (AJUSTAR: complétalos aquí o por variable de entorno antes de emitir
    # una cotización real a nombre de BRMS, si no el PDF va a salir con esos
    # campos en blanco).
    BRMS_RUC = os.environ.get("BRMS_RUC", "")
    BRMS_ADDRESS = os.environ.get("BRMS_ADDRESS", "")
    # Única cuenta bancaria que se muestra cuando la cotización es de BRMS
    # (Braulio confirmó que, a diferencia de Harraso, BRMS no muestra
    # Banco de la Nación ni cuenta de ahorro — solo esta). El formato del
    # número (agencia-cuenta-dígito-moneda) es el que usa BCP para mostrar
    # sus cuentas a clientes — AJUSTAR el nombre del banco en
    # app/templates/cotizaciones/pdf.html si en realidad es de otro banco.
    BRMS_BANK_ACCOUNT = os.environ.get("BRMS_BANK_ACCOUNT", "480-4768721-0-81")

    # decolecta.com: tipo de cambio SUNAT (liquidación de Gastos) y
    # consulta de RUC (autocompletar proveedor al registrar un gasto).
    # Ambos servicios comparten el mismo token (DECOLECTA_TOKEN) — decisión
    # explícita de Braulio para no pedir una cuenta aparte. Ver README,
    # sección "Liquidaciones", y app/integrations/sunat_exchange_rate.py /
    # app/integrations/sunat_ruc.py.
    DECOLECTA_BASE_URL = os.environ.get("DECOLECTA_BASE_URL", "")
    DECOLECTA_RUC_BASE_URL = os.environ.get("DECOLECTA_RUC_BASE_URL", "")
    DECOLECTA_TOKEN = os.environ.get("DECOLECTA_TOKEN", "")

    # Integración WhatsApp -> n8n -> Liquidaciones (1 sep, pedido de
    # Braulio: "quiero usar n8n para integrar Whatsapp... que tomando una
    # foto a la factura se llene automaticamente los campos"). Secreto
    # compartido que autentica al workflow de n8n contra
    # POST /liquidaciones/whatsapp/intake (ver app/routes/liquidaciones.py)
    # — ese endpoint lo llama un servicio externo, no un usuario con sesión
    # iniciada, así que no usa login ni el csrf_token normal: en su lugar
    # exige este token en la cabecera "X-Webhook-Token". Vacío por defecto
    # a propósito: mientras esté vacío, el endpoint rechaza TODAS las
    # peticiones (nunca queda abierto sin querer). Ver n8n/README-n8n.md
    # para cómo configurarlo en el workflow.
    N8N_WEBHOOK_TOKEN = os.environ.get("N8N_WEBHOOK_TOKEN", "")
