# Brief for counsel: SkillCurrent beta and first paid plan

Send this to a startup or marketing lawyer with a request for **one
fixed-fee quote** covering everything below. One bundled review costs less
than four separate ones.

**From:** ApexForm Life Pty Ltd (ABN [[ABN]]), Perth, Western Australia,
apexformlife.com.

> **Fill in before sending:** the ABN, and the countries where you expect
> pilot teams and ad audiences. Use a lawyer who can advise on Australian law
> and, if you'll advertise or sell abroad, on those markets too.

## What SkillCurrent is

- **The product.** SkillCurrent is open-source software (MIT licence), run
  on the customer's own servers. It manages the instruction files ("skills")
  that teams give AI coding tools.
- **Data.** The software sends no data to us.
- **What runs now.** A free beta pilot with 5 to 10 teams, recruited through
  a public sign-up page.
- **What's planned.** After the beta, a paid support plan, flat per
  organisation. `BUSINESS.md` has the model.

## Australian law to cover

ApexForm Life is an Australian company, so ask counsel to cover these
alongside any overseas rules:

- **Australian Consumer Law:** misleading or deceptive conduct, for the ad
  claims and the beta page, and warranties that can't be excluded, for the
  beta terms and the order form.
- **Privacy Act 1988 and the Australian Privacy Principles:** whether they
  apply to ApexForm Life, and if not, whether to follow them anyway, since
  pilot teams will ask.
- **Spam Act 2003:** the cold emails in `marketing/ads/copy.md` and the
  beta emails. They need consent, which may be inferred from a published
  work address, plus sender identification and a working unsubscribe.
- **GST:** how to show prices to Australian and overseas customers, and when
  registration is required. Ask your accountant too.

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
2. SkillCurrent's directory now has its own MIT `LICENSE`, copyright
   ApexForm Life Pty Ltd. It sits inside a repository whose root licence
   names "AgentLand Contributors". Is that enough to make our copyright and
   licence clear, or should SkillCurrent move to its own repository?
3. Is a Developer Certificate of Origin sign-off enough for outside
   contributions, given we sell support for the code?

## Documents to attach

- `docs/beta/terms.md`
- `marketing/ads/copy.md`, `marketing/ads/claims.md`, and a few images from
  `marketing/ads/out/`
- `BUSINESS.md`
- A screenshot of the beta page with its form and privacy line
