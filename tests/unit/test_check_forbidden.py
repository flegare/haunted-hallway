"""Proves scripts/check_forbidden.sh can fail: plant one sample of every kind and demand a catch.

Samples are assembled from fragments so this file does not contain the forbidden words itself.
"""
import os
import re
import resource
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_forbidden.sh"
NEUTRAL = ("Haunted Hallway", "noreply@haunted-hallway.invalid")

SAMPLES = {   # generic shapes only: specific words live in the private list, never in this repository
    "private-ip": "host " + "192." + "168.1.5",
    "email": "write to someone@example." + "com",
    "home-path": "see /ho" + "me/alice/notes",
    "secrets": "key=" + "AK" + "IA" + "A" * 16,
    "legacy-ids": "id 20261105" + "-123456-" + "abcdef12",
    "french": "caf" + chr(0xE9),
    "attribution": "generated " + "with a tool",
}


def git(repo, *args, env=None, check=True):
    e = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    e.update(env or {})
    return subprocess.run(["git", *args], cwd=repo, env=e, check=check, capture_output=True, text=True)


def make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    (repo / "scripts").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", NEUTRAL[0])
    git(repo, "config", "user.email", NEUTRAL[1])
    shutil.copy(SCRIPT, repo / "scripts" / "check_forbidden.sh")
    (repo / "README.md").write_text("A clean readme.\n")
    return repo


def commit(repo, message="work", env=None):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, env=env)


def run_check(repo, extra_env=None, args=()):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    env.pop("HH_FORBIDDEN_EXTRA", None)
    env.pop("HH_GH_JSON_FILE", None)
    env.pop("HH_ALLOW_SKIP", None)
    if resource.getrlimit(resource.RLIMIT_AS)[0] != resource.RLIM_INFINITY:
        env["HH_SKIP_TRUFFLEHOG"] = "1"   # the Go scanner crashes under the test memory cap; run it by hand without a cap
    env.update(extra_env or {})
    out = subprocess.run(["bash", "scripts/check_forbidden.sh", *args], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=180)
    return out.returncode, out.stdout + out.stderr


OPTIONAL_CHECKS = {"github-description", "private-word-list", "trufflehog"}   # may be SKIP without a finding


def no_findings(rc, out):
    """Exit 0, or exit 3 where the ONLY checks that did not run are the optional inputs of this run."""
    skipped = {line.split()[1] for line in out.splitlines() if line.startswith("SKIP ")}
    return rc == 0 or (rc == 3 and "fails=0" in out and skipped <= OPTIONAL_CHECKS)


def status_of(output, check):
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[1] == check:
            return parts[0], (parts[2] if len(parts) > 2 else "")
    raise AssertionError(f"no line for {check}:\n{output}")


def test_clean_repo_passes(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo)
    rc, out = run_check(repo)
    assert no_findings(rc, out), out
    assert "result: clean" not in out or rc == 0   # a SKIP (no list, no GitHub JSON) is never "clean"
    for name in ("tree-contents", "tree-file-names", "history", "commit-identity", "commit-trailers", "tarball"):
        assert status_of(out, name)[0] == "PASS", out


def test_a_skipped_check_is_not_a_pass(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo)
    rc, out = run_check(repo)
    assert rc == 3 and "NOT PROVEN" in out
    assert status_of(out, "github-description")[0] == "SKIP"
    assert status_of(out, "private-word-list")[0] == "SKIP"


@pytest.mark.parametrize("group", sorted(SAMPLES))
def test_each_kind_is_caught_in_tree_history_and_tarball(tmp_path, group):
    repo = make_repo(tmp_path)
    (repo / "notes.txt").write_text(SAMPLES[group] + "\n")
    commit(repo)
    rc, out = run_check(repo)
    assert rc == 1, out
    for check in ("tree-contents", "history", "tarball"):
        status, detail = status_of(out, check)
        assert status == "FAIL" and f"{group}=" in detail, (check, out)


