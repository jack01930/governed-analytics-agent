"""Create ecommerce schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-01
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the ecommerce analytics tables and query indexes."""
    op.execute("""
        create table categories (
          category_id bigint generated always as identity primary key,
          category_code text not null unique,
          category_name text not null,
          created_at timestamptz not null default now(),
          constraint categories_code_nonempty check (btrim(category_code) <> ''),
          constraint categories_name_nonempty check (btrim(category_name) <> '')
        );

        create table customers (
          customer_id bigint generated always as identity primary key,
          customer_code text not null unique,
          segment text not null,
          region text not null,
          registered_at timestamptz not null,
          constraint customers_segment_allowed check (segment in ('new', 'regular', 'vip')),
          constraint customers_region_nonempty check (btrim(region) <> '')
        );

        create table products (
          product_id bigint generated always as identity primary key,
          sku text not null unique,
          category_id bigint not null references categories(category_id) on delete restrict,
          product_name text not null,
          list_price numeric(14,2) not null,
          unit_cost numeric(14,2) not null,
          is_active boolean not null default true,
          constraint products_price_nonnegative check (list_price >= 0),
          constraint products_cost_nonnegative check (unit_cost >= 0),
          constraint products_cost_not_above_price check (unit_cost <= list_price)
        );

        create table orders (
          order_id bigint generated always as identity primary key,
          order_code text not null unique,
          customer_id bigint not null references customers(customer_id) on delete restrict,
          status text not null,
          ordered_at timestamptz not null,
          region text,
          channel text not null,
          currency text not null default 'CNY',
          gross_amount numeric(14,2) not null,
          discount_amount numeric(14,2) not null default 0,
          shipping_amount numeric(14,2) not null default 0,
          payable_amount numeric(14,2) not null,
          updated_at timestamptz not null,
          constraint orders_status_allowed check (
            status in ('placed', 'paid', 'completed', 'cancelled', 'refunded')
          ),
          constraint orders_channel_allowed check (
            channel in ('organic', 'search', 'social', 'affiliate', 'email')
          ),
          constraint orders_currency_cny check (currency = 'CNY'),
          constraint orders_amounts_nonnegative check (
            gross_amount >= 0 and discount_amount >= 0 and shipping_amount >= 0
            and payable_amount >= 0
          )
        );

        create table order_items (
          order_item_id bigint generated always as identity primary key,
          source_line_id text not null,
          order_id bigint not null references orders(order_id) on delete restrict,
          product_id bigint not null references products(product_id) on delete restrict,
          quantity integer not null,
          unit_price numeric(14,2) not null,
          discount_amount numeric(14,2) not null default 0,
          gross_amount numeric(14,2) not null,
          net_amount numeric(14,2) not null,
          constraint order_items_quantity_positive check (quantity > 0),
          constraint order_items_amounts_nonnegative check (
            unit_price >= 0 and discount_amount >= 0 and gross_amount >= 0 and net_amount >= 0
          )
        );

        create table payments (
          payment_id bigint generated always as identity primary key,
          payment_code text not null unique,
          order_id bigint not null references orders(order_id) on delete restrict,
          status text not null,
          provider text not null,
          amount numeric(14,2) not null,
          paid_at timestamptz,
          created_at timestamptz not null,
          constraint payments_status_allowed check (status in ('pending', 'succeeded', 'failed')),
          constraint payments_provider_allowed check (provider in ('alipay', 'wechat_pay', 'card')),
          constraint payments_amount_nonnegative check (amount >= 0)
        );

        create table refunds (
          refund_id bigint generated always as identity primary key,
          refund_code text not null unique,
          order_id bigint not null references orders(order_id) on delete restrict,
          order_item_id bigint references order_items(order_item_id) on delete restrict,
          status text not null,
          amount numeric(14,2) not null,
          reason text not null,
          refunded_at timestamptz,
          created_at timestamptz not null,
          constraint refunds_status_allowed check (
            status in ('requested', 'succeeded', 'rejected')
          ),
          constraint refunds_amount_positive check (amount > 0),
          constraint refunds_reason_nonempty check (btrim(reason) <> '')
        );

        create table inventory_snapshots (
          inventory_snapshot_id bigint generated always as identity primary key,
          snapshot_at timestamptz not null,
          product_id bigint not null references products(product_id) on delete restrict,
          available_qty integer not null,
          reserved_qty integer not null default 0,
          constraint inventory_snapshot_unique unique (snapshot_at, product_id),
          constraint inventory_quantities_nonnegative check (
            available_qty >= 0 and reserved_qty >= 0
          )
        );

        create table web_sessions (
          session_id bigint generated always as identity primary key,
          session_code text not null unique,
          customer_id bigint references customers(customer_id) on delete restrict,
          order_id bigint references orders(order_id) on delete restrict,
          channel text not null,
          occurred_at timestamptz not null,
          converted boolean not null default false,
          duration_seconds integer not null,
          constraint web_sessions_channel_allowed check (
            channel in ('organic', 'search', 'social', 'affiliate', 'email')
          ),
          constraint web_sessions_duration_nonnegative check (duration_seconds >= 0),
          constraint web_sessions_conversion_order check (not converted or order_id is not null)
        );

        create table marketing_campaigns (
          campaign_id bigint generated always as identity primary key,
          campaign_code text not null unique,
          campaign_name text not null,
          channel text not null,
          start_at timestamptz not null,
          end_at timestamptz not null,
          spend numeric(14,2) not null,
          constraint campaigns_channel_allowed check (
            channel in ('search', 'social', 'affiliate', 'email')
          ),
          constraint campaigns_dates_ordered check (end_at > start_at),
          constraint campaigns_spend_nonnegative check (spend >= 0)
        );

        create table campaign_attributions (
          attribution_id bigint generated always as identity primary key,
          campaign_id bigint not null references marketing_campaigns(campaign_id)
            on delete restrict,
          order_id bigint not null references orders(order_id) on delete restrict,
          attributed_revenue numeric(14,2) not null,
          attributed_at timestamptz not null,
          constraint campaign_order_unique unique (campaign_id, order_id),
          constraint attribution_revenue_nonnegative check (attributed_revenue >= 0)
        );

        create table pipeline_runs (
          pipeline_run_id bigint generated always as identity primary key,
          pipeline_name text not null,
          started_at timestamptz not null,
          finished_at timestamptz,
          status text not null,
          watermark timestamptz,
          row_count bigint,
          error_code text,
          constraint pipeline_status_allowed check (status in ('running', 'succeeded', 'failed')),
          constraint pipeline_row_count_nonnegative check (row_count is null or row_count >= 0),
          constraint pipeline_finish_after_start check (
            finished_at is null or finished_at >= started_at
          )
        );

        create index products_category_id_idx on products (category_id);
        create index customers_segment_idx on customers (segment);
        create index customers_region_idx on customers (region);
        create index orders_customer_ordered_idx on orders (customer_id, ordered_at);
        create index orders_status_ordered_idx on orders (status, ordered_at);
        create index orders_region_ordered_idx on orders (region, ordered_at);
        create index orders_channel_ordered_idx on orders (channel, ordered_at);
        create index order_items_order_id_idx on order_items (order_id);
        create index order_items_product_id_idx on order_items (product_id);
        create index order_items_source_line_id_idx on order_items (source_line_id);
        create index payments_order_status_idx on payments (order_id, status);
        create index payments_paid_at_idx on payments (paid_at) where status = 'succeeded';
        create index refunds_order_id_idx on refunds (order_id);
        create index refunds_order_item_id_idx on refunds (order_item_id);
        create index refunds_refunded_at_idx on refunds (refunded_at) where status = 'succeeded';
        create index inventory_product_snapshot_idx on inventory_snapshots
          (product_id, snapshot_at desc);
        create index web_sessions_customer_occurred_idx on web_sessions (customer_id, occurred_at);
        create index web_sessions_order_id_idx on web_sessions (order_id);
        create index web_sessions_channel_occurred_idx on web_sessions (channel, occurred_at);
        create index campaigns_channel_start_idx on marketing_campaigns (channel, start_at);
        create index attributions_campaign_id_idx on campaign_attributions (campaign_id);
        create index attributions_order_id_idx on campaign_attributions (order_id);
        create index pipeline_name_started_idx on pipeline_runs (pipeline_name, started_at desc);
        """)


def downgrade() -> None:
    """Drop ecommerce analytics tables in dependency order."""
    op.execute("""
        drop table if exists campaign_attributions;
        drop table if exists pipeline_runs;
        drop table if exists web_sessions;
        drop table if exists inventory_snapshots;
        drop table if exists refunds;
        drop table if exists payments;
        drop table if exists order_items;
        drop table if exists orders;
        drop table if exists marketing_campaigns;
        drop table if exists products;
        drop table if exists customers;
        drop table if exists categories;
        """)
