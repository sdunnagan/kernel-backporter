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

Current version: **0.1.2**

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

Instead, it maintains a separate reference clone, by default:

```text
~/projects/kernel-backport/reference/cs10
```

That reference is refreshed from current official CS10 on each run unless
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
cp kernel-backporter-v0.1.2 ~/bin/kernel-backporter
chmod 755 ~/bin/kernel-backporter
```

Verify the installed version:

```bash
kernel-backporter --version
```

Expected:

```text
kernel-backporter 0.1.2
```

Every normal run also prints the version as its first line.

## Typical workflow

Always begin with a dry run:

```bash
kernel-backporter \
    -l stable \
    -j RHEL-223619 \
    -c e7e6902fbd19 \
    --target v7.0 \
    --dry-run
```

If the preflight is clean and the proposed sequence is correct, rerun without
`--dry-run`:

```bash
kernel-backporter \
    -l stable \
    -j RHEL-223619 \
    -c e7e6902fbd19 \
    --target v7.0
```

A successful dry run ends with output similar to:

```text
Preflight result: CLEAN
Sequence:
  1. e7e6902fbd19b25630cf6a258c44cb385f16b1c8 [requested] Drivers: hv: Provide arch-neutral implementation of get_vtl()

--dry-run specified; real working tree was not modified.
```

A successful real run then applies exactly the preflighted sequence to the
working tree.

On an interactive terminal, a clean preflight is shown in bold green and a
stopped preflight is shown in bold red. Patch-series statuses that are already
present in the working tree or current CS10 are also shown in green. Color is
disabled automatically when standard output is redirected, when `TERM=dumb`, or
when the `NO_COLOR` environment variable is set.

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
- preflight is clean;
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
  ~/projects/kernel-backport/reference/linux-stable
  ~/projects/kernel-backport/reference/linux-next
  ~/projects/kernel-backport/reference/cs10

Reports:
  ~/backports/kernel-backporter/<requested-upstream-sha>/report.txt
  ~/backports/kernel-backporter/<requested-upstream-sha>/report.json

Saved upstream diffs:
  ~/backport_commit_diffs/<upstream-sha>.diff

Downstream format-patch output:
  ~/
```

The output directories can be changed with `--reference-root`, `--report-dir`,
`--diff-dir`, and `--patch-dir`.

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
    Upstream target boundary, for example v7.0. Required.

--repo PATH
    Downstream working repository. Default: $KERNEL_SRC_DIR, then current directory.

--reference-root PATH
    Tool-managed reference repositories. Default: ~/projects/kernel-backport/reference.

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

A clean preflight means the proposed upstream sequence applies textually to the
current working tree and, if configured, passes the requested check command. It
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
