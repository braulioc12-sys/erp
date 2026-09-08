# WhatsApp → ERP: fotos de facturas por WhatsApp (1 sep)

Esto es lo que arma el pedido de Braulio: *"quiero usar n8n para integrar
Whatsapp a la plataforma... que tomando una foto a la factura se llene
automaticamente los campos de datos de proveedor, monto entre otros"*.

**Actualización (1 sep, tarde):** el workflow ya no es un archivo JSON para
importar — se construyó y se probó directamente dentro de tu cuenta de n8n
(usando el conector oficial de n8n), así que ya existe ahí, listo para
configurar. El archivo `whatsapp-factura-intake.json` de esta misma carpeta
queda como referencia histórica del primer borrador (nunca se probó contra
una instancia real); el workflow real y validado es el que está en tu n8n.

## Cómo queda el flujo

1. Alguien manda una foto de una factura/boleta por WhatsApp al número de
   negocio de Harraso.
2. n8n descarga la foto y la manda a un modelo de IA con visión (Anthropic)
   pidiéndole que extraiga: RUC del proveedor, razón social, monto,
   moneda, N° de documento y fecha.
3. n8n manda la foto (como base64, dentro de un JSON) + esos datos al ERP
   (`POST /liquidaciones/whatsapp/intake`).
4. El ERP **no crea un gasto automáticamente** — crea un borrador
   "PENDIENTE" en Liquidaciones → 📷 Borradores WhatsApp. Fue decisión
   explícita: la IA puede leer mal un monto o un RUC, y esto es dinero
   real.
5. Un Administrador u Operador entra a esa pantalla, ve la foto al lado de
   los datos ya precargados, los corrige si hace falta, y:
   - **Aprueba** → recién ahí se crea el gasto real en Liquidaciones (con
     la misma foto como comprobante), o
   - **Rechaza** → si la foto no era una factura válida (nunca se crea
     ningún gasto).
6. n8n le responde por WhatsApp a quien mandó la foto: "Recibido..." si
   todo salió bien, o un aviso de error si el ERP no pudo crear el
   borrador (y también si lo que mandaron no era una foto).

## Dónde está el workflow

Se llama **"WhatsApp → Borrador de gasto (Harraso)"** y ya está creado en
tu proyecto personal de n8n. Ábrelo desde el panel de n8n (Workflows) o
directamente en:

https://harraso.app.n8n.cloud/workflow/jO479ogRhMviCHII

Tiene una nota amarilla ("Configuración inicial") pegada arriba a la
izquierda del canvas con este mismo resumen de credenciales.

## Cómo se probó antes de entregártelo

Cada nodo se armó consultando la definición exacta del nodo (no adivinando
nombres de campos), se validó con el validador de workflows de n8n, y
después se corrió una **ejecución de prueba real dentro de tu cuenta de
n8n** (con datos simulados de una foto de factura, sin gastar ninguna
llamada real a Meta/WhatsApp ni a Anthropic) — confirmando que: el mensaje
de WhatsApp se interpreta bien, el filtro "¿es una foto?" enruta
correctamente, la conversión a base64 conserva la foto, el nodo de código
que interpreta la respuesta de la IA extrae bien los 6 campos, y el enrutado
final (éxito/error) funciona. Lo único que **no** se pudo probar desde acá
es la parte que necesita tus credenciales reales (Meta, tu ERP en Render) —
por eso el Paso 4 de abajo es una prueba con una foto real, la primera vez
que el workflow habla con esos servicios de verdad.

## Qué necesitas tener antes de empezar

- Tu cuenta de WhatsApp Business API ya conectada (nos confirmaste que ya
  la tienes) — el token de acceso permanente/de larga duración de tu app
  en Meta for Developers.
- Acceso para editar variables de entorno de tu ERP en Render (Dashboard →
  tu servicio → Environment).
- **Ya no necesitas obligatoriamente una cuenta propia en
  console.anthropic.com**: al crear el workflow, n8n asignó automáticamente
  el nodo "Extraer datos con IA" a sus créditos propios (Gateway credits).
  Si prefieres usar tu propia cuenta de Anthropic en su lugar, puedes
  cambiarlo abriendo esa credencial en el nodo — pero por defecto ya
  funciona sin que hagas nada ahí.

## Paso 1: generar el token secreto del ERP

Este token es lo único que evita que cualquiera en internet le pueda
mandar "gastos falsos" a tu ERP llamando directo a la URL del webhook (sin
pasar por WhatsApp). Genera uno largo y aleatorio, por ejemplo corriendo
esto en tu computadora:

