# SkillCurrent business model

How SkillCurrent makes money without breaking the promise that sells it:
self-hosted, open source, and "we never see your skills". Figures marked
**hypothesis** are starting points to test with pilot teams, not prices to
publish. Section 5 says how to test them and what to do with each result.

Written 23 September 2026. Revisit after the beta's exit interviews.

**The business:** SkillCurrent is a product of ApexForm Life Pty Ltd, Perth,
Western Australia (apexformlife.com). ApexForm Life holds the copyright,
signs the order forms and issues the invoices.

## 1. The constraint that shapes everything

- **The code is open source (MIT) and runs on the customer's servers.**
  Anyone may use it for free, so the software itself can't be the thing you
  sell.
- **The tool sends nothing back to you.** That is a selling point, and it
  means you can't count seats or usage. Pricing must not depend on
  metering you can't do.

So SkillCurrent sells **assurance around the free tool**: help getting it
right, evidence for auditors, and a named person to call. It does not sell
the tool itself. This is the support-and-assurance model: the code stays
fully open, and customers pay for what the code can't give them on its own.

## 2. Who pays, and why

**Buyer:** a platform, developer-experience or security lead at an
organisation that runs two or more AI coding agents and must show
separation of duties for changes.

**Why they pay:** open-source software is free to run, but it isn't free to
trust. This buyer has to answer questions from auditors and security review:
who approved this, what exactly was approved, and which machines run it. The
tool records those answers. The paid plan makes them easy to hand over, and
puts a person behind them.

**Who doesn't pay:** solo developers and small teams on one agent. They use
the free tool or their vendor's built-in sharing. That is fine. They are
the free tier and a source of word of mouth.

## 3. The offer

| | **Community** | **Supported** | **Enterprise** |
|---|---|---|---|
| Price | Free | **Hypothesis:** $149 a month per organisation, or $1,490 a year | **Hypothesis:** from $6,000 a year, quoted |
| Software | Full tool, MIT licence | The same tool | The same tool |
| Seats | Unlimited | Unlimited, flat per organisation | Unlimited |
| Support | GitHub issues, best effort | Email, reply within one working day | Named contact, reply within one working day, agreed escalation |
| Onboarding | `BETA.md` and docs | One 60-minute install session | Install session plus a migration of existing skills |
| Upgrades | Release notes | Upgrade help and a heads-up before breaking changes | The same, plus a scheduled upgrade call |
| Audit evidence | The commands exist (`skills history`, `activity --json`, `beta-report`) | An evidence pack for audits: who approved what, digests, drift history | The same, plus answers to your security questionnaire |
| Paperwork | Licence only | Invoice, simple order form | Order form, data-processing terms if needed, invoice billing |

**Why flat per organisation:** you can't count seats without telemetry, and
per-seat pricing punishes exactly the adoption you want. SkillReg, one of
the two standalone competitors, already prices this way.

**Why these numbers** (hypotheses): the competitor scan on 23 September
2026 found:

| Competitor | Model | Price found |
|---|---|---|
| SkillReg (hosted) | per organisation | $29 and $99 a month |
| Tessl Team | platform plan | $100 a month |
| Portkey Production | gateway plan | $49 a month |
| Port | per seat | from $30 a seat |
| Corgea | per developer | $39 to $49 a developer |

A self-hosted plan with a human behind it, sold to compliance-minded
buyers, can sit at the top of the per-organisation range. The pilot tests
whether it can.

**Pilot teams:** the Supported plan free for 12 months after it launches.

**Currency and GST.** The figures above are US dollars, because most buyers
and every competitor price found are overseas. ApexForm Life is Australian,
so:

- Australian customers are quoted in Australian dollars, with GST shown
  on the invoice once ApexForm Life is registered for GST.
- Overseas business customers are generally quoted without GST.
- Ask your accountant to confirm both before the first invoice, and whether
  to register for GST now or when turnover requires it. The legal brief
  asks counsel the same.

## 4. What must exist before you charge

Don't sell what isn't built. Before the first invoice:

1. **Evidence pack.** A documented procedure, in a new `docs/evidence.md`,
   that exports approvals, digests, releases and drift for a date range from
   existing commands. A one-command `skillcurrent evidence` export can follow
   once two paying teams have used the manual version.
2. **Support process.** A support mailbox on your domain, a written reply
   target (one working day), and a log of every request. The log is how you
   check the effort assumptions in section 6.
3. **Order form and terms.** A one-page order form: plan, price, term,
   renewal, support scope, and how to cancel. Have the lawyer review it with
   the beta terms (see `docs/business/legal-brief.md`).
