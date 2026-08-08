-- Schema de demonstration : e-commerce avec suivi email.
-- QueryForge ne connait rien de ce fichier : il decouvre tout par introspection.
-- Remplacez-le par votre propre base, le moteur s'adapte.

CREATE TABLE customers (
    id           SERIAL PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE,
    full_name    TEXT NOT NULL,
    country      TEXT NOT NULL,
    segment      TEXT NOT NULL,
    signed_up_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  customers         IS 'Clients inscrits';
COMMENT ON COLUMN customers.segment IS 'Segment marketing du client';

CREATE TABLE products (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    unit_price NUMERIC(10, 2) NOT NULL CHECK (unit_price >= 0)
);

CREATE TABLE orders (
    id          SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status      TEXT NOT NULL,
    total_amount NUMERIC(10, 2) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN orders.status IS 'Etat de la commande dans le tunnel';

CREATE TABLE order_items (
    id         SERIAL PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(10, 2) NOT NULL
);

CREATE TABLE email_campaigns (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE email_events (
    id          SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES email_campaigns(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    sent_at     TIMESTAMPTZ NOT NULL,
    opened_at   TIMESTAMPTZ,
    clicked_at  TIMESTAMPTZ
);

COMMENT ON COLUMN email_events.opened_at IS 'NULL si l email n a jamais ete ouvert';

CREATE INDEX idx_orders_customer   ON orders(customer_id);
CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_items_order       ON order_items(order_id);
CREATE INDEX idx_email_customer    ON email_events(customer_id);
CREATE INDEX idx_email_opened      ON email_events(customer_id) WHERE opened_at IS NULL;
