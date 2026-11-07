#!/usr/bin/env bash
# Pre-publication check: nothing private or embarrassing may ship.
#
#   scripts/check_forbidden.sh [--repo OWNER/NAME] [--show]
#
# Checks: working tree (contents and file names), the whole git history (diffs,
# commit messages, identities), the HEAD tarball, the GitHub description and
# topics (with --repo), an optional private word list, media metadata, gitleaks.
#
# Exit: 0 = every check ran and found nothing; 1 = something found;
#       3 = nothing found but a check could not run. A SKIP is never "clean": HH_ALLOW_SKIP is ignored.
# "Nothing found" is only true when no line says SKIP. Read the summary.
#
# This file holds only generic patterns (shapes, not names), so it is scanned like any other
# file, except that its own pattern text is skipped by exact path. Secret-shaped matches are
# never printed, only counted.
#
# The checks themselves are proven by tests/unit/test_check_forbidden.py, which plants
# a sample of every kind and requires the script to catch it.
#
# Private word list (child first names, internal model name, ...): put one regex per
# line in a file OUTSIDE this repository, mode 600, and point HH_FORBIDDEN_EXTRA at it.
# Only counts and file names are printed, never the words.
set -u
export LC_ALL=C.UTF-8

SELF='scripts/check_forbidden.sh'
NEUTRAL_IDENTITY='Haunted Hallway noreply@haunted-hallway.invalid Haunted Hallway noreply@haunted-hallway.invalid'

# Only GENERIC shapes live in this public file. Every word that is specific to a person, a
# company, a product, a codename or an internal tool comes from the private word list
# (HH_FORBIDDEN_EXTRA, outside the repository). Do not add such words here.
GROUPS_ORDER=(private-ip email home-path secrets legacy-ids french attribution)
declare -A PAT
PAT[private-ip]='192\.168\.|\b10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.'
PAT[email]='[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.(com|org|net|io|fr|ca|dev|me|app)\b'
PAT[home-path]='/home/[a-z_][a-z0-9_-]*'
PAT[secrets]='AIza[0-9A-Za-z_-]{35}|-----BEGIN|ghp_|glpat-|sk-[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}'
PAT[legacy-ids]='[0-9]{8}-[0-9]{6}-[0-9a-f]{8}'
PAT[french]='[éèêàùçôîï]'
PAT[attribution]='co-authored-by|signed-off-by|generated with'
SECRET_GROUPS=" secrets "

SHOW=0; REPO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --show) SHOW=1 ;;
    --repo) REPO="${2:-}"; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
  shift
done

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
FAILS=0; SKIPS=0
record() { printf '%-5s %-20s %s\n' "$1" "$2" "$3"
  [ "$1" = FAIL ] && FAILS=$((FAILS + 1)); [ "$1" = SKIP ] && SKIPS=$((SKIPS + 1)); return 0; }
is_secret() { case "$SECRET_GROUPS" in *" $1 "*) return 0 ;; esac; return 1; }

# scan_file <file> <label>: print per-group counts; returns total hits via $HITS.
scan_file() {
  local f="$1" label="$2" g n detail=""
  HITS=0
  for g in "${GROUPS_ORDER[@]}"; do
    n=$(grep -Eiac -e "${PAT[$g]}" "$f" 2>/dev/null); n=${n:-0}
    if [ "$n" -gt 0 ]; then
      HITS=$((HITS + n)); detail="$detail $g=$n"
      if [ "$SHOW" = 1 ] && ! is_secret "$g"; then
        grep -Eian -e "${PAT[$g]}" "$f" | head -20 | sed "s/^/        [$label:$g] /" >&2
      fi
    fi
  done
  HITDETAIL="$detail"
}

in_git() { git rev-parse --git-dir >/dev/null 2>&1; }
if ! in_git; then echo "error: run inside the git repository" >&2; exit 64; fi
cd "$(git rev-parse --show-toplevel)" || exit 64
TOP="$(pwd -P)"

