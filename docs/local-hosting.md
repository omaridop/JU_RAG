# Local Cloudflare hosting

The public origin runs with Waitress on `127.0.0.1:5000`. Both
`kasitbot.app` and `www.kasitbot.app` point to this origin through the existing
Cloudflare tunnel. The PC must remain powered on and connected to the internet.

`scripts/serve_public.py` supervises the origin, reads the local `.env` for each
restart, and enforces secure cookies, proxy awareness, and debug-off settings.
It checks port 5000 before launching and uses a Windows lock to prevent duplicate
supervisors. Logs are in the ignored `logs/public-server.log` file.

A per-user Windows Startup entry, `JU Assistant.vbs`, starts the supervisor after
sign-in. This is a user-session startup arrangement, not a Windows service that
runs before login. The existing Cloudflare connector has its own supervisor.

## Updating credentials

Edit the ignored `.env` locally. Stop only the process listening on port 5000;
the supervisor starts a new process with the updated configuration. Never run a
second application process against the same Chroma directory. Do not paste
credentials into source files or commit them.

## Verification

- `https://kasitbot.app/healthz` must return HTTP 200.
- Check `/admin/login` and the chat page through HTTPS.
- Test `/api/chat` as well: a health check does not confirm model availability.

## Rollback

Before the switch, the domain pointed to the existing local application on
port 57875. That application and its data were retained. A copy of the original
tunnel configuration is stored outside this repository under the user's
`.cloudflared/backups/kasitbot-20261020-jurag/cloudflare-original.yml`.

Restore that backup to the dedicated attendance tunnel's `Data/cloudflare.yml`,
validate it with `cloudflared tunnel ingress validate`, and restart only that
tunnel's connector. Its existing supervisor will relaunch it. Do not restart
other Cloudflare connectors. Remove the JU Assistant Startup entry and stop its
supervisor and origin if JU_RAG should no longer run locally.
