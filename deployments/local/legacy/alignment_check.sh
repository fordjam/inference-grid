#!/bin/bash
# Print the cross-repo state that must agree between the Air and the mini: remotes, every local
# branch's distance from its upstream (or NO UPSTREAM), dirty files, worktrees, plus the
# research-library ledger and the session-independent assets. Run on both machines, diff the output.
#   scripts/alignment_check.sh            # human-readable
#   scripts/alignment_check.sh --brief    # one line per branch, for diffing
BRIEF=${1:-}
repos=("$HOME/MyMonarch" "$HOME/vix-rs" "$HOME/projects/yt-research-mcp" "$HOME/projects/COT" "$HOME/projects/factory-frontend")
for r in "${repos[@]}"; do
  [ -d "$r/.git" ] || { echo "MISSING $r"; continue; }
  name=$(basename "$r"); remote=$(git -C "$r" remote get-url origin 2>/dev/null || echo "NO REMOTE")
  [ -z "$BRIEF" ] && echo "=== $name  ($remote)  HEAD=$(git -C "$r" rev-parse --abbrev-ref HEAD) $(git -C "$r" rev-parse --short HEAD)"
  git -C "$r" for-each-ref --format='%(refname:short) %(upstream:short) %(objectname:short)' refs/heads | while read -r b u sha; do
    if [ -n "$u" ]; then a=$(git -C "$r" rev-list --count "$u..$b" 2>/dev/null || echo '?'); bh=$(git -C "$r" rev-list --count "$b..$u" 2>/dev/null || echo '?'); state="ahead=$a behind=$bh"; else state="NO-UPSTREAM"; fi
    echo "$name $b $sha $state"
  done
  mod=$(git -C "$r" status --short | grep -vc '^??' || true); unt=$(git -C "$r" status --short | grep -c '^??' || true)
  echo "$name DIRTY modified=$mod untracked=$unt worktrees=$(git -C "$r" worktree list | wc -l | tr -d ' ')"
done
echo "library.db $( [ -f "$HOME/research-library/library.db" ] && shasum -a 256 "$HOME/research-library/library.db" | cut -c1-16 || echo MISSING)  ledger-rows $( [ -f "$HOME/research-library/logs/capacity.jsonl" ] && wc -l < "$HOME/research-library/logs/capacity.jsonl" | tr -d ' ' || echo 0)"
echo "codex-seats $(ls -d "$HOME"/.codex-seat* 2>/dev/null | wc -l | tr -d ' ')  env.local $( [ -f "$HOME/projects/yt-research-mcp/.env.local" ] && echo present || echo absent)  memory-files $(ls "$HOME/.claude/projects/-Users-fordjam/memory" 2>/dev/null | wc -l | tr -d ' ')"
