# Envio del PDF oficial desde mcp_quoter

## Alcance y permisos

`mcp_quoter` transmite el PDF original directamente por HTTPS a este servidor.
Claude solo debe recibir el identificador y el resultado; **no** el Base64.
No se genera otro PDF, no se aceptan rutas o URLs, no se usa Storage y no se
crean buckets. El MIME y sus adjuntos se construyen exclusivamente en memoria.
El servidor no guarda ni registra PDF, Base64, cuerpo ni destinatarios en este flujo.
El servidor SMTP y los destinatarios si pueden conservar el mensaje enviado.

Configurar:

- `MCP_EMAIL_ACCESS_TOKEN`: token administrativo para todos los requests MCP,
  incluido el handshake. En produccion es obligatorio y no puede ser placeholder.
- `MCP_EMAIL_POLL_WEBHOOK_SECRET`: secreto independiente para `/tasks/poll`.
  Si falta, polling devuelve `401`.
- `MCP_EMAIL_QUOTER_TOKEN`: secreto exclusivo, distinto de los anteriores, de al
  menos 32 caracteres. Generar con `secrets.token_urlsafe(32)` y guardar en el
  gestor de secretos. No incluirlo en herramientas/argumentos de Claude.
- `MCP_EMAIL_QUOTER_ACCOUNT`: nombre de una cuenta configurada en
  `MCP_EMAIL_ACCOUNTS_JSON` (o la cuenta unica existente).

La API queda deshabilitada si ambos ajustes QUOTER faltan. Una configuracion
parcial, sin SMTP/Supabase o sin TLS SMTP impide el arranque.
La cuenta y el remitente se resuelven en el servidor; el request no puede
cambiarlos. Se conserva el BCC obligatorio configurado, que tambien recibe el PDF
y cuenta contra el limite de destinatarios. Revisarlo antes de habilitar el flujo.

La credencial QUOTER solo permite enviar cotizaciones y consultar sus intentos,
acotados a esa cuenta; no permite usar MCP, leer/buscar/borrar correo, listar
cuentas, ejecutar polling ni consultar metricas administrativas.

No enviar tokens en rutas o query strings: solo `Authorization: Bearer <secreto>`.
Esto tambien aplica a clientes MCP existentes. Las herramientas MCP y sus
argumentos no cambian, pero los clientes que dependian de handshake abierto o
tokens en rutas deben migrar. Rotar los tokens que antes estuvieron en URLs.

## Contrato HTTP

### POST /v1/quotation-emails

Headers: `Authorization: Bearer <QUOTER_TOKEN>`, `Content-Type: application/json`.
El JSON lo construye `mcp_quoter`, no Claude:

```json
{
  "idempotency_key": "quote-123-v2-send-1",
  "quotation_reference": "quote-123",
  "quotation_version": "2",
  "to": ["compras@example.com"],
  "cc": [],
  "bcc": [],
  "subject": "Cotizacion oficial",
  "body_text": "Adjuntamos la cotizacion solicitada.",
  "body_html": "<p>Adjuntamos la cotizacion solicitada.</p>",
  "attachments": [{
    "filename": "cotizacion-123-v2.pdf",
    "content_type": "application/pdf",
    "content_base64": "<BASE64_ORIGINAL_INSERTADO_POR_MCP_QUOTER>"
  }]
}
```

`cc`, `bcc`, `body_html` y `content_type` son opcionales; el tipo por defecto es
`application/pdf`. Solo se aceptan PDFs. Base64 estandar estricto, sin data URL
ni espacios; los bytes deben tener cabecera PDF y marcador final EOF.
Esto verifica tipo e integridad de transporte, no certifica el contenido ni
sustituye una revision de seguridad del PDF en su origen.
Nombres ASCII simples terminados en `.pdf`, sin rutas, maximo 128 caracteres.
Destinatarios: direcciones simples, sin nombres visibles ni cabeceras.
Asunto: sin caracteres de control, hasta 998 caracteres.
Cuerpo: hasta 100.000 caracteres por representacion, sin NUL.
Los identificadores admiten 1-128 caracteres: alfanumerico inicial, luego
alfanumericos, `_`, `.`, `:`, `-`. Se rechazan campos desconocidos.

Respuesta estructurada, sin contenido del correo:

```json
{
  "attempt_id": "661aecfa-6c47-4aeb-8940-1d286ffbd227",
  "idempotency_key": "quote-123-v2-send-1",
  "quotation_reference": "quote-123",
  "quotation_version": "2",
  "state": "aceptado_por_smtp",
  "created_at": "2026-10-29T20:00:00Z",
  "updated_at": "2026-10-29T20:00:01Z",
  "expires_at": "2027-01-27T20:00:00Z",
  "duration_ms": 1000,
  "message_id": "<661aecfa-6c47-4aeb-8940-1d286ffbd227@example.com>",
  "smtp_queue_id": null,
  "error_code": null,
  "accepted_count": 1,
  "refused_count": 0,
  "duplicate_requests": 0,
  "delivery_confirmed": false
}
```

