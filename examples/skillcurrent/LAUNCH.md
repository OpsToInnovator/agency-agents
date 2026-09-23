# Launching the SkillCurrent beta

How to put the beta page on the internet, recruit five to ten pilot teams,
run the pilot, and decide what the results mean. Work through it in order.

## Where things stand

**Built and tested** (by the test suite, CI and the browser test):

- The beta page, packaged with the tool. `serve --public` makes it the front
  page of an internet-facing host that exposes nothing else.
- Sign-ups stored in the database, rate limited, spam-trapped, never
  overwritten, tagged with the link that brought them (`?c=drift`).
- Waitlist tools: list, count by link tag, delete on request, import.
- `beta-report`, which answers the two beta questions from recorded events,
  and says "never checked" when the sync hook isn't running.
- Deploy recipes: Caddy for HTTPS, Docker Compose, systemd, daily backups.
- The pilot-team guide (`BETA.md`), beta terms, call scripts and email
  templates (`docs/beta/`), and the ad kit (`marketing/ads/`).

**Not verified yet.** Check each before you rely on it:

- The Osaurus skills folder. The Claude Code, Codex, Gemini CLI, Cursor and
  Antigravity folders were checked against their docs on 22 September 2026
  (README, "Install targets"). Vendors move them; recheck before each wave
  of installs.
- The Netlify Forms route, until one test submission shows up in Netlify.
- Windows. CI covers Linux and macOS only.
- Mail deliverability from your beta address, until the test below passes.

## 1. Decisions only you can make

Write each answer down before day 0. The recommendations are defaults, not
facts.

| Decision | Recommendation | Why it matters |
|---|---|---|
| **Who "we" is** | Your name, and a company if you have one | The page, emails and terms say "we". The terms need a name, entity and country. If it's just you, say "I" in posts. |
| **Hosting** | One small VPS running `serve --public` behind Caddy | It is the path that's fully tested end to end, and sign-ups land straight in `skillcurrent beta`. The alternatives are below. |
| **Domain** | A domain you own, with the page on `beta.<domain>` | Needed for HTTPS and a trustworthy reply address. Check the name for trademark conflicts in your market before paying for it. |
| **Reply address** | A mailbox on that domain, with SPF, DKIM and DMARC set up by your mail provider | The page and every email send replies there. Avoid forwarding into a personal inbox and replying with "send mail as" through that inbox's servers: the mail isn't signed for your domain, so it can fail DMARC and land in spam. Don't publish a personal address. |
| **Licence** | Keep the core under MIT and add your own copyright line in `examples/skillcurrent/LICENSE`, before anyone else contributes (`BUSINESS.md` section 8) | The only licence file is the repository's MIT licence, "Copyright (c) 2025 AgentLand Contributors". The business sells support, not access, so it doesn't need a different licence. |
| **Repository home** | Keep it here for the beta. Consider its own repository before Show HN | The install command works from here today. A dedicated repository gives issues, releases and stars their own home. |
| **Team cap** | Invite up to 10 qualified teams and expect two or three to stall | The page promises 5 to 10. |
| **After the beta** | The terms already say it: the source stays open, and pilot teams get any paid plan free for 12 months. The plan itself is in `BUSINESS.md` | Teams will ask. Don't promise prices or dates until the exit interviews have tested them. |
| **Legal review** | One fixed-fee review covering terms, privacy, ad claims, names and the order form. Send `docs/business/legal-brief.md` | Terms and privacy before the first pilot team signs; ad copy before paid spend; the order form before the first invoice. |
| **Exit rules** | Adopt section 6 or edit it, before week 1 | Deciding after you see the data invites wishful reading. |

## 2. Go live (day 0, about half a day)

### Before you start

1. **Merge this change to `main`.** The install command in `BETA.md`
   installs from `main`. Optionally tag the merge (`skillcurrent-v0.2.0`)
   and publish a GitHub release with its known issues.
2. **Turn on private vulnerability reporting** for the repository
   (Settings, then Code security). `SECURITY.md` sends reports there.
3. **Fill in `docs/beta/terms.md`** and delete its operator box.

### Domain and mail

4. Point an `A` record (and `AAAA` if the server has IPv6) for
   `beta.<domain>` at the server's address.
5. Set up the beta mailbox with SPF, DKIM and DMARC as your provider
   documents. Send a test to a Gmail and an Outlook address. Open the
   message headers and check that SPF, DKIM and DMARC all say `pass`.

### The server

6. Create the smallest VM of a provider you trust, with a current Ubuntu
   LTS, SSH keys only, and ports 22, 80 and 443 open.
