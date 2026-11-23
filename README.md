# DD Preset Comparator (DD_PC)

CLI tool that measures and compares Arma 3 mod presets (100+ mods) by local disk size and Steam Workshop API size.

## Identifiers

| Item | Value |
|---|---|
| Project | `DD_PresetComparator` |
| Abbreviation | `DD_PC` |
| Release binary | `DD_PresetComparator.exe` |
| Source | `preset_comparator.py` |

## Use

### From a release

1. Download `DD_PresetComparator.exe` from the GitHub Releases page.
2. Drag one or more `.html` or `.preset2` preset files onto `DD_PresetComparator.exe`.
3. Or double-click `DD_PresetComparator.exe`, then drag files into the console window and press `Enter` on an empty line.

You can also run it from a terminal:

```powershell
DD_PresetComparator.exe "C:\path\preset1.html" "C:\path\preset2.html"
```

### From source

Requires Python 3.11+ (uses the standard library only):

```powershell
python preset_comparator.py "C:\path\preset1.html" "C:\path\preset2.html"
```

## Table format

- Column 1 shows the mod name (`[L]` marks a local mod, `[DLC]` marks a Creator DLC).
- Each preset column shows the mod or DLC size.
- If the local disk size and the Steam API size match, the cell shows one value.
- If they differ, the cell shows `Disk / API` and highlights the mismatched or missing value in yellow text.
- An empty cell with a dark red background means that the mod is not in that preset.
- The footer shows `Mods (total / on disk)`, `TOTAL SIZE (Disk / API)`, and `Diff vs 1st preset (Disk / API)`.

## Options

| Flag | Effect |
|---|---|
| `--sort name\|size\|diff` | Sort rows by name, size (descending), or differences first |
| `--only-diff` | Show only mods that are not in all presets |
| `--show-id` | Show the Steam Workshop ID next to the mod name |
| `--csv <file>` | Export the table to CSV (`;` delimiter, UTF-8 with BOM) |
| `--workshop-dir <path>` | Custom `steamapps\workshop\content\107410` folder |
| `--local-dir <path>` | Extra folder for local mods (can repeat) |
| `--no-color` | Disable ANSI colors |
| `--no-pause` | Exit immediately when `.exe` finishes |
| `-v`, `--version` | Show version and exit |

## Build the executable

Run `build_exe.bat` to build `releases\DD_PresetComparator.exe` (it installs `PyInstaller` with `--user` if missing):

```powershell
build_exe.bat
```

Or build manually:

```powershell
python -m pip install --user pyinstaller
python -m PyInstaller --noconfirm --onefile --clean --distpath releases --workpath build --specpath build --name DD_PresetComparator preset_comparator.py
```
