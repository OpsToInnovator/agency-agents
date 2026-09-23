# Claims register: hook campaign

Every claim the hook campaign makes, what backs it in the product, and the
limits the wording must respect. Keep this file current: advertising rules
(the FTC's substantiation policy, the UK CAP Code) expect you to hold the
evidence *before* an ad runs. This is not legal advice; have counsel review
final copy.

Market context was checked on **23 September 2026**. Re-check it before any
comparison, and at least monthly while ads run.

## The claims

| Claim as worded | What backs it | Limits the wording must respect |
|---|---|---|
| **Whoever wrote or edited a version can't approve it** (the ads: "It won't let you approve your own work"). | `Service.approve` refuses the submitter, and anyone who created or edited the draft since the last approval, discard or deletion (`Store.draft_authors`). The web UI hides Approve from them. Tests: `test_nobody_who_wrote_or_edited_the_draft_can_approve_it` covers editors and creators, `test_deleting_a_skill_resets_who_counts_as_its_author`, and the `refused` blocks in `tests/test_ads.py` cover the submitter case. | It works per account, not per person. An owner adds members and receives their tokens, so one person holding two accounts could pass it. Identity is only enforced when the team works through `serve` with tokens: with a local database file, or `--no-auth`, anyone with access can act as any member. Never say "two people, guaranteed". |
| **A second maintainer approves the exact bytes, stamped with a SHA-256.** Also "Every Version Hash-Stamped". | Approval stores the SHA-256 of the stamped content for every approved version. `skills history` and the approve output show it. | SkillCurrent manages the SKILL.md file only. Multi-file skills (scripts, reference files) aren't supported, so "exact bytes" means the SKILL.md. Approval gates what a release channel serves. A member can still put an unreviewed draft on a machine with `install --ref draft`; `beta-report` counts those installs. Never say "nothing reaches a machine without approval". |
| **Early access first; everyone else when you say so.** | `skills release --channel canary`, `install --channel canary`, and the `skills history` line "canary = …, production = …". Tested in the `early` blocks. | The rollout is human-judged, with no traffic split, automatic metrics or automatic rollback. Canary is a choice for each release: `approve --release production` skips it. A machine joins early access by installing with `--channel canary`. Rolling back means moving the pointer back, and machines follow at their next sync. |
| **Machines running the hook report hand-edited and deleted copies; sync repairs missing and outdated ones and keeps edits.** | `Installer.status` and `sync` report `drift.modified`, `drift.missing` and `drift.outdated`. Sync repairs at the recorded path and keeps modified copies unless `--force`. `beta-report` counts it all. Tested in `test_beta_measurement.py` and the `noticed` blocks. | It covers only copies SkillCurrent installed, and only where the hook actually runs. Machines without the hook, and cloud agents, are not covered. A copy with nothing to restore from (a draft install whose channel has no release) is skipped and reported, and sync carries on with the rest (`test_one_unrepairable_copy_does_not_stop_sync`). Never write "every machine". |
| **Skills only. On your server. No gateway, portal or security suite to adopt first.** | A standalone package: Python 3.11, standard library only, one SQLite file. `deploy/` recipes for your own host. | It needs a server, two maintainers and the hook on each machine, so say so to the audience. It is free during the beta; post-beta pricing isn't decided, so don't imply "free forever". |
| **We never see your skills.** | The tool sends nothing to us. `beta-report` holds counts only, and teams email it themselves. | The public waitlist host stores sign-up details (email, team size, tools, note, link tag), and its privacy line says so. |
| **Every terminal line here is real output.** | `tests/test_ads.py` builds each scenario, runs each shown command and checks each shown line against a whole printed line, in order, plus the exit code. Mutation checks confirmed it fails on a changed line, a silently shortened line, a word lifted from other output, a skipped block and a failing command. | A prompt like `sam$` means the command ran as that member (`--as sam`). Some rows are omitted. A shortened line ends in "…", and a long line wraps onto an indented continuation line. |

## Why competitors don't bundle these

Each mechanic exists somewhere; the combination is what's uncommon.

- **Author can't approve their own.**
  - Anthropic's organisation library now blocks it on Claude surfaces ("You can't approve your own"). It becomes the default for organisations that haven't chosen a setting on 2 October 2026. Owners can still upload directly without review.
  - The open-source skm enforces it by default.
  - Most registries leave review to Git, or treat an automated scan as the second pair of eyes.
- **Early-access channels.**
  - Open-source Nacos has hand-moved labels and a gray stage.
  - Claude Code can run stable and latest marketplaces on different git refs.
  - Alibaba's launch blog claims automatic gray release. Its product docs don't show it.
- **Reporting drift on developer machines.**
  - StepSecurity (commercial) compares content hashes across a fleet of devices.
  - HappySkills and surenode detect local changes.
  - Claude Code's sync of organisation skills overwrites local edits instead of reporting them.
- **Self-hosting.** Tessl offers it on Enterprise only. TrueFoundry, Speakeasy and Nacos also offer it, inside larger platforms.

Likely reasons, all inference:

- The category is months old.
- Each mechanic adds setup work (a server, two maintainers, a hook on every machine), which clashes with "one install command" pitches.
- Security vendors sell automated scanning as the reviewer.
- Vendor-native sync solves drift by overwriting it.

That is why the campaign targets multi-agent teams that need separation of duties and self-hosting, not solo developers.

## Product follow-ups the research surfaced

- Hash and install every file in a skill folder, not just SKILL.md. The MCP skills extension, HappySkills and surenode already do.
- Offer a logged single-maintainer mode for evaluations, so solo trials aren't blocked. Keep production behind a second maintainer.
- Flag in `beta-report` when the approver's account was created by the author.
- Let a project pin a version that sync respects.

## Sources

- Anthropic, organisation skills: https://support.claude.com/en/articles/13119606-provision-and-manage-skills-for-your-organization
- Claude Code, skills synced from claude.ai: https://code.claude.com/docs/en/skills
- skm (author cannot approve by default): https://github.com/DownByBracket/skm
- Nacos skill registry: https://nacos.io/en/docs/latest/manual/user/ai/skill-registry/ ; skill-sync: https://github.com/nacos-group/nacos-cli/blob/main/docs/skill-sync-user-guide.md
- Alibaba MSE launch blog: https://www.alibabacloud.com/blog/skills-registry-public-beta-launches-building-a-private-skill-management-center-for-enterprises_603091
- StepSecurity, agent skills drift: https://docs.stepsecurity.io/developer-machines/ide-and-ai-agents/agent-skills
- HappySkills lock file: https://happyskills.ai/blog/the-lock-file-for-ai-agent-skills/
- surenode skill-discovery: https://github.com/surenode-ai/skill-discovery
- JFrog skills registry: https://docs.jfrog.com/ai-ml/docs/skills-registry
- FTC advertising substantiation: https://www.ftc.gov/legal-library/browse/ftc-policy-statement-regarding-advertising-substantiation
- UK ASA, comparisons and verifiability: https://www.asa.org.uk/advice-online/comparisons-verifiability.html ; the "Only on Uber" ruling: https://www.asa.org.uk/rulings/uber-bv-g21-1115224-uber-bv-only-on-uber-1.html
- Google Ads trademark policy: https://support.google.com/adspolicy/answer/6118?hl=en
