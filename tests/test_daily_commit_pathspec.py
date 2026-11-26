"""The daily scrape's commit leaves nhl/pregame to the pregame job.

Runs the real ``sdv_commit_push`` from ``scripts/daily_nhl_scraper.sh`` (extracted, not
re-implemented) against a throwaway repo with a bare origin, with the pathspec the scrape
passes. nhl/pregame is committed only by the pregame job's 03:00 UTC run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_nhl_scraper.sh"
PATHSPEC = "nhl ':!nhl/pregame'"  # exactly what the scrape passes

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or os.name != "posix", reason="bash + git")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _commit_push(repo: Path, msg: str) -> subprocess.CompletedProcess:
    body = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = body.index("sdv_commit_push() {")
    fn = body[start : body.index("\n}\n", start) + 3]
    env = {**os.environ, "SDV_ROTATE_LOGS": "/nonexistent"}
    return subprocess.run(
        ["bash", "-c", f"{fn}\nsdv_commit_push '{msg}' {PATHSPEC}"],
        cwd=repo, env=env, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git(work, "config", k, v)
    (work / "nhl" / "json").mkdir(parents=True)
    (work / "nhl" / "json" / "1.json").write_text("{}")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "origin", "HEAD:main")
    return work


def _pregame(repo: Path) -> Path:
    f = repo / "nhl" / "pregame" / "2027" / "2026-11-25.json"
    f.parent.mkdir(parents=True)
    f.write_text('{"date": "2026-11-25"}')
    return f


def test_scrape_commits_its_data_and_leaves_pregame_untracked(repo):
    (repo / "nhl" / "json" / "2.json").write_text("{}")
    _pregame(repo)
    out = _commit_push(repo, "NHL Raw Updated (Start: 2027 End: 2027)")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "pushed" in out.stdout
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["nhl/json/2.json"]
    assert _git(repo, "status", "--porcelain", "--", "nhl/pregame").startswith("??")
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")


def test_a_pregame_file_left_staged_does_not_ride_along(repo):
    (repo / "nhl" / "json" / "2.json").write_text("{}")
    _pregame(repo)
    _git(repo, "add", "--", "nhl/pregame")  # a pregame commit that failed after staging
    out = _commit_push(repo, "NHL Raw Updated (Start: 2027 End: 2027)")
    assert out.returncode == 0, out.stdout + out.stderr
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["nhl/json/2.json"]
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["nhl/pregame/2027/2026-11-25.json"]


def test_only_pregame_changed_means_nothing_to_commit(repo):
    _pregame(repo)
    head = _git(repo, "rev-parse", "HEAD")
    out = _commit_push(repo, "NHL Raw Updated (Start: 2027 End: 2027)")
    assert out.returncode == 0 and "nothing to commit" in out.stdout
    assert _git(repo, "rev-parse", "HEAD") == head


def test_the_scrape_passes_this_pathspec():
    body = SCRIPT.read_text(encoding="utf-8")
    assert f'sdv_commit_push "NHL Raw Updated (Start: $i End: $i)" {PATHSPEC}' in body
