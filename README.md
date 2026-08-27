# ERP de Transporte de Carga — Harraso Transport

Sistema web para gestionar Harraso Transport (empresa de transporte de carga): viajes/órdenes de servicio, flota y conductores, clientes, gastos, mantenimiento y facturación, con usuarios y roles (Administrador / Operador).

## Marca

El logo de Harraso Transport / BRMS está en `app/static/img/` (`logo-lockup.png`, el logotipo horizontal usado en el login y el menú lateral; `logo-mark.png`, la marca circular "B" usada como base de los íconos de la app). Los íconos de la PWA (`app/static/icons/icon-192.png` y `icon-512.png`) y el manifest (`app/static/manifest.webmanifest`) ya están generados a partir de ese logo. `COMPANY_NAME` (variable de entorno, ver `.env.example`) controla el nombre que se muestra junto al logo y en el título de la pestaña del navegador — por defecto "Harraso Transport". Si el logo cambia, basta con reemplazar esos dos archivos PNG y regenerar los íconos con el mismo recorte/relleno.

## Stack técnico

- **Backend:** Python 3 + Flask (sin frameworks adicionales de por medio).
- **Base de datos:** SQLite (archivo único, sin servidor de base de datos que configurar). Fácil de migrar a PostgreSQL más adelante si el negocio crece.
- **Frontend:** plantillas Jinja2 renderizadas en el servidor + CSS propio (sin dependencias externas ni CDNs).
- **Autenticación:** sesiones de Flask (cookies firmadas) + contraseñas con hash (Werkzeug).

## Módulos incluidos

- **Panel:** indicadores clave (viajes activos, ingresos del mes, facturas por cobrar, unidades en mantenimiento) y alertas de licencias/mantenimientos próximos a vencer.
- **Viajes / Órdenes de servicio:** registro de viajes con cliente, unidad, conductor, ruta, carga y tarifa; flujo de estados Pendiente → En curso → Entregado (o Cancelado).
- **Clientes:** datos de contacto y facturación.
- **Flota y Conductores:** unidades (placa, capacidad, estado) y conductores (licencia, vencimiento, estado).
- **Gastos:** combustible, peajes, viáticos, mantenimiento u otros, asociados a un viaje y/o unidad. Filtrable por tipo y rango de fechas, con exportación a Excel (`.xlsx`) agrupada por tipo, con subtotales y total general. Se puede adjuntar el comprobante (foto o PDF) a cada gasto, definir **presupuestos mensuales** por unidad o tipo (con alerta en el Panel al acercarse o superarse), y gestionar **anticipos de viáticos** por viaje con su liquidación — ver la sección "Gastos, presupuestos y viáticos" más abajo.
- **Rutas:** catálogo de rutas frecuentes con un monto de viáticos predeterminado, usado para sugerir el anticipo de gastos de cada viaje.
- **Mantenimiento:** historial de mantenimientos por unidad, costo, kilometraje registrado y próxima fecha/kilometraje. Los conceptos (tipos de mantenimiento) se administran desde Catálogos. Si indicas el kilometraje al registrar un mantenimiento, actualiza automáticamente el kilometraje actual de la unidad. Incluye un catálogo de **trabajos con tiempo estimado** (ej. cambio de aceite = 60 min) que se seleccionan al registrar un mantenimiento, y una vista de **historial y costos totales por unidad**.
- **Inspecciones:** checklist de inspección de una unidad (llantas, frenos, luces, etc.) antes de salir o al llegar de un viaje; los ítems del checklist se administran desde Catálogos.
- **Facturación:** genera facturas por cliente a partir de viajes entregados y aún no facturados; controla estado (pendiente, pagada, vencida, anulada); puede enviarse electrónicamente a SUNAT — ver la sección dedicada más abajo.
- **Guías de Remisión:** genera la guía de remisión electrónica ("modalidad Transportista") de un viaje, con los datos de traslado, vehículo y conductor; puede enviarse a SUNAT igual que las facturas.
- **Usuarios:** solo el Administrador puede crear usuarios y asignar el rol Administrador u Operador.
- **Catálogos** (solo Administrador): agrega o desactiva conceptos de mantenimiento y tipos de gasto desde la web, sin tocar código.
- **Ubicación GPS** (solo Administrador): integración con Frotcom para ver la última posición conocida de cada unidad — ver la sección dedicada más abajo.
- **App instalable (PWA):** el sistema se puede "instalar" desde el navegador del celular o la computadora (ícono propio, se abre como app). No requiere nada adicional de tu parte.

