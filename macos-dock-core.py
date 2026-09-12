#!/usr/bin/env python3

import argparse
import base64
import copy
import datetime as dt
import hashlib
import json
import os
import plistlib
import posixpath
import shlex
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse

FORMAT_VERSION = 1
MANAGED_SECTIONS = ("persistent_apps", "persistent_others")
PLIST_KEYS = {
    "persistent_apps": "persistent-apps",
    "persistent_others": "persistent-others",
}
STRIPPED_KEYS = {
    "book",
    "date-added",
    "file-mod-date",
    "guid",
    "last-opened",
    "last-used",
    "mod-date",
    "parent-mod-date",
}
WEB_APP_BUNDLE_PREFIXES = (
    "com.apple.safari.webapp.",
    "com.microsoft.edgemac.app.",
    "com.google.chrome.app.",
    "com.brave.browser.app.",
    "com.vivaldi.vivaldi.app.",
)
BYTES_TAG = "__macos_dock_bytes_b64__"
DATETIME_TAG = "__macos_dock_datetime__"
DISALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


class MacosDockError(RuntimeError):
    pass


def run(command, *, input_bytes=None, check=True):
    try:
        return subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
    except FileNotFoundError as exc:
        raise MacosDockError(f"command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        detail = f": {stderr}" if stderr else ""
        raise MacosDockError(f"command failed: {' '.join(command)}{detail}") from exc


def validate_host(host):
    hostname = host.rsplit("@", 1)[-1].lower()
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]
    if hostname in DISALLOWED_HOSTS:
        raise MacosDockError(
            f"loopback SSH target is not allowed: {host}; use the Mac's .local hostname"
        )


def target_command(host, command, *, input_bytes=None, check=True):
    validate_host(host)
    remote_command = " ".join(shlex.quote(part) for part in command)
    return run(
        ["ssh", host, remote_command],
        input_bytes=input_bytes,
        check=check,
    )


def target_shell(host, command, *, check=True):
    validate_host(host)
    return run(["ssh", host, command], check=check)


def export_dock_plist(host):
    result = target_command(host, ["defaults", "export", "com.apple.dock", "-"])
    try:
        return plistlib.loads(result.stdout)
    except Exception as exc:
        raise MacosDockError(f"invalid Dock plist returned by {host}") from exc


def target_hostname(host):
    result = target_command(host, ["hostname", "-s"])
    hostname = result.stdout.decode("utf-8", errors="replace").strip()
    return hostname or host


def strip_nonportable(value):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if str(key).lower() in STRIPPED_KEYS:
                continue
            result[key] = strip_nonportable(child)
        return result
    if isinstance(value, list):
        return [strip_nonportable(item) for item in value]
    return value