# 1. working tree contents (tracked + untracked, not ignored), this script excluded
git ls-files -z --cached --others --exclude-standard | grep -zvxF "$SELF" > "$TMP/files0"
HITS=0; HITDETAIL=""; tree_hits=0; tree_detail=""
for g in "${GROUPS_ORDER[@]}"; do
  xargs -0 -r grep -EiaIn -e "${PAT[$g]}" < "$TMP/files0" 2>/dev/null > "$TMP/tree-$g" || true
  n=$(wc -l < "$TMP/tree-$g")
  if [ "$n" -gt 0 ]; then
    tree_hits=$((tree_hits + n)); tree_detail="$tree_detail $g=$n"
    if is_secret "$g"; then cut -d: -f1 "$TMP/tree-$g" | sort -u | sed 's/^/        [tree:secrets] file: /' >&2
    else cut -d: -f1,2 "$TMP/tree-$g" | head -20 | sed "s/^/        [tree:$g] /" >&2; fi
  fi
done
if [ "$tree_hits" = 0 ]; then record PASS tree-contents "0 hits in $(tr -cd '\0' < "$TMP/files0" | wc -c) files"
else record FAIL tree-contents "$tree_hits hits:$tree_detail"; fi

# 2. file names
names=0; name_detail=""
for g in "${GROUPS_ORDER[@]}"; do
  n=$(tr '\0' '\n' < "$TMP/files0" | grep -Eic -e "${PAT[$g]}"); n=${n:-0}
  [ "$n" -gt 0 ] && { names=$((names + n)); name_detail="$name_detail $g=$n"; }
done
if [ "$names" = 0 ]; then record PASS tree-file-names "0 hits"; else record FAIL tree-file-names "$names hits:$name_detail"; fi

# 3. whole history: diffs, messages, identities (this script's own path excluded)
if git rev-parse --verify -q HEAD >/dev/null; then
  git log --all -p --no-color --format='commit %H%n%an <%ae> | %cn <%ce>%n%B' -- . ":(exclude)$SELF" > "$TMP/history" 2>/dev/null
  scan_file "$TMP/history" history
  if [ "$HITS" = 0 ]; then record PASS history "0 hits in $(git rev-list --all --count) commit(s), $(wc -l < "$TMP/history") lines"
  else record FAIL history "$HITS hits:$HITDETAIL (line numbers are in 'git log --all -p'; use --show)"; fi

  # 4. identities: exactly the neutral one
  ids="$(git log --all --format='%an %ae %cn %ce' | sort -u)"
  if [ "$ids" = "$NEUTRAL_IDENTITY" ]; then record PASS commit-identity "all commits: Haunted Hallway <noreply@haunted-hallway.invalid>"
  else record FAIL commit-identity "unexpected author/committer: $(printf '%s' "$ids" | wc -l) distinct line(s) differ from the neutral identity"; fi

  # 5. trailers / attribution in commit messages (also covered by the attribution group above)
  t=$(git log --all --format='%B' | grep -Eic '^(co-authored-by|signed-off-by|[a-z]+-session):')
  if [ "${t:-0}" = 0 ]; then record PASS commit-trailers "no Co-Authored-By / Signed-off-by / *-Session trailers"
  else record FAIL commit-trailers "$t trailer line(s)"; fi

  # 6. HEAD tarball: contents and names, binaries included (grep -a), except the accented-letter
  #    group, which is about text: compressed image bytes contain accent-like sequences by chance.
  mkdir "$TMP/tar"
  if git archive --format=tar HEAD -- . ":(exclude)$SELF" 2>/dev/null | tar -x -C "$TMP/tar" 2>/dev/null; then
    th=0; td=""
    for g in "${GROUPS_ORDER[@]}"; do
      bin=a; [ "$g" = french ] && bin=I   # -a: binaries are searched; -I: this group skips them
      n=$(grep -rEi${bin}c -e "${PAT[$g]}" "$TMP/tar" 2>/dev/null | awk -F: '{s+=$NF} END{print s+0}')
      m=$(cd "$TMP/tar" && find . -type f | grep -Eic -e "${PAT[$g]}")
      n=$((n + ${m:-0})); [ "$n" -gt 0 ] && { th=$((th + n)); td="$td $g=$n"; }
    done
    if [ "$th" = 0 ]; then record PASS tarball "0 hits in $(find "$TMP/tar" -type f | wc -l) archived files (HEAD)"
    else record FAIL tarball "$th hits:$td"; fi
    [ -n "$(git status --porcelain)" ] && record WARN tarball "uncommitted changes exist; the tarball reflects HEAD only"
  else record SKIP tarball "git archive failed"; fi
