# MCP Email Server

Servidor de correo stateless: FastAPI publica la API HTTP de cotizaciones y monta
FastMCP en `/mcp`. Persistencia exclusivamente en Supabase; el PDF de cotizaciones
se procesa en memoria y nunca se guarda en la base ni en Storage.

## Despliegue de correo confirmado

Verificado el 2026-10-30 mediante Cloud Run y Secret Manager:

| Ajuste | Valor |
|---|---|
| Proyecto Google Cloud (ID exacto) | `summirizer` |
| Region | `us-central1` |
| Servicio Cloud Run | `mcp-email-server` |
| URL base HTTPS | `https://mcp-email-server-y2eagv4zlq-uc.a.run.app` |
| API de envio | `https://mcp-email-server-y2eagv4zlq-uc.a.run.app/v1/quotation-emails` |
| Host permitido | `mcp-email-server-y2eagv4zlq-uc.a.run.app` |
| Aplicacion del contenedor | `mcp_email_server.server:application` |
| Cuenta fija de cotizaciones | `eduardo` |
| Remitente | `eduardo@meiklabs.com` |
| SMTP | `mail.meiklabs.com:465`, TLS implicito con validacion de certificado |
| BCC obligatorio por defecto | `4452112@bcc.hubspot.com` |

Cloud Run reserva la ruta exacta `/healthz` para sus propias sondas: una peticion
externa a `GET /healthz` recibe un 404 HTML de Google Frontend, aunque la
aplicacion responda. Para monitoreo externo usar cualquiera de estos alias, que
devuelven `200` con `{"status":"ok"}`:

- `GET https://mcp-email-server-y2eagv4zlq-uc.a.run.app/health`
- `GET https://mcp-email-server-y2eagv4zlq-uc.a.run.app/healthz/` (con barra final)

El proyecto activo de una sesion local de gcloud puede ser otro. Usar siempre
`--project=summirizer --region=us-central1` al administrar este servicio.
`partial-ivory-silverfish.fastmcp.app` **no es el servicio de correo** y no debe
recibir sus credenciales.

El contenedor ejecuta FastAPI completa, no una instancia MCP aislada.
Las tres rutas son `POST /v1/quotation-emails`,
`GET /v1/quotation-emails/by-key` (header `Idempotency-Key`) y
`GET /v1/quotation-emails/{attempt_id}`.
El contrato, estados, autenticacion e idempotencia estan documentados en
[quotation-delivery.md](docs/quotation-delivery.md).

## Secretos y despliegues posteriores

`MCP_EMAIL_QUOTER_TOKEN` se obtiene de Secret Manager
(`mcp-email-quoter-token`); no guardar su valor en Git ni mostrarlo en logs.
`MCP_EMAIL_QUOTER_ACCOUNT=eduardo` selecciona la cuenta existente en
`MCP_EMAIL_ACCOUNTS_JSON`. La peticion no puede elegir cuenta ni remitente.
El token QUOTER no autoriza `/mcp`, `/tasks/poll` ni metricas administrativas.

Cloud Run admite invocacion publica para que la autenticacion independiente de
la aplicacion llegue a las rutas de cotizaciones sin un gateway OAuth.
Esto **no** abre los buzones: `/mcp` exige `MCP_EMAIL_ACCESS_TOKEN`, y polling
exige su propio secreto. No sustituir esos secretos por el token QUOTER.

### Consumidores de credenciales

Cada secreto es independiente y cada uno tiene consumidores propios. Rotar uno
solo invalida ese consumidor; nunca autentica a los otros.

| Secreto | Ruta que autoriza | Consumidor a actualizar |
|---|---|---|
| `MCP_EMAIL_ACCESS_TOKEN` | `/mcp` | Conector `meik_mail` en Claude (usuario o admin de la organizacion) |
| `MCP_EMAIL_POLL_WEBHOOK_SECRET` | `/tasks/poll` | Job `mcp-email-poll` de Cloud Scheduler (`X-Webhook-Secret`) |
| `MCP_EMAIL_QUOTER_TOKEN` | `/v1/quotation-emails*` | `mcp_quoter` (`EMAIL_QUOTER_TOKEN` en su `.env`) |

El codigo acepta un token anterior solo mientras se configura en la variable
`*_PREVIOUS` correspondiente. Sin esa variable la credencial vieja se rechaza al
instante; con ella se mantiene viva hasta que se retira o vence
`MCP_EMAIL_PREVIOUS_TOKENS_EXPIRE_AT`. Un token previo nunca cruza de rol:
`quoter_settings` rechaza ademas que cualquier credencial QUOTER coincida con el
admin o el de polling
([quotations.py](src/mcp_email_server/quotations.py)).

### Diagnostico de rechazos en `/mcp`

Cada `401` de `/mcp` deja una advertencia `mcp_auth_rejected` en Cloud Logging con
el motivo y una huella del token recibido (SHA-256 truncado, nunca el valor):

