# WSL2 / Linux wiring

This is the single setup path for the four shipped systemd user timers on WSL2
or an always-on Linux host. The templates cover the alpha and Luca observation
collectors and weekly mirrors. Nightly growth-lane units remain host-authored;
the repository deliberately does not ship them.

The commands below require paths without spaces or colons. systemd splits the
unquoted `ExecStart` template argv, and colons delimit PATH entries, so choose
values containing neither character for `PGL_REPO`, `PGL_HOME`, and
`PGL_PYTHON_BIN`.

## 0. Run preflight first

From the checkout, run both the ambient diagnostic and a prospective check for
the interpreter directory that will be rendered into the units:

```sh
cd /absolute/path/to/persona-growth-loop
python3 -B bin/pgl-preflight
python3 -B bin/pgl-preflight --python-bin /absolute/path/to/pgl-venv/bin
```

With no installed unit and no `--python-bin`/`PGL_PYTHON_BIN`, the UCD result is
`UNDETERMINED`; this avoids a false RED against the bare fallback PATH. The
prospective or installed-unit mode is authoritative. Compare its resolved
`sys.executable` line with the separately labelled ambient-shell line. Fix every
RED before enabling timers. WARN and SKIP lines describe operator decisions or
checks that do not apply on that host.

The exit-code contract is: 0 when every result is determined and none is RED,
1 when any result is RED, and 2 when no result is RED but at least one is
UNDETERMINED. Scripted `pgl-preflight && ...` chains therefore treat exit 2 as a
failure by design; supply `--python-bin` or install the units before using such
a chain as an enablement gate.

A missing PGL path with no existing ancestor at or below `$HOME` is also
UNDETERMINED: preflight will not probe a directory above `$HOME`.

## Platform prerequisites

Use systemd 240 or newer because the services use `StandardOutput=append:` and
`StandardError=append:`. Ubuntu 24.04 is recommended; verify the installed floor:

```sh
systemd --version
```

Ubuntu 24.04's default CPython 3.12 embeds UCD 15.0.0 and is not admitted. PGL
requires **CPython 3.14.x (UCD 16.0.0)**. Keep that interpreter isolated and
render its `bin` directory into `__PGL_PYTHON_BIN__`.

One option is an uv-managed interpreter and venv:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.14
uv venv --python 3.14 "$HOME/.local/share/pgl-venv"
PGL_PYTHON_BIN="$HOME/.local/share/pgl-venv/bin"
"$PGL_PYTHON_BIN/python3" -c "import sys,unicodedata; assert sys.version_info[:2] == (3, 14), sys.version; assert unicodedata.unidata_version == '16.0.0', unicodedata.unidata_version"
```

Alternatively, install `python3.14` and `python3.14-venv` from deadsnakes, then
create a dedicated venv:

```sh
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install python3.14 python3.14-venv
python3.14 -m venv "$HOME/.local/share/pgl-venv"
PGL_PYTHON_BIN="$HOME/.local/share/pgl-venv/bin"
```

Install the repository's declared Python dependency into that venv before the
first run. A system-wide `sudo ln -s ... /usr/local/bin/python3` can technically
make the pinned suffix find CPython 3.14, but it shadows the distribution's
`python3` and may point a root-owned name at user-writable content. Treat that as
a last-resort, explicitly audited exception, not the normal installation path.

### Enable systemd in WSL2

Create or edit `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then run `wsl.exe --shutdown` from Windows PowerShell and start the distribution
again. `bin/pgl-preflight` checks that `systemctl --user` is reachable and reports
linger state.

## Filesystem, paths, timezone, and face configuration

Keep `PGL_HOME` on the distribution's ext4 filesystem, never under `/mnt/c` or
another drvfs mount. The strict 0600/0700 checks can succeed deceptively or lose
their guarantees on a Windows-mounted filesystem. The preflight probes both
`PGL_HOME` and `PGL_HOME/obslog` and removes its temporary probe files.

`PGL_HOME` must also pair exactly with each collector config's `obs_root`. In
particular, the Luca acceptance ledger and journal are derived from that same
root; relocating only `PGL_HOME` or only `config/obs-collector-luca.json` is a
hard RED. Update the alpha and Luca collector configs together when relocating.

Set the system timezone, not a unit `TZ=` environment variable. Calendar timers
use the systemd manager's timezone, while PGL's date buckets remain JST:

```sh
sudo timedatectl set-timezone Asia/Tokyo
timedatectl status
```

Edit the alpha collector config's `host` label to the provenance label chosen
for this machine. Leaving its shipped default in place warns on Linux and is
suppressed on macOS. Luca describes a remote SSH/dispatch source, so preflight
reports its shipped remote label as INFO and does not compare it with the local
operator hostname.

Confirm every non-empty `transcripts_root` exists and contains transcripts. An
empty directory warns that WSL2 transcripts may live on the Windows side. A path
on drvfs produces the same operational warning. The shipped `soul_alert_argv`
is empty, so preflight reports SKIP until an operator installs and configures an
alert command. The repository does not ship `tg-send`. Re-run preflight after
wiring any alert command so argv[0] is resolved through the unit PATH rather
than the ambient shell PATH.

