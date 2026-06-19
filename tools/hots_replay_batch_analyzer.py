import argparse
import csv
import json
import math
import os
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path


sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HEROPROTOCOL = ROOT / "tools" / "heroprotocol"
if str(LOCAL_HEROPROTOCOL) not in sys.path:
    sys.path.insert(0, str(LOCAL_HEROPROTOCOL))


def install_imp_compat():
    import types
    import importlib.util
    import importlib.machinery

    if "imp" in sys.modules:
        return

    imp = types.ModuleType("imp")

    def find_module(name, path=None):
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None:
            raise ImportError(name)
        fp = (
            open(spec.origin, "rb")
            if spec.origin and spec.origin not in ("built-in", "frozen")
            else None
        )
        return fp, spec.origin, (None, None, None)

    def load_module(name, fp, pathname, desc):
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, pathname)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    imp.find_module = find_module
    imp.load_module = load_module
    sys.modules["imp"] = imp


install_imp_compat()

from heroprotocol import versions  # noqa: E402
import mpyq  # noqa: E402


DEFAULT_ACCOUNT_TOON_ID = 245407
DEFAULT_PLAYER_NAMES = {"watareucasul", "winter", "whatareyoucasul"}
TALENT_TIERS = {4, 7, 10, 13, 16, 20}
ARAM_MAPS = {"Lost Cavern", "Silver City", "Industrial District", "Braxis Outpost"}


