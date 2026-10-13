-- Exact read-only ClickHouse query executed through Supabase unified logs.
-- Time-bounded to the real Storage HTTP acceptance request window.
select
  timestamp,
  log_attributes['operation'] as operation,
  log_attributes['req.method'] as method,
  log_attributes['res.statusCode'] as response_status,
  log_attributes['error.statusCode'] as error_status,
  log_attributes['error.errorCode'] as error_code,
  log_attributes['owner'] as actor_uid
from logs
where source = 'storage_logs'
  and log_attributes['context.type'] = 'request'
order by timestamp asc
limit 50;

-- Connector bounds supplied separately:
-- start 2026-10-13T08:20:22Z
-- end   2026-10-13T08:20:27Z
