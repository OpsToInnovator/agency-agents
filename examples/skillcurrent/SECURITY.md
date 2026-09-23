# Security policy for SkillCurrent

## Reporting a vulnerability

Please do not open a public issue. Report it privately through this
repository's **Security** tab ("Report a vulnerability"), which opens a
private advisory only the maintainers can see. Include the version
(`skillcurrent --version`), what you did, what happened, and what an
attacker could gain.

We aim to acknowledge a report within two working days and to agree a fix
and a disclosure date with you. During the beta, the latest commit on
`main` is the only supported version. Fixes ship there first, and pilot
teams hear about them on their email thread.

## What is in scope

- The HTTP server: authentication, the `/api/rpc` operations and their role
  checks, `POST /api/beta`, and what `--public` mode exposes.
- The installer: where it writes files, and how `status` and `sync` decide
  what to overwrite.
- Token issue, storage and rotation.
- The beta page and web UI: injection, and anything that makes a request to
  a third party.

Skill content is out of scope as a vulnerability in SkillCurrent. A skill is
text an agent will read, so review it the way you review code. The checks
are transparent text rules; they are not a sandbox or a malware scanner.

## The security model, plainly

- **Tokens.** Each member gets a random bearer token (`ts_` plus 24 random
  bytes, URL-safe). Only its SHA-256 hash is stored. There is no expiry and
  no SSO; `members rotate-token` replaces one.
- **Transport.** The server speaks plain HTTP. Anything beyond a trusted
  network must go through TLS: `deploy/Caddyfile` does this with automatic
  certificates.
- **`--no-auth`** trusts two request headers as identity. It is for demos on
  your own machine, and `serve` refuses it on anything but a loopback
  address, or together with `--public`.
- **`--public`** is the internet-facing waitlist host. It serves the beta
  page, its assets, `POST /api/beta`, `whoami`, and three waitlist
  operations (list, remove, import) for owners of the waitlist team. The web
  UI and every other operation are refused.
- **The sign-up endpoint** takes at most 16 KB and five sign-ups per client
  address per ten minutes. It has a honeypot field, and it accepts only the
  form's own choices for team size and tools. A repeat post for the same
  email never overwrites or removes what is stored: it can only fill empty
  fields and add tools and a note, and each repeat is counted. Client addresses are held in memory for the rate
  limit and are not logged with `--public` unless `--access-log` is set.
- **Every connection** has a 30-second socket timeout, and a request body
  needs a valid `Content-Length` within the route's size limit. Every
  response carries `nosniff`, `DENY` framing and a strict referrer policy.
  Pages carry a Content Security Policy that loads nothing from another
  origin. Errors never return a traceback.
- **The installer** writes only to `<target folder>/<slug>/SKILL.md`. Slugs
  are lowercase words joined by hyphens, so they cannot climb out of the
  folder. `sync` repairs a copy at the path recorded when it was installed,
  and refuses any recorded path that is not `<slug>/SKILL.md`. It overwrites
  a copy only when it is outdated or missing, or modified and you passed
  `--force`. A member records installs only as themselves, and `sync`
  reads only the caller's own records.
- **The database** is one SQLite file. Protect it with file permissions and
  take backups (`skillcurrent backup`, `deploy/backup.sh`). It is not
  encrypted at rest.

## For operators

- Run `serve` as its own unprivileged user (the Docker image and
  `deploy/skillcurrent.service` do), on loopback behind TLS.
- Use `--trust-proxy` only behind your own reverse proxy. Otherwise a client
  can choose its own address for the rate limit.
- Keep backups off the machine as well.