| Motivo | Significado |
|---|---|
| `missing_authorization_header` | El cliente no envia `Authorization` |
| `unsupported_authorization_scheme` | Envia otro esquema (no `Bearer`) |
| `empty_bearer_token` | `Bearer` sin valor |
| `previous_token_expired` | Usa un token `*_PREVIOUS` con la gracia vencida |
| `token_mismatch` | No coincide con el vigente ni con los de gracia |
| `missing_configured_token` | La instancia no tiene token configurado |

Al arrancar, `mcp_access_control` publica `token=<huella>` del token vigente y
`mcp_previous_tokens_ignored` advierte cuando hay tokens previos que ya no se
aceptan. Comparar la huella del rechazo con la del arranque distingue "el cliente
tiene un token viejo" de "el token del servidor cambio", sin exponer secretos. El
`401` incluye ademas `WWW-Authenticate: Bearer`.

```powershell
gcloud logging read 'resource.labels.service_name="mcp-email-server" AND "mcp_auth_rejected"' --project=summirizer --limit=20 --freshness=1d
```

[cloudbuild.yaml](cloudbuild.yaml) conserva variables y secretos existentes
mediante `--update-env-vars` y `--update-secrets`, y mantiene configurados QUOTER
en futuros despliegues. Antes de usarlo, proporcionar `_CLAMAV_URL` con la URL
actual del scanner para no deshabilitarlo.

```powershell
gcloud builds submit --project=summirizer --config=cloudbuild.yaml --substitutions=_CLAMAV_URL=https://clamav-scanner-y2eagv4zlq-uc.a.run.app
```

Al rotar credenciales, comprobar tambien el reparto de trafico y las URLs con
tags: una revision creada no recibe necesariamente trafico si este estaba fijado
a una revision anterior. No dejar tags apuntando a revisiones con el token viejo.

## Limites de cotizaciones

| Limite | Valor por defecto |
|---|---|
| PDF individual | 10 MiB (10485760 bytes) |
| Total de PDF decodificados | 10 MiB |
| Adjuntos | 1 PDF |
| Solicitud JSON completa | 16 MiB (16777216 bytes), incluido Base64 |
| Destinatarios | 20, sumando To, CC y BCC obligatorio |
| Concurrencia de envios | 2 por cuenta |
| Frecuencia | 10 intentos nuevos por minuto |
| Retencion de metadatos/idempotencia | 90 dias |

Un timeout ambiguo o una aceptacion SMTP seguida de error de persistencia
requieren consultar el intento con la misma clave. Nunca reenviar a ciegas ni
crear otra clave automaticamente. Aceptacion SMTP no confirma entrega final.

## Migracion manual de Supabase

En SQL Editor del proyecto Supabase utilizado por el servicio, ejecutar el
contenido completo de
[scripts/create_quotation_send_attempts.sql](scripts/create_quotation_send_attempts.sql).
Es la query transaccional existente y reaplicable; no crear otra tabla o flujo.
Incluye RLS y revoca acceso de `public`, `anon` y `authenticated` tanto a tabla
como a funciones; solo `service_role` recibe permisos.

Para comprobar permisos despues de aplicarla:

```sql
select c.relrowsecurity as rls_enabled
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relname = 'quotation_send_attempts';

select r.rolname, p.privilege,
       has_table_privilege(r.rolname, 'public.quotation_send_attempts', p.privilege)
         as allowed
from pg_roles r
cross join (values ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) p(privilege)
where r.rolname in ('anon', 'authenticated', 'service_role')
order by r.rolname, p.privilege;

select r.rolname, p.oid::regprocedure as function,
       has_function_privilege(r.rolname, p.oid, 'EXECUTE') as allowed
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
cross join pg_roles r
where n.nspname = 'public'
  and p.proname in (
    'quotation_recover', 'quotation_reserve', 'quotation_claim',
    'quotation_finish', 'quotation_get', 'quotation_metrics', 'quotation_cleanup'
  )
  and r.rolname in ('anon', 'authenticated', 'service_role')
order by r.rolname, p.proname;
```

