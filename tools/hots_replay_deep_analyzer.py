import argparse
import importlib.machinery
import importlib.util
import json
import re
import sys
import traceback
import types
from collections import Counter, defaultdict
from pathlib import Path


sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HEROPROTOCOL = ROOT / "tools" / "heroprotocol"

if str(LOCAL_HEROPROTOCOL) not in sys.path:
    sys.path.insert(0, str(LOCAL_HEROPROTOCOL))


def install_imp_compat():
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


HERO_ABILITY_HINTS = {
    "Garrosh": {
        1078: "Bloodthirst/W target unit",
        1081: "Decimate/R",
        1082: "Double Up active",
        1085: "Indomitable active",
        1077: "Garrosh point ability, likely Q/E",
        1079: "Garrosh point ability, likely Q/E",
        1084: "Garrosh point/self ability, low confidence",
    }
}


def dec(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if value is None:
        return ""
    return str(value)


def mmss(seconds):
    seconds = int(round(seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def fixed_to_seconds(value):
    if isinstance(value, (int, float)):
        return round(value / 4096, 1)
    return None


def kv_values(items, as_string=False):
    out = {}
    for item in items or []:
        key = dec(item.get("m_key"))
        value = item.get("m_value")
        out[key] = dec(value) if as_string else value
    return out


def clean(value):
    if isinstance(value, bytes):
        return dec(value)
    if isinstance(value, dict):
        return {clean(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def available_protocol_builds():
    versions_dir = LOCAL_HEROPROTOCOL / "heroprotocol" / "versions"
    builds = []
    for path in versions_dir.glob("protocol*.py"):
        match = re.fullmatch(r"protocol(\d+)\.py", path.name)
        if match:
            builds.append(int(match.group(1)))
    return sorted(set(builds))


def load_protocol_for_archive(archive):
    header = versions.latest().decode_replay_header(
        archive.header["user_data_header"]["content"]
    )
    build = header["m_version"]["m_baseBuild"]
    fallback_build = None
    try:
        protocol = versions.build(build)
    except Exception:
        builds = [b for b in available_protocol_builds() if b <= build]
        if not builds:
            raise
        fallback_build = max(builds)
        protocol = versions.build(fallback_build)
    return header, protocol, fallback_build


def score_table(score_event):
    table = defaultdict(dict)
    score_times = []
    if not score_event:
        return table, None
    for instance in score_event.get("m_instanceList", []):
        key = dec(instance.get("m_name"))
        for slot, values in enumerate(instance.get("m_values", [])):
            if not values:
                continue
            final = values[-1]
            table[slot][key] = final.get("m_value")
            if final.get("m_time") is not None:
                score_times.append(final.get("m_time"))
    return table, max(score_times) if score_times else None


def resolve_replay(args):
    if args.replay:
        path = Path(args.replay)
        if path.exists():
            return path
        raise FileNotFoundError(path)

    folder = Path(args.folder or Path.home() / "Downloads")
    files = list(folder.glob("*.StormReplay"))
    if args.size:
        files = [path for path in files if path.stat().st_size == args.size]
    if args.name_contains:
        needle = args.name_contains.lower()
        files = [path for path in files if needle in path.name.lower()]
    if not files:
        raise FileNotFoundError(f"No .StormReplay files matched in {folder}")
    return max(files, key=lambda path: path.stat().st_mtime)


def find_player(players, args):
    if args.player_slot is not None:
        return args.player_slot
    if args.player_pid is not None:
        return args.player_pid - 1
    if args.player_name:
        needle = args.player_name.replace(" ", "").lower()
        for idx, player in enumerate(players):
            name = dec(player.get("m_name")).replace(" ", "").lower()
            if name == needle:
                return idx
        for idx, player in enumerate(players):
            name = dec(player.get("m_name")).replace(" ", "").lower()
            if needle in name:
                return idx
    raise RuntimeError("Player not found; pass --player-name, --player-slot, or --player-pid")


def parse_tracker(protocol, archive, players):
    out = {
        "event_counts": Counter(),
        "stat_counts": Counter(),
        "score_event": None,
        "levels": defaultdict(dict),
        "talents": defaultdict(list),
        "deaths": [],
        "camps": [],
        "payload_spawns": [],
        "major_structures": [],
        "core_deaths": [],
        "tracker_errors": [],
    }
    unit_types = {}
    unit_owner = {}

    try:
        stream = archive.read_file("replay.tracker.events")
        events = protocol.decode_replay_tracker_events(stream)
        for ev in events:
            name = ev.get("_event", "")
            out["event_counts"][name] += 1
            t = ev.get("_gameloop", 0) / 16.0
            tag = (ev.get("m_unitTagIndex"), ev.get("m_unitTagRecycle"))

            if name in (
                "NNet.Replay.Tracker.SUnitBornEvent",
                "NNet.Replay.Tracker.SUnitInitEvent",
            ):
                typ = dec(ev.get("m_unitTypeName"))
                unit_types[tag] = typ
                unit_owner[tag] = {
                    "control": ev.get("m_controlPlayerId"),
                    "upkeep": ev.get("m_upkeepPlayerId"),
                    "x": ev.get("m_x"),
                    "y": ev.get("m_y"),
                }
                low = typ.lower()
                if any(token in low for token in ("payload", "sentinel", "samurai")):
                    out["payload_spawns"].append(
                        {
                            "time": mmss(t),
                            "time_s": round(t, 1),
                            "type": typ,
                            "control": ev.get("m_controlPlayerId"),
                            "upkeep": ev.get("m_upkeepPlayerId"),
                            "x": ev.get("m_x"),
                            "y": ev.get("m_y"),
                        }
                    )

            elif name == "NNet.Replay.Tracker.SUnitOwnerChangeEvent":
                unit_owner[tag] = {
                    "control": ev.get("m_controlPlayerId"),
                    "upkeep": ev.get("m_upkeepPlayerId"),
                    "x": ev.get("m_x"),
                    "y": ev.get("m_y"),
                }

            elif name == "NNet.Replay.Tracker.SUnitDiedEvent":
                typ = unit_types.get(tag, "")
                owner = unit_owner.get(tag, {})
                if typ == "KingsCore":
                    out["core_deaths"].append(
                        {
                            "time": mmss(t),
                            "time_s": round(t, 1),
                            "type": typ,
                            "owner_control": owner.get("control"),
                            "owner_upkeep": owner.get("upkeep"),
                            "killer": ev.get("m_killerPlayerId"),
                            "x": ev.get("m_x"),
                            "y": ev.get("m_y"),
                        }
                    )
                elif typ.startswith("TownTownHall"):
                    out["major_structures"].append(
                        {
                            "time": mmss(t),
                            "time_s": round(t, 1),
                            "type": typ,
                            "owner_control": owner.get("control"),
                            "owner_upkeep": owner.get("upkeep"),
                            "killer": ev.get("m_killerPlayerId"),
                            "x": ev.get("m_x"),
                            "y": ev.get("m_y"),
                        }
                    )

            elif name == "NNet.Replay.Tracker.SScoreResultEvent":
                out["score_event"] = ev

            elif name == "NNet.Replay.Tracker.SStatGameEvent":
                event_name = dec(ev.get("m_eventName"))
                out["stat_counts"][event_name] += 1
                int_data = kv_values(ev.get("m_intData"))
                fixed_data = kv_values(ev.get("m_fixedData"))
                string_data = kv_values(ev.get("m_stringData"), as_string=True)

                if event_name == "LevelUp":
                    out["levels"][int_data.get("Level")][int_data.get("PlayerID")] = t
                elif event_name == "TalentChosen":
                    pid = int_data.get("PlayerID")
                    out["talents"][pid].append(
                        {"time": mmss(t), "time_s": round(t, 1), "talent": string_data.get("PurchaseName")}
                    )
                elif event_name == "PlayerDeath":
                    out["deaths"].append(
                        {
                            "time": mmss(t),
                            "time_s": round(t, 1),
                            "player": int_data.get("PlayerID"),
                            "killer": int_data.get("KillingPlayer"),
                            "x": fixed_data.get("PositionX"),
                            "y": fixed_data.get("PositionY"),
                        }
                    )
                elif event_name == "JungleCampCapture":
                    team_value = fixed_data.get("TeamID")
                    team_id = int(team_value / 4096) - 1 if isinstance(team_value, (int, float)) else None
                    out["camps"].append(
                        {
                            "time": mmss(t),
                            "time_s": round(t, 1),
                            "team_id": team_id,
                            "camp_id": int_data.get("CampID"),
                            "camp_type": string_data.get("CampType"),
                        }
                    )
    except Exception as exc:
        out["tracker_errors"].append({"error": str(exc), "trace": traceback.format_exc(limit=3)})
    return out


def cluster_ability_events(events):
    clusters = []
    for event in events:
        if (
            clusters
            and event["ability_id"] == clusters[-1]["ability_id"]
            and event["time_s"] - clusters[-1]["end_s"] < 0.45
        ):
            clusters[-1]["end_s"] = event["time_s"]
            clusters[-1]["raw_count"] += 1
            if event.get("target_player"):
                clusters[-1]["target_players"].append(event["target_player"])
        else:
            clusters.append(
                {
                    "time": event["time"],
                    "time_s": event["time_s"],
                    "end_s": event["time_s"],
                    "ability_id": event["ability_id"],
                    "ability_hint": event.get("ability_hint"),
                    "raw_count": 1,
                    "target_players": [event["target_player"]] if event.get("target_player") else [],
                    "x": event.get("x"),
                    "y": event.get("y"),
                }
            )
    for cluster in clusters:
        cluster["target_players"] = list(dict.fromkeys(cluster["target_players"]))
        cluster["end"] = mmss(cluster["end_s"])
    return clusters


def parse_game_events(protocol, archive, target_slot, hero_english):
    out = {"ability_counts": Counter(), "ability_events": [], "game_errors": []}
    hints = HERO_ABILITY_HINTS.get(hero_english, {})
    try:
        stream = archive.read_file("replay.game.events")
        for ev in protocol.decode_replay_game_events(stream):
            if ev.get("_event") != "NNet.Game.SCmdEvent":
                continue
            user = ev.get("_userid") or {}
            if user.get("m_userId") != target_slot:
                continue
            ability = ev.get("m_abil") or {}
            ability_id = ability.get("m_abilLink")
            if ability_id is None:
                continue
            t = ev.get("_gameloop", 0) / 16.0
            data = ev.get("m_data") or {}
            target_unit = data.get("TargetUnit") or {}
            target_point = data.get("TargetPoint") or {}
            event = {
                "time": mmss(t),
                "time_s": round(t, 2),
                "ability_id": ability_id,
                "ability_cmd": ability.get("m_abilCmdIndex"),
                "ability_hint": hints.get(ability_id),
                "target_player": target_unit.get("m_snapshotControlPlayerId")
                or target_unit.get("m_snapshotUpkeepPlayerId"),
                "x": target_point.get("x"),
                "y": target_point.get("y"),
            }
            out["ability_events"].append(event)
            out["ability_counts"][ability_id] += 1
    except Exception as exc:
        out["game_errors"].append({"error": str(exc), "trace": traceback.format_exc(limit=3)})
    return out


def player_label(pid, name_by_pid, hero_by_pid):
    return f"{pid} {name_by_pid.get(pid, '')} ({hero_by_pid.get(pid, '')})"


def summarize_death_context(target_pid, deaths, ability_clusters):
    contexts = []
    for death in deaths:
        if death.get("player") != target_pid:
            continue
        t = death["time_s"]
        nearby_deaths = [
            d
            for d in deaths
            if t - 20 <= d["time_s"] <= t + 12 and d is not death
        ]
        nearby_abilities = [
            a
            for a in ability_clusters
            if t - 20 <= a["time_s"] <= t + 2
        ]
        contexts.append(
            {
                "death": death,
                "nearby_deaths": nearby_deaths,
                "nearby_abilities": nearby_abilities,
            }
        )
    return contexts


def level_summary(levels, team_pids, enemy_pids):
    rows = []
    for level in [1, 2, 3, 4, 7, 10, 13, 16, 20]:
        values = levels.get(level, {})
        team_time = max([values[pid] for pid in team_pids if pid in values], default=None)
        enemy_time = max([values[pid] for pid in enemy_pids if pid in values], default=None)
        rows.append(
            {
                "level": level,
                "team": mmss(team_time) if team_time is not None else None,
                "enemy": mmss(enemy_time) if enemy_time is not None else None,
                "diff_s": round(team_time - enemy_time, 1)
                if team_time is not None and enemy_time is not None
                else None,
            }
        )
    return rows


def compact_score(score):
    keys = [
        "Takedowns",
        "SoloKill",
        "Assists",
        "Deaths",
        "TimeSpentDead",
        "ExperienceContribution",
        "HeroDamage",
        "SiegeDamage",
        "StructureDamage",
        "MinionDamage",
        "MercCampCaptures",
        "WatchTowerCaptures",
        "Healing",
        "SelfHealing",
        "DamageTaken",
        "DamageSoaked",
        "TeamfightHeroDamage",
        "TeamfightDamageTaken",
        "TimeCCdEnemyHeroes",
        "TimeStunningEnemyHeroes",
        "EscapesPerformed",
        "OutnumberedDeaths",
        "HighestKillStreak",
        "Multikill",
        "MinionKills",
        "RegenGlobes",
        "TimeOnPayload",
        "Level",
        "TeamLevel",
    ]
    return {key: score.get(key) for key in keys if key in score}


def write_markdown(path, result):
    target = result["target"]
    lines = [
        "# HOTS Replay Deep Report",
        "",
        f"Replay: `{result['replay_name']}`",
        f"Map: `{result['map']}`",
        f"Build: `{result['build']}`; protocol used: `{result['protocol_build']}`",
        f"Fallback: `{result['fallback']}`",
        "",
        "## Target",
        "",
        f"- Player: `{target['name']}` / `{target['hero']}`",
        f"- Result: `{'win' if target['won'] else 'loss'}`",
        f"- Score: `{target['score'].get('Takedowns')}/{target['score'].get('Deaths')}/{target['score'].get('Assists')}`",
        f"- Dead time: `{target['score'].get('TimeSpentDead')}s`",
        f"- Damage taken: `{target['score'].get('DamageTaken')}`",
        f"- Structure damage: `{target['score'].get('StructureDamage')}`",
        f"- Camps: `{target['score'].get('MercCampCaptures')}`",
        "",
        "## Teams",
        "",
    ]
    for player in result["players"]:
        side = "target team" if player["team"] == target["team"] else "enemy"
        score = player.get("score", {})
        lines.append(
            f"- `{side}` `{player['pid']}` `{player['name']}` `{player['hero']}` "
            f"`{score.get('Takedowns')}/{score.get('Deaths')}/{score.get('Assists')}` "
            f"struct `{score.get('StructureDamage')}`"
        )

    lines += ["", "## Level Windows", ""]
    for row in result["level_summary"]:
        lines.append(
            f"- `{row['level']}` target team `{row['team']}`, enemy `{row['enemy']}`, diff `{row['diff_s']}`"
        )

    lines += ["", "## Target Talents", ""]
    for talent in target["talents"]:
        lines.append(f"- `{talent['time']}` `{talent['talent']}`")

    lines += ["", "## Target Death Context", ""]
    for ctx in result["target_death_contexts"]:
        death = ctx["death"]
        lines.append(f"### Death {death['time']}")
        lines.append("")
        lines.append(f"- Killer/player id: `{death.get('killer')}`")
        if ctx["nearby_deaths"]:
            names = []
            for item in ctx["nearby_deaths"]:
                names.append(
                    f"`{item['time']}` pid `{item['player']}` killed by `{item.get('killer')}`"
                )
            lines.append("- Nearby deaths: " + ", ".join(names))
        if ctx["nearby_abilities"]:
            ability_text = []
            for ability in ctx["nearby_abilities"][-10:]:
                hint = ability.get("ability_hint") or "unknown"
                ability_text.append(f"`{ability['time']}` `{ability['ability_id']}` {hint}")
            lines.append("- Target abilities before death: " + ", ".join(ability_text))
        lines.append("")

    lines += ["## Camps", ""]
    for camp in result["camps"]:
        side = "target team" if camp["team_id"] == target["team"] else "enemy"
        lines.append(f"- `{camp['time']}` `{side}` `{camp['camp_type']}` id `{camp['camp_id']}`")

    lines += ["", "## Payload And Sentinel Proxy", ""]
    for spawn in result["payload_spawns"]:
        lines.append(
            f"- `{spawn['time']}` `{spawn['type']}` control `{spawn['control']}` "
            f"at `{spawn['x']},{spawn['y']}`"
        )

    lines += ["", "## Structures", ""]
    for structure in result["major_structures"]:
        lines.append(
            f"- `{structure['time']}` `{structure['type']}` owner `{structure['owner_control']}` "
            f"killer `{structure['killer']}`"
        )
    for core in result["core_deaths"]:
        lines.append(
            f"- `{core['time']}` CORE owner `{core['owner_control']}` killer `{core['killer']}`"
        )

    lines += ["", "## Ability Counts", ""]
    for row in result["target_ability_summary"]:
        lines.append(
            f"- `{row['ability_id']}` `{row['hint']}` count `{row['count']}`, "
            f"first `{row['first']}`, last `{row['last']}`"
        )

    path.write_text("\n".join(lines), encoding="utf-8-sig")


def analyze(args):
    replay = resolve_replay(args)
    archive = mpyq.MPQArchive(str(replay))
    header, protocol, fallback_build = load_protocol_for_archive(archive)
    protocol_build = fallback_build or header["m_version"]["m_baseBuild"]
    details = protocol.decode_replay_details(archive.read_file("replay.details"))
    players = details.get("m_playerList", [])
    target_slot = find_player(players, args)
    target_pid = target_slot + 1
    target_player = players[target_slot]
    target_team = target_player.get("m_teamId")

    name_by_pid = {idx + 1: dec(player.get("m_name")) for idx, player in enumerate(players)}
    hero_by_pid = {idx + 1: dec(player.get("m_hero")) for idx, player in enumerate(players)}
    team_by_pid = {idx + 1: player.get("m_teamId") for idx, player in enumerate(players)}
    target_hero_en = dec((target_player.get("m_hero") or target_player.get("m_heroId")))
    if dec(target_player.get("m_hero")) in ("Гаррош", "Garrosh"):
        target_hero_en = "Garrosh"

    tracker = parse_tracker(protocol, archive, players)
    score, score_time = score_table(tracker["score_event"])
    game = parse_game_events(protocol, archive, target_slot, target_hero_en)
    ability_clusters = cluster_ability_events(game["ability_events"])

    team_pids = [pid for pid, team in team_by_pid.items() if team == target_team]
    enemy_pids = [pid for pid, team in team_by_pid.items() if team != target_team]

    ability_summary = []
    by_ability = defaultdict(list)
    for event in game["ability_events"]:
        by_ability[event["ability_id"]].append(event)
    for ability_id, events in sorted(by_ability.items()):
        ability_summary.append(
            {
                "ability_id": ability_id,
                "hint": events[0].get("ability_hint"),
                "count": len(events),
                "first": events[0]["time"],
                "last": events[-1]["time"],
                "target_players": Counter(
                    e.get("target_player") for e in events if e.get("target_player")
                ).most_common(),
            }
        )

    player_rows = []
    for idx, player in enumerate(players):
        pid = idx + 1
        player_rows.append(
            {
                "slot": idx,
                "pid": pid,
                "name": name_by_pid[pid],
                "hero": hero_by_pid[pid],
                "team": player.get("m_teamId"),
                "result": player.get("m_result"),
                "won": player.get("m_result") == 1,
                "score": compact_score(score.get(idx, {})),
            }
        )

    result = {
        "replay": str(replay),
        "replay_name": replay.name,
        "build": header["m_version"]["m_baseBuild"],
        "protocol_build": protocol_build,
        "fallback": fallback_build is not None,
        "elapsed_seconds_header": round(header.get("m_elapsedGameLoops", 0) / 16, 1),
        "score_time": score_time,
        "map": dec(details.get("m_title")),
        "players": player_rows,
        "target": {
            "slot": target_slot,
            "pid": target_pid,
            "name": name_by_pid[target_pid],
            "hero": hero_by_pid[target_pid],
            "team": target_team,
            "won": target_player.get("m_result") == 1,
            "score": compact_score(score.get(target_slot, {})),
            "talents": tracker["talents"].get(target_pid, []),
        },
        "level_summary": level_summary(tracker["levels"], team_pids, enemy_pids),
        "deaths": tracker["deaths"],
        "target_death_contexts": summarize_death_context(
            target_pid, tracker["deaths"], ability_clusters
        ),
        "camps": tracker["camps"],
        "payload_spawns": tracker["payload_spawns"],
        "major_structures": tracker["major_structures"],
        "core_deaths": tracker["core_deaths"],
        "target_ability_summary": ability_summary,
        "target_ability_clusters": ability_clusters,
        "validation": {
            "tracker_event_counts": tracker["event_counts"].most_common(),
            "tracker_stat_counts": tracker["stat_counts"].most_common(),
            "tracker_errors": tracker["tracker_errors"],
            "game_errors": game["game_errors"],
        },
    }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay")
    parser.add_argument("--folder")
    parser.add_argument("--name-contains")
    parser.add_argument("--size", type=int)
    parser.add_argument("--player-name")
    parser.add_argument("--player-slot", type=int)
    parser.add_argument("--player-pid", type=int)
    parser.add_argument("--outdir", default=str(ROOT / "analysis" / "deep_replay"))
    args = parser.parse_args()

    result = analyze(args)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(result["replay_name"]).stem).strip("_")
    json_path = outdir / f"{safe_name}_deep.json"
    md_path = outdir / f"{safe_name}_deep.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(md_path, result)
    print(
        json.dumps(
            {
                "replay": result["replay"],
                "build": result["build"],
                "protocol_build": result["protocol_build"],
                "fallback": result["fallback"],
                "json": str(json_path),
                "md": str(md_path),
                "tracker_errors": len(result["validation"]["tracker_errors"]),
                "game_errors": len(result["validation"]["game_errors"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
