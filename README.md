# kernel-backporter

`kernel-backporter` is a dependency-aware Linux kernel backport orchestrator for
downstream RPM-oriented kernel trees such as CentOS Stream / RHEL kernel work.

It preserves the useful workflow of the older `centos-kernel-backporter` script,
but adds a conservative preflight stage before touching the real working tree. A
requested upstream commit is replayed in an isolated clone, missing upstream
prerequisites may be discovered and tested, current CentOS Stream is checked for
drift, and patch-series context is reported.

The central safety rule is simple:

> `kernel-backporter` may add known upstream prerequisite commits, but it never
> edits conflicted source code automatically.

Current version: **0.2.9**

## What it does

For one requested upstream commit, `kernel-backporter`:

- refreshes tool-managed upstream and current-CS10 reference repositories;
- compares the requested commit and discovered prerequisites against both the
  current working tree and current official CS10;
- detects already-downstream commits using exact SHA/provenance markers and
  stable `patch-id` fallback;
- discovers the original lore-linked patch series when possible and reports
  which peers are already present or still missing;
- preflights the requested backport in an isolated clone of the current working
  tree;
- when a cherry-pick conflicts, searches for plausible upstream prerequisites
  using `Fixes:` relationships, hunk/preimage provenance, nearby context, and
  symbol-definition provenance;
- retries by adding ordinary upstream prerequisite commits only;
- minimizes automatically discovered prerequisites after a clean replay;
- optionally runs a build or test command in the isolated clone;
- applies the proven sequence to the real working tree only after a clean
  preflight;
- creates downstream-formatted commit messages with JIRA, full upstream commit
  context, cherry-pick provenance, and the backporter's `Signed-off-by`;
- writes human-readable and JSON reports, upstream diffs, format-patch output,
  and a detailed conflict dossier when automatic replay cannot be proven clean.

## Why current CS10 is refreshed

The working tree may be a fork with an in-progress backport series.
`kernel-backporter` does **not** reset or update that working tree.

Instead, it maintains separate tool-managed reference clones under the XDG
cache directory. With the normal Linux defaults, current CS10 lives at:

```text
~/.cache/kernel-backporter/reference/cs10
```

If `XDG_CACHE_HOME` is set to an absolute path, that directory is used instead
of `~/.cache`. `KERNEL_BACKPORTER_REFERENCE_ROOT` can override the complete
reference root, and `--reference-root` takes precedence over all defaults.

The current-CS10 reference is refreshed from official CS10 on each run unless
`--skip-refresh` is used. This lets the tool detect cases such as:

```text
working tree:      prerequisite missing
current cs10/main: prerequisite already present
```

or:

```text
working tree:      requested commit missing
current cs10/main: requested commit already present
```

In the latter case, the tool stops and tells the user to sync/rebase instead of
creating a duplicate downstream backport.

The separate current-CS10 reference is therefore advisory and protective; the
real working tree remains the authority for what will actually be applied.

## Requirements

- Python 3
- Git
- a clean downstream kernel working tree
- an upstream remote or explicit upstream URL
- a downstream CS10 remote or explicit downstream URL
- configured Git `user.name` and `user.email`, unless `--signoff-name` and
  `--signoff-email` are supplied

The tool uses only the Python standard library.

## Installation

Install the executable somewhere in `PATH`, for example:

```bash
cp kernel-backporter ~/bin/kernel-backporter
chmod 755 ~/bin/kernel-backporter
```

Verify the installed version:

```bash
kernel-backporter --version
```

Expected:

```text
kernel-backporter 0.2.9
```

Every normal run also prints the version as its first line.

## Automatic continuation (0.2.7)

There is **no `--resume` option**. Invoke the exact same backporter command
for the first and every subsequent batch in a planning cycle. The tool looks
for the execution journal that belongs to the selected plan and repository:

- No journal: start at the frozen plan baseline and create one on the first
  real execution.
- Valid journal: verify its plan identity, Git branch, recorded commit sequence,
  cursor, and last Git HEAD; then continue from its next unprocessed entry.
- Blocked journal plus one manual resolution commit: reconcile only if the
  new commit has the exact upstream cherry-pick provenance trailer.
