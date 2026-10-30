# Adjuntos entrantes seguros

Pipeline fail-closed para exponer adjuntos de correo a Claude sin comprometer
el servidor ni el modelo. Cada capa descarta antes de gastar CPU, red o
tokens, y lo rechazado o pendiente nunca se guarda (IMAP es la fuente de
verdad: se reintenta desde ahí).

## Configuración previa

1. Ejecutar [scripts/create_email_attachments.sql](../scripts/create_email_attachments.sql)
   en Supabase (tablas con RLS sin policies —solo service role— y bucket
   privado `email-attachments`).
2. Poblar la allowlist por mailbox:

   ```sql
   insert into public.attachment_sender_policy (mailbox_id, pattern, action, note)
   values ('agent', '@proveedor.cl', 'allow', 'DTEs del SII'),
          ('agent', 'contador@empresa.cl', 'allow', 'contadora');
   ```

3. Desplegar el escáner ClamAV (`scanner/`) en Cloud Run como servicio privado.
   Conceder `roles/run.invoker` a la cuenta de servicio de `mcp-email-server`;
   el servidor obtiene un token OIDC del metadata server de Cloud Run. Para
   desarrollo fuera de Cloud Run se puede configurar `MCP_EMAIL_CLAMAV_AUTH_TOKEN`.
4. Opcional: fijar `MCP_EMAIL_IMAP_AUTHSERV_ID` si el authserv-id del
   `Authentication-Results` del servidor cPanel difiere del host IMAP.

## Pipeline (orden de ejecución)

0. Auth `/mcp` (existente).
1. **Remitente**: patrón en `attachment_sender_policy` (email exacto o
   `@dominio`, caché de 5 min) **y** DKIM/DMARC `pass` leyendo solo el
   `Authentication-Results` superior con authserv-id del servidor cPanel.
   Sin pass → `untrusted` → solo metadata, nunca descarga.
2. **BODYSTRUCTURE** (sin bajar bytes): ≤10 adjuntos, ≤10 MB por archivo,
   ≤25 MB por correo, extensión + tipo declarado en allowlist
   (PDF, XML, XLSX, DOCX).
3. `FETCH BODY.PEEK[part_id]` solo de esa parte; decodificación CTE; rechequeo
   de tamaño real.
4. **Magic bytes**: el tipo detectado debe coincidir con extensión y
   declarado; ejecutables y contenido ambiguo se rechazan (fail-closed).
5. **Chequeos estructurales**:
   - PDF: sin `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/AA`, `/XFA`,
     `/OpenAction` con acción, cifrado.
   - OOXML: sin `vbaProject.bin`, `externalLink`, `oleObject`, ActiveX ni
     content-type macroEnabled; límites anti zip-bomb (entradas, ratio,
     tamaño descomprimido).
   - XML: `defusedxml` (XXE, billion laughs, DTD).
6. **SHA-256 → dedupe**: un hash ya `clean` salta el escaneo.
7. **ClamAV** (servicio Cloud Run separado y privado, INSTREAM sobre HTTP con
   OIDC validado por IAM). Solo `clean` continúa; escáner caído →
   `pending_scan` y nada se guarda.
8. **Upload** al bucket privado con key `{mailbox_id}/{sha256}.{ext}` (nunca
   el nombre original) e insert de la fila de metadata.
9. **Extracción bajo demanda** (`include_text=true`): subproceso con timeout y
   límite de memoria, sanitización (NFKC, zero-width, bidi overrides, tag
   chars, comentarios HTML, NUL) y `injection_flags` por heurísticas regex
   (marcan, no bloquean). Texto cacheado en Supabase por sha256.
10. **Respuesta a Claude**: acotada y paginada (`text_offset`, `next_offset`),
    envuelta en `<untrusted_attachment sha256=... sender_trust=...>` con aviso
    fijo de "datos, no instrucciones".

## Tools

- `list_emails_metadata` / `get_emails_content`: incluyen `attachments[]` con
  `{part_id, filename, content_type, size_bytes, inline}` (metadata vía
  `BODY.PEEK[HEADER] BODYSTRUCTURE`, sin bajar cuerpos). Las imágenes inline
  (Content-ID o <20 KB) vienen con `inline: true`.
- `download_attachment(email_id, part_id, include_text=false,
  text_offset=0, max_chars=6000, account_name)` →
  `{status, sha256, scan_status, signed_url, expires_at, text?, next_offset?,
  injection_flags}`. `status`: `ok | blocked | pending_scan | error`.
- `sync_unread_emails`: con `MCP_EMAIL_ATTACHMENT_AUTO_ARCHIVE=true` archiva
  automáticamente adjuntos de remitentes allowlisted, con límite de bytes por
  corrida (`MCP_EMAIL_ATTACHMENT_AUTO_ARCHIVE_MAX_BYTES`). En cada poll se
  borran las filas y objetos expirados (`MCP_EMAIL_ATTACHMENT_RETENTION_DAYS`,
  20 por defecto).

## Notas de seguridad

- No existe parámetro `allow_untrusted`: las decisiones de confianza las toma
  un humano vía la tabla de policies, nunca el modelo vía argumentos de tool.
- Solo PostgREST con JSON body y filtros `eq.` parametrizados; los
  identificadores `email_id` (`^[^:]+:\d+$`) y `part_id` (`^\d+(\.\d+)*$`) se
  validan con regex antes de usarse.
- El texto extraído se trata siempre como DATO no confiable.

## Escáner ClamAV (`scanner/`)

Cloud Run separado y privado (2 GiB, min-instances 0): IAM valida el token
OIDC antes de `POST /scan`; el endpoint reenvía el contenido con INSTREAM a
`clamd` local. Las firmas se hornean en la imagen en build (`freshclam` en el
Dockerfile) y un rebuild nocturno con Cloud Scheduler + Cloud Build las
mantiene frescas, evitando cold starts de 20-60 s.