```
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Guárdalo en dos lugares:

1. En Render: variable de entorno `N8N_WEBHOOK_TOKEN` de tu servicio ERP
   (ver `.env.example`). Sin esta variable, el endpoint rechaza **todas**
   las peticiones — por diseño, para que nunca quede abierto sin querer.
2. En n8n, como vas a ver en el Paso 2.

## Paso 2: completar las credenciales del workflow

En el workflow, cada nodo que necesita una credencial ya tiene el
*nombre* correcto puesto (ej. "WhatsApp Trigger (Harraso)") pero está
vacío — n8n crea el nombre al armar el workflow, pero nunca rellena
valores secretos por ti. Ábrelo y completa cada una (clic en el nodo →
la credencial → editar):

1. **WhatsApp Trigger (Harraso)** (nodo "Trigger WhatsApp") — Client ID y
   Client Secret de tu app de WhatsApp Business en Meta for Developers.
2. **WhatsApp Business Cloud (Harraso)** (nodos "Obtener URL de la foto",
   "Confirmar recibido", "Avisar error", "Pedir que mande una foto") — la
   credencial estándar de WhatsApp Business Cloud de n8n: tu token de
   acceso de Meta. Como es la misma credencial en los 4 nodos, la
   completas una sola vez.
3. **WhatsApp Access Token (Header)** (nodo "Descargar foto") — n8n la
   creó como credencial de "autenticación con plantilla"
   (Templated Custom Auth) en vez de Header Auth simple, así que al
   abrirla vas a ver un campo de plantilla ya con
   `{"headers":{"Authorization":"Bearer {{api_key}}"}}` y un campo aparte
   donde pegas tu token de acceso de Meta (el mismo del punto 2 — Meta
   pide el token otra vez para descargar el archivo, aunque el enlace
   venga de la misma cuenta).
4. **Anthropic (o créditos n8n)** (nodo "Extraer datos con IA") — n8n ya
   la dejó usando sus créditos propios (Gateway) automáticamente, no
   necesitas tocar nada acá salvo que prefieras usar tu propia cuenta de
   console.anthropic.com.
5. **ERP Webhook Token (Harraso)** (nodo "Enviar borrador al ERP") —
   también con plantilla: el campo de plantilla trae
   `{"headers":{"X-Webhook-Token":"{{api_key}}"}}`, y pegas ahí el mismo
   valor que pusiste en `N8N_WEBHOOK_TOKEN` en Render (Paso 1).

## Paso 3: revisar el nodo "Normalizar mensaje"

Es el segundo nodo del workflow (después del Trigger). Ahí está fijo
`erp_base_url = https://harraso-erp.onrender.com` — solo cámbialo si tu
URL real en Render es otra. El `whatsapp_phone_number_id` (el ID numérico
de tu número de WhatsApp Business, no el número en sí) se completa solo
leyéndolo del mensaje que llega — no deberías necesitar tocarlo.

## Paso 4: probar con una foto real

1. Activa el workflow (toggle "Active" arriba a la derecha en n8n).
2. Desde un celular, mándale una foto de una factura al número de WhatsApp
   Business de Harraso.
3. Revisa la ejecución en n8n (Executions) — si algo falla, el nodo que
   falló queda marcado en rojo y puedes ver el error exacto (lo más
   probable, si algo falla, es que falte completar alguna credencial del
   Paso 2).
4. Si todo salió bien, entra al ERP → Liquidaciones → 📷 Borradores
   WhatsApp y deberías ver el borrador con la foto y los datos extraídos.
5. Deberías recibir también la respuesta de confirmación en el WhatsApp
   desde el que mandaste la foto.

## Cosas para revisar si algo no cuadra

- **Forma exacta del mensaje del WhatsApp Trigger**: el nodo "Normalizar
  mensaje" ya viene preparado para dos formas distintas en que Meta puede
  entregar el mensaje (una más "plana" y otra anidada dentro de
  `entry[0].changes[0].value`), pero si Meta cambia el formato en el
  futuro, ese es el nodo a revisar primero.
- **Extraer datos con IA**: el modelo de Anthropic configurado es
  `claude-sonnet-4-5-20250929` — revisa el modelo recomendado vigente en
  https://docs.claude.com/en/docs/about-claude/models de vez en cuando.
- Si Meta cambia dónde vive la URL de descarga de medios o exige otro tipo
  de autenticación para descargarla, el nodo a revisar es "Descargar
  foto".

## Sobre el costo y los errores de la IA

- Cada foto procesada consume una llamada a la API de Anthropic — con los
  créditos propios de n8n (Gateway) por defecto, o contra tu cuenta de
  console.anthropic.com si cambiaste la credencial.
- El prompt le pide a la IA que use `null` en cualquier dato que no pueda
  leer con confianza, en vez de inventar un valor — así que es normal ver
  campos vacíos en el borrador si la foto salió borrosa o incompleta;
  se completan a mano en la pantalla de revisión.
- Si la IA devuelve algo que no es JSON válido (poco común, pero puede
  pasar), el borrador igual se crea en el ERP con los campos extraídos
  vacíos — la foto nunca se pierde, solo hay que completar los datos a
  mano al revisar.
