begin;

update public.email_embeddings
set mailbox_id = case mailbox_id
    when 'contacto' then 'audty_contacto'
    when 'eduardo' then 'meik_eduardo'
    when 'administracion' then 'meik_administracion'
    when 'ventas' then 'meik_ventas'
    when 'compras' then 'meik_compras'
    else mailbox_id
end
where mailbox_id in ('contacto', 'eduardo', 'administracion', 'ventas', 'compras');

update public.email_ingest_state
set mailbox_id = case mailbox_id
    when 'contacto' then 'audty_contacto'
    when 'eduardo' then 'meik_eduardo'
    when 'administracion' then 'meik_administracion'
    when 'ventas' then 'meik_ventas'
    when 'compras' then 'meik_compras'
    else mailbox_id
end
where mailbox_id in ('contacto', 'eduardo', 'administracion', 'ventas', 'compras');

commit;

select mailbox_id, count(*) as rows_count
from public.email_embeddings
where mailbox_id in (
    'audty_contacto',
    'meik_eduardo',
    'meik_administracion',
    'meik_ventas',
    'meik_compras'
)
group by mailbox_id
order by mailbox_id;

select mailbox_id, folder, last_uid
from public.email_ingest_state
where mailbox_id in (
    'audty_contacto',
    'meik_eduardo',
    'meik_administracion',
    'meik_ventas',
    'meik_compras'
)
order by mailbox_id, folder;