- Dirty tree, unrelated HEAD changes, hard reset, corrupt journal, or changed
  plan: refuse execution. Worksheet `backporter-status` and
  `downstream-commit` fields cannot override Git and the journal.

A `--dry-run` with an existing journal starts its simulated checks at the
journal cursor but does not change the journal or living worksheet. If a
blocked commit was manually resolved and Git HEAD advanced, perform a normal
run first so that the backporter can validate and record that resolution.

```bash
# Both commands are identical, whether this is batch 1 or batch 20.
kernel-backporter -l stable -j RHEL-223619 --max-attempts 6
kernel-backporter -l stable -j RHEL-223619 --max-attempts 6
```

**Cycle switching safeguard:** Workspace discovery still selects the newest
completed planner run, but a new cycle is **not automatically started** when an
older cycle for this workspace, repository, and branch has an unfinished journal.
The backporter reports the older cycle and asks for an explicit `--results-dir`.
After you deliberately start the newer cycle once, its journal identifies it as
selected and future normal invocations can continue it automatically. Completed
older journals do not block new plans. Explicit `--plan`/`--results-dir` always
select the requested cycle; Git baseline checks still apply.

To *restart* the same plan after a hard reset, first verify the exact plan
baseline and a clean tree, then explicitly archive the stale journal (see
"Read-only plan status" below). The tool never silently discards history.
Previous scripts containing `--resume` must simply remove that option.

## Automatic workspace discovery and completion sounds (0.2.6+)

Use the same `$KERNEL_WORKSPACE_DIR` convention as kernel-builder and
kernel-backport-planner. **There is no need to export a separate plan directory.**
When no `-c`, `--plan` or `--results-dir` is supplied, the backporter selects
**the highest-numbered completed** cycle under:

```text
$KERNEL_WORKSPACE_DIR/backport-plans/
    backport_plan_29/
        backport_plan.json       # frozen machine-readable execution plan
        backport_worksheet.txt  # living, editable engineering worksheet
    backport_plan_29.zip        # planner's immutable completion snapshot
```

The next cycle can be numbered `backport_plan_30` or higher. Selection is by
**numeric run number**, not modification time. A cycle must have both primary
files and its completed ZIP snapshot; an interrupted or partially packaged
newer run is ignored. The selected directory is printed on every invocation.
The planner's JSON is never changed by the backporter.

```bash
export KERNEL_WORKSPACE_DIR="$HOME/projects/rhel-223619"
export CS_VERSION=10

# Source tree is also derived from the workspace (unless --repo is supplied).
"$KERNEL_BACKPORTER" --status

# Preflight six TAKE attempts, without altering journal or worksheet.
"$KERNEL_BACKPORTER" -l stable -j RHEL-223619 \
    --max-attempts 6 --dry-run

# Apply six TAKE attempts, updating the journal and living worksheet.
"$KERNEL_BACKPORTER" -l stable -j RHEL-223619 \
    --max-attempts 6

# Inspect progress and technical notes in the chosen worksheet.
"$KERNEL_BACKPORTER" --status
less "$KERNEL_WORKSPACE_DIR/backport-plans/backport_plan_29/"\
"backport_worksheet.txt"

# Continue from the journal, using exactly the same command.
"$KERNEL_BACKPORTER" -l stable -j RHEL-223619 \
    --max-attempts 6
```

**Important:** If a newer completed planning cycle appears while you are
still executing an unfinished older one, the tool refuses to automatically
start the newer cycle. To continue the older cycle, provide
`--results-dir "$KERNEL_WORKSPACE_DIR/backport-plans/backport_plan_29"`.
To deliberately start the newer cycle after establishing its new Git baseline,
select it once with `--results-dir`. Future normal runs then continue it.
Explicit `-c`, `--plan`, and `--results-dir` always override automatic
selection. The old `KERNEL_BACKPORT_PLANNER_DIR` variable is supported only
when `$KERNEL_WORKSPACE_DIR` is unset; the workspace takes precedence.

For normal runs (including dry runs), the backporter plays a **single
completion sound** using the same Freedesktop sounds as kernel-builder:

- Success: `/usr/share/sounds/freedesktop/stereo/complete.oga`
- Failure, blocked backport, or interruption:
  `/usr/share/sounds/freedesktop/stereo/service-logout.oga`

