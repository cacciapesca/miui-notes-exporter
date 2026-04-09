# miui-notes-exporter

A command-line tool written in Python to convert MIUI Notes backup files (`.bak`) into Markdown or HTML files on your filesystem, preserving folder structure, note timestamps, and inline images.

## Why?

I wanted to move all of my notes written on the default Notes app on my Xiaomi phone to a FOSS alternative. That's when I found out MIUI's Notes app doesn't provide any useful way to export notes in bulk in a cross-compatible format. 

I searched online and found that a tool to export the notes in bulk already existed but relied on Xiaomi's Cloud and that for me was a no-go. 

I decided instead to try to decode the backup format of the Notes app and create this script. 

## How to get your backup file

On your phone: **Settings → About phone → Back up and restore → Mobile device**

Enter your PIN, then select only **Notes** under *Other system app data* and tap **Back up**.

The `.bak` file will be saved to `MIUI/Backup/AllBackup/` inside a folder
named after the date and time of the backup.

## Requirements for this script

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

## Tested on

| | |
|---|---|
| OS | MIUI Global 13.0.3 Stable |
| Notes app | 7.8.6 |


## Limitations

- Only `common` note type is supported. Notes of other types (e.g. mind maps) are skipped with a warning.
- Some metadata (alarm timestamps, background styles) is not carried over to the output files, which is intentional and out of scope for this project.
- as of right now the --keep-html flag doesn't reformat newlines into html compatible newlines

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software, provided that any
modified version is also released under the same license and its source code
is made available.