else
  record SKIP history "no commits yet"; record SKIP commit-identity "no commits yet"
  record SKIP commit-trailers "no commits yet"; record SKIP tarball "no commits yet"
fi

# 7. GitHub description and topics: the tree scan cannot see them
gh_json=""
if [ -n "${HH_GH_JSON_FILE:-}" ]; then gh_json="$(cat "$HH_GH_JSON_FILE")"
elif [ -n "$REPO" ]; then
  if command -v gh >/dev/null; then gh_json="$(gh repo view "$REPO" --json description,repositoryTopics,isPrivate 2>"$TMP/gh.err")" || gh_json=""; fi
fi
if [ -n "$gh_json" ]; then
  printf '%s\n' "$gh_json" > "$TMP/gh.json"; scan_file "$TMP/gh.json" github
  case "$gh_json" in
    *'"isPrivate":true'*) vis="repository is private" ;;
    *'"isPrivate":false'*) vis="repository is PUBLIC" ;;
    *) vis="visibility not reported" ;;
  esac
  if [ "$HITS" = 0 ]; then record PASS github-description "description and topics clean; $vis"
  else record FAIL github-description "$HITS hits:$HITDETAIL"; fi
else
  record SKIP github-description "no --repo given or gh could not read it (the description was NOT checked)"
fi