It tries `canberra-gtk-play`, `pw-play`, then `paplay`. Missing sound players,
files, or audio-server connectivity never affect the exit status. To choose
another sound directory, set `KBB_SOUND_DIR`. `--status`, `--version`, and
help are silent.

## Live Backport Worksheet (backporter 0.2.9)

`kernel-backporter` 0.2.9 consumes the field-based worksheet produced by
`kernel-backport-planner` 0.4.30 or newer. No worksheet version number is
required. Each executable entry has an unmodified, comment-free header and
an `advisor: TAKE|SKIP|REVIEW` field matching
the immutable `backport_plan.json`. Trailing `// Take`, `// Done`, and similar
header status comments are **not supported**. The backporter reads entries
only from the `BACKPORT WORKSHEET (oldest first)` section. Commit-like entries
in informational appendices, such as `SAME PATCH SERIES` and
`ALREADY DOWNSTREAM`, are not executable and are never rewritten.

Three independently owned sets of fields keep provenance clear:

- **Planner-owned:** commit headers, `advisor:`, `advisor-rationale:`, and
  other dependency and platform evidence. Do not edit planner-owned fields;
  validation checks the header order and advisor decisions against JSON.
- **Engineer-owned (optional):** `engineer-decision: TAKE` or `SKIP`, and
  `engineer-rationale:`. These may be edited between backporter invocations.
- **Backporter-owned:** `backporter-status:`, `downstream-commit:`, and wrapped
  `backporter:` conflict diagnostics. These are regenerated from the journal
  and never accepted as proof of execution.

For example, after applying a commit, the worksheet contains:

```text
40f742b07ab2 [DEPENDENCY] ("rk3576-armsom-sige5 board")
      advisor: TAKE
      advisor-rationale: Relevant to the selected backport scope.
      backporter-status: APPLIED
      downstream-commit: 141fc6cb843e0000000000000000000000000000
```

The displayed downstream SHA is a full Git commit identity, not an input.
The journal stores the actual SHA and Git history must agree.

### Engineer decision overrides

Before executing an unprocessed commit, add exactly one engineer decision and
optionally an engineer rationale to that commit's worksheet block:

```text
c37393b36c1d [DEPENDENCY] ("arm64: dts: rockchip: refactor common Orange Pi 5 board")
      advisor: TAKE
      advisor-rationale: Upstream refactoring identified by the advisor.
      engineer-decision: SKIP
      engineer-rationale: Board-specific change outside this MR's scope.
```

An explicit `engineer-decision: TAKE` can promote an advisor SKIP or REVIEW.
The original advisor recommendation stays intact. Engineer SKIPs are recorded
in the journal as `SKIPPED_ENGINEER` with their rationale, and appear as
`backporter-status: SKIPPED_ENGINEER` in the worksheet. Policy skips use
`SKIPPED_POLICY`, and both kinds of skips avoid consuming `--max-attempts`.

**Dependency safeguard:** A skipped commit that appears as an explicit
`advisor_prerequisites` prerequisite of a still-pending TAKE blocks execution.
Restore the prerequisite to TAKE, skip the dependent commit too, or investigate
the dependency. The tool never automatically prunes a dependency chain or
changes the planner's JSON. Already processed journal entries cannot be
reinterpreted by editing engineer decisions; undoing a previously applied
commit requires deliberate Git work and journal reconciliation.

When preflight fails, the worksheet shows:

```text
c37393b36c1d [DEPENDENCY] ("arm64: dts: rockchip: refactor common Orange Pi 5 board")
      advisor: TAKE
      backporter-status: BLOCKED
      backporter: BLOCKED preflight failed: ...
```

`backporter:` diagnostics wrap at **80 columns**, including their indentation.
The full diagnostic, file list, and dossier path remain in the journal and
conflict dossier. After a manual Git backport with the exact upstream
cherry-pick provenance trailer, the next ordinary invocation reconciles it
and writes `backporter-status: APPLIED_MANUALLY` and `downstream-commit:`.
Alternatively, an engineer may SKIP an unresolved blocker if dependency
checks permit it, without changing Git.

Worksheet updates are atomic, preserve other annotations, and remove stale
machine-owned fields. The journal is written first; a failed worksheet write
can be recovered on the next invocation. The backporter detects engineer
changes during a batch and refuses to overwrite concurrently edited worksheets.
Do not manually edit machine-owned status fields. `--status` and `--dry-run`
never modify the worksheet or journal.

