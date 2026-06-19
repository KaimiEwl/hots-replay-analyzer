import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from hots_replay_deep_analyzer import analyze  # noqa: E402


DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
REPORT_DIR = DATA_DIR / "reports"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

app = FastAPI(title="HOTS Replay Analyzer")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "app" / "templates")


def ensure_data_dirs():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def format_number(value):
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return f"{int(value):,}".replace(",", " ")
    return str(value)


def mmss(seconds):
    if seconds is None:
        return "-"
    seconds = int(round(float(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def player_label(player):
    if not player:
        return "Unknown"
    return f"{player.get('name', 'Unknown')} / {player.get('hero', 'Unknown')}"


def stat(score, key, default=0):
    value = score.get(key)
    return default if value is None else value


def is_aram_map(map_name):
    return map_name in {"Lost Cavern", "Silver City", "Industrial District", "Braxis Outpost"}


def team_totals(players):
    totals = {
        "deaths": 0,
        "dead_time": 0,
        "hero_damage": 0,
        "structure_damage": 0,
        "siege_damage": 0,
        "xp": 0,
        "camps": 0,
        "takedowns": 0,
    }
    for player in players:
        score = player.get("score", {})
        totals["deaths"] += stat(score, "Deaths")
        totals["dead_time"] += stat(score, "TimeSpentDead")
        totals["hero_damage"] += stat(score, "HeroDamage")
        totals["structure_damage"] += stat(score, "StructureDamage")
        totals["siege_damage"] += stat(score, "SiegeDamage")
        totals["xp"] += stat(score, "ExperienceContribution")
        totals["camps"] += stat(score, "MercCampCaptures")
        totals["takedowns"] += stat(score, "Takedowns")
    return totals


def add_unique(items, item):
    if item not in items:
        items.append(item)


def team_name(team_id):
    return f"Team {team_id}" if team_id is not None else "Team ?"


def percent(value):
    return f"{round(value * 100)}%"


def comparison_item(severity, title, metric, actual, expected, verdict, actions):
    return {
        "severity": severity,
        "title": title,
        "metric": metric,
        "actual": actual,
        "expected": expected,
        "verdict": verdict,
        "actions": actions[:3],
        "actual_state": "good" if severity == "good" else severity,
        "expected_state": "needed",
    }


def map_guidance(map_name):
    profiles = {
        "Alterac Pass": {
            "name": "Alterac Pass",
            "expected": "Перед Cavalry push должны быть зачищены волны, подготовлен side camp и сохранены 5 игроков. После objective главный call - fort/keep, а не лишний chase.",
            "actions": [
                "За 45-60 секунд до objective подготовить side lane и camp.",
                "Если objective выигран, сразу идти в структуру ближайшей сильной линией.",
                "Не начинать fight, если команда не успевает прийти 5v5 к prisoner channel.",
            ],
        },
        "Paso de Alterac": {
            "name": "Alterac Pass",
            "expected": "Перед Cavalry push должны быть зачищены волны, подготовлен side camp и сохранены 5 игроков. После objective главный call - fort/keep, а не лишний chase.",
            "actions": [
                "За 45-60 секунд до objective подготовить side lane и camp.",
                "Если objective выигран, сразу идти в структуру ближайшей сильной линией.",
                "Не начинать fight, если команда не успевает прийти 5v5 к prisoner channel.",
            ],
        },
        "Cursed Hollow": {
            "name": "Cursed Hollow",
            "expected": "Перед tribute нужен camp на противоположной линии и 5 живых героев. Не каждый tribute надо contest: иногда лучше взять структуру или soak.",
            "actions": [
                "Брать camp за 35-55 секунд до tribute.",
                "Если tribute третий для врага, готовить позицию заранее, а не заходить последним.",
                "Если fight плохой, отдать tribute и забрать structure/camp на другой стороне.",
            ],
        },
        "Infernal Shrines": {
            "name": "Infernal Shrines",
            "expected": "Перед shrine нужны waveclear, camp pressure и позиция в choke. После Punisher надо забрать structure, а не просто выиграть драку.",
            "actions": [
                "Подходить к shrine заранее и занимать choke до начала objective.",
                "Camp должен давить линию во время shrine, а не после него.",
                "После Punisher сразу переводить его в fort/keep.",
            ],
        },
        "Tomb of the Spider Queen": {
            "name": "Tomb of the Spider Queen",
            "expected": "Главная норма карты - не умирать с gems и делать turn-in после pick или camp pressure.",
            "actions": [
                "Не носить много gems без сейфового пути к turn-in.",
                "После kill сразу проверять turn-in, а не искать новую драку.",
                "Перед своим turn-in зачищать mid/top, чтобы Webweavers дали structure.",
            ],
        },
        "Battlefield of Eternity": {
            "name": "Battlefield of Eternity",
            "expected": "Перед immortal phase команда должна решить: race или defense. Смешанный call обычно проигрывает objective и позицию.",
            "actions": [
                "До immortal заранее выбрать race/defense план по составам.",
                "Не драться без talent tier, если immortal можно просто defended.",
                "После выигранного immortal пушить одной линией до structure.",
            ],
        },
        "Dragon Shire": {
            "name": "Dragon Shire",
            "expected": "Норма карты - держать soak и shrine control, а Dragon Knight брать после pick или сильного lane pressure.",
            "actions": [
                "Не бросать soak ради долгой драки за один shrine.",
                "После kill сразу переводить преимущество в shrine или DK channel.",
                "DK должен забирать structure, а не стоять в драках без цели.",
            ],
        },
    }
    return profiles.get(
        map_name,
        {
            "name": map_name or "Unknown map",
            "expected": "Перед objective должны быть 5 живых героев, зачищенные волны и понятный следующий call: structure, camp, boss, objective или reset.",
            "actions": [
                "За 30-60 секунд до objective прекратить случайный poke и подготовить позицию.",
                "После выигранной драки сразу назвать награду: structure, camp, boss или objective.",
                "Если враг первым взял talent tier, играть от waveclear и короткого pick, не от честного 5v5.",
            ],
        },
    )


def build_comparisons(result, team_rows, next_steps):
    target_team_id = result.get("target", {}).get("team")
    target_row = next((team for team in team_rows if team["team_id"] == target_team_id), None)
    if not target_row:
        target_row = next((team for team in team_rows if not team["won"]), None) or (team_rows[0] if team_rows else None)
    enemy_row = next((team for team in team_rows if team is not target_row), None)
    if not target_row or not enemy_row:
        return []

    comparisons = []
    tt = target_row["totals"]
    et = enemy_row["totals"]
    target_label = team_name(target_row["team_id"])
    enemy_label = team_name(enemy_row["team_id"])

    death_actions = [
        "Перед objective считать смерть хуже потерянного poke: если engage не начался, лишний риск не брать.",
        "После потери фронта сразу reset, а не спасать уже проигранную драку.",
        "После 16 уровня любой death должен давать команде понятную компенсацию: keep, boss, objective или core pressure.",
    ]
    if tt["deaths"] >= et["deaths"] + 5 or tt["dead_time"] >= et["dead_time"] + 180:
        severity = "high"
        verdict = "Вы отдали слишком много времени карты. Из-за этого команда позже приходит на objective, хуже защищает линии и не успевает конвертировать выигранные моменты."
        add_unique(next_steps, death_actions[0])
    elif tt["deaths"] > et["deaths"] or tt["dead_time"] > et["dead_time"] + 90:
        severity = "medium"
        verdict = "Смерти не единственная причина матча, но tempo проседал. Нужно снижать preventable deaths перед важными окнами."
        add_unique(next_steps, death_actions[1])
    else:
        severity = "good"
        verdict = "По deaths/dead time команда не проиграла матч грубо. Это можно считать рабочей базой."
    comparisons.append(
        comparison_item(
            severity,
            "Жизни перед важными окнами",
            "Deaths / dead time",
            f"{target_label}: {tt['deaths']} deaths, {mmss(tt['dead_time'])} dead. {enemy_label}: {et['deaths']} deaths, {mmss(et['dead_time'])} dead.",
            "Держать 5 героев живыми перед objective и поздними talent tiers. Лишняя смерть допустима только если она сразу дает structure/objective.",
            verdict,
            death_actions,
        )
    )

    talent_rows = result.get("level_summary", [])
    bad_talent = next(
        (
            row
            for row in talent_rows
            if row.get("level") in {10, 13, 16, 20} and (row.get("diff_s") or 0) >= 15
        ),
        None,
    )
    bad_early = next(
        (
            row
            for row in talent_rows
            if row.get("level") in {4, 7} and (row.get("diff_s") or 0) >= 15
        ),
        None,
    )
    talent_actions = [
        "Если враг первым взял 10/13/16/20, не начинать честный 5v5 до выравнивания таланта.",
        "Играть от waveclear, choke и короткого pick, пока talent tier не сравнялся.",
        "Перед следующим talent tier заранее добрать soak, а не идти на долгий poke в центре карты.",
    ]
    if bad_talent:
        severity = "high"
        actual = f"{target_label} получила level {bad_talent['level']} позже на {round(bad_talent['diff_s'])} секунд."
        verdict = "Это красное окно: в такой момент враг имеет полноценный talent advantage, и обычная драка почти всегда плохая."
        add_unique(next_steps, talent_actions[0])
    elif bad_early:
        severity = "medium"
        actual = f"{target_label} получила level {bad_early['level']} позже на {round(bad_early['diff_s'])} секунд."
        verdict = "Это ранний warning: soak/темп просели до большого talent tier, значит следующий objective нужно готовить осторожнее."
        add_unique(next_steps, talent_actions[2])
    else:
        severity = "good"
        actual = "Критичных задержек на 10/13/16/20 по таймеру не найдено."
        verdict = "Talent windows не выглядят главным провалом. В таких играх важнее смотреть deaths, camps и conversion."
    comparisons.append(
        comparison_item(
            severity,
            "Talent tier нельзя игнорировать",
            "Level timing",
            actual,
            "К 10/13/16/20 подходить одновременно или раньше врага. Если враг взял tier первым - переждать, чистить волны, не принимать полный fight.",
            verdict,
            talent_actions,
        )
    )

    if not is_aram_map(result.get("map")):
        camp_actions = [
            "Брать camp за 30-60 секунд до objective или push-окна, чтобы линия давила сама.",
            "Если camp уже стоит, заранее назначить игрока: кто берет, кто держит soak, кто страхует.",
            "Не брать camp после проигранного objective, если из-за этого падает fort/keep: сначала defense.",
        ]
        camp_diff = et["camps"] - tt["camps"]
        if camp_diff >= 3:
            severity = "high"
            verdict = "Camp pressure проигран сильно. Враг чаще начинал важные окна с готовой side pressure, а ваша команда реагировала позже."
            add_unique(next_steps, camp_actions[0])
        elif camp_diff >= 1:
            severity = "medium"
            verdict = "По лагерям есть отставание. Это не всегда проигрывает матч само, но делает objective тяжелее."
            add_unique(next_steps, camp_actions[1])
        else:
            severity = "good"
            verdict = "По количеству camp captures команда не отстала. Дальше важно смотреть именно тайминг лагерей."
        comparisons.append(
            comparison_item(
                severity,
                "Лагеря должны работать на objective",
                "Camp pressure",
                f"{target_label}: {tt['camps']} camps. {enemy_label}: {et['camps']} camps.",
                "Не отставать по camps больше чем на 1 и брать их до objective, а не случайно после драки.",
                verdict,
                camp_actions,
            )
        )

    conversion = tt["structure_damage"] / max(tt["siege_damage"], 1)
    enemy_conversion = et["structure_damage"] / max(et["siege_damage"], 1)
    structure_actions = [
        "После выигранной драки не искать еще один kill на той же точке: сразу идти в fort/keep, boss, camp или objective.",
        "Если волна уже под структурой, call должен быть structure damage, а не reset без награды.",
        "После objective заранее выбрать линию пуша, чтобы вся команда била одну структуру.",
    ]
    if tt["siege_damage"] >= 50000 and conversion < 0.06:
        severity = "high"
        verdict = "Вы чистили волны и создавали давление, но почти не превращали его в карту. Это как раз формат 'цифры есть, победы нет'."
        add_unique(next_steps, structure_actions[0])
    elif tt["structure_damage"] < 0.65 * max(et["structure_damage"], 1) or conversion < 0.10:
        severity = "medium"
        verdict = "Конвертация ниже нормы: часть хороших окон уходила в воздух вместо fort/keep."
        add_unique(next_steps, structure_actions[1])
    else:
        severity = "good"
        verdict = "Structure conversion выглядит приемлемо: давление хотя бы частично превращалось в карту."
    comparisons.append(
        comparison_item(
            severity,
            "Давление нужно превращать в строения",
            "Siege -> structure",
            f"{format_number(tt['siege_damage'])} siege -> {format_number(tt['structure_damage'])} structure ({percent(conversion)}). У врага: {percent(enemy_conversion)}.",
            "После waveclear, camp, kill или objective должен быть конкретный результат: fort, keep, boss, objective или безопасный reset.",
            verdict,
            structure_actions,
        )
    )

    kill_actions = [
        "После каждого kill сразу задать call: structure, camp, boss, objective или reset.",
        "Если kill далеко от объекта и волны не готовы, не chase: забрать soak/camp и подготовить следующий fight.",
        "После 2 kills не делить команду на три идеи. Все идут в одну награду.",
    ]
    if tt["takedowns"] >= et["takedowns"] - 2 and tt["structure_damage"] < 0.6 * max(et["structure_damage"], 1):
        severity = "medium"
        verdict = "Kill participation был, но карта за это не забиралась. Значит проблема не только в драках, а в call после драки."
        add_unique(next_steps, kill_actions[0])
    elif tt["takedowns"] + 4 < et["takedowns"]:
        severity = "medium"
        verdict = "Команда проиграла по takedowns, поэтому часть карты была потеряна через fight pressure."
    else:
        severity = "good"
        verdict = "По takedowns нет явного разрыва. Следующий слой - проверять, что команда делала после выигранных kills."
    comparisons.append(
        comparison_item(
            severity,
            "Kill должен давать награду",
            "Takedowns -> reward",
            f"{target_label}: {tt['takedowns']} takedowns и {format_number(tt['structure_damage'])} structure damage. {enemy_label}: {et['takedowns']} и {format_number(et['structure_damage'])}.",
            "После kill команда должна забирать карту. Если kill не ведет к structure/camp/objective, он часто не меняет игру.",
            verdict,
            kill_actions,
        )
    )

    guidance = map_guidance(result.get("map"))
    map_actions = guidance["actions"]
    comparisons.append(
        comparison_item(
            "low",
            f"Карта: {guidance['name']}",
            "Map plan",
            f"Реплей сыгран на {result.get('map') or 'unknown map'}. Сейчас анализ применяет общие правила карты и macro-сравнения.",
            guidance["expected"],
            "Это обязательный план на следующую версию разбора: привязать все советы к файлу таймингов конкретной карты.",
            map_actions,
        )
    )
    for action in map_actions[:1]:
        add_unique(next_steps, action)

    return comparisons


def player_advice(player, game_length, map_name):
    score = player.get("score", {})
    deaths = stat(score, "Deaths")
    dead_time = stat(score, "TimeSpentDead")
    hero_damage = stat(score, "HeroDamage")
    siege_damage = stat(score, "SiegeDamage")
    structure_damage = stat(score, "StructureDamage")
    xp = stat(score, "ExperienceContribution")
    camps = stat(score, "MercCampCaptures")
    takedowns = stat(score, "Takedowns")
    healing = stat(score, "Healing")
    damage_taken = stat(score, "DamageTaken")
    points = 0
    issues = []
    actions = []

    if deaths >= 6:
        points += 4
        issues.append(f"{deaths} смертей: слишком много tempo отдано врагу.")
        actions.append("После потери фронта или неудачного engage сразу выходить, не спасать уже проигранную драку.")
    elif deaths >= 4:
        points += 2
        issues.append(f"{deaths} смертей: нужно проверить, какие из них были preventable.")
        actions.append("Перед objective и после 16 уровня играть на сохранение жизни, а не на лишний poke.")

    if dead_time >= 180:
        points += 4
        issues.append(f"{mmss(dead_time)} dead time: команда долго играла без этого героя.")
        actions.append("Не принимать драку, если следующий шаг после нее не дает kill, objective или structure.")
    elif dead_time >= 100:
        points += 2
        issues.append(f"{mmss(dead_time)} dead time: смерти уже стоили карты и темпа.")

    if game_length and game_length >= 900 and xp < 7000 and healing < 20000:
        points += 2
        issues.append("Низкий XP-вклад для длинной игры.")
        actions.append("До 10 уровня не терять soak без прямой компенсации: kill, fort, camp или objective.")

    if not is_aram_map(map_name) and camps == 0 and game_length and game_length >= 900:
        points += 2
        issues.append("0 camp captures на карте с лагерями.")
        actions.append("Проверять camp timer перед objective: лагерь должен создавать давление, а не браться случайно.")

    if siege_damage >= 70000 and structure_damage < 5000:
        points += 3
        issues.append("Много siege damage, но мало structure damage: слабая конвертация давления.")
        actions.append("После выигранного окна сразу выбирать награду: fort/keep, boss, camp, objective или reset.")
    elif takedowns >= 10 and structure_damage < 2500 and game_length and game_length >= 900:
        points += 2
        issues.append("Есть kill participation, но мало урона по строениям.")
        actions.append("После kill не искать еще одну драку на той же точке: перевести преимущество в карту.")

    if hero_damage < 25000 and healing < 20000 and damage_taken < 50000 and game_length and game_length >= 900:
        points += 1
        issues.append("Низкий combat uptime: мало заметного вклада в драки.")
        actions.append("Проверить позиционирование и участие в ключевых fight, особенно на 10/13/16.")

    if not issues:
        issues.append("Профиль выглядит ровно: явных красных флагов по базовым метрикам нет.")
        actions.append("Следующий шаг: смотреть конкретные таймкоды deaths/objectives, а не только scoreboard.")

    severity = "high" if points >= 6 else "medium" if points >= 3 else "low" if points else "good"
    actual_summary = (
        f"{deaths} deaths, {mmss(dead_time)} dead, "
        f"{format_number(structure_damage)} structure, {format_number(camps)} camps"
    )
    expected_summary = actions[0] if actions else "Сохранить этот профиль и дальше проверять таймкоды ключевых fight."

    return {
        "player": player,
        "severity": severity,
        "points": points,
        "issues": issues[:3],
        "actions": actions[:3],
        "actual_summary": actual_summary,
        "expected_summary": expected_summary,
    }


def build_breakdown(result, teams):
    map_name = result.get("map")
    game_length = result.get("score_time") or result.get("elapsed_seconds_header")
    summaries = []
    next_steps = []
    player_cards = [
        player_advice(player, game_length, map_name)
        for player in result.get("players", [])
    ]
    player_cards.sort(key=lambda item: item["points"], reverse=True)

    team_rows = []
    for team_id, players in teams.items():
        totals = team_totals(players)
        won = any(player.get("won") is True for player in players)
        team_rows.append({"team_id": team_id, "players": players, "totals": totals, "won": won})

    comparisons = build_comparisons(result, team_rows, next_steps)

    if len(team_rows) >= 2:
        loser = next((team for team in team_rows if not team["won"]), None)
        winner = next((team for team in team_rows if team["won"]), None)
        if loser and winner:
            lt = loser["totals"]
            wt = winner["totals"]
            if lt["deaths"] >= wt["deaths"] + 5 or lt["dead_time"] >= wt["dead_time"] + 180:
                summaries.append(
                    {
                        "severity": "high",
                        "title": "Главная цена матча: смерти и dead time",
                        "body": (
                            f"Team {loser['team_id']} умерла {lt['deaths']} раз против {wt['deaths']} "
                            f"и провела {mmss(lt['dead_time'])} в смерти. Это обычно ломает soak, objective setup и defense."
                        ),
                    }
                )
                add_unique(next_steps, "Перед objective и после 16 уровня первым делом сохранять жизнь: плохой trade лучше сбросить.")

            if lt["siege_damage"] >= 0.75 * max(wt["siege_damage"], 1) and lt["structure_damage"] < 0.6 * max(wt["structure_damage"], 1):
                summaries.append(
                    {
                        "severity": "medium",
                        "title": "Давление было, конвертации не хватило",
                        "body": (
                            f"Team {loser['team_id']} дала {format_number(lt['siege_damage'])} siege damage, "
                            f"но только {format_number(lt['structure_damage'])} structure damage. Волны чистились, но карта не забиралась."
                        ),
                    }
                )
                add_unique(next_steps, "После 1-2 kills сразу называть следующий call: structure, camp, boss, objective или reset.")

            if not is_aram_map(map_name) and lt["camps"] + 2 <= wt["camps"]:
                summaries.append(
                    {
                        "severity": "medium",
                        "title": "Camp pressure проигран",
                        "body": (
                            f"По лагерям было {lt['camps']} против {wt['camps']}. "
                            "Это часто значит, что objective начинался без side pressure."
                        ),
                    }
                )
                add_unique(next_steps, "Брать camp не потому что он стоит, а за 30-60 секунд до objective или push-окна.")

    target_team = result.get("target", {}).get("team")
    bad_level_windows = [
        row
        for row in result.get("level_summary", [])
        if row.get("level") in {10, 13, 16, 20} and (row.get("diff_s") or 0) >= 15
    ]
    if bad_level_windows:
        first = bad_level_windows[0]
        summaries.append(
            {
                "severity": "high",
                "title": "Опасные talent windows",
                "body": (
                    f"Team {target_team} получила уровень {first['level']} позже на {round(first['diff_s'])} секунд. "
                    "В такие окна нельзя начинать честный 5v5."
                ),
            }
        )
        add_unique(next_steps, "Когда враг первым берет 10/13/16/20, играть от waveclear, choke и короткого pick, не от полной драки.")

    top_player = player_cards[0] if player_cards else None
    if top_player and top_player["points"] >= 3:
        player = top_player["player"]
        summaries.append(
            {
                "severity": top_player["severity"],
                "title": f"Первый кандидат на ручной разбор: {player.get('name')} / {player.get('hero')}",
                "body": "У этого игрока больше всего авто-флагов: " + " ".join(top_player["issues"][:2]),
            }
        )

    if not summaries:
        summaries.append(
            {
                "severity": "low",
                "title": "Явного одного провала по цифрам нет",
                "body": "Базовые метрики не показывают простой причины. Следующий уровень разбора: смотреть таймкоды смертей, objective и fights.",
            }
        )
        add_unique(next_steps, "Выбрать 2-3 ключевых fight по таймлайну смертей и проверить: была ли цель после драки.")

    if not next_steps:
        next_steps = [
            "После выигранной драки сразу конвертировать преимущество в structure, camp, boss или objective.",
            "Не драться в минус talent tier.",
            "Перед поздним objective ценность жизни выше лишнего poke.",
        ]

    summaries = summaries[:5]
    priority_players = [card for card in player_cards if card["severity"] != "good"][:4]
    if not priority_players:
        priority_players = player_cards[:4]

    return {
        "primary": summaries[0],
        "summaries": summaries,
        "supporting_summaries": summaries[1:],
        "comparisons": comparisons,
        "next_steps": next_steps[:5],
        "player_cards": player_cards,
        "priority_players": priority_players,
    }


def build_report_view(result):
    players_by_pid = {player["pid"]: player for player in result.get("players", [])}
    teams = {}
    for player in result.get("players", []):
        teams.setdefault(player.get("team"), []).append(player)

    for player in result.get("players", []):
        score = player.get("score", {})
        player["view"] = {
            "kda": f"{stat(score, 'Takedowns')}/{stat(score, 'Deaths')}/{stat(score, 'Assists')}",
            "hero_damage": format_number(score.get("HeroDamage")),
            "siege_damage": format_number(score.get("SiegeDamage")),
            "structure_damage": format_number(score.get("StructureDamage")),
            "xp": format_number(score.get("ExperienceContribution")),
            "dead_time": mmss(score.get("TimeSpentDead")),
            "camps": format_number(score.get("MercCampCaptures")),
        }

    deaths = []
    for death in result.get("deaths", [])[:80]:
        victim = players_by_pid.get(death.get("player"))
        killer = players_by_pid.get(death.get("killer"))
        deaths.append(
            {
                "time": death.get("time"),
                "victim": player_label(victim),
                "victim_team": victim.get("team") if victim else None,
                "killer": player_label(killer) if killer else f"Player {death.get('killer')}",
            }
        )

    leaders = sorted(
        result.get("players", []),
        key=lambda p: stat(p.get("score", {}), "HeroDamage"),
        reverse=True,
    )[:3]
    danger = sorted(
        result.get("players", []),
        key=lambda p: (stat(p.get("score", {}), "Deaths"), stat(p.get("score", {}), "TimeSpentDead")),
        reverse=True,
    )[:3]

    winner_team = next(
        (
            team_id
            for team_id, players in teams.items()
            if any(player.get("won") is True for player in players)
        ),
        None,
    )

    return {
        "id": result.get("report_id"),
        "map": result.get("map"),
        "replay_name": result.get("replay_name"),
        "length": mmss(result.get("score_time") or result.get("elapsed_seconds_header")),
        "build": result.get("build"),
        "protocol_build": result.get("protocol_build"),
        "fallback": result.get("fallback"),
        "winner_team": winner_team,
        "teams": teams,
        "level_summary": result.get("level_summary", []),
        "deaths": deaths,
        "camps": result.get("camps", [])[:80],
        "structures": result.get("major_structures", [])[:80],
        "core_deaths": result.get("core_deaths", []),
        "leaders": leaders,
        "danger": danger,
        "breakdown": build_breakdown(result, teams),
        "json_path": f"/api/reports/{result.get('report_id')}",
    }


async def save_upload(file: UploadFile, destination: Path):
    size = 0
    with destination.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Replay file is too large.")
            out.write(chunk)
    return size


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/upload")
async def upload_replay(file: UploadFile = File(...)):
    ensure_data_dirs()
    filename = file.filename or ""
    if not filename.lower().endswith(".stormreplay"):
        raise HTTPException(status_code=400, detail="Upload a .StormReplay file.")

    report_id = uuid4().hex
    upload_path = UPLOAD_DIR / f"{report_id}.StormReplay"
    await save_upload(file, upload_path)

    args = SimpleNamespace(
        replay=str(upload_path),
        folder=None,
        name_contains=None,
        size=None,
        player_name=None,
        player_slot=0,
        player_pid=None,
        outdir=str(REPORT_DIR),
    )

    try:
        result = analyze(args)
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Could not parse this replay.")

    result["report_id"] = report_id
    result["uploaded_name"] = filename
    report_path = REPORT_DIR / f"{report_id}.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return RedirectResponse(url=f"/reports/{report_id}", status_code=303)


@app.get("/reports/{report_id}", response_class=HTMLResponse)
async def report(request: Request, report_id: str):
    report_path = REPORT_DIR / f"{report_id}.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found.")
    result = json.loads(report_path.read_text(encoding="utf-8"))
    return templates.TemplateResponse(
        "report.html",
        {
            "request": request,
            "report": build_report_view(result),
        },
    )


@app.get("/api/reports/{report_id}")
async def report_json(report_id: str):
    report_path = REPORT_DIR / f"{report_id}.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found.")
    return json.loads(report_path.read_text(encoding="utf-8"))


@app.on_event("startup")
def startup():
    ensure_data_dirs()
