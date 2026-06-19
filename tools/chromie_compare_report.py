import argparse
import json
import sys
import types
import importlib.machinery
import importlib.util
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


ABILITY_NAMES = {
    (885, 0): "Q",
    (892, 0): "W",
    (888, 0): "E_place",
    (887, 0): "D_detonate",
    (895, 0): "Temporal_Loop",
    (889, 0): "Timewalkers_Pursuit",
    (893, 0): "Slowing_Sands_place",
    (894, 0): "Slowing_Sands_toggle",
    (891, 0): "Time_Out_start",
    (891, 1): "Time_Out_cancel",
}

DEDUP_SECONDS = {
    "Q": 0.35,
    "W": 0.35,
    "E_place": 0.35,
    "D_detonate": 0.50,
    "Temporal_Loop": 3.0,
    "Timewalkers_Pursuit": 0.35,
    "Slowing_Sands_place": 0.35,
    "Slowing_Sands_toggle": 0.35,
    "Time_Out_start": 0.35,
    "Time_Out_cancel": 0.35,
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
    build = header["m_version"]["m_baseBuild"]
    try:
        protocol = versions.build(build)
        fallback = False
    except Exception:
        supported = [candidate for candidate in available_protocol_builds() if candidate <= build]
        if not supported:
            raise
        protocol = versions.build(max(supported))
        fallback = True
    return header, protocol, fallback


def kv_pairs(items):
    out = defaultdict(list)
    for item in items or []:
        out[dec(item.get("m_key"))].append(item.get("m_value"))
    return out


def score_table(score_event):
    table = defaultdict(dict)
    times = []
    if not score_event:
        return table, None
    for instance in score_event.get("m_instanceList", []):
        name = dec(instance.get("m_name"))
        for slot, values in enumerate(instance.get("m_values", [])):
            if not values:
                continue
            final = values[-1]
            table[slot][name] = final.get("m_value")
            if final.get("m_time"):
                times.append(final.get("m_time"))
    return table, max(times) if times else None


def find_chromie_slot(details):
    for idx, player in enumerate(details.get("m_playerList", [])):
        hero = dec(player.get("m_hero")).lower()
        if "chromie" in hero or "cromi" in hero:
            return idx
    raise RuntimeError("Chromie player not found")


def score_subset(score):
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
        "DamageTaken",
        "TeamfightHeroDamage",
        "TeamfightDamageTaken",
        "TimeCCdEnemyHeroes",
        "Tier1Talent",
        "Tier2Talent",
        "Tier3Talent",
        "Tier4Talent",
        "Tier5Talent",
        "Tier6Talent",
        "Tier7Talent",
    ]
    return {key: score.get(key) for key in keys}


def command_target_player(event):
    data = event.get("m_data") or {}
    target = data.get("TargetUnit")
    if not target:
        return None
    return target.get("m_snapshotControlPlayerId") or target.get("m_snapshotUpkeepPlayerId")


def cluster_commands(events, name):
    threshold = DEDUP_SECONDS.get(name, 0.35)
    clusters = []
    for event in events:
        if clusters and event["time"] - clusters[-1]["end"] < threshold:
            clusters[-1]["end"] = event["time"]
            clusters[-1]["raw_count"] += 1
            if event.get("target_player"):
                clusters[-1]["target_players"].append(event["target_player"])
        else:
            clusters.append(
                {
                    "time": event["time"],
                    "time_s": mmss(event["time"]),
                    "end": event["time"],
                    "raw_count": 1,
                    "target_players": [event["target_player"]]
                    if event.get("target_player")
                    else [],
                }
            )
    for cluster in clusters:
        cluster["target_players"] = list(dict.fromkeys(cluster["target_players"]))
        cluster["end"] = round(cluster["end"], 2)
    return clusters


