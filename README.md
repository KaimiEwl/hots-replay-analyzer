# HOTS Replay Analyzer

Tools for parsing Heroes of the Storm `.StormReplay` files and producing structured replay reports.

The current project is a local analyzer prototype. It uses Blizzard's `heroprotocol` parser, extracts replay events, and writes JSON/Markdown reports with score data, deaths, talent windows, camps, structures, and basic analysis flags.

## What is included

- `tools/hots_replay_deep_analyzer.py` - detailed single replay report.
- `tools/hots_replay_batch_analyzer.py` - batch replay summary and flagging.
- `tools/chromie_compare_report.py` - focused Chromie comparison helper.
- `tools/heroprotocol/` - vendored `heroprotocol` dependency used by the analyzers.

Local replay files, generated reports, and personal analysis notes are intentionally ignored by git. Put your own `.StormReplay` files in any local folder and pass their paths to the scripts.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Analyze One Replay

```powershell
python tools\hots_replay_deep_analyzer.py `
  --replay "C:\path\to\match.StormReplay" `
  --player-name "PlayerName" `
  --outdir analysis\deep_replay
```

You can also select the player by slot or pid:

```powershell
python tools\hots_replay_deep_analyzer.py `
  --replay "C:\path\to\match.StormReplay" `
  --player-slot 3 `
  --outdir analysis\deep_replay
```

## Analyze A Folder

```powershell
python tools\hots_replay_batch_analyzer.py `
  --folder "C:\path\to\replays" `
  --player-name "PlayerName" `
  --outdir analysis\batch_report
```

For batch mode, the analyzer needs to know which player is the primary player. You can pass `--player-name` more than once, or pass `--account-toon-id` if you know the HOTS toon id.

## Notes

This repository is prepared as a clean shareable code snapshot. It does not include private replay uploads, generated analysis output, `.env` files, logs, or local project diary notes.

The planned next step is to wrap the analyzer into a small web service where users upload a replay, choose their player, and receive an HTML report.
