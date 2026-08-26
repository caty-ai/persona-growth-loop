# Observation collector wiring

The pgl#5 collector is a nightly, read-only scan of Claude Code transcripts. The
launchd file is a template only; installing it is an operator action. Its 00:05
schedule assumes the host timezone is JST; on another host, adjust the calendar
hour to the local equivalent. The collector assigns records to their fixed JST
timestamp date, and the default CLI run processes the completed previous JST day;
`--date` overrides that bucket explicitly.

For WSL2/Linux systemd setup and GNU command variants, see
[WSL2 / Linux wiring](linux-wiring.md).

Running just after midnight lets the collector process the prior day's activity
in full, and keeps it on the same calendar day as the Monday 01:30 weekly mirror
-- which is what makes the mirror's collector-liveness check
(`mirror/weekly.py::_collector_liveness`, frozen by #14 G14b) see a marker
`date` equal to `weekly run_day - 1`, inside the accepted `{run_day, run_day -
1}` window (#40; see #38 for the identical fix on the Luca face). The
previously shipped 23:45 slot put the collector's most recent completed run a
full calendar day earlier relative to the Monday mirror, landing the marker
`date` on `run_day - 2` and reporting BROKEN on every run once any
staged/adopted phrase existed.

## Install and remove

Set the checkout and Tier L home, create private log storage, render the two
placeholders, then bootstrap the per-user agent:

```sh
python3 -c "import sys,unicodedata; assert sys.version_info[:2] == (3, 14), sys.version; assert unicodedata.unidata_version == '16.0.0', unicodedata.unidata_version"
PGL_REPO=/absolute/path/to/persona-growth-loop PGL_HOME="$HOME/.persona-growth-loop"; install -d -m 700 "$PGL_HOME/logs" "$HOME/Library/LaunchAgents"; sed -e "s|__PGL_REPO__|$PGL_REPO|g" -e "s|__PGL_HOME__|$PGL_HOME|g" "$PGL_REPO/templates/launchd/ai.caty.pgl.obs-collector.plist" > "$HOME/Library/LaunchAgents/ai.caty.pgl.obs-collector.plist"; launchctl bootstrap gui/501 "$HOME/Library/LaunchAgents/ai.caty.pgl.obs-collector.plist"
```

The first command must succeed: the collector requires CPython 3.14.x (UCD 16.0.0).
The template uses `/usr/bin/env python3` with a fixed PATH covering Apple and
common Homebrew locations.

Unload and remove the rendered agent with:

```sh
launchctl bootout gui/501 "$HOME/Library/LaunchAgents/ai.caty.pgl.obs-collector.plist"; rm "$HOME/Library/LaunchAgents/ai.caty.pgl.obs-collector.plist"
```

Use `launchctl kickstart -k gui/501/ai.caty.pgl.obs-collector` for a manual smoke
run after installation. The template deliberately does not install itself.

## Re-wire an already-loaded agent

Use this section, instead of the render/bootstrap steps above, when the agent
is already loaded under an older plist (for example, the previously shipped
23:45 schedule) and needs to pick up a changed
`templates/launchd/ai.caty.pgl.obs-collector.plist`. Overwriting the rendered
plist file on disk is not enough by itself: `launchctl bootstrap` loads a
plist into launchd's in-memory job table once, and an already-loaded agent
keeps running the schedule it was bootstrapped with even after the file on
disk changes underneath it. Only an unload followed by a fresh load makes
launchd re-read the file.

1. Unload the currently loaded agent. A "no such process" error from `bootout`
   is expected and safe to ignore if it was already unloaded:

```sh
launchctl bootout gui/501/ai.caty.pgl.obs-collector 2>/dev/null; true
```

2. Re-render the plist from the current template, exactly as in
   [Install and remove](#install-and-remove) above:

```sh
PGL_REPO=/absolute/path/to/persona-growth-loop PGL_HOME="$HOME/.persona-growth-loop"; install -d -m 700 "$PGL_HOME/logs" "$HOME/Library/LaunchAgents"; sed -e "s|__PGL_REPO__|$PGL_REPO|g" -e "s|__PGL_HOME__|$PGL_HOME|g" "$PGL_REPO/templates/launchd/ai.caty.pgl.obs-collector.plist" > "$HOME/Library/LaunchAgents/ai.caty.pgl.obs-collector.plist"
```

3. Bootstrap the freshly rendered plist back in:

```sh
launchctl bootstrap gui/501 "$HOME/Library/LaunchAgents/ai.caty.pgl.obs-collector.plist"
```

4. Verify the re-wire actually took effect. `launchctl print` reports the
   schedule launchd currently has loaded, not the schedule in the plist file
   on disk, so this is the only way to confirm the swap worked:

```sh
launchctl print gui/501/ai.caty.pgl.obs-collector | grep -A5 calendarinterval
```

Expect a `descriptor` block reading `"Minute" => 5` and `"Hour" => 0` (00:05),
not the stale `"Minute" => 45` / `"Hour" => 23` (23:45).

## Deadman check

A successful run writes `RUN OK date=YYYY-MM-DD records=N` to
`$PGL_HOME/logs/collector.out.log`. Check both the most recent Tier L file and
the success line:

```sh
PGL_HOME=${PGL_HOME:-"$HOME/.persona-growth-loop"}
find "$PGL_HOME/obslog" -type f -name '*.jsonl' -exec stat -f '%m %Sm %N' -t '%Y-%m-%dT%H:%M:%S%z' {} + | sort -nr | head -1
tail -n 50 "$PGL_HOME/logs/collector.out.log" | grep 'RUN OK date='
```

Hook these two checks into the existing weekly cleanup report as the deadman
watcher. A lock skip is intentionally visible as one `RUN SKIP` line in the
error log and must not be treated as a successful run.

## Lock and killswitch discipline

The process holds the per-face mkdir lock `$PGL_HOME/lock-<face>.d` for the
complete run. If it already exists, that run skips loudly: it emits a `[RED]`
digest line with the skip reason and exits non-zero (overlay-contract §9 —
quiet skips are forbidden). The lock is removed in a `finally` path.

Per overlay-contract §9/§10, the observation collector and 30-day prune are
detection/safe-direction readers and continue while `$PGL_HOME/KILLSWITCH`
exists. Do not add a killswitch gate to them. Growth writers, including the
harvester, proposal writer, and applier, stop under the killswitch; those jobs
land in pgl#6 and are outside this wiring.

## Privacy operations

Tier L is host-local, mode `0700` for directories and `0600` for files. Exclude
it from Time Machine after choosing the actual home:

```sh
PGL_HOME=${PGL_HOME:-"$HOME/.persona-growth-loop"}
tmutil addexclusion "$PGL_HOME/obslog"
```

Create `config/obs-denylist.txt` by copying `config/obs-denylist.example.txt`
(the real file is git-ignored and never ships), then add one project substring
per non-comment line. Matching is case-insensitive; a match against either the flattened Claude
project directory name or any transcript-entry `cwd` skips the entire transcript
file. Keep customer project directories and shared/private repositories on this
list, then run the unit suite before installing an updated checkout.

Sidechain user turns are always excluded. Claude Code labels
orchestrator-to-subagent prompts as `isSidechain: true`; treating those prompts
as the configured human would violate the runtime-derived speaker contract.

Usage-log scanning reads `faces/<face>/candidates.jsonl` and `adopted.jsonl`
under `$PGL_HOME`. If both are absent it is a no-op. pgl#6 will produce these
lists; pgl#5 never stores assistant text or surrounding context in usage logs.