def test_a_planted_file_name_is_caught(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ("notes-" + "192." + "168.1.5.txt")).write_text("harmless\n")
    commit(repo)
    rc, out = run_check(repo)
    assert rc == 1 and status_of(out, "tree-file-names")[0] == "FAIL"


def test_history_only_leak_is_caught_after_the_file_is_deleted(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "notes.txt").write_text(SAMPLES["private-ip"] + "\n")
    commit(repo, "add")
    (repo / "notes.txt").unlink()
    commit(repo, "remove")
    rc, out = run_check(repo)
    assert rc == 1
    assert status_of(out, "tree-contents")[0] == "PASS"
    assert status_of(out, "tarball")[0] == "PASS"
    assert status_of(out, "history")[0] == "FAIL"


def test_forbidden_word_in_a_commit_message_is_caught(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "ported from " + SAMPLES["private-ip"])
    rc, out = run_check(repo)
    assert rc == 1 and status_of(out, "history")[0] == "FAIL"


def test_trailers_are_caught(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "work\n\nCo-Authored-" + "By: someone <a@b.invalid>")
    rc, out = run_check(repo)
    assert rc == 1 and status_of(out, "commit-trailers")[0] == "FAIL"


def test_wrong_identity_is_caught(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, env={"GIT_AUTHOR_NAME": "Someone Else", "GIT_AUTHOR_EMAIL": "x@example.invalid"})
    rc, out = run_check(repo)
    assert rc == 1 and status_of(out, "commit-identity")[0] == "FAIL"


def test_secret_values_are_counted_not_printed(tmp_path):
    repo = make_repo(tmp_path)
    secret = SAMPLES["secrets"].split("=")[1]
    (repo / "notes.txt").write_text(f"token {secret}\n")
    commit(repo)
    rc, out = run_check(repo, args=("--show",))
    assert rc == 1 and secret not in out


def test_only_the_scripts_own_path_is_excluded(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "scripts" / "other.sh").write_text("# " + SAMPLES["private-ip"] + "\n")
    commit(repo)
    rc, out = run_check(repo)
    assert rc == 1 and status_of(out, "tree-contents")[0] == "FAIL"


def test_github_description_is_scanned(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo)
    good = tmp_path / "good.json"
    good.write_text('{"description":"Simplified, self-hostable version","repositoryTopics":[],"isPrivate":true}')
    rc, out = run_check(repo, {"HH_GH_JSON_FILE": str(good)})
    assert no_findings(rc, out) and status_of(out, "github-description")[0] == "PASS" and "private" in out
    bad = tmp_path / "bad.json"
    bad.write_text('{"description":"Ported from ' + SAMPLES["private-ip"] + '","repositoryTopics":[],"isPrivate":true}')
    rc, out = run_check(repo, {"HH_GH_JSON_FILE": str(bad)})
    assert rc == 1 and status_of(out, "github-description")[0] == "FAIL"


def make_wordlist(path: Path, mode=0o600):
    path.write_text("zzchildnamezz\n")
    path.chmod(mode)
    return path


def test_private_word_list(tmp_path):
    repo = make_repo(tmp_path)
    words = make_wordlist(tmp_path / "words.txt")
    commit(repo)
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(words)})
    assert no_findings(rc, out) and status_of(out, "private-word-list")[0] == "PASS"
    (repo / "notes.txt").write_text("my friend zzchildnamezz drew this\n")
    commit(repo)
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(words)})
    assert rc == 1 and status_of(out, "private-word-list")[0] == "FAIL"
    assert "zzchildnamezz" not in out                      # counted, never shown
    (repo / "notes.txt").unlink()


def test_private_word_list_must_be_private_and_outside_the_repo(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo)
    open_mode = make_wordlist(tmp_path / "open.txt", 0o644)
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(open_mode)})
    assert rc == 1 and "mode 600" in out
    inside = make_wordlist(repo / "words.txt")
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(inside)})
    assert rc == 1 and "outside this repository" in out
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(tmp_path / "missing.txt")})
    assert rc == 1


