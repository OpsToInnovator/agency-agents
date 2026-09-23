# SkillCurrent beta — ad copy

Rules that every line below follows: one conversion goal (join the beta), no
customer logos, no testimonials, no adoption numbers, no claims about how an
agent will behave. Everything stated is something the shipped tool does.

Audience: platform, DX and AI-enablement leads at teams that use two or more
AI coding tools (Claude Code, Codex, Antigravity, Osaurus, Cursor) and share
skills or agent instructions today by copying files.

Landing page: the beta host's front page (`skillcurrent serve --public`
serves it at `/`, and at `/beta` on any server), or a static build from
`skillcurrent build-landing`. See `LAUNCH.md` for both. Every link below
points there with a concept tag, for example `https://beta.example.com/?c=drift`.
The form stores the tags `c`, `utm_source`, `utm_medium`, `utm_campaign` and
`ref` with each sign-up; `skillcurrent beta --sources` counts them.

Voice: the copy says "we". If you are launching alone, say "I"; readers on
Hacker News and Reddit check. The paths below are the install targets the
tool ships; confirm each against the vendor's current docs before posting.

## Hook campaign: what SkillCurrent does that others don't bundle

A competitor scan on 23 September 2026 found each of these mechanics
somewhere, but not all of them in one self-hosted, skills-only tool:

- **Whoever wrote or edited a version can't approve it.** A second maintainer
  approves the exact bytes, stamped with a SHA-256.
- **Early access first.** New versions go to a canary channel, and a maintainer
  moves production when satisfied.
- **Edits are reported, not wiped.** Machines running the hook report
  hand-edited and deleted copies. Sync repairs missing and outdated ones and
  keeps local edits.
- **Skills only, on your server.** No gateway, portal or security suite to adopt
  first.

`claims.md` lists the evidence and the limits behind each line.

**Who it's for:** platform, developer-experience and security leads at
organisations that run two or more coding agents and need separation of
duties for changes. It also suits teams that self-host, or that can't use a
vendor's cloud sync (API keys, Bedrock, regulated environments).

**Who it isn't for:** solo developers, and teams on one agent that already get
organisation skills from their vendor. Anthropic's organisation library
blocks self-approval and syncs to Claude Code for signed-in users.

**Rules for this campaign:**

1. Say what SkillCurrent does. Never write "only", "first", "no one else" or a
   competitor's name in an ad. The category is months old and each mechanic
   exists somewhere, so a uniqueness claim would be false or soon out of date.
   It is also hard to substantiate under the FTC's and the UK CAP Code's rules
   on comparative claims.
2. Keep coding-agent and vendor brand names out of Google ad text. Keywords
   may use them. Creatives and social posts may name the tools SkillCurrent
   installs into, as compatibility facts.
3. Sync claims say "machines running the hook", never "every machine".
4. Describe the canary as human-judged early access, never as automatic or
   "safe" rollout.
5. Terminal output in the creatives must stay real. `tests/test_ads.py`
   replays every line against the CLI and fails if one drifts.
6. Tag every link: `?c=refused|early|noticed|yours&utm_source=<channel>`.

### Creatives

`out/refused-*`, `out/early-*`, `out/noticed-*`, `out/yours-*`, each at
1200×628, 1080×1080 and 1080×1920. Each one shows real CLI output.

| Concept | Headline | Proof shown |
|---|---|---|
| refused | It won't let you approve your own work. | the author's approval refused, then a second maintainer's approval with the SHA-256 |
| early | Early access first. Everyone else when you say so. | approve, release to canary, history showing canary 1.1.0 and production 1.0.0 |
| noticed | Someone edited their copy. Sync noticed, and kept it. | status finds one modified and one missing copy; sync keeps the edit and repairs the missing one |
| yours | Skills only. On your server. | the install targets: Claude Code, Antigravity, the shared ~/.agents/skills folder, any custom folder |

### Google Search (responsive search ad)

Headlines are at most 30 characters and descriptions at most 90. The test
suite checks both limits, plus the banned words from rule 1 and rule 2.

