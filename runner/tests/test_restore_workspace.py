"""Putting the workspace back after a run (gitops.restore_workspace), on real repositories, and finishing it when the
runner stopped halfway: every git step that changes something is a place the process can die."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from arceus_runner.gitops import Git, GitError, restore_workspace

BRANCH = "arceus-qa/7"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def init(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "readme.txt").write_text("one\n")
    git(path, "add", "readme.txt")
    git(path, "commit", "-q", "-m", "init")


def layout(tmp: Path) -> tuple[Path, list[dict], str]:
    """A workspace as a run leaves it.
    - `worked`: was on `feature`; the run committed on its branch and left a changed and a new file.
    - `gone`: was on `old`, which someone deleted meanwhile: it goes back to the commit it was at.
    - `missing`: in the snapshot, not on disk.
    - the home (the workspace itself) was on `notes`."""
    ws = tmp / "ws"
    init(ws)
    git(ws, "branch", "notes")
    git(ws, "switch", "-q", "-c", BRANCH)

    worked = ws / "worked"
    init(worked)
    git(worked, "switch", "-q", "-c", "feature")
    start = git(worked, "rev-parse", "HEAD")
    git(worked, "switch", "-q", "-c", BRANCH)
    (worked / "agent.txt").write_text("agent\n")
    git(worked, "add", "agent.txt")
    git(worked, "commit", "-q", "-m", "agent work")
    committed = git(worked, "rev-parse", "HEAD")
    (worked / "readme.txt").write_text("changed\n")
    (worked / "stray.txt").write_text("left over\n")

    gone = ws / "gone"
    init(gone)
    git(gone, "switch", "-q", "-c", "old")
    gone_head = git(gone, "rev-parse", "HEAD")
    git(gone, "switch", "-q", "-c", BRANCH)
    git(gone, "branch", "-D", "old")

    repos = [{"repo": "worked", "prev": "feature", "prev_head": start, "sha": start},
             {"repo": "gone", "prev": "old", "prev_head": gone_head, "sha": gone_head},
             {"repo": "missing", "prev": "main", "prev_head": "0" * 40, "sha": "0" * 40}]
    return ws, repos, committed


class Died(Exception):
    pass


class DyingGit(Git):
    """Real git that dies right after its n-th change to a repository: the change is made, nothing after it."""

    def __init__(self, n: int):
        super().__init__(None, "t", "t@t")
        self.left = n

    def _changed(self) -> None:
        self.left -= 1
        if self.left == 0:
            raise Died

    def stash_all(self, repo, message):
        out = super().stash_all(repo, message)
        self._changed()
        return out

    def switch(self, repo, ref):
        super().switch(repo, ref)
        self._changed()

    def home_on(self, home, branch):
        super().home_on(home, branch)
        self._changed()

    def delete_branch_at(self, repo, branch, sha):
        super().delete_branch_at(repo, branch, sha)
        self._changed()


def restore(g: Git, ws: Path, repos: list[dict], said: list[tuple]) -> None:
    restore_workspace(g, ws, BRANCH, repos, "notes", stash_message="arceus: what qa-7 left",
                      on_stashed=lambda repo, stash: said.append(("stashed", repo, stash)),
                      on_committed=lambda repo, sha: said.append(("committed", repo, sha)))


def assert_restored(ws: Path, said: list[tuple], committed: str, stash_said: bool = True) -> None:
    worked, gone = ws / "worked", ws / "gone"
    assert git(worked, "branch", "--show-current") == "feature"
    assert git(worked, "status", "--porcelain") == ""
    [stash] = git(worked, "stash", "list").splitlines()
    assert "arceus: what qa-7 left" in stash
    assert git(worked, "show", "--name-only", "--format=", "stash@{0}^3") == "stray.txt"  # the new file is kept
    assert "changed" in git(worked, "show", "stash@{0}:readme.txt")
    assert git(gone, "branch", "--show-current") == ""
    assert git(gone, "rev-parse", "HEAD") == git(gone, "rev-parse", "main")
    assert git(ws, "branch", "--show-current") == "notes"
    for repo in (ws, worked, gone):
        assert git(repo, "branch", "--list", BRANCH) == ""
    # Said once each, whatever happened between: the stash, and the commit the run made.
    stashes = [s for s in said if s[0] == "stashed"]
    if stash_said:
        assert [(k, r) for k, r, _ in stashes] == [("stashed", "worked")] and stashes[0][2]
    else:
        assert stashes == []
    assert [s for s in said if s[0] == "committed"] == [("committed", "worked", committed)]


def changes_in_a_full_restore(tmp: Path) -> int:
    ws, repos, _ = layout(tmp)
    g = DyingGit(10**6)
    restore(g, ws, repos, [])
    return 10**6 - g.left


def test_a_restore_puts_every_repository_and_the_home_back(tmp_path):
    ws, repos, committed = layout(tmp_path)
    said: list[tuple] = []
    restore(Git(None, "t", "t@t"), ws, repos, said)
    assert_restored(ws, said, committed)


# stash, switch and delete in `worked`; switch and delete in `gone`; home_on and delete in the home.
STEPS = 7


def test_every_change_a_restore_makes_is_counted(tmp_path):
    assert changes_in_a_full_restore(tmp_path) == STEPS


@pytest.mark.parametrize("dies_after", range(1, STEPS + 1))
def test_a_restore_that_died_halfway_is_finished_by_the_next_one(tmp_path, dies_after):
    ws, repos, committed = layout(tmp_path)
    said: list[tuple] = []
    with pytest.raises(Died):
        restore(DyingGit(dies_after), ws, repos, said)
    restore(Git(None, "t", "t@t"), ws, repos, said)
    # Dead between the stash and its event, the event is not made again (the tree is clean by then): the work is in
    # the stash under the run's name all the same.
    assert_restored(ws, said, committed, stash_said=dies_after != 1)


def test_a_branch_moved_after_it_was_read_is_kept(tmp_path):
    """The branch is deleted only at the commit just read: one made since (a person, a late agent) keeps it, and the
    restore stops there instead of losing it."""
    ws, repos, committed = layout(tmp_path)
    worked = ws / "worked"

    class Late(Git):
        def branch_sha(self, repo, branch):
            sha = super().branch_sha(repo, branch)
            if repo == worked and sha:
                tree = git(repo, "rev-parse", f"{sha}^{{tree}}")
                late = git(repo, "commit-tree", tree, "-p", sha, "-m", "late")
                git(repo, "update-ref", f"refs/heads/{branch}", late, sha)
            return sha

    with pytest.raises(GitError):
        restore(Late(None, "t", "t@t"), ws, repos, [])
    assert git(worked, "log", "-1", "--format=%s", BRANCH) == "late"
    assert git(worked, "rev-parse", f"{BRANCH}^") == committed
