{
    'name': 'Orders Data Migration',
    'version': '19.0.1.0.1',
    'category': 'Tools',
    'summary': 'Migrate Sales/Purchase Orders with Deliveries, Invoices and Bills from another Odoo, keeping all links',
    'description': """
Orders Data Migration
=====================
Copy sales orders, purchase orders, deliveries, receipts, customer invoices
and vendor bills from another Odoo database into this one, keeping the links
between them. Test the connection, preview and validate first, then migrate
in batches. A migration report lists every record that was created, skipped
or failed.
    """,
    'author': 'Neemkaroli Consulting LLP',
    'website': 'https://neemkaroliconsulting.com/',
    'support': 'neemkaroligroup@gmail.com',
    'license': 'LGPL-3',
    'price': 29.00,
    'currency': 'EUR',
    'depends': ['sale_management', 'sale_stock', 'purchase', 'purchase_stock', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'views/migration_views.xml',
    ],
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': True,
}
