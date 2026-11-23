#!/usr/bin/env python3
"""DD Preset Comparator (DD_PC) — Arma 3 preset weight and comparison CLI utility.

Parses Arma 3 Launcher exported .html presets (and .preset2 XML files),
fetches Steam Workshop sizes via Steam Web API, scans local disk sizes,
and renders a vertical comparison table in the terminal.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import ctypes
import html
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

VERSION = "1.0.0"

STEAM_API_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
)
STEAM_BATCH_SIZE = 100
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

CLR_RESET = "\033[0m"
CLR_FG_RESET = "\033[39m"
CLR_BOLD = "\033[1m"
CLR_WARN_FG = "\033[38;2;240;185;75m"
BG_ZEBRA_EVEN = ""
BG_ZEBRA_ODD = "\033[48;2;36;40;48m"
BG_MISSING_EVEN = "\033[48;2;88;38;42m"
BG_MISSING_ODD = "\033[48;2;102;45;50m"

MOD_CONTAINER_RE = re.compile(
    r'<tr[^>]*data-type=["\']ModContainer["\'][^>]*>(.*?)</tr>',
    re.IGNORECASE | re.DOTALL,
)
DISPLAY_NAME_RE = re.compile(
    r'<td[^>]*data-type=["\']DisplayName["\'][^>]*>(.*?)</td>',
    re.IGNORECASE | re.DOTALL,
)
STEAM_LINK_RE = re.compile(
    r'href=["\']https?://steamcommunity\.com/(?:sharedfiles|workshop)/filedetails/\?[^"\']*?\bid=(\d+)[^"\']*["\']',
    re.IGNORECASE,
)
LOCAL_SPAN_RE = re.compile(
    r'<span[^>]*class=["\'][^"\']*from-local[^"\']*["\']',
    re.IGNORECASE,
)
DATA_META_LOCAL_RE = re.compile(
    r'data-meta=["\']local:([^|]*)\|([^|]*)\|([^"\']*)["\']',
    re.IGNORECASE,
)


@dataclass
class ModRef:
    key: str  # "steam:<id>" or "local:<folder_lower>"
    name: str
    is_local: bool
    steam_id: Optional[str] = None
    local_folder: Optional[str] = None
    local_hint_path: Optional[str] = None


@dataclass
class PresetData:
    name: str
    path: Path
    mods: Dict[str, ModRef]  # canonical key -> ModRef


@dataclass
class ModSizeInfo:
    key: str
    api_bytes: Optional[int] = None
    api_title: Optional[str] = None
    disk_bytes: Optional[int] = None
    disk_path: Optional[Path] = None


def enable_windows_ansi_and_utf8() -> None:
    """Enable VT100 ANSI escape codes and UTF-8 output on Windows."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


def format_bytes(num_bytes: Optional[int]) -> str:
    """Format byte count into human-readable MB or GB."""
    if num_bytes is None:
        return "missing"
    if num_bytes < 0:
        return "-"
    gib = 1024 ** 3
    mib = 1024 ** 2
    kib = 1024
    if num_bytes >= gib:
        return f"{num_bytes / gib:.2f} GB"
    if num_bytes >= mib:
        return f"{num_bytes / mib:.1f} MB"
    if num_bytes >= kib:
        return f"{num_bytes / kib:.1f} KB"
    return f"{num_bytes} B"


def format_signed_bytes(delta: int) -> str:
    """Format signed byte difference."""
    sign = "+" if delta > 0 else ("-" if delta < 0 else "")
    if delta == 0:
        return "0 B"
    return f"{sign}{format_bytes(abs(delta))}"


_CJK_PUNCT_TRANS = str.maketrans(
    {
        "【": "[",
        "】": "]",
        "「": "[",
        "」": "]",
        "『": "[",
        "』": "]",
        "《": "<",
        "》": ">",
        "、": ",",
        "，": ",",
        "。": ".",
    }
)


def normalize_display_text(text: str) -> str:
    """Normalize fullwidth Unicode characters and strip invisible/control chars."""
    text = unicodedata.normalize("NFKC", text).translate(_CJK_PUNCT_TRANS)
    return "".join(
        ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cc")
    ).strip()


