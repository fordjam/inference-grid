# Capacity PWA

Run `inference-grid-capacity --port 8040 --upstream http://127.0.0.1:8020/api/usage` with an existing sanitized local quota feed. Open http://127.0.0.1:8040 in a browser. The server listens only on loopback and rejects unexpected Host headers.

The PWA shows five provider cards, separate short/weekly/monthly allowances where reported, remaining/used toggles, reset timing, observation freshness and reported work. Missing windows are shown as unknown. Freshness expires after 15 minutes; a successful HTTP connection is not proof of fresh provider data.

The optional `--overlay` JSON file contains sanitized account observations. Newer timestamps win; a newer failed read replaces an old success. Credentials and arbitrary upstream fields are not forwarded. A native credential refresher is host configuration, not part of the app shell.

Install from a supporting desktop browser, or use Safari Add to Dock. The service worker caches only static app assets; API responses are never cached. Offline mode shows unavailable live data. Phone installation requires a separately configured authenticated HTTPS endpoint reachable from the phone; this loopback deployment does not provide that.

The local quota feed and this server must remain running. No login service or external hosting is installed automatically.
