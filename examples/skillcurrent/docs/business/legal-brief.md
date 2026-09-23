# Brief for counsel: SkillCurrent beta and first paid plan

Send this to a startup or marketing lawyer with a request for **one
fixed-fee quote** covering everything below. One bundled review costs less
than four separate ones.

> **Fill in before sending:** [[your name]], [[company name and
> registration, or "an individual"]], [[country, and state if relevant]],
> [[countries where you expect pilot teams and ad audiences]].

## What SkillCurrent is

- **The product.** SkillCurrent is open-source software (MIT licence), run
  on the customer's own servers. It manages the instruction files ("skills")
  that teams give AI coding tools.
- **Data.** The software sends no data to us.
- **What runs now.** A free beta pilot with 5 to 10 teams, recruited through
  a public sign-up page.
- **What's planned.** After the beta, a paid support plan, flat per
  organisation. `BUSINESS.md` has the model.

## What to review, in priority order

### 1. Before the first pilot team signs: beta terms and privacy

- **Beta terms:** `docs/beta/terms.md`. Teams accept by replying "I
  agree" to an email.
- **The sign-up page.** It collects work email, team size, tools used, a
  free-text note, and which link brought the visitor.
- **The page's privacy line** is generated to match how the form submits. We
  keep no cookies or analytics, and hold the visitor's network address in
  memory only.

Questions:

1. Is accepting by email reply enough for a free pilot between businesses?
2. Do the terms and the privacy line cover the data protection law where the
   visitors and pilot teams are (for example the UK or EU), including the
   lawful basis, retention and deletion requests?
3. Do we need a separate privacy notice page, and what must it say?
4. Is the warranty disclaimer ("as is") adequate for a pilot?

### 2. Before any paid advertising: ad copy

- **Ad copy:** `marketing/ads/copy.md`.
- **Creatives:** the images in `marketing/ads/out/`.
- **Evidence file:** `marketing/ads/claims.md`. It lists every claim, the
  evidence in the product, and the limits the wording respects.

Questions:

1. Is the evidence in `claims.md` enough to substantiate each claim before
   the ads run (FTC; UK CAP Code; wherever else we advertise)?
2. The copy avoids comparisons and words like "only". Is anything still an
   implied comparison?
3. The creatives and social posts name the AI tools SkillCurrent installs
   into (Claude Code, Codex, Cursor, Gemini, Antigravity) as compatibility
   facts. Is that acceptable use of those marks? Google ad text leaves the
   names out.
4. "Free during the beta": does anything more need saying, given that a paid
   plan may follow?

### 3. Before the first invoice: the paid plan

- **Offer:** a Supported plan, flat per organisation, monthly or annual.
  It includes email support with a one-working-day reply target, an install
  session, and an audit evidence pack (see `BUSINESS.md` section 3).
- **Enterprise:** annual contracts, possibly with data-processing terms.

Questions:

1. What should a one-page order form contain (term, renewal, cancellation,
   support scope, liability cap)?
2. Is a reply-time target safe to state without a formal SLA?
3. Do we need data-processing terms, given that the software runs on the
   customer's servers and we see no customer data? What changes if a
   customer sends us logs or exports for support?
4. What changes if we later offer a hosted version?

### 4. Names and ownership

1. Is "SkillCurrent" clear to use as a product name in our markets?
2. The code sits in a repository whose only licence file names "AgentLand
   Contributors". What must we add so our own copyright and licence are
   clear for SkillCurrent's directory?
3. Is a Developer Certificate of Origin sign-off enough for outside
   contributions, given we sell support for the code?

## Documents to attach

- `docs/beta/terms.md`
- `marketing/ads/copy.md`, `marketing/ads/claims.md`, and a few images from
  `marketing/ads/out/`
- `BUSINESS.md`
- A screenshot of the beta page with its form and privacy line