<!-- rsa-hook -->
```text
H: Authors Can't Self-Approve
H: A Second Maintainer Approves
H: Approve the Exact Bytes
H: Separation of Duties, Built In
H: Canary First, Then Everyone
H: Early Access for New Skills
H: Roll Back by Moving a Pointer
H: See Hand-Edited Skill Copies
H: Edits Reported, Not Wiped
H: Self-Hosted Skills Catalog
H: Skills Only, On Your Server
H: No Gateway to Adopt First
H: Review for SKILL.md Files
H: Every Version Hash-Stamped
H: Free During the Beta
D: Whoever wrote or edited a version can't approve it. Another maintainer approves the bytes.
D: Release to an early-access channel first, then production when a maintainer decides.
D: Machines running the hook report hand-edited and deleted copies. Sync repairs the rest.
D: Self-hosted: Python standard library and one SQLite file. Pilot open to 5 to 10 teams.
```

**Keywords** (phrase and exact match): "skills registry", "agent skills
registry", "SKILL.md", "share agent skills with team", "skill versioning",
"claude code skills team", "codex skills team", "agent skills governance".
Brand names are allowed in keywords, but not in ad text.

**Negative keywords:** skills on their own match job-seeking and learning
searches, so exclude: skillshare, course, class, training, certification,
resume, cv, job, jobs, soft skills, interview, salary.

Start with a small daily cap on the skills terms and check `skillcurrent
beta --sources` weekly. The scan found no readable competitor ad on these
terms, but that says nothing about what they cost.

### LinkedIn (sponsored or organic)

**refused**

> It won't let you approve your own work.
>
> When skills are shared by copying files, whoever edits one last decides what everyone's agent reads.
>
> In SkillCurrent, whoever wrote or edited a version can't approve it. A second maintainer signs off on the exact bytes, stamped with a SHA-256, before the version can be released to a channel. The terminal in the image is real output, and a test keeps it that way.
>
> Self-hosted, built for teams with two maintainers and more than one coding agent. Free beta pilot for 5 to 10 teams → [link]?c=refused&utm_source=linkedin

**early**

> Early access first. Everyone else when you say so.
>
> Release a new version of a skill to your canary channel first. The machines on it pick it up at their next sync. A maintainer moves production when the early group is happy, and rolls back by moving the pointer back.
>
> It's human-judged: no traffic split, no automatic metrics, no automatic rollback. We'd rather say that plainly than dress it up.
>
> Free beta pilot for teams on more than one coding agent → [link]?c=early&utm_source=linkedin

**noticed**

> Someone edited their copy of a skill. Sync noticed, and kept it.
>
> Machines running the SkillCurrent hook report hand-edited and deleted copies to the team. Missing and outdated copies are brought back to the released version. A hand-edited one is left alone and counted, so the team can decide whether the edit should become the next version.
>
> It works with the skill folders that Claude Code, Codex, Gemini CLI, Cursor and Antigravity read. Free beta pilot → [link]?c=noticed&utm_source=linkedin

**yours**

> Skills only. On your server.
>
> No gateway, developer portal or security suite to adopt first. SkillCurrent is Python 3.11 with no dependencies and one SQLite file. It runs on your machines, and we never see your skills.
>
> Free during the beta pilot, for 5 to 10 teams that run more than one coding agent → [link]?c=yours&utm_source=linkedin

### X / Threads

Each post stays within 280 characters, counting a link as 23; the test
checks this.

<!-- x-hook -->
- It won't let you approve your own work. In SkillCurrent, whoever wrote or edited a version can't approve it; a second maintainer signs off on the exact bytes. Self-hosted, free beta pilot → [link]
- Early access first, everyone else when you say so. Release new skill versions to a canary channel first; a maintainer moves production. Human-judged: no traffic split, no auto-rollback. Beta → [link]
- Someone edited their copy of a skill. Sync noticed, and kept it. Machines running the SkillCurrent hook report hand-edited and deleted copies to the team, and restore missing ones from the release → [link]
- Skills only, on your server. No gateway or portal to adopt first: Python 3.11, no dependencies, one SQLite file. We never see your skills. Free beta pilot → [link]
<!-- /x-hook -->