def json_encode_value(value):
    if isinstance(value, bytes):
        return {BYTES_TAG: base64.b64encode(value).decode("ascii")}
    if isinstance(value, dt.datetime):
        return {DATETIME_TAG: value.isoformat()}
    if isinstance(value, dict):
        return {str(key): json_encode_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [json_encode_value(item) for item in value]
    if isinstance(value, tuple):
        return [json_encode_value(item) for item in value]
    return value


def json_decode_value(value):
    if isinstance(value, dict):
        if set(value) == {BYTES_TAG}:
            return base64.b64decode(value[BYTES_TAG])
        if set(value) == {DATETIME_TAG}:
            return dt.datetime.fromisoformat(value[DATETIME_TAG])
        return {key: json_decode_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [json_decode_value(item) for item in value]
    return value


def build_dump(host):
    plist = export_dock_plist(host)
    return {
        "format": FORMAT_VERSION,
        "source": {
            "host": host,
            "hostname": target_hostname(host),
        },
        "persistent_apps": json_encode_value(
            strip_nonportable(plist.get("persistent-apps", []))
        ),
        "persistent_others": json_encode_value(
            strip_nonportable(plist.get("persistent-others", []))
        ),
    }


def validate_dump(data, source_name="dump"):
    if not isinstance(data, dict):
        raise MacosDockError(f"{source_name}: root must be a JSON object")
    if data.get("format") != FORMAT_VERSION:
        raise MacosDockError(
            f"{source_name}: unsupported format {data.get('format')!r}; expected {FORMAT_VERSION}"
        )
    for section in MANAGED_SECTIONS:
        if not isinstance(data.get(section), list):
            raise MacosDockError(f"{source_name}: {section} must be an array")


def read_dump(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise MacosDockError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MacosDockError(f"invalid JSON in {path}: {exc}") from exc
    validate_dump(data, path)
    return data


def json_bytes(data):
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def write_output(data, output_path=None):
    content = json_bytes(data)
    if output_path is None:
        sys.stdout.buffer.write(content)
        return

    destination = Path(output_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def tile_type(entry):
    return str(entry.get("tile-type", "")) if isinstance(entry, dict) else ""


def tile_data(entry):
    if not isinstance(entry, dict):
        return {}
    data = entry.get("tile-data", {})
    return data if isinstance(data, dict) else {}


def entry_label(entry):
    data = tile_data(entry)
    label = data.get("file-label") or data.get("label")
    if label:
        return str(label)
    entry_type = tile_type(entry)
    return entry_type or "unknown"


def normalized_label(label):
    normalized = unicodedata.normalize("NFKC", str(label))
    return " ".join(normalized.split()).casefold()


def entry_bundle_id(entry):
    data = tile_data(entry)
    bundle_id = data.get("bundle-identifier")
    return str(bundle_id) if bundle_id else None


def entry_url(entry):
    data = tile_data(entry)
    file_data = data.get("file-data", {})
    if not isinstance(file_data, dict):
        return None
    url = file_data.get("_CFURLString")
    return str(url) if url else None


def normalized_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return url.rstrip("/")
    path = posixpath.normpath(unquote(parsed.path))
    return f"file://{path}"


def is_web_app(entry):
    bundle_id = entry_bundle_id(entry)
    if bundle_id and bundle_id.casefold().startswith(WEB_APP_BUNDLE_PREFIXES):
        return True

    url = entry_url(entry)
    if not url:
        return False
    path = unquote(urlparse(url).path).casefold()
    return any(
        marker in path
        for marker in (
            "/edge apps.localized/",
            "/chrome apps.localized/",
            "/brave apps.localized/",
            "/vivaldi apps.localized/",
        )
    )


def entry_base_identity(entry):
    entry_type = tile_type(entry)
    if entry_type in {"spacer-tile", "small-spacer-tile", "flex-spacer-tile"}:
        return f"spacer:{entry_type}"

    if is_web_app(entry):
        label = entry_label(entry)
        if label != "unknown":
            return f"webapp:{normalized_label(label)}"

    bundle_id = entry_bundle_id(entry)
    if bundle_id:
        return f"bundle:{bundle_id}"

    url = normalized_url(entry_url(entry))
    if url:
        return f"url:{url}"

    label = entry_label(entry)
    if label != "unknown":
        return f"label:{normalized_label(label)}"

    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"entry:{digest}"


def is_spacer(entry):
    return entry_base_identity(entry).startswith("spacer:")


def decorate_entries(entries):
    decorated = []
    spacer_counts = {}
    seen_regular = set()

    for entry in entries:
        base = entry_base_identity(entry)
        if is_spacer(entry):
            occurrence = spacer_counts.get(base, 0) + 1
            spacer_counts[base] = occurrence
            identity = f"{base}#{occurrence}"
        else:
            identity = base
            if identity in seen_regular:
                continue
            seen_regular.add(identity)
        decorated.append((identity, entry))

    return decorated


def path_exists(host, path):
    command = f"test -e {shlex.quote(path)}"
    result = target_shell(host, command, check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise MacosDockError(f"cannot inspect path on {host}: {path}")


def find_bundle_path(host, bundle_id):
    query = f"kMDItemCFBundleIdentifier == {json.dumps(bundle_id)}"
    command = f"mdfind {shlex.quote(query)} | head -n 1"
    result = target_shell(host, command, check=False)
    if result.returncode != 0:
        raise MacosDockError(f"cannot search applications on {host}")
    path = result.stdout.decode("utf-8", errors="replace").strip()
    return path or None


def find_app_by_label_path(host, label):
    app_name = f"{label}.app"
    command = (
        f"find \"$HOME/Applications\" /Applications /System/Applications "
        f"-type d -name {shlex.quote(app_name)} -print 2>/dev/null | head -n 1"
    )
    result = target_shell(host, command, check=False)
    if result.returncode != 0:
        raise MacosDockError(f"cannot search applications on {host}")
    path = result.stdout.decode("utf-8", errors="replace").strip()
    return path or None


def file_url_to_path(url):
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    return unquote(parsed.path)


def find_installed_app_path(host, entry):
    if is_web_app(entry):
        label = entry_label(entry)
        if label != "unknown":
            return find_app_by_label_path(host, label)
        return None

    bundle_id = entry_bundle_id(entry)
    if bundle_id:
        found_path = find_bundle_path(host, bundle_id)
        if found_path:
            return found_path

    path = file_url_to_path(entry_url(entry))
    if path and path_exists(host, path):
        return path

    label = entry_label(entry)
    if label != "unknown":
        return find_app_by_label_path(host, label)
    return None


def read_bundle_id(host, path):
    result = target_command(
        host,
        ["mdls", "-raw", "-name", "kMDItemCFBundleIdentifier", path],
        check=False,
    )
    if result.returncode != 0:
        return None
    bundle_id = result.stdout.decode("utf-8", errors="replace").strip()
    if not bundle_id or bundle_id == "(null)":
        return None
    return bundle_id


def set_entry_app_location(entry, path, bundle_id=None):
    result = copy.deepcopy(strip_nonportable(entry))
    data = result.setdefault("tile-data", {})
    file_data = data.setdefault("file-data", {})
    url = Path(path).as_uri()
    if not url.endswith("/"):
        url += "/"
    file_data["_CFURLString"] = url
    file_data["_CFURLStringType"] = 15
    data["file-label"] = entry_label(entry)
    if bundle_id:
        data["bundle-identifier"] = bundle_id
    return result


def prepare_restore_entries(host, desired_entries, current_entries, section):
    current_map = dict(decorate_entries(current_entries))
    prepared = []

    for identity, desired_entry in decorate_entries(desired_entries):
        current_entry = current_map.get(identity)
        if current_entry is not None:
            prepared.append(copy.deepcopy(current_entry))
            continue

        if section != "persistent_apps" or is_spacer(desired_entry):
            prepared.append(copy.deepcopy(desired_entry))
            continue

        found_path = find_installed_app_path(host, desired_entry)
        if found_path:
            local_bundle_id = read_bundle_id(host, found_path)
            prepared.append(
                set_entry_app_location(desired_entry, found_path, local_bundle_id)
            )
            continue

        bundle_id = entry_bundle_id(desired_entry)
        print(
            f"WARNING: application not installed on {host}: {entry_label(desired_entry)}"
            + (f" ({bundle_id})" if bundle_id else ""),
            file=sys.stderr,
        )

    return prepared


def restore_dump(host, dump_data):
    current = export_dock_plist(host)

    for section in MANAGED_SECTIONS:
        desired_entries = json_decode_value(dump_data[section])
        current_entries = current.get(PLIST_KEYS[section], [])
        current[PLIST_KEYS[section]] = prepare_restore_entries(
            host,
            desired_entries,
            current_entries,
            section,
        )

    plist_bytes = plistlib.dumps(current, fmt=plistlib.FMT_XML, sort_keys=False)
    target_command(
        host,
        ["defaults", "import", "com.apple.dock", "-"],
        input_bytes=plist_bytes,
    )
    target_command(host, ["killall", "Dock"], check=False)


def creates_cycle(graph, source, target):
    if source == target:
        return True
    stack = [target]
    visited = set()
    while stack:
        node = stack.pop()
        if node == source:
            return True
        if node in visited:
            continue
        visited.add(node)
        stack.extend(graph.get(node, ()))
    return False


def add_edge(graph, source, target, *, allow_conflict=False):
    if source == target:
        return False
    graph.setdefault(source, set())
    graph.setdefault(target, set())
    if target in graph[source]:
        return True
    if creates_cycle(graph, source, target):
        if allow_conflict:
            return False
        raise MacosDockError("internal ordering cycle in primary dump")
    graph[source].add(target)
    return True


def stable_topological_sort(nodes, graph, priority):
    indegree = {node: 0 for node in nodes}
    for source in nodes:
        for target in graph.get(source, ()):
            indegree[target] += 1

    ready = [node for node in nodes if indegree[node] == 0]
    result = []

    while ready:
        ready.sort(key=priority)
        node = ready.pop(0)
        result.append(node)
        for target in graph.get(node, ()):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)

    if len(result) != len(nodes):
        raise MacosDockError("cannot resolve Dock ordering")
    return result


def merge_section(entries_a, entries_b):
    decorated_a = decorate_entries(entries_a)
    decorated_b = decorate_entries(entries_b)
    ids_a = [identity for identity, _ in decorated_a]
    ids_b = [identity for identity, _ in decorated_b]
    map_a = dict(decorated_a)
    map_b = dict(decorated_b)

    nodes = list(ids_a)
    for identity in ids_b:
        if identity not in map_a:
            nodes.append(identity)

    graph = {node: set() for node in nodes}

    for source, target in zip(ids_a, ids_a[1:]):
        add_edge(graph, source, target)

    for source, target in zip(ids_b, ids_b[1:]):
        add_edge(graph, source, target, allow_conflict=True)

    index_a = {identity: index for index, identity in enumerate(ids_a)}
    index_b = {identity: index for index, identity in enumerate(ids_b)}
    insertion = {identity: index for index, identity in enumerate(nodes)}
    large = len(nodes) + len(ids_a) + len(ids_b) + 1

    def priority(identity):
        a_index = index_a.get(identity, large)
        b_index = index_b.get(identity, large)
        return (min(a_index, b_index), a_index, b_index, insertion[identity])

    ordered_ids = stable_topological_sort(nodes, graph, priority)
    result = []
    for identity in ordered_ids:
        if identity in map_a:
            result.append(copy.deepcopy(map_a[identity]))
        else:
            result.append(copy.deepcopy(map_b[identity]))
    return result


def merge_dumps(dump_a, dump_b):
    result = {
        "format": FORMAT_VERSION,
        "source": {
            "merged_from": [dump_a.get("source"), dump_b.get("source")],
        },
    }
    for section in MANAGED_SECTIONS:
        entries_a = json_decode_value(dump_a[section])
        entries_b = json_decode_value(dump_b[section])
        merged = merge_section(entries_a, entries_b)
        result[section] = json_encode_value(merged)
    return result


def labels_by_identity(entries):
    return {identity: entry_label(entry) for identity, entry in decorate_entries(entries)}


def relative_order_conflicts(entries_a, entries_b):
    ids_a = [identity for identity, _ in decorate_entries(entries_a)]
    ids_b = [identity for identity, _ in decorate_entries(entries_b)]
    set_b = set(ids_b)
    shared = [identity for identity in ids_a if identity in set_b]
    position_a = {identity: index for index, identity in enumerate(ids_a)}
    position_b = {identity: index for index, identity in enumerate(ids_b)}
    conflicts = []

    for left_index, left in enumerate(shared):
        for right in shared[left_index + 1 :]:
            relation_a = position_a[left] < position_a[right]
            relation_b = position_b[left] < position_b[right]
            if relation_a != relation_b:
                conflicts.append((left, right, relation_a))
    return conflicts


def dump_source_host(dump_data):
    source = dump_data.get("source")
    if not isinstance(source, dict):
        return None
    host = source.get("host")
    return host if isinstance(host, str) and host else None


def application_installed(host, entry, cache):
    if not host:
        return None

    cache_key = (host, entry_base_identity(entry))
    if cache_key in cache:
        return cache[cache_key]

    try:
        installed = find_installed_app_path(host, entry) is not None
    except MacosDockError as exc:
        print(f"WARNING: installation check failed for {host}: {exc}", file=sys.stderr)
        installed = None

    cache[cache_key] = installed
    return installed


def diff_section(
    name,
    entries_a,
    entries_b,
    *,
    host_a=None,
    host_b=None,
    installation_cache=None,
):
    decorated_a = decorate_entries(entries_a)
    decorated_b = decorate_entries(entries_b)
    ids_a = [identity for identity, _ in decorated_a]
    ids_b = [identity for identity, _ in decorated_b]
    map_a = dict(decorated_a)
    map_b = dict(decorated_b)
    set_a = set(ids_a)
    set_b = set(ids_b)
    labels = labels_by_identity(entries_b)
    labels.update(labels_by_identity(entries_a))

    lines = []
    only_a = [identity for identity in ids_a if identity not in set_b]
    only_b = [identity for identity in ids_b if identity not in set_a]
    conflicts = relative_order_conflicts(entries_a, entries_b)

    if only_a or only_b or conflicts:
        lines.append(f"{name}:")

    cache = installation_cache if installation_cache is not None else {}

    if only_a:
        lines.append("  only in A:")
        for identity in only_a:
            marker = ""
            if name == "persistent_apps" and host_b:
                installed = application_installed(host_b, map_a[identity], cache)
                if installed is False:
                    marker = " [missing on B]"
            lines.append(f"    - {labels[identity]}{marker}")

    if only_b:
        lines.append("  only in B:")
        for identity in only_b:
            marker = ""
            if name == "persistent_apps" and host_a:
                installed = application_installed(host_a, map_b[identity], cache)
                if installed is False:
                    marker = " [missing on A]"
            lines.append(f"    - {labels[identity]}{marker}")

    if conflicts:
        lines.append("  order conflicts:")
        for left, right, a_before in conflicts:
            if a_before:
                lines.append(
                    f"    - A: {labels[left]} < {labels[right]}; B: {labels[right]} < {labels[left]}"
                )
            else:
                lines.append(
                    f"    - A: {labels[right]} < {labels[left]}; B: {labels[left]} < {labels[right]}"
                )
    return lines


def diff_dumps(dump_a, dump_b):
    lines = []
    host_a = dump_source_host(dump_a)
    host_b = dump_source_host(dump_b)
    installation_cache = {}

    for section in MANAGED_SECTIONS:
        section_lines = diff_section(
            section,
            json_decode_value(dump_a[section]),
            json_decode_value(dump_b[section]),
            host_a=host_a,
            host_b=host_b,
            installation_cache=installation_cache,
        )
        if section_lines:
            if lines:
                lines.append("")
            lines.extend(section_lines)
    if not lines:
        return "No managed Dock differences.\n"
    return "\n".join(lines) + "\n"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="macos-dock",
        description="Dump, restore, merge, and compare macOS Dock contents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    dump_parser = subparsers.add_parser("dump", help="Dump Dock contents from a host")
    dump_parser.add_argument("host", help="SSH target; loopback targets are not allowed")
    dump_parser.add_argument("output", nargs="?", help="output JSON file; stdout if omitted")

    restore_parser = subparsers.add_parser("restore", help="Restore Dock contents to a host")
    restore_parser.add_argument("host", help="SSH target; loopback targets are not allowed")
    restore_parser.add_argument("dump", help="dump JSON file")

    merge_parser = subparsers.add_parser("merge", help="Merge two Dock dumps; dump A wins conflicts")
    merge_parser.add_argument("dump_a", help="primary dump JSON file")
    merge_parser.add_argument("dump_b", help="secondary dump JSON file")
    merge_parser.add_argument("output", nargs="?", help="output JSON file; stdout if omitted")

    diff_parser = subparsers.add_parser(
        "diff",
        help="Compare two Dock dumps and mark applications missing from the other host",
    )
    diff_parser.add_argument("dump_a", help="first dump JSON file")
    diff_parser.add_argument("dump_b", help="second dump JSON file")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "dump":
        write_output(build_dump(args.host), args.output)
        return 0

    if args.command == "restore":
        restore_dump(args.host, read_dump(args.dump))
        return 0

    if args.command == "merge":
        dump_a = read_dump(args.dump_a)
        dump_b = read_dump(args.dump_b)
        write_output(merge_dumps(dump_a, dump_b), args.output)
        return 0

    if args.command == "diff":
        dump_a = read_dump(args.dump_a)
        dump_b = read_dump(args.dump_b)
        sys.stdout.write(diff_dumps(dump_a, dump_b))
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MacosDockError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
