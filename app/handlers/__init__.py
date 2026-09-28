"""Feature handlers.

Convention: each module exposes ``register(application)`` and
``app/bot.py`` calls it from ``build_application()``.

Modules:
    app/handlers/start.py      /start, /help, /trending + welcome callbacks
    app/handlers/search.py     auto-filter (group + PM text search, file delivery)
    app/handlers/callbacks.py  pagination, movie detail, did-you-mean, stream links
    app/handlers/index.py      channel file auto-indexing + new-movie alerts
    app/handlers/forcesub.py   smart force-subscribe ("I've Joined" auto-deliver)
    app/handlers/requests.py   "Request this movie" user flow
    app/handlers/admin.py      admin commands: stats/broadcast/ban/settings/requests
    app/handlers/common.py     shared helpers (NOT handlers): user/group upserts,
                               admin guard, settings cache, force-sub checks
"""