`message_id` es el RFC Message-ID generado y persistido **antes** de contactar
SMTP, util para conciliacion, no prueba de entrega. `smtp_queue_id` conserva
solo el identificador si la respuesta SMTP tiene el formato `queued as ID`;
otros formatos quedan en null. No se guarda la respuesta textual del proveedor.
Una aceptacion parcial lleva `error_code=smtp_partial_rejection` y los contadores
correspondientes; no asumir que todos los destinatarios fueron aceptados.

- `200`: intento terminal, nuevo o existente. **Leer `state`**, no inferir exito
  del HTTP 200.
- `202`: mismo intento aun `pendiente` o `procesando`; consultar posteriormente.
- `401`: credencial ausente, invalida o perteneciente a otro rol.
- `409`: misma clave con contenido diferente; no se envia.
- `413`: cuerpo HTTP excede el limite (tambien con transferencia chunked).
- `415` / `422`: tipo HTTP o contrato invalido; el error nunca incluye el PDF.
- `408`: carga excede el timeout total.
- `429`: limite de carga, concurrencia o frecuencia; respetar `Retry-After`.
- `503`: Supabase no disponible o resultado SMTP no persistido. Si incluye
  `attempt_id`, consultarlo; si no, consultar por clave. **No crear otra clave
  para resolver un timeout/503**.

### Consultas autorizadas

- `GET /v1/quotation-emails/{attempt_id}`, con el mismo bearer QUOTER.
- `GET /v1/quotation-emails/by-key`, con el mismo bearer y header
  `Idempotency-Key: quote-123-v2-send-1`.
- `404` si el intento no existe para esa cuenta o expiro y fue eliminado.

## Estados e idempotencia

| Estado | Significado y accion |
|---|---|
| `pendiente` | Reserva persistida, SMTP no iniciado. Consultar; no hay cola de reintentos. |
| `procesando` | Un trabajador obtuvo el intento; SMTP puede estar en curso. Consultar. |
| `aceptado_por_smtp` | SMTP acepto DATA para al menos un destinatario. No significa entrega. |
| `fallido` | Fallo previo al envio o rechazo SMTP explicito; no se reintenta automaticamente. |
| `resultado_desconocido` | Timeout/desconexion durante envio, cancelacion o trabajador perdido. Conciliar antes de reenviar. |

Una transaccion PostgreSQL serializa reserva y cuotas por cuenta usando advisory
lock. La restriccion unica `(account_id, idempotency_key)` y la transicion atomica
de `pendiente` a `procesando` evitan duplicados entre trabajadores/instancias.
La huella SHA-256 incluye bytes originales de cada PDF, nombres, referencias,
version, destinatarios, asunto, cuerpos, remitente y BCC efectivo.
No se guarda ese contenido: solo la huella y metadatos de estado.

Misma clave/huella devuelve el intento existente, incluso si fallo o es ambiguo.
Una clave distinta permite un reenvio deliberado, sujeto a cuotas.
El cliente debe conservar la clave antes del primer POST y reutilizarla tras
errores de transporte. Nunca reintentar SMTP automaticamente.

El lease dura `QUOTATION_TIMEOUT_SECONDS + 90` segundos desde la reserva.
Lecturas, reservas, metricas y limpieza recuperan leases vencidos:
`pendiente` pasa a `fallido/expired_before_smtp`; `procesando` pasa a
`resultado_desconocido/worker_interrupted`. No se vuelve a enviar tras reiniciar.
Una respuesta SMTP tardia conocida puede completar un intento recuperado.
No existe garantia de entrega exactamente una vez entre PostgreSQL y SMTP:
esa ventana se representa honestamente como resultado desconocido.

## Limites y observabilidad

Valores por defecto, con prefijo `MCP_EMAIL_`:

| Variable | Valor |
|---|---|
| `QUOTATION_MAX_REQUEST_BYTES` | 16777216 (JSON completo, antes de parsearlo) |
| `QUOTATION_MAX_FILE_BYTES` | 10485760 |
| `QUOTATION_MAX_TOTAL_BYTES` | 10485760 (bytes decodificados) |
| `QUOTATION_MAX_ATTACHMENTS` | 1 |
| `QUOTATION_MAX_RECIPIENTS` | 20, incluyendo CC/BCC obligatorio |
| `QUOTATION_CONCURRENCY` | 2 envios globales por cuenta y 2 cargas por proceso |
| `QUOTATION_PER_MINUTE` | 10 nuevas reservas por cuenta en ventana movil |
| `SMTP_TIMEOUT_SECONDS` | 30 por operacion SMTP |
| `QUOTATION_TIMEOUT_SECONDS` | 120 para toda la conversacion SMTP |
| `QUOTATION_UPLOAD_TIMEOUT_SECONDS` | 30 para toda la carga HTTP |
| `QUOTATION_RETENTION_DAYS` | 90 desde la reserva |