def strip_tags(raw: str) -> str:
    return normalize_display_text(html.unescape(re.sub(r"<[^>]+>", "", raw)))


def parse_html_preset(file_path: Path) -> PresetData:
    text = file_path.read_text(encoding="utf-8", errors="replace")
    mods: Dict[str, ModRef] = {}

    for block in MOD_CONTAINER_RE.findall(text):
        name_match = DISPLAY_NAME_RE.search(block)
        display_name = strip_tags(name_match.group(1)) if name_match else "Unknown Mod"

        steam_match = STEAM_LINK_RE.search(block)
        if steam_match:
            sid = steam_match.group(1)
            key = f"steam:{sid}"
            if key not in mods:
                mods[key] = ModRef(
                    key=key,
                    name=display_name,
                    is_local=False,
                    steam_id=sid,
                )
            continue

        meta_match = DATA_META_LOCAL_RE.search(block)
        is_local = bool(LOCAL_SPAN_RE.search(block) or meta_match)
        if is_local:
            folder_name = display_name
            if meta_match:
                meta_name = html.unescape(meta_match.group(1)).strip()
                meta_folder = html.unescape(meta_match.group(2)).strip()
                if meta_folder:
                    folder_name = meta_folder
                if not display_name and meta_name:
                    display_name = meta_name
            key = f"local:{folder_name.lower()}"
            if key not in mods:
                mods[key] = ModRef(
                    key=key,
                    name=display_name,
                    is_local=True,
                    local_folder=folder_name,
                )

    return PresetData(name=file_path.stem, path=file_path, mods=mods)


def parse_preset2_xml(file_path: Path) -> PresetData:
    tree = ET.parse(file_path)
    root = tree.getroot()
    mods: Dict[str, ModRef] = {}

    for id_elem in root.findall(".//published-ids/id"):
        val = (id_elem.text or "").strip()
        if val.startswith("steam:"):
            sid = val.split(":", 1)[1].strip()
            if sid.isdigit():
                key = f"steam:{sid}"
                mods[key] = ModRef(
                    key=key,
                    name=f"Workshop {sid}",
                    is_local=False,
                    steam_id=sid,
                )
        elif val.startswith("local:"):
            raw_path = val.split(":", 1)[1].strip().rstrip("\\/")
            folder_name = Path(raw_path).name or raw_path
            key = f"local:{folder_name.lower()}"
            mods[key] = ModRef(
                key=key,
                name=folder_name,
                is_local=True,
                local_folder=folder_name,
                local_hint_path=raw_path,
            )

    return PresetData(name=file_path.stem, path=file_path, mods=mods)


def parse_preset(file_path: Path) -> PresetData:
    suffix = file_path.suffix.lower()
    if suffix in (".preset2", ".defaultpreset2"):
        return parse_preset2_xml(file_path)
    return parse_html_preset(file_path)


def get_launcher_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Arma 3 Launcher"
    return Path.home() / "AppData" / "Local" / "Arma 3 Launcher"


def load_launcher_steam_cache() -> Dict[str, dict]:
    """Load %LOCALAPPDATA%/Arma 3 Launcher/Steam.json keyed by steam:<id>."""
    steam_json = get_launcher_dir() / "Steam.json"
    cache: Dict[str, dict] = {}
    if not steam_json.is_file():
        return cache
    try:
        data = json.loads(steam_json.read_text(encoding="utf-8", errors="replace"))
        for ext in data.get("Extensions", []):
            ext_id = ext.get("Id")
            if ext_id:
                cache[str(ext_id)] = ext
    except Exception:
        pass
    return cache


def load_launcher_local_config() -> Tuple[List[Path], List[Path]]:
    """Return (known_local_mods, search_directories) from Local.json & defaults."""
    known_mods: List[Path] = []
    search_dirs: List[Path] = []
    local_json = get_launcher_dir() / "Local.json"
    if local_json.is_file():
        try:
            data = json.loads(local_json.read_text(encoding="utf-8", errors="replace"))
            for p in data.get("knownLocalMods", []):
                known_mods.append(Path(p))
            for key in ("autodetectionDirectories", "userDirectories"):
                for p in data.get(key, []):
                    search_dirs.append(Path(p))
        except Exception:
            pass

    for fallback in (
        Path(r"E:\A3Projects\.mods\.build"),
        Path(r"E:\A3Projects\.mods\.release"),
        Path(r"E:\A3Mods"),
    ):
        if fallback.exists():
            search_dirs.append(fallback)

    return known_mods, search_dirs


