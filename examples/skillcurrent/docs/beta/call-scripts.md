# Beta call scripts

Four conversations per pilot team: a qualification call, the install
session, a day-14 check-in and an exit interview. Weekly reports arrive by
email in between. Ask the same questions of every team and write the
answers down in the same shape, so the teams can be compared at the end.

Say who you are at the start of every call, and say that you built the tool.
Don't promise features, dates or prices.

## 1. Qualification call (20 to 30 minutes)

**Goal:** decide together whether the team is a fit, and book the install
session if it is.

**A team is a fit when all of these hold:**

- It uses two or more AI coding tools that read skills or agent
  instructions.
- More than one person writes or edits those skills today.
- It can run a small Python 3.11 service that every member can reach.
- It has two people who can act as maintainers, since approval needs a
  second person.
- Someone will send the weekly report for four to six weeks.

**Script**

1. *Open (2 min).* "Thanks for signing up. I built SkillCurrent and I'm
   running this beta myself. I'd like to hear how your team handles skills
   today, then tell you what the beta involves, and we can decide together
   whether it's worth your time."
2. *How they work today (10 min).* Ask each one and write the answer down:
   - "Which AI coding tools does the team use, and roughly how many people?"
   - "Where do your skills or agent instructions live today, and how do
     they get from one person to another?"
   - "Tell me about the last time a copy of a skill caused trouble. What
     happened, and how did you find out?"
   - "Who can change a skill today? Does anyone review the change?"
   - "If you could fix one thing about this, what would it be?"
3. *Fit check (3 min).* Go through the five conditions above. If one fails,
   say so plainly and use the "not a fit right now" email afterwards.
4. *What the beta involves (5 min).*
   - "You run it on your own machines; we never see your skills."
   - "We're watching two things: whether you keep review switched on, and
     whether sync ever catches a bad copy, meaning a hand-edited or deleted
     one."
   - "Once a week a maintainer emails us a report of counts. No content, no
     names. You can read it before you send it."
   - "It's rough in places. You get direct help from me for four to six
     weeks. Either of us can stop at any time."
5. *Book the install session (3 min).* Find a 60-minute slot with the
   person who will run the server and both maintainers. Send the invitation
   email with `BETA.md` and the terms.
6. *Close.* "What would make this a waste of your time?" Write the answer
   down; it is your first exit criterion for this team.

**Notes to keep** (one row per team): tools, team size, how skills move
today, last incident, review today, their one fix, fit (yes or no, and
why), install date.

## 2. Install session (60 minutes, screen share)

**Goal:** by the end, the team has a released skill installed on at least
two machines in two tools, and the hook is writing to `sync.log`.

Follow `BETA.md` step by step. Checkpoints:

| Minute | Step | Done when |
|---|---|---|
| 0 to 5 | Agenda, who does what | the server person is sharing their screen |
| 5 to 15 | Server: install, `init`, `serve` behind TLS | `curl https://<their host>/api/health` returns `"ok": true` |
| 15 to 25 | Members: `members add`, tokens sent privately, `~/.skillcurrent/env` | `skillcurrent whoami` works for every member present |
| 25 to 40 | One real skill: `import`, `checks`, `submit`, approval by the second maintainer | `skills approve ... --release production` prints a SHA-256 |
| 40 to 50 | Install on two machines, two tools | `skillcurrent status` shows `current` on both |
| 50 to 57 | The hook, on each machine | `tail ~/.skillcurrent/sync.log` shows a run |
| 57 to 60 | First report | `skillcurrent beta-report` says anything but "never checked" for sync |

**Things that go wrong, and what to do**

- *Python older than 3.11.* Install 3.11 from python.org or Homebrew, then
  run the `pipx install` line from `BETA.md` again with `--force --python
  python3.11` added.
- *`skillcurrent` not found by the hook.* The `SKILLCURRENT_BIN` line in
  `~/.skillcurrent/env` fixes it. Check that it holds a full path.
- *A tool doesn't pick up the skill.* Check its skills folder in the tool's
  current documentation. If it differs from `skillcurrent targets`, use
  `--target custom --dir` and write it down; it's a bug for us to fix.
- *Checks fail on an imported skill.* That's expected. Fix one skill
  together and leave the rest as homework.

**Close:** agree the day the first weekly report will arrive, and who sends
it.

## 3. Weekly report (email, about 10 minutes of your time)

When a report arrives:

1. Record both verdicts and the counts in your tracking sheet.
2. If sync says **never checked**, send the "never checked" email the same
   day. The team's data is useless for question two until the hook runs.
3. If review says **partly**, read the reasons. A draft install or
   `--no-auth` is worth a short question; a rubber stamp is worth a
   conversation at day 14.
4. If a week's report doesn't arrive by Wednesday, send one reminder.

## 4. Day-14 check-in (20 minutes)

1. "What have you used it for since we set it up?"
2. "Has review stopped anything, or slowed anything down?"
3. "Has sync repaired or flagged a copy? Did anyone notice?"
4. "What's the most annoying thing about it?"
5. "Are you still up for the rest of the beta?" If not, thank them and run
   the exit interview now.

## 5. Exit interview (30 minutes)

1. "Would you keep using it if the beta ended today? Why?"
2. "What would make you stop using it?"
3. "Did review catch anything real? Tell me about it."
4. "Did sync matter? Would you have noticed the bad copy without it?"
5. "Who else on your team or in your company should have been involved?"
6. "What would you have to see before paying for something like this?"
   Listen; don't quote a price yet.
7. "For the whole team, per month, at what price would it be so expensive
   you wouldn't consider it?" Then: "At what price would it be so cheap
   you'd doubt it?"
8. "If a supported plan cost [the hypothesis price in `BUSINESS.md`], would
   you buy it when the beta ends? Yes, no, or 'I'd need to ask someone'?"
   If they name someone, write down who. Record all three answers in the
   tracking sheet; `BUSINESS.md` section 5 says how to use them.
9. "May I describe your results publicly? Anonymously only, or with your
   name?" Get any yes in writing, by email, and keep it.
10. "Who else in your company should see this?"

Afterwards, send the wrap-up email and offer to delete their data.
