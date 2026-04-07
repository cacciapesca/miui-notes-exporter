# miui-notes-exporter

A command-line tool to convert MIUI Notes backup files (`.bak`) into Markdown or HTML files on your filesystem, preserving folder structure, note timestamps, and image attachments.

## Requirements

- Python 3.10+
- `markdownify`
- `beautifulsoup4`

Install dependencies with:
```
pip install markdownify beautifulsoup4
```

## Usage

```
python miui_notes_converter.py <path/to/Notes(com.miui.notes).bak> [options]
```

| Flag | Description |
|---|---|
| `--keep-html` | Output notes as `.html` instead of converting to Markdown |
| `--linux-filenames` | Only strip `/` and `\` from filenames, keeping Windows-unsafe characters |

Output is written to a `Notes/` folder in the current working directory.

## How to get your backup file

On your phone: **Settings → About phone → Back up and restore → Mobile device**

Enter your PIN, then select only **Notes** under *Other system app data* and tap **Back up**.

The `.bak` file will be saved to `MIUI/Backup/AllBackup/` inside a folder
named after the date and time of the backup.

## Tested on

| | |
|---|---|
| OS | MIUI Global 13.0.3 Stable |
| Notes app | 7.8.6 |


## Limitations

- Only `common` note type is supported. Notes of other types (e.g. mind maps) are skipped with a warning.
- Some metadata (alarm timestamps, background styles) is not carried over to the
  output files, which is intentional and out of scope for this project.
## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software, provided that any
modified version is also released under the same license and its source code
is made available.