Las cargas grandes se rechazan por Content-Length sin consumir el cuerpo;
sin esa cabecera se corta al superar el maximo durante la lectura.
Supabase tiene timeout explicito de 30 segundos. No hay temporales.
Los logs del flujo solo llevan ID, estado, duracion y codigos de error fijos.

`GET /internal/quotation-metrics`, con el bearer **administrativo**, informa
`total`, `failed`, `unknown`, `smtp_accepted` y `duplicates_avoided` de los
intentos retenidos. No son contadores acumulados historicos tras la limpieza.
No se exponen PDFs, claves ni datos del correo en estas metricas.
Exportar alertas por `unknown > 0`, crecimiento de `failed` y errores de persistencia.

## Migracion, retencion y despliegue

1. Aplicar [create_quotation_send_attempts.sql](../scripts/create_quotation_send_attempts.sql)
   primero a un proyecto Supabase de prueba. Tabla con RLS sin politicas publicas;
   funciones y tabla accesibles solo con `service_role`. No usar la clave anon.
2. Programar diariamente `select public.quotation_cleanup();` mediante pg_cron
   o un scheduler confiable. No esta activado automaticamente por la migracion.
   Por ejemplo, con pg_cron habilitado:
   ```sql
   select cron.schedule(
     'quotation-metadata-cleanup', '17 3 * * *',
     'select public.quotation_cleanup();'
   );
   ```
   Tambien se puede invocar `/rest/v1/rpc/quotation_cleanup` con service role desde
   el scheduler. Nunca exponer esa credencial a `mcp_quoter` o Claude.
3. La idempotencia esta garantizada mientras se retenga la fila. Tras borrarla,
   la misma clave podria producir otro envio. `mcp_quoter` debe usar claves
   globalmente unicas y no reenviar claves historicas despues de la retencion.
4. Provisionar los secretos y la cuenta fija en staging; en Render agregarlos al
   entorno del servicio, en Cloud Run a Secret Manager/env preservando todos los
   secretos existentes en `--set-secrets`. Esta entrega no altera el despliegue
   existente para activar una cuenta o credencial no confirmada.
5. Configurar proxy/gateway para no registrar Authorization, cuerpo ni query
   strings sensibles. Uvicorn del contenedor tiene access logs desactivados.
   Los logs automaticos de la plataforma requieren configuracion independiente.
6. Confirmar conectividad SMTP TLS desde el host de staging. La arquitectura
   existente advierte que Render Free bloquea SMTP: **no habilitar produccion
   alli sin verificar conectividad**; usar un host permitido (por ejemplo Cloud
   Run) o acordar por separado un transporte HTTP. No se cambia el transporte aqui.
7. El timeout del proxy debe superar carga + SMTP + operaciones Supabase
   (recomendado al menos 300 segundos con defaults).

### Validacion

Pruebas locales:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_server.py tests\test_smtp_adapter.py tests\test_quotations.py -q
```

Para validar SQL real, iniciar un contenedor PostgreSQL **desechable** sin datos
de produccion, configurar `MCP_EMAIL_TEST_POSTGRES_CONTAINER` con su nombre y
ejecutar `tests\test_quotation_sql.py`. Las pruebas crean y eliminan una base
aislada; verifican migracion reaplicable, permisos, reservas/claims concurrentes,
cuotas, recuperacion, retencion y flujo HTTP -> SupabaseAdapter -> SQL -> MIME/SMTP
simulado. No requieren instalar otro driver ni modifican el Supabase productivo.

Antes de desplegar, ejecutar en staging con SMTP real y destinatario controlado:

- Credenciales ausentes/invalidas/otro rol; ninguna operacion de correo permitida.
- PDF oficial recibido identico (comparar SHA-256 en origen y adjunto recibido).
- Dos POST simultaneos con la misma clave: un solo mensaje SMTP.
- Misma clave con PDF/cuerpo/version distinto: `409`; nueva clave: otro intento.
- Caida/reinicio antes y durante SMTP, timeout despues de DATA, proveedor caido:
  resultados persistidos coherentes, sin reenvio automatico.
- Limites exactos, solicitudes chunked y carga lenta.
- Logs, tablas y Storage sin PDF/Base64; limpieza de metadatos y alertas activas.
- Cliente MCP existente con bearer desde `initialize`.

Las pruebas con SMTP simulado y PostgreSQL desechable no sustituyen esa
verificacion externa de conectividad, recepcion y logs de plataforma.