def test_this_repository_is_clean_right_now():
    rc, out = run_check(ROOT)
    assert no_findings(rc, out), out


def test_private_word_list_reports_only_places_and_the_pattern_count(tmp_path):
    """The list is private: a finding is a file:line, a history line number or a name count. Never a pattern."""
    repo = make_repo(tmp_path)
    words = tmp_path / "outside" / "fake-words.txt"
    words.parent.mkdir()
    words.write_text("zzfakealpha\nzz[f]akebeta\n\nzzfakegamma+\n")          # 3 patterns, one blank line
    words.chmod(0o600)
    (repo / "notes.txt").write_text("line one\nline two\nthe zzfakealpha is here\nline four\n")
    (repo / "zzfakegammaaa.txt").write_text("ok\n")                          # a file NAME that matches
    commit(repo)
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(words)})
    assert rc == 1
    assert "3 pattern(s) read" in out
    assert "notes.txt:3" in out                                              # the line number of the finding
    assert "file-names=1" in out
    for secret in ("zzfakealpha", "zzfakebeta", "zz[f]akebeta", "zzfakegamma", "is here"):
        assert secret not in out.replace("zzfakegammaaa.txt", ""), secret
    assert "zzfakegammaaa" not in out                                        # not even the matching file name
    assert re.search(r"private-word-list:history\] line \d+", out)           # history: a line number only
    # a clean tree states how many patterns it read
    (repo / "notes.txt").unlink(); (repo / "zzfakegammaaa.txt").unlink()
    git(repo, "rm", "-rq", "--cached", "--ignore-unmatch", ".")
    repo2 = make_repo(tmp_path, "clean")
    commit(repo2)
    rc, out = run_check(repo2, {"HH_FORBIDDEN_EXTRA": str(words)})
    assert no_findings(rc, out) and "0 hits, 3 pattern(s) read" in out


def test_private_word_list_single_file_still_cites_only_file_and_line(tmp_path):
    """With one tracked file grep drops the file name; the text must still never leak."""
    repo = tmp_path / "solo"
    (repo / "scripts").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", NEUTRAL[0]); git(repo, "config", "user.email", NEUTRAL[1])
    shutil.copy(SCRIPT, repo / "scripts" / "check_forbidden.sh")        # excluded from the scan as the script itself
    (repo / "notes.txt").write_text("a\nb\nthe zzfakealpha\n")
    commit(repo)
    words = tmp_path / "w.txt"; words.write_text("zzfakealpha\n"); words.chmod(0o600)
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(words)})
    assert rc == 1 and "[private-word-list:tree] notes.txt:3" in out
    assert "zzfakealpha" not in out and "the zz" not in out


def test_the_public_script_keeps_only_generic_groups():
    """Specific words must come from the private list. If someone adds a group here, this goes red."""
    text = SCRIPT.read_text()
    groups = re.search(r"GROUPS_ORDER=\(([^)]*)\)", text).group(1).split()
    assert groups == ["private-ip", "email", "home-path", "secrets", "legacy-ids", "french", "attribution"]
    assert set(re.findall(r"^PAT\[([a-z-]+)\]=", text, re.M)) == set(groups)


def test_private_list_covers_commit_messages_and_the_github_description(tmp_path):
    words = tmp_path / "outside" / "w.txt"
    words.parent.mkdir()
    words.write_text("zzfakedelta\n")
    words.chmod(0o600)
    repo = make_repo(tmp_path)
    commit(repo, "ported from zzfakedelta")
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(words)})
    assert rc == 1 and "private-word-list:history" in out and "zzfakedelta" not in out
    repo2 = make_repo(tmp_path, "second")
    commit(repo2)
    bad = tmp_path / "gh.json"
    bad.write_text('{"description":"about zzfakedelta","repositoryTopics":[],"isPrivate":true}')
    rc, out = run_check(repo2, {"HH_FORBIDDEN_EXTRA": str(words), "HH_GH_JSON_FILE": str(bad)})
    assert rc == 1 and "github=1" in out and "zzfakedelta" not in out


