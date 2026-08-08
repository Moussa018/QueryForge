-- Donnees de demonstration generees proceduralement.
-- Volume vise : ~800 clients, ~6000 commandes, ~9000 evenements email.
-- Assez pour que les plans EXPLAIN et les graphiques soient realistes.

INSERT INTO customers (email, full_name, country, segment, signed_up_at)
SELECT
    'client' || i || '@exemple.fr',
    (ARRAY['Camille','Dominique','Claude','Sasha','Charlie','Alex','Morgan','Noa'])[1 + (i % 8)]
        || ' ' ||
    (ARRAY['Martin','Bernard','Dubois','Thomas','Robert','Petit','Durand'])[1 + (i % 7)],
    (ARRAY['France','Belgique','Suisse','Canada','Maroc'])[1 + (i % 5)],
    (ARRAY['particulier','pro','grand_compte'])[1 + (i % 3)],
    now() - (random() * 700)::int * interval '1 day'
FROM generate_series(1, 800) i;

INSERT INTO products (name, category, unit_price)
SELECT
    'Produit ' || i,
    (ARRAY['informatique','maison','sport','livre','jardin'])[1 + (i % 5)],
    round((5 + random() * 300)::numeric, 2)
FROM generate_series(1, 120) i;

-- Commandes concentrees sur les 120 derniers jours, avec une part importante
-- sur le mois courant pour que les questions "ce mois-ci" renvoient des lignes.
INSERT INTO orders (customer_id, status, total_amount, created_at)
SELECT
    1 + (random() * 799)::int,
    (ARRAY['pending','paid','shipped','delivered','cancelled'])[1 + (i % 5)],
    round((20 + random() * 900)::numeric, 2),
    CASE
        WHEN i % 3 = 0
            THEN date_trunc('month', CURRENT_DATE) + (random() * 27)::int * interval '1 day'
        ELSE now() - (random() * 120)::int * interval '1 day'
    END
FROM generate_series(1, 6000) i;

INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT
    o.id,
    1 + (random() * 119)::int,
    1 + (random() * 4)::int,
    round((5 + random() * 300)::numeric, 2)
FROM orders o
CROSS JOIN generate_series(1, 1 + (random() * 2)::int) AS n;

INSERT INTO email_campaigns (name, sent_at)
SELECT
    'Campagne ' || to_char(now() - i * interval '1 month', 'Mon YYYY'),
    now() - i * interval '1 month'
FROM generate_series(0, 11) i;

-- Un taux d'ouverture purement aleatoire ne suffit pas : avec ~11 emails par
-- client, presque tout le monde finit par en ouvrir un, et la question phare
-- ("commande beaucoup mais n'ouvre aucun email") ne renvoie plus rien.
-- On isole donc une vraie cohorte de non-ouvreurs : 1 client sur 7 n'ouvre
-- jamais rien, les autres ouvrent environ un email sur deux.
INSERT INTO email_events (campaign_id, customer_id, sent_at, opened_at, clicked_at)
SELECT
    1 + (random() * 11)::int,
    cust,
    sent,
    CASE WHEN cust % 7 <> 0 AND random() > 0.45
         THEN sent + (random() * 48)::int * interval '1 hour' END,
    CASE WHEN cust % 7 <> 0 AND random() > 0.85
         THEN sent + (random() * 72)::int * interval '1 hour' END
FROM (
    SELECT
        1 + (random() * 799)::int AS cust,
        now() - (random() * 300)::int * interval '1 day' AS sent
    FROM generate_series(1, 9000)
) s;

-- Les estimations EXPLAIN dependent des statistiques du planificateur.
ANALYZE;
