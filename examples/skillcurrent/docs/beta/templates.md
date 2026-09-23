# Beta email and post templates

Plain text. Replace every `[...]`. Send from the beta mailbox, and keep
each team's messages in one thread; weekly reports come back on it. The
beta page promises a reply within two working days, so answer every
sign-up inside that, even when the answer is "not yet".

## Emails

### 1. Reply to a new sign-up

> Subject: Your SkillCurrent beta sign-up
>
> Hi [first name],
>
> Thanks for signing up. I'm [your name], and I built SkillCurrent.
>
> Before anything else, I'd like to hear how your team handles skills
> today. Could we talk for 20 minutes? Here are some times: [two or three
> slots, with time zone]. Or pick one here: [booking link].
>
> If a call doesn't suit you, three quick answers by reply work too:
> 1. Which AI coding tools does your team use, and how many people write skills?
> 2. How do skills get from one person to another today?
> 3. When did a copy of a skill last cause trouble?
>
> [your name]

### 2. Not a fit right now

> Subject: Re: Your SkillCurrent beta sign-up
>
> Hi [first name],
>
> Thanks for the time today. I don't think the beta is the right fit yet,
> because [the one reason: e.g. only one person writes skills today]. The
> beta only measures things that need [that condition], so it would cost
> you time without telling either of us much.
>
> The tool is open source if you'd like to try it on your own:
> [repository link]. I'll keep your sign-up unless you'd rather I deleted
> it. Just say so.
>
> [your name]

### 3. Invitation to the pilot

> Subject: SkillCurrent beta: install session and what to prepare
>
> Hi [first name],
>
> Great to talk. You're in. Here's what happens next.
>
> Install session: [date and time, time zone], 60 minutes, with [server
> person] and your two maintainers: [names]. Invite: [calendar link].
>
> Before then, please read the team guide, especially "Before the install
> session" (15 minutes): [link to BETA.md]
>
> The beta terms are short: [link to terms]. Reply "I agree" if they work
> for you, or tell me what doesn't.
>
> [your name]

### 4. Day before the install session

> Subject: Tomorrow's SkillCurrent install session
>
> Hi [first name],
>
> Quick check for tomorrow at [time]:
> - Python 3.11 or newer and pipx on the server and on each laptop
> - the server machine is chosen, and TLS is ready if members reach it
>   over the internet
> - both maintainers can join
> - a backup of ~/.claude/skills (and any other skills folders)
>
> If any of these is a problem, reply and we'll sort it out before we start.
>
> [your name]

### 5. Weekly report reminder (Monday)

> Subject: SkillCurrent beta: this week's report
>
> Hi [first name],
>
> When you have two minutes, please run this on the server (or with your
> maintainer token) and attach the file to a reply:
>
>     skillcurrent --json beta-report --days 7 > beta-report.json
>
> It holds counts only. Have a look before you send it. And tell me
> anything that got in your way this week, even small things.
>
> [your name]

### 6. The report says "never checked"

> Subject: Re: SkillCurrent beta: this week's report
>
> Hi [first name],
>
> Thanks for the report. The sync question says "never checked", which
> usually means the hook isn't running on anyone's machine. Could one
> person run this and send me the last lines?
>
>     ~/.skillcurrent/hook.sh; tail -n 20 ~/.skillcurrent/sync.log
>
> If the log is empty or shows an error, a 10-minute call will fix it:
> [slots].
>
> [your name]

### 7. Day-14 check-in

> Subject: SkillCurrent beta: two weeks in
>
> Hi [first name],
>
> We're two weeks in. Could we take 20 minutes this week to hear how it's
> going, what's annoying, and whether it's still worth your time? [slots]
>
> [your name]

### 8. Wrap-up

> Subject: SkillCurrent beta: thank you
>
> Hi [first name],
>
> Thank you for the last [n] weeks. Here's what your reports showed:
> [one or two sentences, their numbers only].
>
> SkillCurrent stays on your machines and keeps working, and its source
> stays open. If we introduce a paid plan, you'll hear first, and your team
> gets it free for 12 months after it launches.
>
> I'll keep your sign-up, reports and our notes until [date], as the terms
> say. If you'd like them deleted now, reply "delete" and I'll confirm
> when it's done.
>
> [your name]

### 9. The pilot is full

> Subject: Re: Your SkillCurrent beta sign-up
>
> Hi [first name],
>
> Thanks for signing up. The current pilot group is full. I've kept your
> entry and will write when the next group starts, or sooner if a place
> opens. If you'd rather not wait, the tool is open source: [repository
> link].
>
> [your name]

### 10. Deletion confirmed

Run `skillcurrent beta remove <email>` on the waitlist host, delete any
reports and notes, then:

> Subject: Re: [their subject]
>
> Hi [first name],
>
> Done. I've deleted your sign-up[, the reports you sent and my notes].
> Backups that held it roll off within 14 days.
>
> [your name]

## Community posts

Say you built the tool, every time. Post only where it answers what the
thread or channel is about, and read the channel's rules first.

### Ask an upstream maintainer before posting in their Discussions

> Hi [name], I built SkillCurrent, a small self-hosted catalog for team
> skills (review, versions with digests, sync across Claude Code, Codex,
> Antigravity and Osaurus). It lives under examples/ in
> OpsToInnovator/agency-agents, which builds on this repository. Would a
> post in Discussions asking for beta teams be welcome, or would you rather
> I didn't? Either answer is fine.

### Comment on an open feature request about team skill sharing

Only where the thread asks for team-level sharing, versioning or sync.
Don't comment on closed threads.

> Until something like this ships natively, I've been building a
> self-hosted stopgap: a catalog where a second maintainer approves an
> exact version (with a SHA-256), releases go to canary and then
> production, and `sync` keeps each machine's copy in line across Claude
> Code, Codex, Antigravity and Osaurus. Disclosure: I built it. It's open
> source: [repository link]. It doesn't change how [product] itself shares
> skills.

### Discord (read the channel's self-promotion rules first)

> I built a small self-hosted catalog for team skills: review by someone
> other than the author, immutable versions with digests, and `sync` that
> reports hand-edited or missing copies. I'm looking for 5 to 10 teams on
> two or more AI coding tools to try it for a few weeks. Source: [repository
> link]. Beta: [beta page link]. Happy to answer questions here.