def test_short_patterns_are_whole_words_and_long_ones_are_not(tmp_path):
    """4 characters or fewer: a whole word only. Longer: found anywhere, as written."""
    words = tmp_path / "outside" / "w.txt"
    words.parent.mkdir()
    words.write_text("zzq\nzzlongpattern\nab12\nzzmid55\n")                  # 3 chars, long, exactly 4, 7 chars
    words.chmod(0o600)

    def run(text):
        repo = make_repo(tmp_path, f"r{abs(hash(text)) % 10**8}")
        (repo / "notes.txt").write_text(text)
        commit(repo)
        return run_check(repo, {"HH_FORBIDDEN_EXTRA": str(words)})

    rc, out = run("xzzqx and wzzq and zzqy and xab12 and ab12x\n")         # short pattern only INSIDE words of letters
    assert no_findings(rc, out) and "0 hits, 4 pattern(s) read" in out, out
    rc, out = run("line one\nsee ZZQ, here\nlast\n")                        # the same word, whole, other case
    assert rc == 1 and "notes.txt:2" in out and "4 pattern(s) read" in out
    assert "ZZQ" not in out and "zzq" not in out
    rc, out = run("fine\nsee ab12.\n")                                      # exactly 4 characters: whole word too
    assert rc == 1 and "notes.txt:2" in out
    rc, out = run("fine\nsee ab123 here\n")                                # followed by a digit: still a finding
    assert rc == 1 and "notes.txt:2" in out
    rc, out = run("fine\nmy xxzzlongpatternyy here\n")                      # a long pattern is found inside a word
    assert rc == 1 and "notes.txt:2" in out and "zzlong" not in out
    rc, out = run("fine\nmy xxzzmid55yy here\n")                            # 5 characters or more: found inside a word
    assert rc == 1 and "notes.txt:2" in out and "zzmid" not in out


def fake_short_list(tmp_path):
    words = tmp_path / "outside" / "short.txt"
    words.parent.mkdir(exist_ok=True)
    words.write_text("zza\n")                      # a fake 3-letter name
    words.chmod(0o600)
    return words


def _scan_one(tmp_path, name, text, file_name="notes.txt"):
    repo = make_repo(tmp_path, name)
    (repo / file_name).write_text(text)
    commit(repo)
    return run_check(repo, {"HH_FORBIDDEN_EXTRA": str(fake_short_list(tmp_path))})


@pytest.mark.parametrize("text", ["ok\nzza_1 here\n", "ok\nzza1 here\n", "ok\nBlake ZzaBlake x\n",
                                  "ok\nblakeZza x\n", "ok\nsee Zza.\n", "ok\n(zza)\n", "ok\nZZA_x\n"])
def test_a_short_name_next_to_a_non_letter_or_a_case_change_is_found(tmp_path, text):
    rc, out = _scan_one(tmp_path, "r", text)
    assert rc == 1 and "[private-word-list:tree] notes.txt:2" in out and "zza" not in out.lower().replace("pattern", "")


@pytest.mark.parametrize("text", ["ok\nwordzzaword\n", "ok\nzzalpha\n", "ok\nbuzza\n", "ok\nZzalpha\n", "ok\nWORDZZAWORD\n"])
def test_a_short_name_inside_a_word_of_letters_is_not_found(tmp_path, text):
    rc, out = _scan_one(tmp_path, "r", text)
    assert no_findings(rc, out) and "0 hits, 1 pattern(s) read" in out