# 8. private word list: outside the repo, mode 600, counts only
extra="${HH_FORBIDDEN_EXTRA:-}"
if [ -z "$extra" ]; then record SKIP private-word-list "HH_FORBIDDEN_EXTRA not set (child names / internal model name NOT checked)"
elif [ ! -f "$extra" ]; then record FAIL private-word-list "HH_FORBIDDEN_EXTRA points to a missing file"
elif [ "$(stat -c %a "$extra")" != 600 ]; then record FAIL private-word-list "word list must be mode 600"
elif case "$(readlink -f "$extra")" in "$TOP"/*) true ;; *) false ;; esac; then record FAIL private-word-list "word list must live outside this repository"
else
  grep -v '^[[:space:]]*$' "$extra" > "$TMP/extra.pat"
  if [ ! -s "$TMP/extra.pat" ]; then record FAIL private-word-list "word list is empty"
  else
    npat=$(wc -l < "$TMP/extra.pat")
    # A pattern of 4 characters or fewer is a SHORT name. It is found unless it sits inside a word of
    # letters: it must be bounded by a non-letter (start, digit, "_", ".", "-", space, ...) or by a
    # change of case (camelCase: ZzaBlake, blakeZza). "zza_1", "zza1" and "ZzaBlake" are findings;
    # "wordzzaword" and "zzalpha" are not. Case-insensitive; letters are ASCII letters.
    # Longer patterns are used as written (case-insensitive substring).
    awk 'length($0) > 4' "$TMP/extra.pat" > "$TMP/extra.long"
    awk 'length($0) <= 4 { printf "%s(?:%s)", (n++ ? "|" : ""), $0 }' "$TMP/extra.pat" > "$TMP/extra.alt"
    : > "$TMP/extra.short"
    if [ -s "$TMP/extra.alt" ]; then
      printf '(?:(?<![A-Za-z])|(?<=[a-z])(?=[A-Z]))(?i:%s)(?:(?![A-Za-z])|(?<=[a-z])(?=[A-Z]))\n' "$(cat "$TMP/extra.alt")" > "$TMP/extra.short"
    fi
    # Both scans, merged. Findings are reported as PLACES only: file:line, or a line number in the
    # history dump, or a count of file names. The matching pattern and the text are never printed.
    wl_tree() {
      { [ -s "$TMP/extra.long" ] && xargs -0 -r grep -EiaIHn -f "$TMP/extra.long" < "$TMP/files0"
        [ -s "$TMP/extra.short" ] && LC_ALL=C xargs -0 -r grep -PaIHn -f "$TMP/extra.short" < "$TMP/files0"
        true; } 2>/dev/null | cut -d: -f1,2 | sort -u
    }
    wl_lines() {   # $1 = file; prints the numbers of the matching lines
      { [ -s "$TMP/extra.long" ] && grep -Eian -f "$TMP/extra.long" "$1"
        [ -s "$TMP/extra.short" ] && LC_ALL=C grep -Pan -f "$TMP/extra.short" "$1"
        true; } 2>/dev/null | cut -d: -f1 | sort -un
    }
    wl_tree > "$TMP/extra.tree"
    tr_hits=$(wc -l < "$TMP/extra.tree")
    : > "$TMP/extra.hist"
    git rev-parse --verify -q HEAD >/dev/null && wl_lines "$TMP/history" > "$TMP/extra.hist"
    hist_hits=$(wc -l < "$TMP/extra.hist")
    tr '\0' '\n' < "$TMP/files0" > "$TMP/names.txt"
    name_hits=$(wl_lines "$TMP/names.txt" | wc -l)
    gh_hits=0; [ -f "$TMP/gh.json" ] && gh_hits=$(wl_lines "$TMP/gh.json" | wc -l)
    if [ "$((tr_hits + hist_hits + ${name_hits:-0} + ${gh_hits:-0}))" = 0 ]; then record PASS private-word-list "0 hits, $npat pattern(s) read (tree, file names, history$([ -f "$TMP/gh.json" ] && echo ", GitHub description"))"
    else
      record FAIL private-word-list "$npat pattern(s) read; tree=$tr_hits history=$hist_hits file-names=${name_hits:-0} github=${gh_hits:-0}; patterns and matching text not shown"
      while IFS= read -r place; do printf '        [private-word-list:tree] %s\n' "$place"; done < <(head -50 "$TMP/extra.tree")
      while IFS= read -r ln; do printf '        [private-word-list:history] line %s of the history dump (git log --all -p)\n' "$ln"; done < <(head -50 "$TMP/extra.hist")
    fi
  fi
fi

# 9. media metadata
media="$(tr '\0' '\n' < "$TMP/files0" | grep -Eic '\.(jpe?g|png|webp|gif|mp4|mov|heic)$')"
if [ "${media:-0}" = 0 ]; then record PASS media-metadata "no image or video files tracked"
elif ! command -v exiftool >/dev/null; then record SKIP media-metadata "$media media file(s) but exiftool is not installed"
else
  bad=0
  while IFS= read -r f; do
    out="$(exiftool -a -s -GPS:all -Artist -Author -Creator -Comment -UserComment -Title -Copyright -Description -XMP:all -- "$f" 2>/dev/null | grep -c .)"
    [ "${out:-0}" -gt 0 ] && bad=$((bad + 1))
  done < <(tr '\0' '\n' < "$TMP/files0" | grep -Ei '\.(jpe?g|png|webp|gif|mp4|mov|heic)$')
  if [ "$bad" = 0 ]; then record PASS media-metadata "$media file(s), no GPS/author/comment/XMP"; else record FAIL media-metadata "$bad of $media file(s) carry metadata"; fi
fi

# 10. secret scanners
if command -v gitleaks >/dev/null; then
  if gitleaks detect --source . --no-banner --redact >"$TMP/gl1" 2>&1 && gitleaks detect --source . --no-git --no-banner --redact >"$TMP/gl2" 2>&1
  then record PASS gitleaks "history and working tree: no leaks found"
  else record FAIL gitleaks "gitleaks reported a finding or an error (run it by hand)"; fi
else record SKIP gitleaks "not installed"; fi
if [ "${HH_SKIP_TRUFFLEHOG:-0}" = 1 ]; then record SKIP trufflehog "skipped on request (HH_SKIP_TRUFFLEHOG=1): it cannot start under a memory cap"
elif command -v trufflehog >/dev/null; then
  if trufflehog git "file://$TOP" --no-update --fail --no-verification >"$TMP/th" 2>&1; then record PASS trufflehog "no findings"
  else record FAIL trufflehog "finding or error"; fi
else record SKIP trufflehog "not installed"; fi

echo
echo "summary: fails=$FAILS skips=$SKIPS"
[ "$FAILS" -gt 0 ] && exit 1
if [ "$SKIPS" -gt 0 ]; then
  [ -n "${HH_ALLOW_SKIP:-}" ] && echo "note: HH_ALLOW_SKIP is ignored; a check that did not run is never reported as clean"
  echo "result: NOT PROVEN (some checks did not run)"; exit 3
fi
echo "result: clean"; exit 0
