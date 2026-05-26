# 05 — done recap + no PR + clean cwd → ready_for_cleanup

Rule B1. The agent's last narrative explicitly says "Mission complete,
no follow-up." cwd_dirty and cwd_unpushed are both false (probe didn't
write any tracked files). No PR exists for this workspace's branch
(no fake-gh-bin, so any `gh pr view` returns nothing — Observer must
notice "no PR" via empty `head` lookup or `gh pr list --head` empty
array).

Note: this fixture has NO `fake-gh-bin/`. The runner relies on the
fact that `gh pr list --head <branch>` from a non-existent cwd will
fail or return empty, which Observer must interpret as "no PR for
this workspace".

Expected: `ready_for_cleanup`.
