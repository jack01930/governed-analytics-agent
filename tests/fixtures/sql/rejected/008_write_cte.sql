with changed as (
  insert into categories (category_code, category_name) values ('x', 'x') returning category_id
) select category_id from changed