A hard Git reset does not clear the journal; `--status` reports divergence.
After verifying the baseline and deliberately archiving a stale journal, a
new execution regenerates machine-owned worksheet status from the new journal.

`--results-dir DIR` selects a completed planning cycle and its standard JSON
and worksheet files. `--plan FILE` selects an explicit JSON plan; provide
`--worksheet FILE` when you want the matching worksheet maintained live.
Single-commit mode (`-c SHA`) does not use planner worksheets.

**Migration note:** An old worksheet with trailing `// Take` or `// Done`
headers must be converted to the current named-field layout using the matching
planner JSON, without changing its advisor decisions. The backporter ignores
and removes an obsolete `worksheet-format: 2` line when updating the worksheet.
An existing journal can continue only if the plan digest, Git
baseline, branch, and journal state still match. Do not swap a different plan
into an active session or change an active worksheet during execution.

## Read-only plan status (0.2.4)

Use `--status` with `--plan` or a selected planning-cycle directory to
inspect journal progress **without**
fetching/updating references, indexing the kernel tree, changing Git, or
writing a journal or progress report. Jira is optional for this command.
Run it from the intended downstream kernel repository (or pass `--repo`).

```bash
cd ~/projects/rhel-223619/centos-stream-10
PLAN_ROOT="$HOME/Documents/red_hat_notes/tasks/305_arm_rebase_upstream_v7"
kernel-backporter --plan \
    "$PLAN_ROOT/backport_plan_28/backport_plan_28.json" --status
```

Status displays total TAKE and SKIP/REVIEW entries, journal cursor,
recorded applied/present/skipped counts, remaining TAKE commits, the next
TAKE commit, blocked state, and recorded versus actual Git HEAD. It loads
only the plan, journal, and local Git metadata. `--journal FILE` can select a
nonstandard journal path. `--status` cannot be combined with execution flags.
It exits 0 for a readable, consistent state (including a recorded blocker),
2 for mismatched HEAD or dirty worktree, and 1 for a corrupt or mismatched
journal.

**Important after `git reset --hard`:** Git reset does not reset the
backporter's journal. If the journal records six applied commits but the
branch has returned to the plan baseline, `--status` reports `DIVERGED` and
explicitly warns that those recorded commits are **not verified** on the
current branch. Normal execution cannot reuse that stale journal. The status command never deletes, rewrites, or reconciles it.

To replay the same plan from scratch, first verify that the working HEAD is
exactly the plan baseline and the tree is clean; then **archive** the stale
journal under a distinct name before starting a new batch. For example:

```bash
journal="$HOME/backports/kernel-backporter/plans/"\
"backport_plan_28-548db77a06c1/journal.json"
test -f "$journal" || exit 1
test ! -e "$journal.before-reset" || exit 1
mv -- "$journal" "$journal.before-reset"
```

This is an explicit user action, not something `--status` performs. Preserve
archived journals for historical provenance. The next real invocation will
create a new journal for the same plan and current repository.

## Plan-driven backports (0.2.0)

Plan mode executes bounded batches from immutable JSON generated by the
planner. The planner supplies candidate decisions, the engineer may override
those through the named-field worksheet, and `kernel-backporter` never invents new
dependencies in plan mode. Generate a matching `backport_plan.json` and
`backport_worksheet.txt` with `kernel-backport-planner` 0.4.30 or newer.

Start on the same clean CS10 tree and at the exact downstream commit recorded in
`downstream`/`replay_start` in the JSON. With a completed numbered planning
cycle and its worksheet:

```bash
cd "$KERNEL_WORKSPACE_DIR/centos-stream-$CS_VERSION"
PLAN_DIR="$KERNEL_WORKSPACE_DIR/backport-plans/backport_plan_01"

# Preview the next six TAKE attempts without changing Git or the journal.
kernel-backporter -j RHEL-223619 \
    --results-dir "$PLAN_DIR" --max-attempts 6 --dry-run

# Apply a reviewed batch, then inspect the journal and worksheet.
kernel-backporter -j RHEL-223619 \
    --results-dir "$PLAN_DIR" --max-attempts 6
kernel-backporter --results-dir "$PLAN_DIR" --status
```