### Alertas del panel

Además de licencias y mantenimientos por vencer (fecha), el panel avisa cuando una unidad se acerca (o ya superó) su próximo mantenimiento **por kilometraje**, comparando el kilometraje actual de la unidad contra el kilometraje programado del mantenimiento. El umbral de aviso son 1000 km (ajustable en `KM_ALERT_THRESHOLD`, en `app/routes/mantenimiento.py`). El kilometraje actual de una unidad se actualiza de tres formas: editándolo manualmente en Flota, indicándolo al registrar un mantenimiento, o automáticamente vía la sincronización con Frotcom (si está configurada).

### Permisos por rol

| Módulo | Administrador | Operador |
|---|---|---|
| Panel | Ver | Ver |
| Viajes | Ver/Crear/Editar | Ver/Crear/Editar |
| Clientes | Ver/Crear/Editar | Ver/Crear/Editar |
| Flota y Conductores | Ver/Crear/Editar | Solo ver |
| Gastos (incl. presupuestos y viáticos) | Ver/Crear/Editar | Ver/Crear/Editar |
| Rutas | Ver/Crear/Editar | Solo ver |
| Mantenimiento (incl. trabajos y costos por unidad) | Ver/Crear/Editar | Solo ver |
| Inspecciones | Ver/Crear/Editar | Ver/Crear/Editar |
| Facturación | Ver/Crear/Editar/Enviar a SUNAT | Sin acceso |
| Guías de Remisión | Ver/Crear/Editar/Enviar a SUNAT | Ver/Crear/Editar/Enviar a SUNAT |
| Usuarios | Ver/Crear/Editar | Sin acceso |
| Catálogos | Ver/Crear/Editar | Sin acceso |
| Ubicación GPS | Ver/Sincronizar | Sin acceso |

Esto se puede ajustar fácilmente editando el diccionario `PERMISSIONS` en `app/auth.py`.

## Mantenimiento: trabajos con tiempo estimado e historial por unidad

Desde **Mantenimiento → Trabajos y tiempos** se administra un catálogo de trabajos de mantenimiento con su tiempo estimado en minutos (ej. "Cambio de aceite" = 60 min, "Cambio de filtro de aire" = 30 min), con un botón para agregar trabajos nuevos que no estén en la lista. Al registrar un mantenimiento, se pueden marcar uno o más trabajos realizados y el sistema suma automáticamente el tiempo estimado total (útil para planificar cuánto tiempo va a estar la unidad en taller). Esto es independiente del "Concepto" de mantenimiento (que sigue viniendo del catálogo en Catálogos) — el concepto clasifica el registro, los trabajos estiman el tiempo.

**Mantenimiento → Historial y costos por unidad** muestra, por cada unidad, el número de mantenimientos, el costo total acumulado y la fecha del último, con un enlace para ver el historial completo de esa unidad.

## Inspecciones de unidades

Desde el detalle de un viaje (o desde el menú **Inspecciones**) se puede registrar un checklist de inspección de la unidad — "antes de salir" o "al llegar" — con un ítem por fila (llantas, frenos, luces, niveles, extintor, etc.), marcando cada uno como OK, Falla o N/A, y una observación opcional. Los ítems del checklist se administran desde **Catálogos → Ítems de inspección** (solo Administrador), igual que los conceptos de mantenimiento o los tipos de gasto.

## Gastos: presupuestos y viáticos

