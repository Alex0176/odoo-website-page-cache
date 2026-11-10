# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from . import store, triggers


def post_init_hook(env):
    """Create the change journal and triggers right away; starts with an empty cache."""
    triggers.ensure_triggers(env.cr)
    store.clear_all(env.cr.dbname)


def uninstall_hook(env):
    """Leave no trigger, journal table or cached page behind."""
    triggers.drop_all(env.cr)
    store.clear_all(env.cr.dbname)