Repeating the same apply command continues from the persisted journal.
Without `--results-dir`, the newest completed planning cycle is discovered
from `$KERNEL_WORKSPACE_DIR/backport-plans` (subject to the unfinished-cycle
safety guard). For an explicit JSON path, use `--plan FILE` and optionally
`--worksheet FILE` to synchronize its matching worksheet.

`--max-attempts` counts **eligible TAKE attempts**, including a failed preflight,
not successful commits; its default is one. Policy SKIP and REVIEW entries,
engineer SKIP overrides, and
commits already proven present do not count. There is no special `--commit 0`:
`--commit SHA` and `--plan FILE` are mutually exclusive. Existing single-commit
behavior is unchanged.

The executor reads `order` rather than iterating the JSON `commits` dictionary.
It verifies all ordered entries are selected and missing, the plan's target
exists, and the **working HEAD matches the frozen plan baseline** on the first
run. On each later invocation it verifies the journal's plan hash, repository,
branch, target, recorded entries and last committed HEAD. It checks
explicit advisor prerequisites before each TAKE. A missing prerequisite, a SKIP/REVIEW prerequisite, a conflict, an empty
cherry-pick, or a changed working tree **stops the batch**. It will not silently
add another upstream prerequisite or edit conflicted files.

All attempted TAKE commits are cherry-picked into one cumulative isolated clone
first. `--dry-run` simulates the **cumulative** changes, without changing the
real working tree or journal. A real run preflights each commit, then creates a
normal downstream-formatted signed-off Git commit. After each success it writes
an atomic journal under:

```text
~/backports/kernel-backporter/plans/<plan-name>-<plan-digest>/journal.json
```

You can override the journal location with `--journal`. Never edit the journal
manually. Each entry records the upstream SHA, its disposition, and the produced
downstream SHA when applicable. Running the same plan again automatically
continues from that journal rather than overwriting it.

### When a batch stops

A preflight conflict never touches the working tree. Investigate the reported
files, then use the **single-commit** mode or a deliberate manual cherry-pick to
resolve the blocked commit. If you commit a manual resolution, include the
exact provenance trailer:

```text
(cherry picked from commit <full 40-character upstream SHA>)
```

On the next normal invocation, one such intervening manually committed
resolution is reconciled against the blocked upstream SHA. The tool refuses
unrelated HEAD changes, missing provenance, or multiple unexpected commits. If a real-tree cherry-pick
unexpectedly conflicts, it leaves the repository for manual inspection rather
than resetting it. Resolve and commit with provenance before resuming.

The planner's `advisor_prerequisites` are treated as explicit requirements,
including when an engineer overrides decisions in the worksheet.
Some recommendations in run 17 reference commits **outside its ordered TAKE
set**. They will halt plan mode until inspected and resolved. A planner's
replay-clean label is only an advisory prediction, not a guarantee that a TAKE
commit will apply or build on the evolving CS10 tree.

### Builds

Version 0.2.0 **does not invoke kernel-builder automatically**. Run it manually
at a known downstream HEAD after a small batch, and after conflict resolution.
Record the tested HEAD, architecture/configuration, and build log externally.
`--check-command` belongs to the existing single-commit workflow and is
rejected in planned mode. Automatic build checkpoints can be added later without
coupling the two projects.

### Tests

```bash
python3 -m unittest -v test_kernel_backporter.py
```

The test suite uses disposable local Git repositories and does not modify CS10.

## Typical workflow

Always begin with a dry run:

```bash
kernel-backporter \
    -l stable \
    -j RHEL-223619 \
    -c e7e6902fbd19 \
    --dry-run
```

If the preflight succeeds and the proposed sequence is correct, rerun without
`--dry-run`:

```bash
kernel-backporter \
    -l stable \
    -j RHEL-223619 \
    -c e7e6902fbd19
```

In single-commit mode, omitting `--target` selects the fetched upstream
`origin/HEAD` as the analysis boundary. The local reference checkout's `HEAD`
may be older than the latest fetched branch, so the remote-tracking HEAD is
preferred. If it is unavailable, the backporter warns and uses local `HEAD`.
The selected ref and its resolved SHA are printed and recorded in the report.
An explicit `--target` still pins the analysis boundary for reproducibility.
The requested SHA must be reachable from that boundary; otherwise the tool
stops instead of analyzing unrelated upstream history. In plan mode, the
planner's JSON target remains authoritative.

