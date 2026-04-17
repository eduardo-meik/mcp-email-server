# Deploy En Google Cloud

Este proyecto encaja bien en Cloud Run porque ya es stateless, expone HTTP y usa la variable `PORT` del entorno.

## Arquitectura recomendada

- Cloud Run para servir `/mcp`, `/healthz` y `/tasks/poll`.
- Cloud Scheduler para llamar periódicamente a `/tasks/poll`.
- Supabase para persistencia.
- CPanel para IMAP y SMTP.

## Compatibilidad con CPanel email

- Recepción: sí, usando IMAP SSL en puerto `993`.
- Envío: sí, siempre que tu cuenta de CPanel use SMTP submission por `465` o `587`.
- No dependes de Render, así que aquí no aplica el bloqueo de `465` y `587`.
- Google Cloud bloquea `25` hacia destinos externos, así que no uses SMTP en `25`.

## Variables de entorno mínimas

Configura estas variables en Cloud Run:

### No secretas

- `MCP_EMAIL_ENV=production`
- `MCP_EMAIL_LOG_LEVEL=INFO`
- `MCP_EMAIL_POLL_BATCH_SIZE=25`
- `MCP_EMAIL_IMAP_MAILBOX=INBOX`
- `MCP_EMAIL_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1`
- `MCP_EMAIL_OPENROUTER_EMBEDDING_MODEL=nvidia/llama-nemotron-embed-vl-1b-v2:free`
- `MCP_EMAIL_SUPABASE_SCHEMA=public`
- `MCP_EMAIL_SUPABASE_MESSAGES_TABLE=email_embeddings`
- `MCP_EMAIL_SUPABASE_SYNC_STATE_TABLE=email_ingest_state`

### Secretas

- `MCP_EMAIL_IMAP_HOST`
- `MCP_EMAIL_IMAP_USERNAME`
- `MCP_EMAIL_IMAP_PASSWORD`
- `MCP_EMAIL_MAILBOX_ID`
- `MCP_EMAIL_SMTP_HOST`
- `MCP_EMAIL_SMTP_PORT`
- `MCP_EMAIL_SMTP_USERNAME`
- `MCP_EMAIL_SMTP_PASSWORD`
- `MCP_EMAIL_SMTP_FROM_ADDRESS`
- `MCP_EMAIL_OPENROUTER_API_KEY`
- `MCP_EMAIL_SUPABASE_URL`
- `MCP_EMAIL_SUPABASE_SERVICE_ROLE_KEY`
- `MCP_EMAIL_POLL_WEBHOOK_SECRET`

## Valores típicos de CPanel

Usa los valores reales de tu hosting, pero normalmente son parecidos a estos:

- `MCP_EMAIL_IMAP_HOST=mail.tudominio.com`
- `MCP_EMAIL_IMAP_PORT=993`
- `MCP_EMAIL_SMTP_HOST=mail.tudominio.com`
- `MCP_EMAIL_SMTP_PORT=465` o `587`
- `MCP_EMAIL_SMTP_USERNAME=agente@tudominio.com`
- `MCP_EMAIL_SMTP_FROM_ADDRESS=agente@tudominio.com`

Si usas `465`, ajusta:

- `MCP_EMAIL_SMTP_USE_TLS=true`
- `MCP_EMAIL_SMTP_STARTTLS=false`

Si usas `587`, ajusta:

- `MCP_EMAIL_SMTP_USE_TLS=false`
- `MCP_EMAIL_SMTP_STARTTLS=true`

## Despliegue con Cloud Build

Este repo incluye [cloudbuild.yaml](/c:/Meik_Apps/Tools/mcp-email-server-2/cloudbuild.yaml).

### 1. Crear repositorio en Artifact Registry

```bash
gcloud artifacts repositories create mcp-email-server \
  --repository-format=docker \
  --location=us-central1
```

### 2. Lanzar el build y deploy

```bash
gcloud builds submit --config cloudbuild.yaml
```

### 3. Cargar secretos y puertos SMTP/IMAP

Puedes hacerlo desde la consola de Cloud Run o con `gcloud run services update`.

Ejemplo:

```bash
gcloud run services update mcp-email-server \
  --region us-central1 \
  --update-env-vars MCP_EMAIL_IMAP_PORT=993,MCP_EMAIL_SMTP_PORT=587,MCP_EMAIL_SMTP_USE_TLS=false,MCP_EMAIL_SMTP_STARTTLS=true
```

## Scheduler para sincronizar correos

La app expone `POST /tasks/poll`. Puedes programarla cada 5 minutos.

Ejemplo:

```bash
gcloud scheduler jobs create http mcp-email-poll \
  --location us-central1 \
  --schedule "*/5 * * * *" \
  --uri "https://TU-SERVICIO-URL/tasks/poll" \
  --http-method POST \
  --headers "X-Webhook-Secret=TU_SECRET"
```

Si tu servicio no es público, usa OIDC con una service account de Cloud Scheduler. Si sí es público, mantén al menos el header `X-Webhook-Secret`.

## Verificaciones rápidas

### Salud

```bash
curl https://TU-SERVICIO-URL/healthz
```

### Poll manual

```bash
curl -X POST https://TU-SERVICIO-URL/tasks/poll \
  -H "X-Webhook-Secret: TU_SECRET"
```

### MCP

El endpoint MCP queda publicado en:

```text
https://TU-SERVICIO-URL/mcp
```

## Recomendación operativa

- Para un MCP público, usa Cloud Run público y protege `POST /tasks/poll` con `X-Webhook-Secret`.
- Para máxima seguridad, usa Cloud Run autenticado para tareas internas y deja solo el endpoint MCP detrás del esquema de acceso que vayas a usar.
- Si tu proveedor CPanel restringe por IP, tendrás que allowlistear la salida de Google Cloud o usar salida estática.