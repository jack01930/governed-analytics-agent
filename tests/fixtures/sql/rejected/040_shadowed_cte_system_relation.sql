select rolname
from pg_roles
where exists (
  with pg_roles as (
    select category_id from categories
  )
  select 1 from pg_roles
)
order by rolname
limit 20;