7. Install SkillCurrent as a service (from `deploy/skillcurrent.service`):

   ```bash
   sudo apt-get install -y python3 python3-venv git     # check: python3 --version is 3.11 or newer
   sudo useradd --system --home /var/lib/skillcurrent --create-home skillcurrent
   sudo -u skillcurrent python3 -m venv /var/lib/skillcurrent/venv
   sudo -u skillcurrent /var/lib/skillcurrent/venv/bin/pip install \
     "git+https://github.com/OpsToInnovator/agency-agents@main#subdirectory=examples/skillcurrent"
   sudo -u skillcurrent /var/lib/skillcurrent/venv/bin/skillcurrent \
     --db /var/lib/skillcurrent/skillcurrent.sqlite init --team beta --owner <you>
   ```

   Copy `deploy/skillcurrent.service` to `/etc/systemd/system/`, set the
   `SKILLCURRENT_CONTACT` and `SKILLCURRENT_SITE_URL` lines to your address
   and `https://beta.<domain>`, then
   `sudo systemctl enable --now skillcurrent`. Prefer Docker? Use
   `deploy/docker-compose.yml` the same way; its header has the commands.
8. Install Caddy from its official packages, put `deploy/Caddyfile` in
   `/etc/caddy/Caddyfile` with your hostname, and reload it. Caddy gets the
   certificate by itself once DNS points at the machine.
9. Check it from your laptop:

   ```bash
   curl -sI https://beta.<domain>/ | head -n 12      # 200, HSTS, CSP, nosniff
   curl -s https://beta.<domain>/api/health          # {"ok": true, ..., "public": true}
   curl -s -o /dev/null -w '%{http_code}\n' https://beta.<domain>/app   # 404: no web UI in public mode
   ```

### Prove the whole path once

10. Open `https://beta.<domain>/?c=launch-test` in a browser. The privacy
    line under the form should say your address is "never written to disk".
    Sign up with a real address you control.
11. On the server, confirm the entry and its tag, then remove it:

    ```bash
    sc="sudo -u skillcurrent /var/lib/skillcurrent/venv/bin/skillcurrent --db /var/lib/skillcurrent/skillcurrent.sqlite --team beta --as <you>"
    $sc beta --sources
    $sc beta remove <that address>
    ```

12. Share the link in a chat app and check that the preview card shows.

### Keep it running

13. **Backups.** Copy `deploy/backup.sh` to `/var/lib/skillcurrent/`, make
    it executable, and add `15 3 * * * /var/lib/skillcurrent/backup.sh` to the
    skillcurrent user's crontab (`sudo crontab -u skillcurrent -e`). Copy
    the backups folder to storage off the machine too.
14. **Uptime.** Point any uptime monitor at `https://beta.<domain>/api/health`
    every five minutes, alerting your phone.
15. **Daily habit.** Once a day, list new sign-ups and answer each one. The
    page promises a reply within two working days.

### The zero-server alternatives

- **Static page plus Netlify Forms.** Run
  `skillcurrent build-landing site --site-url https://beta.<domain> --endpoint netlify --contact <beta address>`
  and deploy the `site` folder to Netlify, with form detection turned on in
  Netlify's settings (its docs say forms are only detected once it is).
  Sign-ups land in Netlify's dashboard. Export them as CSV and run `skillcurrent beta import` to use
  the waitlist tools. Netlify stores the entries, and the page's privacy
  line says so. **Not verified here:** submit one test entry and see it
  arrive before you share the link.
- **Static page plus email.** `--endpoint mailto` needs no server at all:
  the visitor's email app opens with their entry. Expect fewer completed
  sign-ups, since the visitor has to press send.
- **Static page, form posting to your server.** `--endpoint https://beta-api.<domain>/api/beta`,
  with `serve --public --allow-origin https://beta.<domain>` on the server.

## 3. Recruiting, weeks 1 to 4

Warm first, then communities, then public launches. Say you built it,
every time. Tag every link with its concept (`?c=method`, `?c=drift`,
`?c=bytes`) and the channel (`&utm_source=reddit`), then check what worked
with `skillcurrent beta --sources`.