- **Comprobantes:** al registrar un gasto puedes adjuntar una foto o un PDF del recibo/boleta, de dos formas: el campo "tomar foto ahora" abre la cámara directamente en el celular (usa el atributo estándar HTML `capture="environment"` — funciona en Chrome/Android y en Safari/iOS al agregar el sistema a la pantalla de inicio como PWA); el campo "o subir un archivo" abre el selector normal de galería/archivos, para elegir una foto ya tomada o un PDF. Solo se llena uno de los dos. El archivo se guarda en el servidor y aparece como enlace "Ver" en la lista de gastos. *Aviso:* en hosting gratuito con disco efímero (ver la sección de Render más abajo) estos archivos se pierden al reiniciar/redesplegar, igual que la base de datos SQLite — el mismo `AUTO_SEED_DEMO` no los recupera. Para un uso serio en producción, sube a un plan con disco persistente (ver esa sección).
- **Presupuestos** (Gastos → Presupuestos): define un tope mensual de gasto por unidad o por tipo de gasto (ej. "Combustible: S/ 2000/mes"). Cuando el gasto acumulado del mes llega al 90% del presupuesto o lo supera, aparece una alerta en el Panel.
- **Rutas y viáticos** (menú Rutas): define, por cada ruta frecuente (origen → destino), un monto estándar de viáticos. Desde el detalle de un viaje puedes "Confirmar anticipo de viáticos" — el sistema sugiere el monto de la ruta si existe una configurada, y confirma que el conductor recibió ese dinero. Más adelante, desde el mismo anticipo, el botón "Liquidar" compara el monto entregado contra la suma de los gastos reales registrados para ese viaje y muestra el saldo a favor o el exceso.

## Integración con Frotcom (GPS)

El sistema incluye la integración lista para conectarse a Frotcom y mostrar la última ubicación conocida de cada unidad (Menú → Ubicación GPS, solo Administrador). Para activarla:

1. Solicita a Frotcom (contacta a tu ejecutivo de cuenta o a su soporte) acceso a su **"API V2"** — te darán una URL base y credenciales (usuario/contraseña o token).
2. Define estas variables de entorno con esos datos: `FROTCOM_BASE_URL`, `FROTCOM_USERNAME`, `FROTCOM_PASSWORD`.
3. En cada unidad (Flota → editar unidad), completa el campo **"ID en el proveedor de GPS"** con el identificador exacto que usa Frotcom para esa unidad (puede ser su ID interno o la placa, según cómo esté configurada tu cuenta).
4. Entra a **Ubicación GPS** y haz clic en "Sincronizar con Frotcom".

**Aviso importante:** el cliente de Frotcom (`app/integrations/frotcom.py`) implementa el patrón más común para este tipo de APIs (login → token → consulta de posiciones), pero Frotcom entrega la documentación exacta de sus endpoints dentro de tu propia cuenta (es autodocumentada y puede variar por plan/región). **Esta integración no pudo probarse contra la API real** porque no se contó con credenciales de Frotcom durante el desarrollo. Cuando tengas tus credenciales, revisa la referencia de tu cuenta y ajusta en ese archivo (están marcados con comentarios "AJUSTAR"):
- El endpoint y formato exacto de login.
- El endpoint que devuelve las posiciones/odómetro y los nombres de sus campos.

El resto del sistema funciona con total normalidad sin esta integración — simplemente no habrá datos de ubicación hasta confirmarla.

## Facturación electrónica (SUNAT)

El sistema puede emitir **facturas** y **guías de remisión electrónica (modalidad Transportista)** y enviarlas a SUNAT. Antes de activarlo conviene entender que hay dos caminos totalmente distintos, y el sistema usa el segundo:

**Camino 1 — Conexión directa a SUNAT ("SEE del Contribuyente").** Requiere comprar un certificado digital propio (~S/150–600/año), armar y firmar digitalmente el XML en formato UBL 2.1, y hablar el webservice SOAP de SUNAT. Es una integración pesada, cara de mantener y con un margen de error alto si no se hace con experiencia previa — normalmente solo tiene sentido para empresas grandes con muchísimo volumen de comprobantes.

