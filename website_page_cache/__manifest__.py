# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Website Page Cache",
    "summary": "Serves public shop and blog pages from a shared file cache instead of rendering them for "
    "every visitor, crawler and search engine",
    "version": "19.0.1.4.0",
    "category": "Website",
    "author": "Biricon IT Services GmbH",
    "website": "https://github.com/Alex0176/odoo-website-page-cache",
    "license": "LGPL-3",
    "depends": ["website"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/dashboard_views.xml",
    ],
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
}