| When | Channel | What to do |
|---|---|---|
| Week 1 | **People you know** | 10 to 20 personal messages to people who run two or more AI coding tools in a team. Ask for a conversation, not a sign-up. This is where the first two or three teams usually come from. |
| Week 1 | **LinkedIn**, organic | The "method" post from `marketing/ads/copy.md`, from your own profile. One post a week after that, rotating concepts. |
| Week 1 | **Upstream maintainer** | Ask before posting in msitarzewski/agency-agents Discussions (template in `docs/beta/templates.md`). Post only on a yes. |
| Week 2 | **Discord** | The Claude Developers and Osaurus servers, in the channel their rules allow. Join a few days early and take part first. |
| Week 2 | **Open feature requests** | Two threads ask for exactly this: anthropics/claude-code#28729 (repository-backed org skills) and openai/codex#24517 (team skill hub). Both were open on 22 September 2026. Reread each before commenting and use the comment template. Skip closed threads. |
| Week 2 | **Reddit** | r/ClaudeAI, and r/ChatGPTCoding in its self-promotion thread if it has one. Read each sub's rules that day. Answer every reply for the first few hours. |
| Week 3 or later | **Show HN** | Only once two teams are installed and someone other than you has followed `BETA.md` successfully. Link the repository, which people can install today, and put the beta link in your first comment. Block the whole day to answer. |
| Week 3 or later | **Lists and newsletters** | awesome-claude-code (through its submission form), and Console.dev if you meet its criteria. |
| Throughout | **Cold email** | At most ten a week, written by hand, to addresses people publish for work contact. Never harvest addresses from GitHub. Stop at the first "no". |
| Deferred | **Paid ads** | Not until three teams have completed installs and you know which message brings qualified sign-ups. The creatives are ready in `marketing/ads/out/`. |

Other open-source tools for sharing skills exist (for example "skillshare"
and "skills-manager"). Read them before Show HN, and be ready to say
plainly what SkillCurrent does differently: review of exact bytes, and
evidence of what each machine holds. Don't knock them.

## 4. Running each team

Each team moves through the same stages. Keep one row per team in a
spreadsheet, with the date of each stage:

signed up → replied (within two working days) → qualification call →
invited → install session → weekly reports → day-14 check-in → exit
interview → decision recorded

- Scripts for every call: `docs/beta/call-scripts.md`.
- Emails for every stage: `docs/beta/templates.md`.
- Weekly reports come back by email, on the team's own thread. Never ask
  for them in a public issue.
- Record both verdicts from every report in the sheet: review (`yes`,
  `partly`, `unclear`, `no activity`) and sync (`yes`, `no bad copy seen`,
  `never checked`).

## 5. Day 14: if nobody has signed up

Check the plumbing first: submit a test entry, and confirm your replies
aren't landing in spam. Then find the stage where people drop out:

- **No sign-ups at all.** The message isn't reaching the right people, or
  isn't landing. Stop broadcasting. Have ten direct conversations with
  people who fit, ask how they'd describe the problem, and rewrite the
  headline in their words.
- **Sign-ups, but no calls.** Reply faster, ask fewer questions, and offer
  the three-question email instead of a call.
- **Calls, but no fits.** Look at which fit condition fails most. If it's
  "only one person writes skills", the problem may not exist yet at that
  size; aim at platform leads in larger teams.
- **Installs stall.** The install is too hard. Fix the most common snag
  before recruiting more.

## 6. Exit criteria (proposed; settle them before week 1)

- **Enough evidence to judge:** at least five teams completed the install
  session and sent three or more weekly reports each.
- **Keep building:** at least three of those teams would keep using it, and
  most say "yes" to review, and at least one team's sync caught a bad copy.
- **Rethink review:** most teams show "partly" because of draft installs or
  rubber-stamp approvals. Review is friction they route around; make it
  lighter before going further.
- **Rethink sync:** after four weeks every team says "no bad copy seen", or
  most say "never checked". Drift may not be the pain you thought it was.
- **Stop or change course:** fewer than three teams complete the install by
  week 6, or nobody would keep using it.
- **Deadline:** decide by the end of week 8, whatever the data looks like.

## 7. Time budget

Estimates, not measurements. Adjust after week 2.

| Week | Work | Hours |
|---|---|---|
| 0 | Decisions, domain, mail, server, smoke test, terms | 6 to 8 |
| 1 | Warm outreach, LinkedIn, replies, first calls | 5 to 7 |
| 2 | Community posts, replies, calls, first installs | 6 to 8 |
| 3 | Show HN day, installs | 8 to 10 |
| 4 to 6 | Weekly reports, day-14 calls, fixes | 4 to 6 a week |
| 7 to 8 | Exit interviews, the decision | about 5 |

Each team costs about three hours over the pilot: a 30-minute call, a
60-minute install, 10 minutes a week on reports, a 20-minute check-in and a
30-minute exit interview.

## 8. Accounts to have ready

- A **GitHub** account for Discussions and issue comments.
- A **Hacker News** account, ideally one you already use.
- A **Reddit** account with ordinary participation. Some subreddits set
  minimum account age or karma.
- **Discord**, joined to the servers a few days before you post.
- A **LinkedIn** profile that says what you're building.
- A **booking link** for calls. Optional, but it saves a round of email.

## 9. Costs

A domain, the smallest VM your provider offers, and a mailbox on your
domain. At typical prices that's a few dollars to a few tens of dollars a
month. Everything else here is free.
