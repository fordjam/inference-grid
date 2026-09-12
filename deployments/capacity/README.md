# Authenticated Railway capacity PWA

This is the standalone deployment bundle for the phone dashboard. Deploy this directory as the build root. It receives sanitized snapshots from a local uploader; it does not contain native provider credentials or collectors.

Required environment: GRID_PUBLIC_HOST (exact HTTPS hostname), GRID_USERNAME, GRID_PASSWORD_SALT (hex), GRID_PASSWORD_HASH (scrypt n=16384,r=8,p=1), GRID_SESSION_KEY (random >=32 characters), GRID_UPLOAD_TOKEN (separate random >=32 characters), GRID_SNAPSHOT_DB (default /data/capacity.sqlite), PORT (default8080). Attach a persistent volume at /data. Keep credentials out of source control and configure them via the hosting secret store.

The browser receives a Secure/HttpOnly/SameSite=Strict signed session cookie. The upload endpoint accepts a separate bearer token that cannot read the dashboard. Login and logout accept the exact HTTPS Origin. Login also supports absent/null Origin only with a signed, cookie-bound form token; explicit foreign origins are refused. Anonymous API reads are denied. API responses are never stored by the service worker.

Run `python -m unittest -v test_cloud` to test login, origins, cookie integrity, upload isolation, payload limits and snapshot persistence.

Run `python upload.py /path/to/private-client.json` on the Mac. Client JSON contains local_feed, remote_url and upload_token; protect it with mode0600. Run once perminute with the host service manager. The local collector/PWA feed must be available; failures leave the last cloud snapshot intact. No provider keys, local task names or prompts are uploaded. When uploads stop for two minutes the dashboard marks the Mac as not reporting; each provider also has its own observation age.

On iPhone open the HTTPS address in Safari, sign in and use Share → Add to Home Screen. No VPN is required. The site remains available while the Mac sleeps, but new usage cannot arrive until its collectors and uploader run again.

Node.js 22 runs the display checks: `node test_display.cjs` and `node test_refresh.cjs`. The Refresh button downloads the latest uploaded snapshot; it does not bypass provider cooldowns or trigger new native collection.