def test_a_short_name_in_a_file_name_is_found_in_the_history_and_in_the_description(tmp_path):
    rc, out = _scan_one(tmp_path, "r1", "ok\n", file_name="kid_zza.txt")
    assert rc == 1 and "file-names=1" in out and "kid_" not in out
    repo = make_repo(tmp_path, "r2")
    (repo / "a.txt").write_text("x\n")
    commit(repo, message="add zza_2 notes")
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(fake_short_list(tmp_path))})
    assert rc == 1 and "history=" in out and "history=0" not in out
    gh = tmp_path / "gh.json"
    gh.write_text('{"description":"for Zza1","repositoryTopics":[],"isPrivate":true}')
    repo = make_repo(tmp_path, "r3")
    commit(repo)
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(fake_short_list(tmp_path)), "HH_GH_JSON_FILE": str(gh)})
    assert rc == 1 and "github=1" in out


def test_the_short_patterns_are_combined_not_the_long_ones_lost(tmp_path):
    words = tmp_path / "outside" / "both.txt"
    words.parent.mkdir()
    words.write_text("zza\nlongfakepattern\nyyb\n")
    words.chmod(0o600)
    repo = make_repo(tmp_path)
    (repo / "n.txt").write_text("a\nthe yyb_9 one\nmy xlongfakepatternx\nzza,\nnothing\n")
    commit(repo)
    rc, out = run_check(repo, {"HH_FORBIDDEN_EXTRA": str(words)})
    assert rc == 1 and "tree=3" in out and "3 pattern(s) read" in out
    for n in (2, 3, 4):
        assert f"[private-word-list:tree] n.txt:{n}" in out


@pytest.mark.parametrize("allow", ["1", "true", "yes", "0"])
def test_allow_skip_never_makes_a_skipped_check_clean(tmp_path, allow):
    repo = make_repo(tmp_path)
    commit(repo)
    env = {"HH_ALLOW_SKIP": allow}
    rc, out = run_check(repo, env)
    assert rc == 3 and "NOT PROVEN" in out and "result: clean" not in out
    assert status_of(out, "github-description")[0] == "SKIP"


def test_the_final_check_with_every_input_is_clean_only_without_a_skip(tmp_path):
    """With the GitHub JSON and a word list given, nothing is SKIP (trufflehog aside, which a memory cap forbids)."""
    repo = make_repo(tmp_path)
    commit(repo)
    gh = tmp_path / "ok.json"
    gh.write_text('{"description":"A small thing","repositoryTopics":[{"name":"flask"}],"isPrivate":true}')
    words = tmp_path / "outside" / "w.txt"
    words.parent.mkdir()
    words.write_text("zzchildnamezz\n")
    words.chmod(0o600)
    rc, out = run_check(repo, {"HH_GH_JSON_FILE": str(gh), "HH_FORBIDDEN_EXTRA": str(words)})
    skipped = [line.split()[1] for line in out.splitlines() if line.startswith("SKIP ")]
    if os.environ.get("HH_SKIP_TRUFFLEHOG") != "1" and resource.getrlimit(resource.RLIMIT_AS)[0] == resource.RLIM_INFINITY:
        assert skipped == [] and rc == 0 and "result: clean" in out
    else:
        assert skipped == ["trufflehog"] and rc == 3


def test_the_tarball_scan_searches_binaries_but_accent_bytes_in_an_image_are_not_a_finding(tmp_path):
    """Compressed image bytes contain accent-like sequences by chance: the accented-letter group skips
    binary files. Every other group still searches them."""
    repo = make_repo(tmp_path, "acc")
    (repo / "pic.bin").write_bytes(b"\x89PNG\x00\x01\x02" + "caf".encode() + bytes([0xC3, 0xA9]) + b"\x00\xff\xfe")
    commit(repo)
    rc, out = run_check(repo)
    assert status_of(out, "tarball")[0] == "PASS", out
    repo = make_repo(tmp_path, "sec")
    (repo / "pic.bin").write_bytes(b"\x89PNG\x00\x01\x02 write to someone@example." + b"com \x00\xff")
    commit(repo)
    rc, out = run_check(repo)
    assert status_of(out, "tarball")[0] == "FAIL" and "email=" in out, out
