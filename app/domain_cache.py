"""Called as proxy-sub by the root worker; database writes stay unprivileged."""
import sys

import server

snapshot = server.generator.get_snapshot(server.db)
if '--check' in sys.argv:
    options = server.generator.defaults(server.CONFIG)
    options.update({key: snapshot['settings'].get(key, options[key]) for key in ('preferredIps', 'nodeLimit')})
    server.generator.compile_settings(options, server.CONFIG)
else:
    server.generator.save(snapshot['settings'], server.CONFIG, server.db)
if '--clear-sessions' in sys.argv:
    with server.db() as conn:
        conn.execute('DELETE FROM sessions')