def discover_arma3_and_workshop_dirs(
    custom_workshop_dir: Optional[Path] = None,
) -> Tuple[Optional[Path], List[Path]]:
    """Find Arma 3 install path and Workshop 107410 directories."""
    arma3_dir: Optional[Path] = None
    workshop_dirs: List[Path] = []

    if custom_workshop_dir:
        workshop_dirs.append(custom_workshop_dir)

    if os.name == "nt":
        try:
            import winreg

            for root_key, subkey in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Bohemia Interactive\arma 3"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Bohemia Interactive\arma 3"),
            ):
                try:
                    with winreg.OpenKey(root_key, subkey) as k:
                        val, _ = winreg.QueryValueEx(k, "main")
                        if val:
                            arma3_dir = Path(val)
                            break
                except OSError:
                    continue
        except ImportError:
            pass

    if arma3_dir and arma3_dir.exists():
        derived_ws = arma3_dir.parent.parent / "workshop" / "content" / "107410"
        if derived_ws.exists():
            workshop_dirs.append(derived_ws)

    # Deduplicate preserving order
    unique_ws: List[Path] = []
    seen = set()
    for d in workshop_dirs:
        norm = str(d).lower()
        if norm not in seen:
            seen.add(norm)
            unique_ws.append(d)

    return arma3_dir, unique_ws


