"""The ad creatives show terminal output. Every line they show must be real.

Blocks in marketing/ads/creatives.html marked ``data-real="<scenario>"`` hold
``$ skillcurrent ...`` commands and the output lines the ad prints under each.
This test builds the named scenario in a sandbox (the setup the ad does not
show), runs each command as printed, and checks every shown line appears in
what the command actually printed. A line ending in "…" is a shortened line:
only the part before it is checked.
"""

import html
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CREATIVES = ROOT / "marketing" / "ads" / "creatives.html"
FIXTURES = ROOT / "tests" / "fixtures"


def _team(run):
    run("init --team northstar --owner maya")
    run("members add noah --role maintainer")
    run("members add sam --role maintainer")


def _submitted_v1(run, home):
    _team(run)
    run("--as sam skills new ./refund-handoff/SKILL.md")
    run("--as sam skills checks refund-handoff")
    run('--as sam skills submit refund-handoff --note "baseline"')


def _released_v1(run, home):
    _submitted_v1(run, home)
    run("--as noah skills approve refund-handoff --release production")
    run("install refund-handoff")
    run("install refund-handoff --target codex")


def _submitted_v2(run, home):
    _released_v1(run, home)
    run("--as sam skills edit refund-handoff ./refund-handoff-v2/SKILL.md")
    run("--as sam skills checks refund-handoff")
    run('--as sam skills submit refund-handoff --note "confirm order number first"')


def _drifted(run, home):
    """Someone hand-edits the Claude Code copy; the Codex copy is deleted."""
    _released_v1(run, home)
    claude = home / ".claude" / "skills" / "refund-handoff" / "SKILL.md"
    claude.write_text(claude.read_text() + "\nmy local tweak\n")
    (home / ".agents" / "skills" / "refund-handoff" / "SKILL.md").unlink()


SCENARIOS = {
    "fresh": lambda run, home: None,
    "submitted-v1": _submitted_v1,
    "released-v1": _released_v1,
    "submitted-v2": _submitted_v2,
    "drifted": _drifted,
}


PROMPT = re.compile(r"^(?:(?P<who>[a-z0-9-]+))?\$ (?P<cmd>skillcurrent\b.*)$")
REAL_FOOTER = "Every terminal line here is real output"