**Camino 2 — A través de un OSE (Operador de Servicios Electrónicos), como NubeFacT, Efact, BizLinks o Facturalo Perú.** Le mandas un JSON simple por HTTPS con los datos del comprobante, el OSE arma el XML, lo firma con su propio certificado, lo envía a SUNAT y te devuelve el PDF, el XML y la constancia de aceptación (CDR). Es el camino que usa casi cualquier negocio pequeño o mediano, y **es el que implementa este sistema** (`app/integrations/sunat_ose.py`), siguiendo el formato público que documenta NubeFacT (RUTA + TOKEN + JSON), que es el más extendido entre OSEs peruanos orientados a REST. El OSE cobra por comprobante emitido (consulta tarifas directamente con el proveedor que elijas) — es un costo aparte del hosting del ERP.

Ten en cuenta también que, según cambios normativos recientes, los negocios que superan cierto nivel de ingresos anuales (un umbral en UIT) están obligados a emitir a través de un OSE en vez de ir directo a SUNAT — confírmalo con tu contador, ya que las cifras y fechas exactas de esta obligación cambian con el tiempo.

### Cómo activarlo

1. Contrata un OSE autorizado por SUNAT (por ejemplo, NubeFacT: [nubefact.com](https://www.nubefact.com)) y crea primero una cuenta de **pruebas/sandbox**.
2. Con las credenciales que te den, define estas variables de entorno:
   - `OSE_RUTA`: la URL del endpoint que te indique tu OSE.
   - `OSE_TOKEN`: el token de autenticación de tu cuenta.
   - `COMPANY_RUC` y `COMPANY_ADDRESS`: el RUC y la dirección fiscal de tu propia empresa (el emisor de los comprobantes).
   - `INVOICE_SERIES` y `WAYBILL_SERIES`: las series que hayas dado de alta para facturas y guías (por defecto `F001` y `T001`).
3. Registra el RUC de cada cliente (Clientes → editar) — es obligatorio para emitir una factura electrónica.
4. Para conductores, registra también su **DNI** (Flota → Conductores → editar) — se necesita para las guías de remisión.
5. Emite una factura de prueba (Facturación → detalle de una factura → "Enviar a SUNAT") o una guía (Viajes → viaje en curso/entregado → "Generar guía de remisión" → "Enviar a SUNAT") y revisa la respuesta.

### Aviso importante — léelo antes de emitir comprobantes reales

Esta integración **no ha podido probarse contra una cuenta real de ningún OSE**, porque esta instalación no cuenta con credenciales de NubeFacT ni de otro proveedor. El archivo `app/integrations/sunat_ose.py` sigue el formato público que NubeFacT documenta en su web, pero varios valores son catálogos oficiales de SUNAT que **debes confirmar antes de usarlo en producción** (están marcados con comentarios "AJUSTAR" en el código):

- Tipo de comprobante (factura vs. guía transportista).
- Tipo de documento de identidad del cliente/conductor (RUC, DNI).
- Unidad de medida de los ítems.
- Tipo de IGV.
- Motivo de traslado y modalidad de transporte (específicos de la guía de remisión).
- El formato exacto de la respuesta del OSE (nombres de campos de éxito/error, enlaces de PDF/XML/CDR).

**Antes de emitir un solo comprobante a un cliente real, pide a tu contador (o a alguien con experiencia en facturación electrónica peruana) que revise los primeros comprobantes emitidos en modo de pruebas.** Un código de catálogo equivocado puede hacer que SUNAT rechace el comprobante, o que lo acepte mal clasificado — con las implicancias tributarias que eso conlleva. Mientras tanto, el resto del sistema (facturas y guías internas, control de estados, historial) funciona con normalidad sin esta integración; simplemente los comprobantes quedan como "No enviados" hasta que la actives y confirmes.

## Cómo correrlo en tu computadora

Requisitos: Python 3.10 o superior.

```bash
# 1. Entra a la carpeta del proyecto
cd erp-transporte

# 2. Crea un entorno virtual (recomendado)
python3 -m venv .venv
source .venv/bin/activate        # En Windows: .venv\Scripts\activate

# 3. Instala las dependencias
pip install -r requirements.txt

# 4. Copia el archivo de variables de entorno
cp .env.example .env
# Abre .env y cambia SECRET_KEY por un valor aleatorio y largo

# 5. Carga datos de ejemplo (usuarios, clientes, flota, viajes)
python seed.py

# 6. Inicia el servidor de desarrollo
python run.py
```

Abre http://localhost:5000 en tu navegador. Usuarios de ejemplo creados por `seed.py`:

- **Administrador:** `admin@erp.local` / `admin1234`
- **Operador:** `operador@erp.local` / `operador1234`

**Importante:** cambia estas contraseñas (o crea tus propios usuarios y desactiva estos) antes de usar el sistema con datos reales.

## Cómo desplegarlo en Render (plan gratuito)

El proyecto ya incluye `requirements.txt` (con `gunicorn` para producción) y un `Procfile`, listos para Render.

### 1. Sube el proyecto a GitHub

Necesitas un repositorio en GitHub porque Render despliega desde ahí.

1. Crea un repositorio nuevo y vacío en [github.com/new](https://github.com/new) (sin README, sin .gitignore — ya tenemos uno).
2. En tu terminal, dentro de la carpeta `erp-transporte`:
   ```bash
   git init
   git add .
   git commit -m "ERP de transporte inicial"
   git branch -M main
   git remote add origin https://github.com/TU-USUARIO/TU-REPO.git
   git push -u origin main
   ```

### 2. Crea el Web Service en Render

1. Entra a [render.com](https://render.com) y crea una cuenta (puedes usar tu cuenta de GitHub para registrarte, así Render ya queda conectado).
2. Click en **New +** → **Web Service**.
3. Conecta el repositorio que acabas de subir.
4. Configura:
   - **Name:** `erp-transporte` (o el nombre que prefieras; será parte de la URL pública).
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn run:app`
   - **Instance Type:** Free
5. En **Environment Variables**, agrega:
   - `SECRET_KEY` → un valor largo y aleatorio (por ejemplo, genera uno con `python3 -c "import secrets; print(secrets.token_hex(32))"`)
   - `COMPANY_NAME` → el nombre de tu empresa
   - `SESSION_COOKIE_SECURE` → `1`
6. Click en **Create Web Service**. Render construye y despliega automáticamente (toma 1-2 minutos). Al terminar te da una URL pública tipo `https://erp-transporte.onrender.com` — esa es la que compartes.

### 3. Inicia sesión

Abre la URL y entra con los usuarios de ejemplo (se crean solos, ver siguiente sección):

- **Administrador:** `admin@erp.local` / `admin1234`
- **Operador:** `operador@erp.local` / `operador1234`

Cambia estas contraseñas desde el módulo **Usuarios** en cuanto quieras usarlo con datos reales.

### Sobre el plan gratuito: la base de datos se reinicia

Render explica en su documentación que, sin un disco persistente (que solo está disponible en planes de pago), **cualquier archivo local se pierde cada vez que el servicio se reinicia o se vuelve a desplegar** — y en el plan gratuito el servicio se "duerme" tras 15 minutos sin visitas y se reinicia al recibir la siguiente solicitud. Como este proyecto guarda todo en un archivo SQLite, eso significa que los datos que se carguen mientras el servicio está despierto se conservan, pero pueden perderse cuando vuelve a dormirse. ([Persistent Disks – Render Docs](https://render.com/docs/disks); [Deploy for Free – Render Docs](https://render.com/docs/free))

Para que el enlace siempre funcione para que la gente lo pruebe, la app ya viene preparada para esto: **si detecta que la base de datos está vacía, recrea automáticamente los usuarios y datos de ejemplo al iniciar** (ver `AUTO_SEED_DEMO` en `app/__init__.py`). Así, aunque el servicio se duerma y despierte, el link de demo siempre va a tener con qué iniciar sesión — perfecto para que la gente lo pruebe. Lo que sí puede perderse entre reinicios es cualquier dato *nuevo* que alguien cargue durante una sesión de prueba (un cliente o viaje que agreguen ellos, por ejemplo).

Si más adelante quieres usarlo en serio con tu negocio (datos que no se deben perder nunca), tienes dos opciones:

- **Subir a un plan de pago con disco persistente** (Render: agrega un "Persistent Disk" montado en `/opt/render/project/src/instance` y define la variable `DATABASE_PATH=/opt/render/project/src/instance/erp.db`; luego pon `AUTO_SEED_DEMO=0` para que no se recreen los usuarios de ejemplo).
- **Migrar a PostgreSQL** (ver siguiente sección) — Render ofrece una base de datos Postgres gratuita, aunque expira 30 días después de creada.

### Alternativa: crecer a PostgreSQL

Si el negocio crece y necesitas más de un servidor, mayor concurrencia, o simplemente no quieres depender de un disco persistente, el siguiente paso natural es migrar de SQLite a PostgreSQL (por ejemplo con Neon, Supabase, o el Postgres de Render/Railway). La capa de acceso a datos está centralizada en `app/db.py`, lo que facilita ese cambio más adelante.

## Estructura del proyecto

```
erp-transporte/
├── app/
│   ├── __init__.py         # fábrica de la aplicación Flask
│   ├── auth.py              # login, sesiones y permisos por rol
│   ├── db.py                 # acceso a SQLite
│   ├── helpers.py            # utilidades (fechas, montos, códigos correlativos, pretty_label)
│   ├── reports.py             # generación del reporte de gastos en Excel
│   ├── seed_data.py           # datos y catálogos de ejemplo (usados por seed.py y por el auto-seed)
│   ├── schema.sql            # esquema de la base de datos
│   ├── integrations/
│   │   ├── frotcom.py          # cliente de la API de Frotcom (GPS) — ver aviso en el archivo
│   │   └── sunat_ose.py         # cliente OSE para facturación electrónica SUNAT — ver aviso en el archivo
│   ├── routes/                # un blueprint por módulo
│   │   ├── dashboard.py
│   │   ├── clientes.py
│   │   ├── flota.py
│   │   ├── viajes.py
│   │   ├── gastos.py
│   │   ├── mantenimiento.py
│   │   ├── facturacion.py
│   │   ├── guias.py              # guías de remisión electrónica (modalidad Transportista)
│   │   ├── inspecciones.py        # checklist de inspección de unidades
│   │   ├── rutas.py                # rutas frecuentes con viáticos predeterminados
│   │   ├── viaticos.py             # anticipos de viáticos y liquidación
│   │   ├── usuarios.py
│   │   ├── catalogos.py         # catálogos editables (conceptos, tipos de gasto, ítems de inspección)
│   │   └── integraciones.py      # pantalla y sincronización de ubicación GPS
│   ├── templates/             # vistas Jinja2, organizadas por módulo
│   └── static/
│       ├── css/style.css        # estilos (sin dependencias externas)
│       ├── manifest.webmanifest  # manifest de la PWA
│       ├── sw.js                  # service worker (cache mínimo de estáticos)
│       └── icons/                  # íconos de la PWA
├── config.py                  # configuración (lee variables de entorno)
├── seed.py                    # datos de ejemplo
├── run.py                     # punto de entrada para desarrollo
├── requirements.txt
├── Procfile                   # para despliegue con gunicorn
└── .env.example
```

## Próximos pasos sugeridos

- Terminar de confirmar y probar la integración con Frotcom contra tu cuenta real (ver sección dedicada arriba).
- Contratar un OSE, confirmar los catálogos SUNAT marcados como "AJUSTAR" y probar la facturación electrónica en modo sandbox antes de usarla con clientes reales (ver sección dedicada arriba).
- Exportar facturas y reportes a PDF.
- Notificaciones automáticas (correo/WhatsApp) para licencias y mantenimientos próximos a vencer.
- Ver la ubicación de la flota en un mapa (hoy se muestra como tabla de coordenadas).
- Reportes de rentabilidad por viaje (tarifa menos gastos asociados).
- Migrar de SQLite a PostgreSQL si el volumen de datos o usuarios concurrentes crece.
