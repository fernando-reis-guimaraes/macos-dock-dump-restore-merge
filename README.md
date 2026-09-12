# macos-dock-dump-restore-merge

Dump, compare, merge, and restore the macOS Dock across multiple Macs.

If you use two Macs, keeping the Dock consistent quickly becomes a manual chore. macOS does not provide a built-in workflow to synchronize Dock layout across machines, and copying the Dock preferences wholesale is not a good portability strategy: app paths, browser-installed web apps, bookmark data, and other metadata can be machine-specific.

`macos-dock` treats the Dock as something that can be **backed up, compared, merged, and restored intentionally**.

## What it does

- Dumps the portable parts of a Dock into JSON.
- Compares two Dock dumps and reports missing items and ordering conflicts.
- Detects whether an app that is absent from one Dock is actually installed on the other Mac.
- Merges two Dock layouts into a stable union.
- Restores the merged layout while reusing local app metadata whenever possible.
- Keeps native apps and browser-installed PWAs with the same visible name as separate applications.
- Preserves repeated Dock spacers.
- Provides full raw backup/restore commands for disaster recovery.

The portable workflow manages:

- `persistent-apps`
- `persistent-others`
- spacers inside those sections

It intentionally does **not** synchronize unrelated Dock preferences such as autohide, magnification, size, orientation, recent apps, Mission Control settings, or hot corners.

## Why not just copy `com.apple.dock.plist`?

A raw plist is excellent for restoring the **same Mac** after a mistake. It is not a good interchange format between Macs.

Dock entries can contain machine-specific information such as bookmark blobs, file metadata, local paths, and browser-generated bundle identifiers. `macos-dock dump` removes non-portable metadata and `macos-dock restore` resolves applications against the destination Mac.

For safety, the project provides both models:

```text
portable dump/restore
    synchronize Dock contents between Macs

raw dump/restore
    exact whole-Dock backup and rollback
```

## Requirements

Controller Mac:

- macOS
- Python 3.9+
- OpenSSH client
- `plutil`

Target Macs:

- SSH access
- standard macOS command-line tools (`defaults`, `mdfind`, `mdls`, `codesign`, `plutil`, `find`)

The target Mac does **not** need this repository installed. All target operations are executed over SSH.

Loopback targets (`localhost`, `127.0.0.1`, and `::1`) are intentionally rejected. Use the target Mac's hostname, for example `macbook.local` or `macmini.local`.

## Installation

Clone the repository:

```bash
git clone git@github.com:fernando-reis-guimaraes/macos-dock-dump-restore-merge.git
cd macos-dock-dump-restore-merge
```

Run it directly:

```bash
./macos-dock --help
```

Or add it to a directory already on your `PATH`:

```bash
ln -s "$(pwd)/macos-dock" "$HOME/bin/macos-dock"
```

## Commands

```text
macos-dock dump <host> [output]
macos-dock restore <host> <dump>
macos-dock merge <dump-a> <dump-b> [output]
macos-dock diff <dump-a> <dump-b>
macos-dock raw-dump <host> [output]
macos-dock raw-restore <host> <plist>
```

### `dump`

Exports the portable, managed Dock sections from a Mac:

```bash
macos-dock dump macbook.local macbook.dock.json
macos-dock dump macmini.local macmini.dock.json
```

If `output` is omitted, JSON is written to stdout.

### `diff`

Compare two portable dumps:

```bash
macos-dock diff macbook.dock.json macmini.dock.json
```

Example output:

```text
persistent_apps:
  only in A:
    - Calendar [bundle:com.apple.iCal]
  only in B:
    - Example [webapp:example; bundle:com.microsoft.edgemac.app.example]
```

When source-host metadata is available, an app that is not installed on the opposite Mac is explicitly marked:

```text
- Example App [bundle:com.example.app] [missing on B]
```

This makes `diff` useful before a merge: install applications that should exist on both Macs, repeat the dump/diff, and leave only intentional machine-specific differences.

### `merge`

Create the union of two Dock layouts:

```bash
macos-dock merge \
  macbook.dock.json \
  macmini.dock.json \
  merged.dock.json
```

Dump A is the primary layout. Its ordering constraints win when A and B disagree irreconcilably.

Items that exist only in B are inserted while preserving as much compatible ordering information from both layouts as possible.

### `restore`

Apply a portable dump to a target Mac:

```bash
macos-dock restore macbook.local merged.dock.json
```

Restore does not blindly copy the source entry. When possible it reuses the target Mac's existing Dock representation. For applications that are installed but not already in the Dock, it resolves the local app path and bundle identifier before creating the entry.

Applications that cannot be found on the target are skipped with a warning rather than creating a broken Dock item.

After restore, the Dock process is restarted automatically.

### `raw-dump`

Back up the complete `com.apple.dock` domain before experimenting:

```bash
mkdir -p dock-backup

macos-dock raw-dump \
  macbook.local \
  dock-backup/macbook.com.apple.dock.plist

macos-dock raw-dump \
  macmini.local \
  dock-backup/macmini.com.apple.dock.plist
```

The plist is validated before the destination file is atomically replaced.

### `raw-restore`

Restore an exact whole-Dock backup:

```bash
macos-dock raw-restore \
  macbook.local \
  dock-backup/macbook.com.apple.dock.plist
```

`raw-restore` replaces the complete `com.apple.dock` domain and restarts the Dock. Use it as rollback/disaster recovery, not as the normal cross-Mac synchronization mechanism.

## Recommended two-Mac workflow

First, take exact rollback snapshots:

```bash
macos-dock raw-dump macbook.local dock-backup/macbook.plist
macos-dock raw-dump macmini.local dock-backup/macmini.plist
```

Then create portable dumps:

```bash
macos-dock dump macbook.local macbook.dock.json
macos-dock dump macmini.local macmini.dock.json
```

Inspect differences:

```bash
macos-dock diff macbook.dock.json macmini.dock.json
```

Create the merged layout:

```bash
macos-dock merge macbook.dock.json macmini.dock.json merged.dock.json
```

Preview what each Mac will gain or reorder:

```bash
macos-dock diff macbook.dock.json merged.dock.json
macos-dock diff macmini.dock.json merged.dock.json
```

Restore one Mac first and verify it:

```bash
macos-dock restore macbook.local merged.dock.json
macos-dock dump macbook.local macbook.after.dock.json
macos-dock diff macbook.after.dock.json merged.dock.json
```

The ideal result is:

```text
No managed Dock differences.
```

Then repeat the restore and verification on the second Mac.

## Application identity

A visible app name is not enough to identify a Dock entry reliably.

`macos-dock` distinguishes applications using a portability-aware identity strategy:

1. spacers by spacer type and occurrence;
2. recognized browser web apps/PWAs by normalized visible label;
3. native applications by bundle identifier;
4. file URL when no bundle identifier exists;
5. normalized label as a fallback;
6. canonical content hash as a final fallback.

This matters when, for example, a native `ChatGPT.app` and an Edge/Safari PWA named `ChatGPT` are both installed. They remain separate Dock entries.

For browser web apps, installation-specific bundle identifiers can differ across Macs or browsers. The logical web-app identity is therefore based on the normalized app label, while the destination Mac keeps its own local bundle/path metadata.

## Merge behavior

The merge is deterministic and stable:

- every logical item from A and B is included once;
- repeated spacers are preserved;
- A's adjacency/order constraints are added first;
- B's constraints are added when they do not create a cycle;
- A wins irreconcilable ordering conflicts;
- a stable topological sort produces the final sequence;
- for the same logical identity, A's portable metadata is retained in the merged dump.

The restore phase then localizes entries for the destination Mac.

## Safety and privacy

The repository contains no credentials or machine-specific configuration.

Generated dumps can contain application names, bundle identifiers, local file paths, and source hostnames. Treat your own dump and raw plist files as local data and review them before publishing or committing them elsewhere.

Raw Dock backups are especially machine-specific and should generally stay out of source control.

## License

MIT. See [LICENSE](LICENSE).