4. **Payment.** Card or invoice through a payment provider you trust. Annual
   invoices suit the enterprise buyer and cut your admin.
5. **Your own copyright line.** Done: `LICENSE` in this directory names
   ApexForm Life Pty Ltd (section 8).

Features for larger teams, such as single sign-on, go on the Enterprise
roadmap only when a paying customer asks for them.

## 5. Validating the price before publishing it

The ads say "free during the beta" and nothing more. Keep it that way
until this test is done.

**In every exit interview** (see `docs/beta/call-scripts.md`), ask:

1. "What would you have to see before paying for something like this?"
2. "For the whole team, per month, at what price would it be so expensive
   you wouldn't consider it?"
3. "At what price would it be so cheap you'd doubt it?"
4. "If the Supported plan cost [hypothesis price], would you buy it when
   the beta ends? Yes, no, or 'I'd need to ask [who]'?"

**Decision rules**, once at least five teams have answered:

- **Set the price** within the range where "too cheap" and "too
  expensive" answers overlap. Keep the median "too expensive" figure as the
  ceiling.
- **Go ahead with the Supported plan** if three or more teams say yes, or
  name a buyer to ask, at a price of $99 or more a month.
- **If the median "too expensive" answer is under $99:** support alone
  won't carry the business. Test the alternative in section 9.
- **If two or more teams won't run a server:** the demand is for a hosted
  version. That is a different business with different costs (section 9).

Record every answer in the tracking sheet, next to the team's beta
verdicts. A team that kept review on and saw sync catch a bad copy is the
strongest price signal you'll get.

## 6. Unit economics (illustrative)

Your time is the main cost; the customer runs the software. The hosting
cost is one small server for the beta page and waitlist.

**Assumptions to check against the support log:**

- A Supported organisation needs about 1.5 hours of your time a month.
- The install session is 1 hour, once.
- An Enterprise organisation needs about 4 hours a month.

**At the hypothesis prices:**

| | Supported | Enterprise |
|---|---|---|
| Revenue per organisation, per month | $149 | $500 ($6,000 a year) |
| Your hours per organisation, per month | 1.5 | 4 |
| Revenue per hour of your time, before payment fees | about $99 | about $125 |

**What one person can carry:** about 40 Supported organisations at 1.5
hours each is 60 hours a month. That is $5,960 a month, or about $71,500 a
year, before fees and tax. Three Enterprise customers at $6,000 add
$18,000 a year for about 12 hours a month.

**Two numbers decide whether it works.** Watch them from the first paying
customer:

- **Hours per organisation.** If the log shows well over 1.5, raise the
  price or narrow what support covers.
- **Renewals.** Annual renewal is the business. Aim for at least 80% of
  Supported organisations renewing. Below that, the tool isn't sticky
  enough to charge for.

## 7. Go-to-market, in order

1. **Beta pilot**, free (running now; see `LAUNCH.md`). Converts to
   Supported with 12 months free.
2. **Organic channels:** warm network, LinkedIn, communities, open feature
   threads, Show HN. The ad kit in `marketing/ads/` is ready.
3. **Paid search on skills terms**, only after three teams have installed,
   the lawyer has reviewed the copy, and you know which message brings
   qualified sign-ups.
4. **Enterprise:** direct conversations with security and platform leads
   the pilot introduces you to. Ask every pilot team: "Who else in your
   company should see this?"

### Campaigns by plan and stage

It's a timeline first, then an audience split. The three plans are the same
software under one brand, so they never get separate products, sites or ad
accounts. What differs is who you talk to, where, and what you ask them to
do.

**Until the week 8 decision: one campaign.** Every ad, post and email
recruits pilot teams and sends them to the beta page. Show no plans and no
prices. The ads say "free during the beta" and nothing more (section 5).
Running plan-specific ads now would advertise a price you haven't tested,
and would split a small audience before you know which message works.

**After the price is validated: three tracks.**

| | **Community** | **Supported** | **Enterprise** |
|---|---|---|---|
| Who | Developers and small teams trying skills | Platform and developer-experience leads at teams on two or more agents | Security, compliance and platform leads at larger organisations |
| Where | GitHub, Hacker News, Reddit, Discord and the open feature threads; no paid spend | LinkedIn, paid search on skills terms, the warm network | Direct outreach, introductions from pilot and Supported teams, security meet-ups |
| Message | How it works: the method, drift and bytes concepts | The hook: refused, early, noticed, yours | Audit evidence and separation of duties: refused, noticed, the evidence pack |
| Ask | Star or install it from the README | Start the Supported plan, or book an install session | Talk to us |
| Tag | `utm_campaign=community` | `utm_campaign=supported` | `utm_campaign=enterprise` |
| Success measure | Installs, issues and stars; people who move up to Supported | Paying organisations; cost per paying organisation | Qualified conversations; signed annual contracts |