## Launch concepts

| Concept | Headline | When to use |
|---|---|---|
| method | Better methods. Not more files. | brand line; cold audiences, sponsorships, OG image |
| drift | Which version is your teammate running? | the problem; retargeting, communities that already feel the pain |
| bytes | Approve exact bytes. | governance angle; platform and security leads |

Creatives for each concept ship in three sizes under `out/`: 1200×628
(link card / OG), 1080×1080 (feed), 1080×1920 (story).

## LinkedIn (sponsored post or organic)

**method**

> Better methods. Not more files.
>
> Your team's AI skills live in ~/.claude/skills, ~/.agents/skills, ~/.gemini/config/skills… one private copy per person. Nobody knows which version a teammate runs, and an edit that helps one person silently regresses another.
>
> SkillCurrent is one reviewed catalog for those skills. Draft in a safe place. Run checks against the exact draft. A different maintainer approves an immutable, digest-stamped version. Then `status` and `sync` show every copy as current, outdated, modified or missing.
>
> We're onboarding 5 to 10 teams that use two or more AI coding tools. Free during the beta, self-hosted, standard library only, one SQLite file.
>
> Join the beta → [link]

**drift**

> Which version of that skill is your teammate running?
>
> If the answer is "whichever one they copied last," you already know how this ends: the fix that helped one person quietly breaks another, and nobody can say when.
>
> SkillCurrent keeps one reviewed catalog and tells every environment whether its copy is current, outdated, modified or missing. One command brings it back in line.
>
> Beta, free, self-hosted. We want 5 to 10 teams on two or more AI coding tools → [link]

**bytes**

> Approve exact bytes.
>
> Approval that attaches to a moving document isn't approval. In SkillCurrent a different maintainer approves an immutable version with its SHA-256, a channel points at it, and rollback means pointing the channel back. Every environment reports which bytes it holds.
>
> Evidence, not a blanket guarantee: a passing check says something about those bytes, never about how an agent will behave.
>
> Free beta pilot for platform and AI-enablement teams → [link]

## X / Threads (≤ 280 characters each)

- Better methods. Not more files. One reviewed catalog of your team's AI skills, synced across Claude Code, Codex, Antigravity and Osaurus. Free beta pilot, self-hosted → [link]
- Which version of that skill is your teammate running? If you don't know, that's the product. SkillCurrent: status, sync, and an approval that attaches to exact bytes. Beta → [link]
- Approve exact bytes. A different maintainer signs off on a digest-stamped version; rollback points the channel back. Skills for AI coding tools, done like software. Beta → [link]
- "Installed" is not the same as "loaded." SkillCurrent keeps them apart: a copy only counts as loaded when your tool's hook reports it. Beta pilot for teams on 2+ AI coding tools → [link]

## Reddit (r/ClaudeAI; r/ChatGPTCoding's weekly self-promotion thread) — text post, no hype

Read each subreddit's rules on the day you post; several limit
self-promotion to a weekly thread. Post from an account with a normal
comment history, and answer every reply for the first few hours.

**Title:** We built a reviewed catalog for team AI skills (SKILL.md) — looking for 5–10 beta teams

**Body:**

> Most teams we talk to share skills for Claude Code and Codex by copying SKILL.md folders around. It works until it doesn't: someone improves a skill, someone else is still on the old copy, a third person edited theirs by hand, and nobody can say which version caused the bad run.
>
> SkillCurrent is a small self-hosted tool that fixes exactly that and nothing more:
>
> - one catalog; drafts go through checks (structure, no secrets, no machine paths, your own rules)
> - a different maintainer approves; the version is immutable and carries its SHA-256
> - canary and production channels, rollback by pointing the channel back
> - `status` / `sync` show every copy as current, outdated, modified, missing or deprecated
>
> Python 3.11, no dependencies, one SQLite file, CLI + web UI. Free during the beta. We're not claiming it makes agents behave; a passing check is evidence about the bytes, not a guarantee.
>
> If your team uses two or more AI coding tools and wants to try it, the beta form is here: [link]. Happy to answer questions in the thread.

