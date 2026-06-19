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

    return {
        "id": result.get("report_id"),
        "map": result.get("map"),
        "replay_name": result.get("replay_name"),
        "length": mmss(result.get("score_time") or result.get("elapsed_seconds_header")),
        "build": result.get("build"),
        "protocol_build": result.get("protocol_build"),
        "fallback": result.get("fallback"),
        "teams": teams,
        "level_summary": result.get("level_summary", []),
        "deaths": deaths,
        "camps": result.get("camps", [])[:80],
        "structures": result.get("major_structures", [])[:80],
        "core_deaths": result.get("core_deaths", []),
        "leaders": leaders,
        "danger": danger,
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