def analyze(path, label):
    archive = mpyq.MPQArchive(str(path))
    header, protocol, fallback = load_protocol(archive)
    details = protocol.decode_replay_details(archive.read_file("replay.details"))
    players = details.get("m_playerList", [])
    chromie_slot = find_chromie_slot(details)
    chromie_pid = chromie_slot + 1
    chromie = players[chromie_slot]
    team_id = chromie.get("m_teamId")
    team_pids = [
        idx + 1
        for idx, player in enumerate(players)
        if player.get("m_teamId") == team_id
    ]
    enemy_pids = [
        idx + 1
        for idx, player in enumerate(players)
        if player.get("m_teamId") is not None and player.get("m_teamId") != team_id
    ]
    pid_to_player = {
        idx + 1: {
            "name": dec(player.get("m_name")),
            "hero": dec(player.get("m_hero")),
            "team": player.get("m_teamId"),
        }
        for idx, player in enumerate(players)
    }

    score_event = None
    talents = []
    player_deaths = []
    team_level_times = {}
    enemy_level_times = {}
    structures = []

    for ev in protocol.decode_replay_tracker_events(
        archive.read_file("replay.tracker.events")
    ):
        name = ev.get("_event", "")
        t = ev.get("_gameloop", 0) / 16.0
        if name == "NNet.Replay.Tracker.SScoreResultEvent":
            score_event = ev
            continue
        if name != "NNet.Replay.Tracker.SStatGameEvent":
            continue
        event_name = dec(ev.get("m_eventName"))
        ints = kv_pairs(ev.get("m_intData"))
        strings = kv_pairs(ev.get("m_stringData"))
        fixed = kv_pairs(ev.get("m_fixedData"))

        if event_name == "TalentChosen":
            pid_values = ints.get("PlayerID") or []
            if pid_values and pid_values[0] == chromie_pid:
                purchase = (strings.get("PurchaseName") or [""])[0]
                talents.append({"time": round(t, 1), "time_s": mmss(t), "talent": dec(purchase)})
        elif event_name == "LevelUp":
            pid_values = ints.get("PlayerID") or []
            level_values = ints.get("Level") or []
            if not pid_values or not level_values:
                continue
            pid = pid_values[0]
            level = level_values[0]
            if pid in team_pids and level not in team_level_times:
                team_level_times[level] = round(t, 1)
            elif pid in enemy_pids and level not in enemy_level_times:
                enemy_level_times[level] = round(t, 1)
        elif event_name == "PlayerDeath":
            values = ev.get("m_intData") or []
            victim = None
            killers = []
            for item in values:
                key = dec(item.get("m_key"))
                if key == "PlayerID":
                    victim = item.get("m_value")
                elif key == "KillingPlayer":
                    killers.append(item.get("m_value"))
            player_deaths.append(
                {
                    "time": round(t, 1),
                    "time_s": mmss(t),
                    "victim": victim,
                    "victim_hero": pid_to_player.get(victim, {}).get("hero"),
                    "victim_name": pid_to_player.get(victim, {}).get("name"),
                    "victim_side": "team" if victim in team_pids else "enemy",
                    "killers": killers,
                    "killer_heroes": [
                        pid_to_player.get(pid, {}).get("hero") for pid in killers
                    ],
                }
            )
        elif event_name == "TownStructureDeath":
            structures.append(
                {
                    "time": round(t, 1),
                    "time_s": mmss(t),
                    "unit": dec((strings.get("UnitType") or [""])[0]),
                    "killer": ints.get("KillingPlayer", []),
                    "game_time_fixed": fixed.get("GameTime", []),
                }
            )

    scores, game_length = score_table(score_event)
    chromie_score = score_subset(scores.get(chromie_slot, {}))
    if not game_length:
        game_length = max([d["time"] for d in player_deaths] or [0])

    raw_by_ability = defaultdict(list)
    for ev in protocol.decode_replay_game_events(archive.read_file("replay.game.events")):
        if ev.get("_event") != "NNet.Game.SCmdEvent":
            continue
        if (ev.get("_userid") or {}).get("m_userId") != chromie_slot:
            continue
        abil = ev.get("m_abil") or {}
        key = (abil.get("m_abilLink"), abil.get("m_abilCmdIndex"))
        name = ABILITY_NAMES.get(key)
        if not name:
            continue
        raw_by_ability[name].append(
            {
                "time": ev.get("_gameloop", 0) / 16.0,
                "target_player": command_target_player(ev),
            }
        )

    clusters_by_ability = {
        name: cluster_commands(events, name)
        for name, events in sorted(raw_by_ability.items())
    }
    command_summary = {}
    for name, clusters in clusters_by_ability.items():
        raw = len(raw_by_ability[name])
        count = len(clusters)
        command_summary[name] = {
            "raw": raw,
            "count": count,
            "per_min": round(count / (game_length / 60.0), 2) if game_length else None,
        }

    enemy_deaths = [d for d in player_deaths if d["victim_side"] == "enemy"]
    loop_windows = []
    for cluster in clusters_by_ability.get("Temporal_Loop", []):
        targets = cluster["target_players"]
        deaths_near = [
            d
            for d in enemy_deaths
            if cluster["time"] <= d["time"] <= cluster["time"] + 10.0
        ]
        target_deaths = [d for d in deaths_near if d["victim"] in targets]
        loop_windows.append(
            {
                "time": cluster["time"],
                "time_s": cluster["time_s"],
                "targets": [
                    {
                        "pid": pid,
                        "hero": pid_to_player.get(pid, {}).get("hero"),
                        "name": pid_to_player.get(pid, {}).get("name"),
                    }
                    for pid in targets
                ],
                "target_killed_10s": bool(target_deaths),
                "enemy_deaths_10s": [
                    {
                        "time_s": d["time_s"],
                        "hero": d["victim_hero"],
                        "name": d["victim_name"],
                    }
                    for d in deaths_near
                ],
            }
        )

    ult_start = None
    if loop_windows:
        ult_start = loop_windows[0]["time"]
    elif clusters_by_ability.get("Slowing_Sands_place"):
        ult_start = clusters_by_ability["Slowing_Sands_place"][0]["time"]

    post_ult = None
    if ult_start and game_length > ult_start:
        post_ult_minutes = (game_length - ult_start) / 60.0
        post_ult = {}
        for name in ["Temporal_Loop", "Slowing_Sands_place", "Q", "W", "E_place"]:
            clusters = [
                c
                for c in clusters_by_ability.get(name, [])
                if c["time"] >= ult_start
            ]
            post_ult[name] = {
                "count": len(clusters),
                "per_min": round(len(clusters) / post_ult_minutes, 2)
                if post_ult_minutes
                else None,
            }

    return {
        "label": label,
        "file": str(path),
        "build": header["m_version"]["m_baseBuild"],
        "fallback_protocol": fallback,
        "map": dec(details.get("m_title")),
        "game_length": round(game_length, 1),
        "game_length_s": mmss(game_length),
        "chromie_slot": chromie_slot,
        "chromie_pid": chromie_pid,
        "player": dec(chromie.get("m_name")),
        "hero": dec(chromie.get("m_hero")),
        "team": team_id,
        "won": chromie.get("m_result") == 1,
        "allies": [
            dec(player.get("m_hero"))
            for player in players
            if player.get("m_teamId") == team_id
        ],
        "enemies": [
            dec(player.get("m_hero"))
            for player in players
            if player.get("m_teamId") is not None and player.get("m_teamId") != team_id
        ],
        "score": chromie_score,
        "talents": talents,
        "level_times": {
            "team": {str(k): mmss(v) for k, v in sorted(team_level_times.items()) if k in {4, 7, 10, 13, 16, 20}},
            "enemy": {str(k): mmss(v) for k, v in sorted(enemy_level_times.items()) if k in {4, 7, 10, 13, 16, 20}},
        },
        "command_summary": command_summary,
        "post_ult": post_ult,
        "loop_windows": loop_windows,
        "chromie_deaths": [d for d in player_deaths if d["victim"] == chromie_pid],
        "team_deaths_count": sum(1 for d in player_deaths if d["victim_side"] == "team"),
        "enemy_deaths_count": len(enemy_deaths),
        "enemy_deaths": enemy_deaths,
        "structures": structures,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("replays", nargs="+", help="label=path")
    args = parser.parse_args()

    records = []
    for item in args.replays:
        label, raw_path = item.split("=", 1)
        records.append(analyze(Path(raw_path), label))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out), "records": len(records)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