Resultado esperado: RLS activado; `allowed=false` para `anon` y
`authenticated`; `allowed=true` para `service_role`. Deben aparecer siete
funciones por rol. La tabla solo contiene metadatos y resultados.
La limpieza diaria se configura por separado segun
[quotation-delivery.md](docs/quotation-delivery.md#migracion-retencion-y-despliegue).

La comprobacion HTTP de una funcion existente no certifica por si sola todos
los permisos de la migracion: ejecutar tambien las consultas anteriores.

## Verificacion del 2026-10-30

- Revision activa: `mcp-email-server-00026-tcc`, con 100% del trafico y el tag
  `quotation-api-check` apuntando a la misma revision.
- Periodo de gracia activo para el conector `meik_mail`: la version `1` del
  secreto `mcp-email-access-token` esta habilitada y montada en
  `MCP_EMAIL_ACCESS_TOKEN_PREVIOUS`, con vencimiento
  `MCP_EMAIL_PREVIOUS_TOKENS_EXPIRE_AT=2026-11-06T23:59:59Z`.
  Verificado: token vigente `200`, token en gracia `200`, token desconocido
  `401`, metricas administrativas con el token en gracia `200`.
- El token en gracia no cruza de rol: con el en `/v1/quotation-emails` se
  obtiene `401`. QUOTER vigente `422`; QUOTER revocado `401`. Polling con el
  secreto vigente `200` y con el revocado `401` (sin gracia configurada).
- Token QUOTER rotado en Secret Manager; la version vigente es la `3` y las
  versiones `1` y `2` estan deshabilitadas. Toda credencial anterior devuelve
  `401` y la vigente con `{}` devuelve `422`.
- Entorno sincronizado con el `.env` local: `MCP_EMAIL_ACCESS_TOKEN` (version 2
  del secreto) y `MCP_EMAIL_POLL_WEBHOOK_SECRET` (version 3) ahora coinciden con
  el entorno local y sus versiones anteriores quedaron deshabilitadas.
  El handshake MCP con el token nuevo devuelve `200` y `/tasks/poll` con el
  secreto nuevo devuelve `200`; las credenciales anteriores devuelven `401`.
- `MCP_EMAIL_ENV` se mantiene deliberadamente en `production` aunque el `.env`
  local diga `development`, para no desactivar la exigencia de
  `MCP_EMAIL_ACCESS_TOKEN` al arrancar. `MCP_EMAIL_CLAMAV_URL` y
  `MCP_EMAIL_IMAP_MAILBOX` se conservan porque el `.env` no las define.
- Las variables `EMAIL_*` del `.env` corresponden a `mcp_quoter` y no se
  despliegan en este servicio.

### Incidente del 2026-10-30 14:53-14:56 (hora local)

Causa confirmada en los logs HTTP de Cloud Run, no en la aplicacion:

- 14:53 se creo la revision `00024-ckh` con `MCP_EMAIL_ACCESS_TOKEN` rotado y
  `MCP_EMAIL_POLL_WEBHOOK_SECRET` rotado.
- 14:54:58 y 14:56:12, cuatro `POST /mcp` con `userAgent=Claude-User`
  respondieron `401`. Ese es el conector `meik_mail` de Claude, que conservaba
  el token administrativo anterior. El correo de prueba nunca llego a SMTP
  porque la peticion fue rechazada antes de entrar a la aplicacion.
- 14:55:09 el job `mcp-email-poll` respondio `401`; se actualizo su header
  `X-Webhook-Secret` y la corrida de las 15:01 volvio a `200`.
- No hubo ninguna peticion a `/v1/quotation-emails` entre las 14:00 y las 15:00
  salvo las de verificacion con `python-httpx`. El diagnostico de
  `mcp_quoter` como `autenticacion_correo` no proviene de un 401 del servicio
  de correo en ese momento.
- Ultimas llamadas identificadas como `mcp_quoter` (`userAgent=node`)
  respondieron `401` a las 12:52 y 13:05, cuando aun usaba un token QUOTER
  anterior. Su `.env` local ya coincide con la version vigente.
- Los dos secretos siguen siendo independientes; no hubo reemplazo de un
  secreto compartido. El fallo fue por rotacion sin actualizar consumidores.
- Mitigacion aplicada: se implemento el periodo de gracia descrito en
  `docs/quotation-delivery.md` y se habilito para el token administrativo, de
  modo que el conector `meik_mail` vuelve a operar mientras se reconecta.
  El resto de rutas mantiene la validacion estricta.

- Sin token o con token incorrecto: `401`. Clave inexistente: `404` con
  `quotation_attempt_not_found`, sin redireccion ni error OAuth.
- QUOTER rechazado con `401` en MCP, polling y metricas administrativas.
  El handshake MCP con la credencial administrativa vigente devuelve `200`.
- Dos POST concurrentes con una misma clave devolvieron el mismo intento:
  `56d5f658-8d44-405d-964c-9f8e03e99691`, con un duplicado evitado y estado
  `aceptado_por_smtp`. SMTP acepto el destinatario autorizado y el BCC obligatorio.
  Esto no certifica recepcion final en el buzon.
- La fila real de Supabase conserva solo metadatos, sin PDF, cuerpo ni Base64.
  En 22 entradas recientes de logs de la revision no aparecieron los secretos,
  cuerpo ni Base64 utilizados en la prueba.
- 102 pruebas correctas, incluyendo PostgreSQL desechable: migracion
  reaplicable, permisos restringidos, concurrencia, timeout ambiguo y fallo de
  persistencia despues de aceptacion SMTP sin reenvio automatico.
- La tabla y las funciones de envio/consulta ya existen y funcionan en Supabase.
  **No se ejecuto una migracion en produccion durante esta intervencion**:
  por decision del operador, la reaplicacion manual y la comprobacion completa
  de permisos productivos con las consultas anteriores quedan a su cargo.

## Desarrollo

Arquitectura y configuracion general: [ARCHITECTURE.md](ARCHITECTURE.md).
Ejecutar pruebas sin cargar secretos del entorno local; para las pruebas SQL
usar un contenedor PostgreSQL desechable y
`MCP_EMAIL_TEST_POSTGRES_CONTAINER`, como describe
[quotation-delivery.md](docs/quotation-delivery.md#validacion).
