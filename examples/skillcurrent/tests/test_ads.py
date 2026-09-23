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


def real_blocks() -> list[tuple[str, str, list[tuple[str, list[str]]]]]:
    """(board id, scenario, [(command, shown output lines)]) for every data-real block."""
    page = CREATIVES.read_text(encoding="utf-8")
    blocks = []
    for board in re.finditer(r'<div class="board[^"]*"[^>]*id="([^"]+)"(.*?)(?=<div class="board|<script>)', page, re.S):
        for m in re.finditer(r'<(?:pre|div)[^>]*data-real="([^"]+)"[^>]*>(.*?)</(?:pre|div)>\s*<!--/real-->', board.group(2), re.S):
            text = html.unescape(re.sub(r"<br\s*/?>", "\n", m.group(2)))
            text = re.sub(r"<[^>]+>", "", text)
            steps: list[tuple[str, list[str]]] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("$ "):
                    steps.append((line[2:], []))
                else:
                    assert steps, f"{board.group(1)}: output before any command: {line!r}"
                    steps[-1][1].append(line)
            blocks.append((board.group(1), m.group(1), steps))
    return blocks


def test_creatives_have_real_blocks():
    blocks = real_blocks()
    assert blocks, "no data-real blocks found in creatives.html"
    for board, scenario, steps in blocks:
        assert scenario in SCENARIOS, f"{board}: unknown scenario {scenario!r}"
        assert steps, f"{board}: empty real block"


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

    def run(command: str, check: bool = True) -> str:
        argv = shlex.split(command)
        if argv and argv[0] == "skillcurrent":
            argv = argv[1:]
        done = subprocess.run([sys.executable, "-m", "skillcurrent", *argv], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        if check:
            assert done.returncode == 0, f"setup {command!r} failed: {done.stderr}"
        return done.stdout + done.stderr

    SCENARIOS[scenario](run, home)
    for command, shown in steps:
        assert command.startswith("skillcurrent "), f"{board}: ad shows a command that is not skillcurrent: {command!r}"
        printed = " ".join(run(command, check=False).split())
        for line in shown:
            want = " ".join(line.rstrip("…").split())
            assert want in printed, f"{board}: {command!r} does not print {line!r}; it printed {printed!r}"


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