A successful dry run ends with output similar to:

```text
Preflight result: SUCCESS
Sequence:
  1. e7e6902fbd19b25630cf6a258c44cb385f16b1c8 [requested] Drivers: hv: Provide arch-neutral implementation of get_vtl()

--dry-run specified; real working tree was not modified.
```

A successful real run then applies exactly the preflighted sequence to the
working tree.

On an interactive terminal, a successful preflight is shown as `SUCCESS` in
bold yellow and a stopped preflight is shown in bold red. A successful real
backport is reported as `Backport result: COMMITTED` in bold green.
Patch-series statuses that are already present in the working tree or current
CS10 are also shown in green. If the requested commit is already represented in
the working tree, the tool prints a bold yellow notice and exits immediately
without running or reporting a preflight. Color is disabled automatically when
standard output is redirected, when `TERM=dumb`, or when the `NO_COLOR`
environment variable is set.

## Patch-series discovery

When the requested upstream commit contains a lore `Link:` whose message ID
looks like part of a `git send-email` series, `kernel-backporter` searches the
local upstream history for peers from the same series.

Example:

```text
Patch-series discovery: inferred-from-lore-message-id
  02  13423063c7cb  PRESENT-WORKTREE      arm64: kvm, smccc: Introduce and use API for getting hypervisor UUID
  03  36ebd328a57a  MISSING               arm64: hyperv: Use SMCCC to detect hypervisor presence
  04  f41ceff17486  MISSING               Drivers: hv: Enable VTL mode for arm64
  05  e7e6902fbd19  REQUESTED             Drivers: hv: Provide arch-neutral implementation of get_vtl()
  ...
```

Series statuses mean:

- `REQUESTED` — the commit explicitly requested with `-c`.
- `PRESENT-WORKTREE` — the peer is already represented in the current working
  tree.
- `PRESENT-CS10-TODAY` — the peer is absent from the working tree but
  represented in current official CS10.
- `MISSING` — the peer was not found in either the working tree or current CS10.

`MISSING` means the commit is available for consideration as a backport; it does
**not** mean the tool recommends taking it.

Patch-series peers are informational only. A peer is never auto-backported
merely because it belongs to the same series.

## Downstream identity detection

The tool indexes both the working-tree history and current-CS10 history and
recognizes upstream identity using:

1. exact commit SHA;
2. explicit `(cherry picked from commit <sha>)` provenance;
3. `Upstream commit <sha>` provenance;
4. stable-style `commit <sha> upstream.` provenance;
5. RHEL-style embedded `commit <sha>` provenance;
6. stable `git patch-id` equality as a fallback.

The large downstream provenance index is streamed rather than loaded into one
giant Python string, and historical commit-message bytes are decoded with
`surrogateescape` so odd legacy metadata does not crash the indexer.

Use `--no-patch-id` to disable the patch-id fallback.

## Dependency discovery

The requested commit is always the starting point. A `Fixes:` target may be
seeded as an initial prerequisite when it is an upstream ancestor of the
requested commit and is not already downstream.

If replay conflicts, the tool scores possible prerequisite commits using
evidence such as:

- `Fixes:` targets;
- commits blamed for the exact preimage of conflicting hunks;
- nearby hunk context;
- commits that introduced functions, macros, structures, typedefs, or other
  symbols newly used by the patch.

It adds the strongest candidate one at a time and reruns the isolated replay.
This makes each addition an experiment rather than treating every commit in a
file's history as a dependency.

Automatically added dependencies must be ordinary non-merge upstream ancestors
of the requested commit. Upstream merge commits are never automatically
cherry-picked.

After a clean replay, automatically discovered prerequisites are tested for
removal so nonessential candidates can be discarded.

If the tool cannot prove a clean prerequisite sequence, it stops. It does not
synthesize or edit a conflict resolution.

## Isolated preflight

Preflight is performed in a temporary shared clone of the real downstream
repository, checked out at the working tree's original `HEAD`.

For each selected upstream commit, the tool performs a no-commit cherry-pick.
Successful steps are committed only inside the disposable simulation clone so
subsequent prerequisites/requested commits see the correct evolving tree.