def dec(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if value is None:
        return ""
    return str(value)


def mmss(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def normalize_player_name(value):
    return dec(value).replace(" ", "").lower()


def available_protocol_builds():
    versions_dir = LOCAL_HEROPROTOCOL / "heroprotocol" / "versions"
    builds = []
    for path in versions_dir.glob("protocol*.py"):
        stem = path.stem
        if stem.startswith("protocol") and stem[8:].isdigit():
            builds.append(int(stem[8:]))
    return sorted(set(builds))


def load_protocol(archive):
    header = versions.latest().decode_replay_header(
        archive.header["user_data_header"]["content"]
    )
    base_build = header["m_version"]["m_baseBuild"]
    try:
        protocol = versions.build(base_build)
        fallback = False
    except Exception:
        supported = [build for build in available_protocol_builds() if build <= base_build]
        if not supported:
            raise
        protocol = versions.build(max(supported))
        fallback = True
    return header, protocol, fallback


def score_table(score_event):
    table = defaultdict(dict)
    if not score_event:
        return table
    for instance in score_event.get("m_instanceList", []):
        name = dec(instance.get("m_name"))
        for slot, values in enumerate(instance.get("m_values", [])):
            if not values:
                continue
            table[slot][name] = values[-1].get("m_value")
    return table


def find_player(details, account_toon_id=DEFAULT_ACCOUNT_TOON_ID, player_names=None):
    players = details.get("m_playerList", [])
    player_names = player_names or DEFAULT_PLAYER_NAMES
    for idx, player in enumerate(players):
        toon = player.get("m_toon") or {}
        if account_toon_id is not None and toon.get("m_id") == account_toon_id:
            return idx, player
    for idx, player in enumerate(players):
        name = normalize_player_name(player.get("m_name"))
        if name in player_names:
            return idx, player
    return None, None


def team_players(details, team_id):
    out = []
    for idx, player in enumerate(details.get("m_playerList", [])):
        if player.get("m_teamId") == team_id:
            out.append(idx)
    return out


def parse_tracker(protocol, archive, player_slot, player_team):
    score_event = None
    deaths = []
    team_deaths = []
    enemy_deaths = []
    talents = []
    level_events = []
    camp_events = []
    structure_deaths = []
    core_death = None
    objective_events = []

    unit_types = {}

    def unit_tag(ev):
        return (ev.get("m_unitTagIndex"), ev.get("m_unitTagRecycle"))

    try:
        stream = archive.read_file("replay.tracker.events")
    except Exception:
        return {
            "score": {},
            "deaths": [],
            "team_deaths": [],
            "enemy_deaths": [],
            "talents": [],
            "level_events": [],
            "camp_events": [],
            "structure_deaths": [],
            "core_death": None,
            "objective_events": [],
        }

    for ev in protocol.decode_replay_tracker_events(stream):
        name = ev.get("_event", "")
        t = ev.get("_gameloop", 0) / 16.0

        if name == "NNet.Replay.Tracker.SScoreResultEvent":
            score_event = ev

        elif name == "NNet.Replay.Tracker.SUnitBornEvent":
            unit_types[unit_tag(ev)] = dec(ev.get("m_unitTypeName"))

        elif name == "NNet.Replay.Tracker.SUnitOwnerChangeEvent":
            typ = unit_types.get(unit_tag(ev), "")
            if typ == "TownMercCampCaptureBeacon":
                owner = ev.get("m_controlPlayerId")
                # HOTS player ids 1-5/6-10 are heroes, 11/12 are team upkeep.
                side = "unknown"
                if owner == 11:
                    side = "left"
                elif owner == 12:
                    side = "right"
                camp_events.append(
                    {
                        "time": round(t, 1),
                        "time_s": mmss(t),
                        "owner": owner,
                        "side": side,
                        "x": ev.get("m_x"),
                        "y": ev.get("m_y"),
                    }
                )

        elif name == "NNet.Replay.Tracker.SUnitDiedEvent":
            typ = unit_types.get(unit_tag(ev), "")
            if typ in {"KingsCore", "TownTownHallL2", "TownTownHallL3"} or "Fort" in typ:
                structure_deaths.append(
                    {
                        "time": round(t, 1),
                        "time_s": mmss(t),
                        "type": typ,
                        "owner": ev.get("m_killerPlayerId"),
                        "x": ev.get("m_x"),
                        "y": ev.get("m_y"),
                    }
                )
            if typ == "KingsCore":
                core_death = {
                    "time": round(t, 1),
                    "time_s": mmss(t),
                    "killer": ev.get("m_killerPlayerId"),
                }

        elif name.endswith("SPlayerDeathEvent") or "PlayerDeath" in name:
            # Some protocol versions expose this event; older replays may not.
            victim = ev.get("m_victimPlayerId")
            entry = {
                "time": round(t, 1),
                "time_s": mmss(t),
                "victim": victim,
                "killers": ev.get("m_killingPlayerIds") or ev.get("m_killingPlayers"),
            }
            if victim == player_slot + 1:
                deaths.append(entry)
            # Player ids are 1-based in many tracker events.
            if player_team == 0:
                if victim and 1 <= victim <= 5:
                    team_deaths.append(entry)
                elif victim and 6 <= victim <= 10:
                    enemy_deaths.append(entry)
            elif player_team == 1:
                if victim and 6 <= victim <= 10:
                    team_deaths.append(entry)
                elif victim and 1 <= victim <= 5:
                    enemy_deaths.append(entry)

        elif "Talent" in name:
            pid = ev.get("m_playerId") or ev.get("m_player") or ev.get("m_userId")
            talents.append(
                {
                    "time": round(t, 1),
                    "time_s": mmss(t),
                    "player": pid,
                    "event": name,
                    "talent": dec(ev.get("m_talentId") or ev.get("m_talentName")),
                }
            )

        elif "ScoreValue" in name or "Level" in name:
            pass

        elif name == "NNet.Replay.Tracker.SStatGameEvent":
            event_name = dec(ev.get("m_eventName"))
            if event_name:
                lower = event_name.lower()
                if any(token in lower for token in ["level", "talent"]):
                    level_events.append(
                        {"time": round(t, 1), "time_s": mmss(t), "name": event_name, "data": ev}
                    )
                if any(
                    token in lower
                    for token in [
                        "souleaters",
                        "webweaver",
                        "objective",
                        "shrine",
                        "temple",
                        "beacon",
                        "payload",
                        "plant",
                        "dragon",
                        "altar",
                    ]
                ):
                    objective_events.append(
                        {"time": round(t, 1), "time_s": mmss(t), "name": event_name}
                    )

    return {
        "score": score_table(score_event),
        "deaths": deaths,
        "team_deaths": team_deaths,
        "enemy_deaths": enemy_deaths,
        "talents": talents,
        "level_events": level_events,
        "camp_events": camp_events,
        "structure_deaths": structure_deaths,
        "core_death": core_death,
        "objective_events": objective_events,
    }


def analyze_replay(path, account_toon_id=DEFAULT_ACCOUNT_TOON_ID, player_names=None):
    archive = mpyq.MPQArchive(str(path))
    header, protocol, fallback = load_protocol(archive)
    details = protocol.decode_replay_details(archive.read_file("replay.details"))

    player_slot, player = find_player(details, account_toon_id, player_names)
    if player is None:
        raise RuntimeError("Primary player not found")

    map_name = dec(details.get("m_title"))
    hero = dec(player.get("m_hero"))
    name = dec(player.get("m_name"))
    team = player.get("m_teamId")
    result = player.get("m_result")
    valid_result = result in (1, 2)
    won = True if result == 1 else False if result == 2 else None
    players = details.get("m_playerList", [])
    allies = [dec(players[i].get("m_hero")) for i in team_players(details, team)]
    enemies = [
        dec(p.get("m_hero"))
        for p in players
        if p.get("m_teamId") is not None and p.get("m_teamId") != team
    ]

    tracker = parse_tracker(protocol, archive, player_slot, team)
    score = tracker["score"].get(player_slot, {})
    game_length = None
    if tracker["score"]:
        times = []
        # SScoreResultEvent m_time is in seconds in observed builds.
        # Use final score time where available.
        # Fall back to core death or file name if not available.
        for slot_values in tracker["score"].values():
            if slot_values:
                pass
    if score:
        # Most scores are final-only; SScoreResultEvent time is not retained in score_table.
        game_length = tracker["core_death"]["time"] if tracker["core_death"] else None
    if not game_length and tracker["core_death"]:
        game_length = tracker["core_death"]["time"]

    deaths_count = score.get("Deaths")
    dead_time = score.get("TimeSpentDead")
    xp = score.get("ExperienceContribution")
    hero_damage = score.get("HeroDamage")
    siege_damage = score.get("SiegeDamage")
    structure_damage = score.get("StructureDamage")
    merc_caps = score.get("MercCampCaptures")
    takedowns = score.get("Takedowns")
    assists = score.get("Assists")
    kills = score.get("SoloKill")
    gems = score.get("GemsTurnedIn")

    flags = []
    severity = 0
    if deaths_count is not None:
        if deaths_count >= 6:
            flags.append("high_deaths")
            severity += 4
        elif deaths_count >= 4:
            flags.append("medium_deaths")
            severity += 2
    if dead_time is not None:
        if dead_time >= 180:
            flags.append("high_dead_time")
            severity += 4
        elif dead_time >= 100:
            flags.append("medium_dead_time")
            severity += 2
    if xp is not None and game_length and game_length > 900 and xp < 9000:
        flags.append("low_xp")
        severity += 2
    if merc_caps is not None and merc_caps == 0 and map_name not in ARAM_MAPS:
        flags.append("zero_camps")
        severity += 1
    if hero_damage is not None and game_length and game_length > 900 and hero_damage < 25000:
        flags.append("low_hero_damage")
        severity += 1
    if structure_damage is not None and game_length and game_length > 900 and structure_damage < 5000:
        flags.append("low_structure_conversion")
        severity += 1
    if won is False:
        severity += 2

    return {
        "file": str(path),
        "name": path.name,
        "date_from_name": path.name[:19],
        "size": path.stat().st_size,
        "build": header["m_version"]["m_baseBuild"],
        "fallback_protocol": fallback,
        "map": map_name,
        "player_name": name,
        "hero": hero,
        "team": team,
        "won": won,
        "valid_result": valid_result,
        "result": result,
        "allies": allies,
        "enemies": enemies,
        "game_length": round(game_length, 1) if game_length else None,
        "game_length_s": mmss(game_length) if game_length else "",
        "score": {
            "takedowns": takedowns,
            "solo_kills": kills,
            "assists": assists,
            "deaths": deaths_count,
            "dead_time": dead_time,
            "xp": xp,
            "hero_damage": hero_damage,
            "siege_damage": siege_damage,
            "structure_damage": structure_damage,
            "merc_caps": merc_caps,
            "gems": gems,
            "healing": score.get("Healing"),
            "damage_taken": score.get("DamageTaken"),
        },
        "events": {
            "player_deaths": tracker["deaths"][:20],
            "team_deaths": tracker["team_deaths"][:50],
            "enemy_deaths": tracker["enemy_deaths"][:50],
            "camp_events": tracker["camp_events"][:80],
            "structures": tracker["structure_deaths"][:80],
            "objectives": tracker["objective_events"][:80],
        },
        "flags": flags,
        "severity": severity,
    }


def batch_key(index, size=5):
    return index // size + 1


def write_outputs(records, errors, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    raw_path = outdir / "all_replays_summary.json"
    raw_path.write_text(json.dumps({"records": records, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = outdir / "all_replays_summary.csv"
    fields = [
        "idx",
        "batch",
        "date_from_name",
        "name",
        "map",
        "hero",
        "won",
        "game_length_s",
        "deaths",
        "dead_time",
        "xp",
        "hero_damage",
        "siege_damage",
        "structure_damage",
        "merc_caps",
        "gems",
        "severity",
        "flags",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for i, r in enumerate(records):
            score = r["score"]
            writer.writerow(
                {
                    "idx": i + 1,
                    "batch": batch_key(i),
                    "date_from_name": r["date_from_name"],
                    "name": r["name"],
                    "map": r["map"],
                    "hero": r["hero"],
                    "won": r["won"],
                    "game_length_s": r["game_length_s"],
                    "deaths": score.get("deaths"),
                    "dead_time": score.get("dead_time"),
                    "xp": score.get("xp"),
                    "hero_damage": score.get("hero_damage"),
                    "siege_damage": score.get("siege_damage"),
                    "structure_damage": score.get("structure_damage"),
                    "merc_caps": score.get("merc_caps"),
                    "gems": score.get("gems"),
                    "severity": r["severity"],
                    "flags": ",".join(r["flags"]),
                }
            )

    md_path = outdir / "batch_overview.md"
    lines = []
    lines.append("# Batch Overview")
    lines.append("")
    lines.append(f"Всего успешно прочитано replay: `{len(records)}`.")
    lines.append(f"Ошибок чтения: `{len(errors)}`.")
    lines.append("")

    maps = Counter(r["map"] for r in records)
    heroes = Counter(r["hero"] for r in records)
    flags = Counter(flag for r in records for flag in r["flags"])
    lines.append("## Общая Статистика")
    lines.append("")
    valid_records = [r for r in records if r.get("valid_result")]
    wins = sum(1 for r in valid_records if r["won"] is True)
    lines.append(f"- Valid result replay: `{len(valid_records)}/{len(records)}`; unknown/result=0 excluded: `{len(records) - len(valid_records)}`.")
    lines.append(f"- Winrate: `{wins}/{len(valid_records)}` = `{(wins / len(valid_records) * 100 if valid_records else 0):.1f}%`.")
    lines.append("- Топ карт: " + ", ".join(f"`{k}` {v}" for k, v in maps.most_common(12)) + ".")
    lines.append("- Топ героев: " + ", ".join(f"`{k}` {v}" for k, v in heroes.most_common(15)) + ".")
    lines.append("- Авто-флаги: " + ", ".join(f"`{k}` {v}" for k, v in flags.most_common()) + ".")
    lines.append("")

    lines.append("## Батчи По 5 Replay")
    lines.append("")
    for b in range(1, math.ceil(len(records) / 5) + 1):
        chunk = records[(b - 1) * 5 : b * 5]
        if not chunk:
            continue
        chosen = max(chunk, key=lambda r: (r["severity"], r["won"] is False, r["score"].get("deaths") or 0, r["score"].get("dead_time") or 0))
        chunk_flags = Counter(flag for r in chunk for flag in r["flags"])
        lines.append(f"### Batch {b:03d}")
        lines.append("")
        chosen_result = "win" if chosen["won"] is True else "loss" if chosen["won"] is False else "unknown"
        lines.append(f"Deep-dive candidate: `{chosen['name']}` — `{chosen['hero']}` на `{chosen['map']}`, {chosen_result}, severity `{chosen['severity']}`.")
        if chunk_flags:
            lines.append("Common flags: " + ", ".join(f"`{k}` {v}/5" for k, v in chunk_flags.most_common()) + ".")
        else:
            lines.append("Common flags: нет явных авто-флагов.")
        lines.append("")
        lines.append("| # | Replay | Hero | Map | Result | KDA-ish | Dead time | XP | Camps | Flags |")
        lines.append("|---:|---|---|---|---|---:|---:|---:|---:|---|")
        for idx, r in enumerate(chunk, start=(b - 1) * 5 + 1):
            s = r["score"]
            kda = f"{s.get('takedowns') or 0}/{s.get('deaths') or 0}"
            lines.append(
                f"| {idx} | `{r['date_from_name']}` | `{r['hero']}` | `{r['map']}` | "
                f"{'W' if r['won'] is True else 'L' if r['won'] is False else '?'} | `{kda}` | `{s.get('dead_time')}` | "
                f"`{s.get('xp')}` | `{s.get('merc_caps')}` | `{', '.join(r['flags'])}` |"
            )
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return raw_path, csv_path, md_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--account-toon-id", type=int, default=DEFAULT_ACCOUNT_TOON_ID)
    parser.add_argument(
        "--player-name",
        action="append",
        default=[],
        help="Primary player name hint. Can be passed multiple times.",
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    files = sorted(folder.glob("*.StormReplay"), key=lambda p: p.name)
    if args.limit:
        files = files[: args.limit]
    player_names = set(DEFAULT_PLAYER_NAMES)
    player_names.update(normalize_player_name(name) for name in args.player_name)

    records = []
    errors = []
    for idx, path in enumerate(files, start=1):
        try:
            record = analyze_replay(path, args.account_toon_id, player_names)
            records.append(record)
            if idx % 25 == 0:
                print(f"parsed {idx}/{len(files)}")
        except Exception as exc:
            errors.append(
                {
                    "file": str(path),
                    "name": path.name,
                    "error": str(exc),
                    "trace": traceback.format_exc(limit=2),
                }
            )
            print(f"error {idx}/{len(files)} {path.name}: {exc}", file=sys.stderr)

    raw_path, csv_path, md_path = write_outputs(records, errors, Path(args.outdir))
    print(json.dumps({
        "records": len(records),
        "errors": len(errors),
        "raw": str(raw_path),
        "csv": str(csv_path),
        "md": str(md_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