**How the tracks connect.** Community is the top of the funnel and costs
nothing but your time. Supported is where paid spend goes. Enterprise is
mostly sales conversations, and the ads only have to earn the first meeting.
Each track's ask leads to the next one up: once the plan exists, the README
should mention the Supported plan, and the pricing page should offer "talk
to us" for larger teams.

**When a track starts:**

- **Community:** at the week 8 decision, whatever it is. The tool stays
  free and open either way.
- **Supported:** when section 4's list is done and the price is published.
  Start with organic posts and LinkedIn. Add paid search only once three
  organisations have paid, so you know what a customer is worth before
  buying clicks.
- **Enterprise:** after the first two Supported customers, so there's a
  reference and a tested evidence pack to show.

**What changes on the page.** Add one pricing page with three columns.
Community ads link to the README, Supported ads to the pricing page, and
Enterprise emails to a "talk to us" address. Keep the tag on every link:
the beta form already stores `utm_campaign`, and `skillcurrent beta
--sources` counts sign-ups by tag.

**What never changes between tracks:** the claims. Every track draws on
`marketing/ads/claims.md`, and every claim keeps the limits written there,
whoever the audience.

## 8. Licence and ownership

- **Keep the core under MIT.** Versions already published under MIT stay
  MIT for anyone who has them. Changing the licence later only affects new
  versions, and would undercut the "open source, on your server" pitch. The
  business doesn't need it: it sells support, not access.
- **Your own copyright line is in place.** `examples/skillcurrent/LICENSE`
  is the MIT licence, copyright ApexForm Life Pty Ltd, and
  `pyproject.toml` names ApexForm Life as the author. The repository's root
  licence still names "AgentLand Contributors" for everything else in it.
- **Ask contributors to sign off their commits** with a Developer
  Certificate of Origin line. It records that they had the right to
  contribute, which keeps ownership clear if you sell support for their
  code. Ask the lawyer whether you need more than that.
- **Consider its own repository** before Show HN. A dedicated repository
  gives the product, its issues and its releases one home.

## 9. If the model doesn't hold

Decide by the end of the beta's week 8, using the exit criteria in
`LAUNCH.md`:

- **Teams want it but won't run a server:** build a hosted version. Price
  it per organisation against SkillReg and localskills.sh. This means
  running customer data, which brings new security and privacy work.
- **Teams use it but won't pay for support:** keep the tool free, and sell
  fixed-price services around it: installs, migrations, and skill-library
  reviews for AI-enablement teams. Price each engagement from the hours in
  your support log.
- **Teams don't keep using it:** stop, and write down what the beta
  showed. The tool stays open source for anyone who wants it.

## 10. Risks

| Risk | What it looks like | What to do |
|---|---|---|
| Vendors build it in | Anthropic's organisation library already blocks self-approval on Claude surfaces, and becomes the default on 2 October 2026 | Stay the tool for teams on several agents, and for self-hosted and regulated teams. Recheck the vendors every month. |
| Support eats your time | The log shows well over 1.5 hours per organisation | Raise the price, narrow the scope, or write the missing docs. |
| Nobody renews | Renewals under 80% | Find out why in a call before the renewal date, not after. |
| One-person business | You are the support team | Keep the reply target honest. Don't sell an SLA you can't staff. |
| Claims outrun the product | Ads or sales copy promise more than the code does | Keep `marketing/ads/claims.md` current and have counsel review it. |

## 11. The next 90 days

| When | What | Done when |
|---|---|---|
| Now | ABN and contact address in the beta terms; lawyer engaged (see `docs/business/legal-brief.md`) | Fixed-fee quote accepted |
| Weeks 1 to 4 | Recruit and install pilot teams (`LAUNCH.md`) | 5 teams installed |
| Weeks 4 to 8 | Weekly reports, day-14 calls, exit interviews with the price questions | 5 teams answered section 5 |
| Week 8 | Decide: Supported plan, hosted version, services, or stop | Decision written down with the evidence |
| Weeks 8 to 12 | Build the evidence pack procedure, order form, support mailbox; publish the Supported price | First paying organisation, or the alternative from section 9 under way |