def fetch_steam_sizes(
    steam_ids: List[str],
    steam_cache: Dict[str, dict],
) -> Dict[str, Tuple[Optional[int], Optional[str]]]:
    """Fetch (file_size, title) from Steam Web API in batches of 100 with cache fallback."""
    results: Dict[str, Tuple[Optional[int], Optional[str]]] = {}
    unique_ids = sorted(set(steam_ids))

    for idx in range(0, len(unique_ids), STEAM_BATCH_SIZE):
        batch = unique_ids[idx : idx + STEAM_BATCH_SIZE]
        params = {"itemcount": str(len(batch))}
        for i, sid in enumerate(batch):
            params[f"publishedfileids[{i}]"] = sid

        encoded = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(
            STEAM_API_URL,
            data=encoded,
            headers={"User-Agent": f"DD_PresetComparator/{VERSION}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            details = payload.get("response", {}).get("publishedfiledetails", [])
            for item in details:
                sid = str(item.get("publishedfileid", ""))
                if not sid:
                    continue
                if item.get("result") == 1 and "file_size" in item:
                    try:
                        size_val = int(item["file_size"])
                    except (TypeError, ValueError):
                        size_val = None
                    title_val = item.get("title")
                    results[sid] = (size_val, title_val)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            # Fallback handled below
            pass

    # Fallback to Steam.json for any missing/unlisted items
    for sid in unique_ids:
        if sid not in results or results[sid][0] is None:
            cached = steam_cache.get(f"steam:{sid}")
            if cached:
                size_val = cached.get("FileSystemSpaceRequired")
                if size_val is None:
                    size_val = cached.get("StorageInfo", {}).get("FileSystemSize")
                try:
                    size_int = int(size_val) if size_val is not None else None
                except (TypeError, ValueError):
                    size_int = None
                title_val = cached.get("DisplayName") or (
                    results[sid][1] if sid in results else None
                )
                results[sid] = (size_int, title_val)
            elif sid not in results:
                results[sid] = (None, None)

    return results


def dir_size_bytes(path: Path) -> int:
    """Fast recursive directory size calculation using os.scandir."""
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def resolve_mod_disk_path(
    mod: ModRef,
    workshop_dirs: List[Path],
    steam_cache: Dict[str, dict],
    known_local_map: Dict[str, Path],
    local_search_dirs: List[Path],
) -> Optional[Path]:
    if not mod.is_local and mod.steam_id:
        for ws_dir in workshop_dirs:
            candidate = ws_dir / mod.steam_id
            if candidate.is_dir():
                return candidate
        cached = steam_cache.get(mod.key)
        if cached:
            ext_path = cached.get("ExtensionPath")
            if ext_path:
                p = Path(ext_path)
                if p.is_dir():
                    return p
        return None

    # Local mod resolution
    if mod.local_hint_path:
        hint = Path(mod.local_hint_path)
        if hint.is_dir():
            return hint

    folder = mod.local_folder or mod.name
    candidates_names = [folder]
    if not folder.startswith("@"):
        candidates_names.append(f"@{folder}")
    else:
        candidates_names.append(folder.lstrip("@"))

    for cname in candidates_names:
        if cname.lower() in known_local_map:
            p = known_local_map[cname.lower()]
            if p.is_dir():
                return p

    for sdir in local_search_dirs:
        if not sdir.is_dir():
            continue
        for cname in candidates_names:
            candidate = sdir / cname
            if candidate.is_dir():
                return candidate

    return None


def resolve_all_mod_sizes(
    all_mods: Dict[str, ModRef],
    custom_workshop_dir: Optional[Path] = None,
    extra_local_dirs: Optional[List[Path]] = None,
) -> Dict[str, ModSizeInfo]:
    steam_cache = load_launcher_steam_cache()
    known_local_mods, local_search_dirs = load_launcher_local_config()
    if extra_local_dirs:
        local_search_dirs = list(extra_local_dirs) + local_search_dirs

    known_local_map: Dict[str, Path] = {}
    for p in known_local_mods:
        known_local_map[p.name.lower()] = p

    _, workshop_dirs = discover_arma3_and_workshop_dirs(custom_workshop_dir)

    steam_ids = [m.steam_id for m in all_mods.values() if not m.is_local and m.steam_id]
    steam_sizes = fetch_steam_sizes(steam_ids, steam_cache) if steam_ids else {}

    path_map: Dict[str, Optional[Path]] = {}
    for key, mod in all_mods.items():
        path_map[key] = resolve_mod_disk_path(
            mod, workshop_dirs, steam_cache, known_local_map, local_search_dirs
        )

    disk_sizes: Dict[str, Optional[int]] = {}
    paths_to_scan = {k: p for k, p in path_map.items() if p is not None}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        future_to_key = {
            pool.submit(dir_size_bytes, p): k for k, p in paths_to_scan.items()
        }
        for fut in concurrent.futures.as_completed(future_to_key):
            k = future_to_key[fut]
            try:
                disk_sizes[k] = fut.result()
            except Exception:
                disk_sizes[k] = None

    result: Dict[str, ModSizeInfo] = {}
    for key, mod in all_mods.items():
        api_bytes: Optional[int] = None
        api_title: Optional[str] = None
        if not mod.is_local and mod.steam_id and mod.steam_id in steam_sizes:
            api_bytes, api_title = steam_sizes[mod.steam_id]
        result[key] = ModSizeInfo(
            key=key,
            api_bytes=api_bytes,
            api_title=api_title,
            disk_bytes=disk_sizes.get(key),
            disk_path=path_map.get(key),
        )
    return result


def char_width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf", "Cc"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def visible_len(text: str) -> int:
    plain = ANSI_RE.sub("", text)
    return sum(char_width(ch) for ch in plain)


def pad_visible(text: str, width: int, align: str = "left") -> str:
    diff = width - visible_len(text)
    if diff <= 0:
        return text
    if align == "right":
        return (" " * diff) + text
    if align == "center":
        left = diff // 2
        right = diff - left
        return (" " * left) + text + (" " * right)
    return text + (" " * diff)


def truncate_text(text: str, max_len: int) -> str:
    if visible_len(text) <= max_len:
        return text
    if max_len <= 3:
        out: List[str] = []
        w = 0
        for ch in text:
            cw = char_width(ch)
            if w + cw > max_len:
                break
            out.append(ch)
            w += cw
        return "".join(out)
    target = max_len - 3
    out = []
    w = 0
    for ch in text:
        cw = char_width(ch)
        if w + cw > target:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "..."


def wrap_display_text(text: str, max_width: int) -> List[str]:
    """Wrap text into multiple lines of at most max_width terminal columns without truncation."""
    if max_width <= 0:
        return [text]
    if visible_len(text) <= max_width:
        return [text]

    # Split on spaces first, then split oversized tokens after '_' or '-'
    raw_words = text.split()
    tokens: List[Tuple[str, bool]] = []  # (piece, prepend_space_if_not_line_start)
    for word in raw_words:
        if visible_len(word) <= max_width:
            tokens.append((word, True))
        else:
            parts = re.split(r"(?<=[_-])", word)
            for idx, part in enumerate(parts):
                if part:
                    tokens.append((part, idx == 0))

    lines: List[str] = []
    current = ""
    for tok, needs_space in tokens:
        sep = " " if (current and needs_space) else ""
        candidate = f"{current}{sep}{tok}"
        if visible_len(candidate) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        # If single token still exceeds max_width, slice by char_width
        if visible_len(tok) <= max_width:
            current = tok
        else:
            buf: List[str] = []
            w = 0
            for ch in tok:
                cw = char_width(ch)
                if w + cw > max_width and buf:
                    lines.append("".join(buf))
                    buf = [ch]
                    w = cw
                else:
                    buf.append(ch)
                    w += cw
            current = "".join(buf)

    if current:
        lines.append(current)
    return lines or [""]


def warn_text(text: str, use_color: bool) -> str:
    return f"{CLR_WARN_FG}{text}{CLR_FG_RESET}" if use_color else text


def format_disk_and_api(
    disk_bytes: Optional[int],
    api_bytes: Optional[int],
    is_local: bool,
    use_color: bool,
) -> str:
    """Format cell weight: single value if equal, otherwise disk primary + highlighted mismatch."""
    if is_local:
        return format_bytes(disk_bytes) if disk_bytes is not None else warn_text("missing", use_color)

    if disk_bytes is not None and api_bytes is not None:
        disk_str = format_bytes(disk_bytes)
        api_str = format_bytes(api_bytes)
        if disk_str == api_str or disk_bytes == api_bytes:
            return disk_str
        return f"{disk_str} / {warn_text(api_str, use_color)}"

    if disk_bytes is None and api_bytes is not None:
        return f"{warn_text('missing', use_color)} / {format_bytes(api_bytes)}"

    if disk_bytes is not None and api_bytes is None:
        return f"{format_bytes(disk_bytes)} / {warn_text('no API', use_color)}"

    return warn_text("missing", use_color)


def format_total_disk_and_api(
    disk_bytes: int, api_bytes: int, use_color: bool, bold: bool = False
) -> str:
    disk_str = format_bytes(disk_bytes)
    api_str = format_bytes(api_bytes)
    if disk_str == api_str or disk_bytes == api_bytes:
        out = disk_str
    else:
        out = f"{disk_str} / {warn_text(api_str, use_color)}"
    return f"{CLR_BOLD}{out}{CLR_RESET}" if (bold and use_color) else out


def format_diff_disk_and_api(
    disk_delta: int, api_delta: int, use_color: bool
) -> str:
    d_disk = format_signed_bytes(disk_delta)
    d_api = format_signed_bytes(api_delta)
    if d_disk == d_api or disk_delta == api_delta:
        return d_disk
    return f"{d_disk} / {warn_text(d_api, use_color)}"


def format_cell(
    present: bool,
    mod: ModRef,
    size_info: ModSizeInfo,
    col_width: int,
    use_color: bool,
    row_bg: str = "",
    missing_bg: str = "",
) -> str:
    if not present:
        if use_color and missing_bg:
            return f"{missing_bg}{' ' * (col_width + 2)}{CLR_RESET}"
        return " " * (col_width + 2)

    cell_content = format_disk_and_api(
        size_info.disk_bytes, size_info.api_bytes, mod.is_local, use_color
    )
    padded = pad_visible(cell_content, col_width, align="center")
    if use_color and row_bg:
        return f"{row_bg} {padded} {CLR_RESET}"
    return f" {padded} "


def compute_preset_totals(
    preset: PresetData, size_map: Dict[str, ModSizeInfo]
) -> Tuple[int, int, int, int]:
    """Return (mod_count, downloaded_count, api_total_bytes, disk_total_bytes)."""
    mod_count = len(preset.mods)
    downloaded_count = 0
    api_total = 0
    disk_total = 0
    for key in preset.mods:
        info = size_map[key]
        if info.api_bytes is not None:
            api_total += info.api_bytes
        if info.disk_bytes is not None:
            downloaded_count += 1
            disk_total += info.disk_bytes
    return mod_count, downloaded_count, api_total, disk_total


def render_table(
    presets: List[PresetData],
    all_mods: Dict[str, ModRef],
    size_map: Dict[str, ModSizeInfo],
    sort_by: str = "name",
    only_diff: bool = False,
    show_id: bool = False,
    use_color: bool = True,
) -> None:
    keys = list(all_mods.keys())

    if only_diff and len(presets) > 1:
        keys = [k for k in keys if not all(k in p.mods for p in presets)]

    def display_label(mod: ModRef) -> str:
        name = normalize_display_text(mod.name)
        info = size_map[mod.key]
        if name.startswith("Workshop ") and info.api_title:
            name = normalize_display_text(info.api_title)
        suffix = " [L]" if mod.is_local else (f" [{mod.steam_id}]" if show_id and mod.steam_id else "")
        return f"{name}{suffix}"

    def sort_key(k: str):
        mod = all_mods[k]
        info = size_map[k]
        max_sz = max(info.api_bytes or 0, info.disk_bytes or 0)
        label = display_label(mod).lower()
        if sort_by == "size":
            return (-max_sz, label)
        if sort_by == "diff":
            missing_count = sum(1 for p in presets if k not in p.mods)
            is_diff = 0 if 0 < missing_count < len(presets) else 1
            return (is_diff, -missing_count, label)
        return (label,)

    keys.sort(key=sort_key)

    max_name_len = 46
    name_col_width = max(
        [visible_len("Mod Name"), visible_len("Diff vs 1st preset (Disk / API)")]
        + [min(visible_len(display_label(all_mods[k])), max_name_len) for k in keys]
    )
    name_col_width = min(max(name_col_width, 30), max_name_len)

    preset_col_widths: List[int] = []
    for p in presets:
        clean_name = normalize_display_text(p.name)
        w = min(max(visible_len(clean_name), visible_len("Disk / API"), 21), 24)
        preset_col_widths.append(w)

    def hline(char: str = "-") -> str:
        parts = [char * (name_col_width + 2)]
        for w in preset_col_widths:
            parts.append(char * (w + 2))
        return "+" + "+".join(parts) + "+"

    def print_row(first_col: str, other_cols: List[str]) -> None:
        cells = [f" {pad_visible(first_col, name_col_width)} "]
        for col, w in zip(other_cols, preset_col_widths):
            cells.append(f" {pad_visible(col, w, align='center')} ")
        print("|" + "|".join(cells) + "|")

    print()
    print(hline("="))
    wrapped_headers = [
        wrap_display_text(normalize_display_text(p.name), w)
        for p, w in zip(presets, preset_col_widths)
    ]
    header_height = max((len(lines) for lines in wrapped_headers), default=1)
    for line_idx in range(header_height):
        first_col = "Mod Name" if line_idx == 0 else ""
        row_cols = [
            lines[line_idx] if line_idx < len(lines) else ""
            for lines in wrapped_headers
        ]
        print_row(first_col, row_cols)
    sub_cols = ["Disk / API" for _ in presets]
    print_row(f"Total unique mods: {len(all_mods)}", sub_cols)
    print(hline("="))

    if not keys:
        print_row("(no rows to display)", ["-" for _ in presets])
    else:
        for idx, k in enumerate(keys):
            mod = all_mods[k]
            label = truncate_text(display_label(mod), name_col_width)
            is_odd = idx % 2 == 1
            row_bg = BG_ZEBRA_ODD if (use_color and is_odd) else BG_ZEBRA_EVEN
            missing_bg = (BG_MISSING_ODD if is_odd else BG_MISSING_EVEN) if use_color else ""

            padded_label = pad_visible(label, name_col_width)
            first_cell = (
                f"{row_bg} {padded_label} {CLR_RESET}"
                if (use_color and row_bg)
                else f" {padded_label} "
            )
            row_cells = [
                format_cell(
                    k in p.mods,
                    mod,
                    size_map[k],
                    w,
                    use_color,
                    row_bg=row_bg,
                    missing_bg=missing_bg,
                )
                for p, w in zip(presets, preset_col_widths)
            ]
            print("|" + "|".join([first_cell] + row_cells) + "|")

    print(hline("="))

    totals = [compute_preset_totals(p, size_map) for p in presets]

    count_cols = [f"{cnt} / {dl}" for cnt, dl, _, _ in totals]
    print_row("Mods (total / on disk)", count_cols)

    total_cols = [
        format_total_disk_and_api(disk_t, api_t, use_color, bold=True)
        for _, _, api_t, disk_t in totals
    ]
    print_row("TOTAL SIZE (Disk / API)", total_cols)

    if len(presets) >= 2:
        base_api = totals[0][2]
        base_disk = totals[0][3]
        diff_cols = ["-"]
        for _, _, api_t, disk_t in totals[1:]:
            diff_cols.append(
                format_diff_disk_and_api(disk_t - base_disk, api_t - base_api, use_color)
            )
        print_row("Diff vs 1st preset (Disk / API)", diff_cols)

    print(hline("-"))
    unique_api = sum(
        size_map[k].api_bytes for k in all_mods if size_map[k].api_bytes is not None
    )
    unique_disk = sum(
        size_map[k].disk_bytes for k in all_mods if size_map[k].disk_bytes is not None
    )
    print(
        f"Unique mods: {len(all_mods)} | "
        f"Total unique size (Disk / API): {format_total_disk_and_api(unique_disk, unique_api, use_color)}"
    )
    if use_color:
        print(
            f"Legend: {BG_MISSING_EVEN}   {CLR_RESET} missing in preset | "
            f"{CLR_WARN_FG}yellow text{CLR_FG_RESET} Steam API size differs from disk (or not downloaded)"
        )
    print()


def export_csv(
    out_path: Path,
    presets: List[PresetData],
    all_mods: Dict[str, ModRef],
    size_map: Dict[str, ModSizeInfo],
) -> None:
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        header = ["Mod Name", "Key", "Type"]
        for p in presets:
            header.extend([f"{p.name} (API Bytes)", f"{p.name} (Disk Bytes)"])
        writer.writerow(header)

        for k in sorted(all_mods.keys(), key=lambda x: all_mods[x].name.lower()):
            mod = all_mods[k]
            info = size_map[k]
            row = [mod.name, mod.key, "Local" if mod.is_local else "Steam"]
            for p in presets:
                if k in p.mods:
                    row.append("" if info.api_bytes is None else str(info.api_bytes))
                    row.append("" if info.disk_bytes is None else str(info.disk_bytes))
                else:
                    row.extend(["MISSING", "MISSING"])
            writer.writerow(row)


def parse_drag_and_drop_paths(raw: str) -> List[str]:
    """Split concatenated, single-quoted, double-quoted, or unquoted Windows paths."""
    normalized = re.sub(r'(?<=\S)(?=["\']?[A-Za-z]:[\\/])', " ", raw.strip())
    normalized = re.sub(r'(["\'])(?=["\'])', r"\1 ", normalized)
    tokens: List[str] = []
    for match in re.finditer(r'"([^"]+)"|\'([^\']+)\'|([^\s"\']+)', normalized):
        tok = match.group(1) or match.group(2) or match.group(3)
        if tok:
            tok = tok.strip().strip("\"'")
            if tok:
                tokens.append(tok)
    return tokens


def interactive_select_presets() -> List[Path]:
    print("\n=== DD Preset Comparator (DD_PC) ===")
    print("Checks and compares Arma 3 mod presets (.html / .preset2) by local disk size and Steam Workshop API size.")
    print("\nHow to use:")
    print("  - Drag & drop one or more .html / .preset2 files into this window (all at once or one by one).")
    print("  - Press Enter on an empty line to start the comparison.")
    print("  - Type 'q' and press Enter to exit.\n")

    selected: List[Path] = []
    seen = set()
    while True:
        try:
            raw = input("> ").strip()
        except EOFError:
            break

        if raw.lower() in ("q", "quit", "exit"):
            return []

        if not raw:
            if selected:
                break
            print("No files selected yet. Drag & drop .html / .preset2 file(s) here, or type 'q' to exit:")
            continue

        added_now = 0
        for token in parse_drag_and_drop_paths(raw):
            p = Path(token)
            if p.is_file():
                rp = str(p.resolve()).lower()
                if rp not in seen:
                    seen.add(rp)
                    selected.append(p)
                    added_now += 1
            else:
                print(f"File not found: {token}")

        if selected and added_now > 0:
            names = ", ".join(p.name for p in selected)
            print(
                f"Selected ({len(selected)}): {names}\n"
                "Drag & drop more files, or press Enter to run comparison:"
            )
        elif not selected:
            print("No valid files found. Drag & drop .html / .preset2 files or type 'q' to exit:")

    return selected


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="DD_PresetComparator",
        description="DD Preset Comparator (DD_PC): compare Arma 3 preset sizes (.html / .preset2) between local disk and Steam Workshop API.",
    )
    parser.add_argument(
        "presets",
        nargs="*",
        help="Paths to preset files (.html or .preset2)",
    )
    parser.add_argument(
        "--sort",
        choices=("name", "size", "diff"),
        default="name",
        help="Row sort order: name, size (descending), or diff (mismatches first)",
    )
    parser.add_argument(
        "--only-diff",
        action="store_true",
        help="Show only mods not present in all presets",
    )
    parser.add_argument(
        "--show-id",
        action="store_true",
        help="Show Steam Workshop ID next to mod name",
    )
    parser.add_argument(
        "--workshop-dir",
        type=Path,
        default=None,
        help=r"Path to Steam Workshop folder (...\workshop\content\107410)",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        action="append",
        default=[],
        help="Extra folder to search for local mods (can be specified multiple times)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in terminal",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Export comparison table to a CSV file",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="Do not wait for Enter before exiting when running as .exe",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser


def run_comparison(preset_paths: List[Path], args: argparse.Namespace) -> int:
    valid_paths: List[Path] = []
    for p in preset_paths:
        if not p.is_file():
            print(f"Error: file not found: {p}", file=sys.stderr)
            return 1
        valid_paths.append(p)

    presets: List[PresetData] = []
    all_mods: Dict[str, ModRef] = {}
    for p in valid_paths:
        pdata = parse_preset(p)
        presets.append(pdata)
        for k, mod in pdata.mods.items():
            if k not in all_mods:
                all_mods[k] = mod

    print(
        f"Loaded presets: {len(presets)} | Unique mods: {len(all_mods)}. "
        "Querying Steam API and scanning disk..."
    )

    size_map = resolve_all_mod_sizes(
        all_mods,
        custom_workshop_dir=args.workshop_dir,
        extra_local_dirs=args.local_dir,
    )

    render_table(
        presets=presets,
        all_mods=all_mods,
        size_map=size_map,
        sort_by=args.sort,
        only_diff=args.only_diff,
        show_id=args.show_id,
        use_color=not args.no_color,
    )

    if args.csv:
        export_csv(args.csv, presets, all_mods, size_map)
        print(f"CSV saved: {args.csv}")

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    enable_windows_ansi_and_utf8()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    had_cli_presets = bool(args.presets)
    preset_paths: List[Path] = [Path(p) for p in args.presets]
    exit_code = 0

    while True:
        if not preset_paths:
            preset_paths = interactive_select_presets()
            if not preset_paths:
                print("Exiting.")
                return exit_code

        exit_code = run_comparison(preset_paths, args)
        preset_paths = []

        if args.no_pause:
            return exit_code
        if had_cli_presets and not getattr(sys, "frozen", False):
            return exit_code

        try:
            next_raw = input(
                "\nPress Enter for a new comparison, drag & drop new files, or type 'q' to exit: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return exit_code

        if next_raw.lower() in ("q", "quit", "exit"):
            return exit_code

        if next_raw:
            dropped = [Path(t) for t in parse_drag_and_drop_paths(next_raw) if Path(t).is_file()]
            if dropped:
                preset_paths = dropped


if __name__ == "__main__":
    raise SystemExit(main())
