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

- Revision activa: `mcp-email-server-00022-vgw`, con 100% del trafico.
- Token QUOTER rotado en Secret Manager; version anterior deshabilitada.
  La credencial anterior devuelve `401` y la nueva con `{}` devuelve `422`.
- Sin token o con token incorrecto: `401`. Clave inexistente: `404` con
  `quotation_attempt_not_found`, sin redireccion ni error OAuth.
- QUOTER rechazado con `401` en MCP, polling y metricas administrativas.
  El handshake MCP con la credencial administrativa sigue devolviendo `200`.
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
