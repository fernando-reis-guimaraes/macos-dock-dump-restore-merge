#!/usr/bin/env python3

import importlib.util
import shlex
import sys
from pathlib import Path


def load_core():
    core_path = Path(__file__).with_name("macos-dock-core.py")
    spec = importlib.util.spec_from_file_location("macos_dock_core", core_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load macos-dock core: {core_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = load_core()
original_read_bundle_id = core.read_bundle_id


def entry_display_name(entry):
    label = core.entry_label(entry)
    identity = core.entry_base_identity(entry)
    bundle_id = core.entry_bundle_id(entry)

    if identity.startswith("webapp:") and bundle_id:
        return f"{label} [{identity}; bundle:{bundle_id}]"

    return f"{label} [{identity}]"


def labels_by_identity(entries):
    return {
        identity: entry_display_name(entry)
        for identity, entry in core.decorate_entries(entries)
    }


def read_bundle_id(host, path):
    bundle_id = original_read_bundle_id(host, path)
    if bundle_id:
        return bundle_id

    codesign_result = core.target_command(
        host,
        ["codesign", "-dv", "--verbose=4", path],
        check=False,
    )
    codesign_output = (
        codesign_result.stderr + b"\n" + codesign_result.stdout
    ).decode("utf-8", errors="replace")

    for line in codesign_output.splitlines():
        if line.startswith("Identifier="):
            identifier = line.removeprefix("Identifier=").strip()
            if identifier:
                return identifier

    info_plist = path.rstrip("/") + "/Contents/Info.plist"
    plist_result = core.target_command(
        host,
        ["plutil", "-extract", "CFBundleIdentifier", "raw", info_plist],
        check=False,
    )
    if plist_result.returncode == 0:
        identifier = plist_result.stdout.decode(
            "utf-8",
            errors="replace",
        ).strip()
        if identifier:
            return identifier

    return None


def find_app_paths_by_label(host, label):
    app_name = f"{label}.app"
    command = (
        'for root in "$HOME/Applications" /Applications /System/Applications; do '
        '[ -d "$root" ] || continue; '
        f'find "$root" -type d -name {shlex.quote(app_name)} '
        '-prune -print 2>/dev/null; '
        "done"
    )
    result = core.target_shell(host, command, check=False)
    if result.returncode != 0:
        raise core.MacosDockError(f"cannot search applications on {host}")

    paths = []
    seen = set()
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        path = line.strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def is_web_app_path(host, path):
    bundle_id = read_bundle_id(host, path)
    if bundle_id and bundle_id.casefold().startswith(core.WEB_APP_BUNDLE_PREFIXES):
        return True

    normalized_path = path.casefold()
    return any(
        marker in normalized_path
        for marker in (
            "/edge apps.localized/",
            "/chrome apps.localized/",
            "/brave apps.localized/",
            "/vivaldi apps.localized/",
        )
    )


def find_installed_app_path(host, entry):
    label = core.entry_label(entry)

    if core.is_web_app(entry):
        if label == "unknown":
            return None
        for path in find_app_paths_by_label(host, label):
            if is_web_app_path(host, path):
                return path
        return None

    bundle_id = core.entry_bundle_id(entry)
    if bundle_id:
        found_path = core.find_bundle_path(host, bundle_id)
        if found_path and read_bundle_id(host, found_path) == bundle_id:
            return found_path

        path = core.file_url_to_path(core.entry_url(entry))
        if (
            path
            and core.path_exists(host, path)
            and read_bundle_id(host, path) == bundle_id
        ):
            return path

        if label != "unknown":
            for candidate_path in find_app_paths_by_label(host, label):
                if read_bundle_id(host, candidate_path) == bundle_id:
                    return candidate_path

        return None

    path = core.file_url_to_path(core.entry_url(entry))
    if path and core.path_exists(host, path):
        return path

    if label != "unknown":
        paths = find_app_paths_by_label(host, label)
        if paths:
            return paths[0]
    return None


core.labels_by_identity = labels_by_identity
core.read_bundle_id = read_bundle_id
core.find_installed_app_path = find_installed_app_path


def main(argv=None):
    return core.main(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except core.MacosDockError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