## Hacker News (Show HN)

**Title:** Show HN: SkillCurrent – a reviewed catalog for team AI skills, with digests and rollback

**URL:** the source, not the form: https://github.com/OpsToInnovator/agency-agents/tree/main/examples/skillcurrent
(Show HN is for things people can try. The repo installs with one `pip`
command; the beta form goes in the first comment.)

**First comment (author):**

> Skills (a SKILL.md with front matter) are now the unit every AI coding tool loads, and teams share them by copying files. We wanted the boring software-engineering properties back: review by someone other than the author, immutable versions with a digest, channels with rollback, and a way to see which machines hold which bytes.
>
> It's deliberately small: Python standard library, one SQLite file, a CLI, an HTTP API and a single-file web UI. The checks are transparent text rules, not a model; we say so on every result.
>
> To try it: `pip install "git+https://github.com/OpsToInnovator/agency-agents@main#subdirectory=examples/skillcurrent"`, then follow the walkthrough in the README. We're also running a free beta pilot with 5 to 10 teams on two or more AI coding tools, with direct support: [link].

## Google Search (responsive search ad)

Headlines (≤ 30 chars):
- Team AI Skills, Reviewed
- One Catalog for SKILL.md
- Approve Exact Bytes
- Skills Drift? Sync Them
- Skills for Coding Agents
- Free Beta Pilot
- Self-Hosted, No Deps

Descriptions (≤ 90 chars):
- One reviewed catalog of your team's AI skills. Checks, approval, channels, rollback, sync.
- See which copies are current, outdated, modified or missing. Beta for teams on 2+ AI tools.
- Immutable versions with SHA-256 digests. Approved by someone other than the author.

Keywords to start (exact/phrase): "claude code skills", "codex skills", "SKILL.md", "share skills team claude code", "agent skills catalog".

## Newsletter blurb (sponsorship, ~60 words)

> **SkillCurrent** — one reviewed catalog for your team's AI skills. Draft, check, approve exact bytes, release to canary then production, and see which machines hold which version. Self-hosted, no dependencies, one SQLite file. Free during the beta pilot for teams that use two or more AI coding tools. [Join the beta]

## Cold email to a platform lead (plain text)

Send these one at a time, by hand, to an address the person publishes for
work contact (a company site, a profile that invites email). Do not harvest
addresses from GitHub commits or profiles: GitHub's Acceptable Use Policy
forbids using information from the service to send unsolicited email. Say
who you are and how you found them, and stop at the first "no".

ApexForm Life is an Australian company, so Australia's Spam Act 2003
applies to these emails. Send only where consent can be inferred: the
person published the address for work contact, without saying they don't
want unsolicited mail, and the email relates to their role. Identify
ApexForm Life as the sender, and honour every "no" or unsubscribe
promptly. Have the lawyer confirm this before a cold-email push
(`docs/business/legal-brief.md`).

Subject: which version of your skills is each machine running?

> Hi [name],
>
> Quick one. If your team shares Claude Code / Codex skills by copying SKILL.md folders, you probably can't answer that subject line today. We built a small self-hosted tool that can: one reviewed catalog, checks on the exact draft, approval by a second maintainer with a SHA-256 on the version, canary/production channels with rollback, and `status`/`sync` for every copy.
>
> We're onboarding 5 to 10 teams for a free beta and would trade direct support for two things: whether you keep review switched on, and whether sync ever catches a bad copy.
>
> Worth 20 minutes? The details and the sign-up are here: [link]
>
> [your name], ApexForm Life Pty Ltd, Perth, Western Australia · apexformlife.com
> I found you through [where]. If this isn't relevant, reply "no" or "unsubscribe" and I won't write again.