The simulation directory is removed after each run.

If `--check-command` is supplied, the command runs in that isolated clone after
a conflict-free replay:

```bash
kernel-backporter \
    -l stable \
    -j RHEL-223619 \
    -c <upstream-sha> \
    --target v7.0 \
    --dry-run \
    --check-command 'make -j16 ARCH=arm64 olddefconfig Image'
```

A failed build/test is not permission for the tool to invent a source-code fix.
It may perform a narrow symbol-prerequisite recovery pass, otherwise it stops
and reports the failure.

## Applying to the real working tree

The real working tree is modified only when:

- `--dry-run` is not specified;
- preflight succeeds;
- the working `HEAD` has not changed since analysis began; and
- the working tree is still clean.

The preflighted sequence is then applied oldest-first.

If a real-tree cherry-pick unexpectedly fails even though preflight succeeded,
the tool stops and leaves the conflict in the working tree for inspection rather
than guessing at a resolution.

## Downstream commit-message format

For each applied upstream commit, `kernel-backporter` creates a downstream
commit with the configured downstream Git identity as author/committer and
embeds the original upstream commit in the message.

Example:

```text
Drivers: hv: Provide arch-neutral implementation of get_vtl()

JIRA: https://issues.redhat.com/browse/RHEL-223619

commit e7e6902fbd19b25630cf6a258c44cb385f16b1c8
Author: Roman Kisel <romank@linux.microsoft.com>
Date:   Mon Apr 28 14:07:35 2025 -0700

    Drivers: hv: Provide arch-neutral implementation of get_vtl()

    <full upstream commit description and trailers>

(cherry picked from commit e7e6902fbd19b25630cf6a258c44cb385f16b1c8)
Signed-off-by: Steve Dunnagan <sdunnaga@redhat.com>
```

This makes the downstream backporter, original upstream author, exact upstream
commit, and JIRA association explicit.

## Output files

Default locations are:

```text
Reference repositories:
  ~/.cache/kernel-backporter/reference/linux-stable
  ~/.cache/kernel-backporter/reference/linux-next
  ~/.cache/kernel-backporter/reference/cs10

Reports:
  ~/backports/kernel-backporter/<requested-upstream-sha>/report.txt
  ~/backports/kernel-backporter/<requested-upstream-sha>/report.json

Saved upstream diffs:
  ~/backport_commit_diffs/<upstream-sha>.diff

Downstream format-patch output:
  ~/
```

The reference root can also be set with
`KERNEL_BACKPORTER_REFERENCE_ROOT`. Otherwise it follows `XDG_CACHE_HOME`,
falling back to `~/.cache`. The other output directories can be changed with
`--report-dir`, `--diff-dir`, and `--patch-dir`.

### Report contents

The text/JSON reports include, as applicable:

- requested commit and subject;
- upstream target;
- original working-tree `HEAD`;
- current-CS10 `HEAD`;
- proposed/applied prerequisite sequence and reasons;
- preflight status;
- check-command result;
- unresolved conflict details;
- dependencies found in current CS10 but absent from the working tree;
- patch-series peers and their downstream status;
- later upstream fixes carrying `Fixes:` references to the requested commit;
- downstream commits created by a real run;
- conflict-dossier location.

## Conflict dossier

When a clean sequence cannot be proven, the tool writes an inspection dossier
under:

```text
~/backports/kernel-backporter/<requested-upstream-sha>/
```

Depending on the conflict, it may contain:

- `requested.diff`;
- `conflict-summary.txt`;
- `candidate-dependencies.txt`;
- `later-downstream-context.txt`;
- `later-merge-risks.txt`;
- per-file snapshots of upstream parent/after/target state;
- per-file working downstream state;
- per-file current-CS10 state;
- upstream file history.

The dossier is intended to support manual engineering when automatic
upstream-prerequisite replay is no longer sufficient.

## CLI reference