## Render and install the user units

Set the three paths, create private log storage, render every template, and
verify no placeholder remains:

```sh
PGL_REPO=/absolute/path/to/persona-growth-loop
PGL_HOME="$HOME/.persona-growth-loop"
PGL_PYTHON_BIN="$HOME/.local/share/pgl-venv/bin"
install -d -m 700 "$PGL_HOME/logs" "$HOME/.config/systemd/user"

for template in "$PGL_REPO"/templates/systemd/*.service "$PGL_REPO"/templates/systemd/*.timer; do
  unit="$HOME/.config/systemd/user/$(basename "$template")"
  sed -e "s|__PGL_REPO__|$PGL_REPO|g" \
      -e "s|__PGL_HOME__|$PGL_HOME|g" \
      -e "s|__PGL_PYTHON_BIN__|$PGL_PYTHON_BIN|g" \
      "$template" > "$unit"
done

if grep -R '__PGL_[A-Z_]*__' "$HOME/.config/systemd/user"/ai.caty.pgl.*; then
  echo 'unrendered PGL placeholder' >&2
  false
fi

python3 -B "$PGL_REPO/bin/pgl-preflight"
systemd-analyze verify "$HOME/.config/systemd/user"/ai.caty.pgl.*.service "$HOME/.config/systemd/user"/ai.caty.pgl.*.timer
systemctl --user daemon-reload
systemctl --user enable --now \
  ai.caty.pgl.obs-collector.timer \
  ai.caty.pgl.obs-collector-luca.timer \
  ai.caty.pgl.mirror-weekly.timer \
  ai.caty.pgl.mirror-weekly-luca.timer
loginctl enable-linger "$USER"
```

`systemd-analyze verify` is a static structure check of the rendered files; it
does not require a running user manager.

The weekly services carry `After=` and `Wants=` for their own face's collector
service. When overdue timers are coalesced at startup, this orders the collector
before its weekly mirror. The schedules retain at least 60 minutes of ordinary
collector-to-weekly headroom. On normal weeks, when the VM is up and the
collector already ran at 00:05, the weekly service's `Wants=` re-activates the
collector immediately before the mirror. That extra run is harmless—the
collector atomically replaces the day's records—and guarantees a fresh marker.

The host-authored nightly units, if enabled, use 00:15 for alpha and 04:00 for
Luca. Keep their services and timers outside `templates/systemd/`; validate their
argv, working directory, PATH, log paths, and per-face gates against the macOS
operator contracts before enabling them.

## VM lifecycle and timer health

WSL2 may tear down an idle VM. `loginctl enable-linger` keeps the user manager
running only while the VM itself is up; it cannot keep a stopped WSL2 VM alive.
Without a Windows-side keep-alive or wake action, timers fire only while the VM
is running. The shipped timers use `Persistent=true`, so an elapsed window is
caught up when the VM and user manager next start.

For guaranteed wall-clock firing, create a Windows Task Scheduler job that runs
`wsl.exe -d <distro> -- true` often enough to wake/keep the distribution alive,
or have Task Scheduler invoke `systemctl --user start <unit>` in that distro for
the required job. An always-on Linux host such as a VPS does not have this WSL2
lifecycle caveat.

Check timer health after installation and after any Windows restart:

```sh
systemctl --user list-timers 'ai.caty.pgl.*'
```

`LAST`/`PASSED` show whether the previous firing occurred; `NEXT`/`LEFT` show the
next host-local firing. A blank `LAST` before the first run is expected. An
overdue timer that runs on startup demonstrates `Persistent=true` catch-up.

For marker behavior, one missed collector day is absorbed by the mirror's
accepted `run_day - 1` marker. BROKEN requires at least two consecutive missed
collector days or the independent 48-hour staleness floor. After that downtime,
a self-describing BROKEN report is expected; it is not a timer defect. The
weekly `After=`/`Wants=` relationship prevents a collector/weekly race during
the common coalesced catch-up transaction, but does not erase genuinely stale
input.

## Linux command translations

The macOS runbooks use BSD `stat`. Use these GNU translations on Linux:

- `stat -f '%m %Sm %N' -t '...'` → `stat -c '%Y %y %n'`
- `stat -f '%Lp %N'` → `stat -c '%a %n'`
- For the weekly marker check, `stat -f '%m %Sm %N' -t '...' "$PGL_HOME/reports/weekly/latest-alpha.json"` → `stat -c '%Y %y %n' "$PGL_HOME/reports/weekly/latest-alpha.json"`

Linux verification does not require `plutil` or launchd. Use `journalctl --user
-u <service>` plus the files under `$PGL_HOME/logs/` for job diagnostics.

## Cron fallback

Cron is documentation-only and has no shipped template. Use it only on a Linux
host without systemd user units, reproduce the four pinned schedules and unit
PATH exactly, create `$PGL_HOME/logs` first, and accept that cron has neither
`Persistent=true` catch-up nor the weekly collector `After=`/`Wants=` ordering.