def _steps(board: str, raw: str) -> list[tuple[str | None, str, list[str]]]:
    """(member, command, shown lines) from one terminal block.

    "sam$ skillcurrent ..." runs as member sam; "$ skillcurrent ..." as the scenario's default user.
    An indented line continues the output line above it (a deliberate wrap for the ad's width).
    """
    steps: list[tuple[str | None, str, list[str]]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = PROMPT.match(line.strip())
        if m:
            steps.append((m.group("who"), m.group("cmd"), []))
            continue
        assert steps, f"{board}: output before any command: {line!r}"
        shown = steps[-1][2]
        if line[:1].isspace():
            assert shown, f"{board}: a continuation line with nothing to continue: {line!r}"
            shown[-1] = shown[-1] + " " + line.strip()
        else:
            shown.append(line.strip())
    return steps


def real_blocks() -> list[tuple[str, str, list[tuple[str | None, str, list[str]]]]]:
    """(board id, scenario, steps) for every data-real block in the creatives."""
    page = CREATIVES.read_text(encoding="utf-8")
    blocks = []
    for board in re.finditer(r'<div class="board[^"]*"[^>]*id="([^"]+)"(.*?)(?=<div class="board|<script>)', page, re.S):
        for m in re.finditer(r'<(?:pre|div)[^>]*data-real="([^"]+)"[^>]*>(.*?)</(?:pre|div)>\s*<!--/real-->', board.group(2), re.S):
            text = re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", m.group(2)))
            blocks.append((board.group(1), m.group(1), _steps(board.group(1), html.unescape(text))))
    return blocks


def test_creatives_have_real_blocks():
    page = CREATIVES.read_text(encoding="utf-8")
    blocks = real_blocks()
    assert blocks, "no data-real blocks found in creatives.html"
    # A block the parser skipped (a missing /real marker, single quotes, attribute order) would go unchecked.
    markup = re.sub(r"<!--.*?-->", "", page, flags=re.S)  # the header comment describes data-real; don't count it
    assert len(blocks) == len(re.findall(r"data-real=[\"']", markup)), "a data-real block was not parsed"
    parsed_boards = {b for b, _, _ in blocks}
    for board in re.finditer(r'<div class="board[^"]*"[^>]*id="([^"]+)"(.*?)(?=<div class="board|<script>)', page, re.S):
        if REAL_FOOTER in board.group(2):
            assert board.group(1) in parsed_boards, f"{board.group(1)} says its terminal output is real but has no checked block"
    for board, scenario, steps in blocks:
        assert scenario in SCENARIOS, f"{board}: unknown scenario {scenario!r}"
        assert steps, f"{board}: empty real block"
        for _, command, shown in steps:
            assert shown, f"{board}: {command!r} shows no output, so nothing about it is checked"


def _norm(line: str) -> str:
    return " ".join(line.split())


@pytest.mark.parametrize("board,scenario,steps", real_blocks() or [("none", "none", [])], ids=lambda v: v if isinstance(v, str) else None)
def test_ad_terminal_lines_are_real_output(board, scenario, steps, tmp_path):
    if board == "none":
        pytest.skip("no data-real blocks")
    for name in ("refund-handoff", "refund-handoff-v2"):
        shutil.copytree(FIXTURES / name, tmp_path / name)
    home = tmp_path / "home"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "SKILLCURRENT_DB": str(tmp_path / "db.sqlite"),
           "SKILLCURRENT_HOME": str(home), "SKILLCURRENT_HOST": "laptop", "COLUMNS": "200",
           "SKILLCURRENT_TEAM": "northstar", "SKILLCURRENT_USER": "maya"}

    def run(command: str, check: bool = True, who: str | None = None) -> tuple[int, list[str]]:
        argv = shlex.split(command)
        if argv and argv[0] == "skillcurrent":
            argv = argv[1:]
        if who:
            argv = ["--as", who, *argv]
        done = subprocess.run([sys.executable, "-m", "skillcurrent", *argv], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        if check:
            assert done.returncode == 0, f"setup {command!r} failed: {done.stderr}"
        return done.returncode, [_norm(l) for l in (done.stdout + done.stderr).splitlines() if l.strip()]

    SCENARIOS[scenario](lambda c: run(c), home)
    for who, command, shown in steps:
        rc, printed = run(command, check=False, who=who)
        # The exit code must fit what the ad shows: an error line means the command failed, and
        # `status` exits 2 when a copy needs attention. Anything else must succeed.
        allowed = {1} if any(s.startswith("error (") for s in shown) else {0}
        if shlex.split(command)[1:2] == ["status"]:
            allowed.add(2)
        assert rc in allowed, f"{board}: {command!r} exited {rc}; printed {printed!r}"
        at = 0
        for line in shown:
            want = _norm(line)
            if want.endswith("…"):  # shortened on purpose: must be the start of a real line
                prefix = want[:-1].rstrip()
                hit = next((i for i in range(at, len(printed)) if printed[i].startswith(prefix)), None)
            else:  # shown in full: must be a whole real line
                hit = next((i for i in range(at, len(printed)) if printed[i] == want), None)
            assert hit is not None, f"{board}: {command!r} does not print {line!r} (as a whole line, in order); it printed {printed!r}"
            at = hit + 1


COPY = ROOT / "marketing" / "ads" / "copy.md"
# Rule 1 and 2 of the hook campaign: no uniqueness claims, and no competitor or agent brand in Google ad text.
# "Skills only" states scope and is fine; "the only tool" claims uniqueness and is not.
BANNED_IN_ADS = re.compile(r"\b(the only|only (?:one|tool|registry|catalog|platform|product|way)|first-ever|no one else|nobody else|unique|unlike)\b", re.I)
BRANDS = re.compile(r"\b(claude|anthropic|codex|openai|cursor|gemini|antigravity|osaurus|copilot|portkey|tessl|jfrog|skillreg|corgea|nacos)\b", re.I)


def rsa_lines() -> tuple[list[str], list[str]]:
    text = COPY.read_text(encoding="utf-8")
    block = re.search(r"<!-- rsa-hook -->\s*```text\n(.*?)```", text, re.S).group(1)
    heads = [line[3:] for line in block.splitlines() if line.startswith("H: ")]
    descs = [line[3:] for line in block.splitlines() if line.startswith("D: ")]
    return heads, descs


def test_google_ad_copy_fits_and_follows_the_rules():
    heads, descs = rsa_lines()
    assert len(heads) == 15 and len(descs) == 4  # a full responsive search ad
    for h in heads:
        assert len(h) <= 30, f"headline over 30 characters: {h!r} ({len(h)})"
    for d in descs:
        assert len(d) <= 90, f"description over 90 characters: {d!r} ({len(d)})"
    for line in heads + descs:
        assert not BANNED_IN_ADS.search(line), f"uniqueness claim in ad text: {line!r}"
        assert not BRANDS.search(line), f"brand name in Google ad text: {line!r}"
    assert len(set(heads)) == len(heads)
    # Google's Repetition policy: a headline shouldn't reappear inside a description.
    for h in heads:
        for d in descs:
            assert h.lower() not in d.lower(), f"headline {h!r} repeated in description {d!r}"


def test_x_posts_fit_and_follow_the_rules():
    text = COPY.read_text(encoding="utf-8")
    block = re.search(r"<!-- x-hook -->\n(.*?)<!-- /x-hook -->", text, re.S).group(1)
    posts = [line[2:] for line in block.splitlines() if line.startswith("- ")]
    assert len(posts) == 4
    for post in posts:
        counted = len(post.replace("[link]", "x" * 23))  # X counts every link as 23 characters
        assert counted <= 280, f"post over 280 characters ({counted}): {post!r}"
        assert not BANNED_IN_ADS.search(post), f"uniqueness claim: {post!r}"


def test_hook_creatives_carry_no_uniqueness_claims():
    page = CREATIVES.read_text(encoding="utf-8")
    start = page.index("hook: refused")
    visible = re.sub(r"<[^>]+>", " ", page[start:page.index("<script>")])
    visible = re.sub(r'data-real="[^"]*"', " ", visible)
    for claim in ("only one", "the only", "no one else", "nobody else", "first-ever", "unlike"):
        assert claim not in visible.lower(), f"uniqueness claim in a hook creative: {claim!r}"