```text
-c, --commit SHA
    Upstream commit to backport. Required.

-j, --jira ID
    Jira ID, for example RHEL-223619. Required.

-l, --linux-repo {stable,next}
    Upstream reference tree. Default: stable.

--target TAG_OR_COMMIT
    Optional upstream analysis boundary for single-commit mode, for example
    v7.3. By default, use the refreshed upstream origin/HEAD (falling back to
    the local HEAD if no origin/HEAD is available). The resolved SHA is
    recorded in reports. Plan mode obtains its target from the validated JSON.

--repo PATH
    Downstream working repository. Default: workspace/centos-stream-$CS_VERSION
    when KERNEL_WORKSPACE_DIR and CS_VERSION are set, then $KERNEL_SRC_DIR,
    then current directory.

--reference-root PATH
    Tool-managed reference repositories. Default precedence:
    $KERNEL_BACKPORTER_REFERENCE_ROOT, then
    $XDG_CACHE_HOME/kernel-backporter/reference, then
    ~/.cache/kernel-backporter/reference.

--upstream-url URL
    Override the discovered upstream clone URL.

--downstream-url URL
    Override the discovered downstream/CS10 clone URL.

--downstream-remote NAME
    Working-tree remote used to discover the downstream URL. Default: cs10.

--downstream-branch NAME
    Current downstream branch. Default: main.

--skip-refresh
    Do not fetch/update the tool-managed reference repositories.

--dry-run
    Perform analysis, series reporting, dependency discovery, preflight, and reports without modifying the real working tree.

--no-patch-id
    Disable stable patch-id fallback for downstream identity detection.

--series / --no-series
    Enable or disable lore/message-ID patch-series reporting. Enabled by default.

--check-command COMMAND
    Run a build/test shell command in the disposable replay clone after a clean sequence.

--context-lines N
    Number of nearby context lines used for conflict provenance. Default: 3.

--max-symbols N
    Maximum newly used symbols considered during dependency discovery. Default: 40.

--max-rounds N
    Maximum dependency-discovery/replay rounds. Default: 12.

--max-later-context N
    Maximum later upstream context commits inspected. Default: 12.

--max-merge-risks N
    Maximum later merge risks reported. Default: 10.

--report-dir PATH
    Report root. Default: ~/backports/kernel-backporter.

--diff-dir PATH
    Saved upstream diff directory. Default: ~/backport_commit_diffs.

--patch-dir PATH
    Directory for downstream format-patch files. Default: ~/.

--jira-url URL
    Jira base URL. Default: https://issues.redhat.com/browse.

--signoff-name NAME
--signoff-email EMAIL
    Override Git user.name/user.email for the downstream Signed-off-by line.

--debug
    Print Git commands and additional diagnostics.

--version
    Print the installed kernel-backporter version.
```

## Exit codes

- `0` — analysis/preflight succeeded; on a real run, the preflighted sequence
  was applied.
- `1` — tool, Git, validation, or setup error.
- `2` — unresolved replay conflict; manual resolution is required.
- `3` — optional build/test command failed without an unresolved cherry-pick
  conflict.
- `130` — interrupted with Ctrl-C / `KeyboardInterrupt`.

## Recommended usage discipline

`kernel-backporter` is an engineering assistant, not a policy engine.

A successful preflight means the proposed upstream sequence applies textually
to the current working tree and, if configured, passes the requested check
command. It
does not prove that every upstream feature belongs in the downstream product.

Likewise:

- `MISSING` in a patch-series report means “not represented downstream,” not
  “take this patch.”
- an automatically discovered prerequisite is evidence-backed, but still
  deserves subsystem review;
- later series peers and later `Fixes:` commits are context for the engineer,
  not automatic backport instructions;
- successful application should still be followed by the normal kernel
  configure/build/test and review process.

The intended workflow is therefore:

```text
choose upstream commit
        |
        v
kernel-backporter --dry-run
        |
        +--> inspect patch series / CS10 drift / prerequisites
        |
        v
clean and semantically appropriate?
        |
        v
kernel-backporter (real run)
        |
        v
inspect commit + build/test + continue rebase
```

## Relationship to kernel-backport-planner

`kernel-backport-planner` and `kernel-backporter` solve different parts of the
workflow:

- **kernel-backport-planner** scans a broad upstream range and focus paths to
  produce a candidate backport plan, including dependency/context candidates.
- **kernel-backporter** takes one selected upstream commit and performs detailed
  per-commit provenance checking, series reporting, dependency discovery,
  isolated replay, and optional real application.

A typical large subsystem rebase uses the planner first, then works through
selected commits with `kernel-backporter` one at a time.
