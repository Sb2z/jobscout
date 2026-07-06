"""JobScout — agent de veille d'offres d'emploi.

Boucle agent classique : collecte multi-sources -> scoring contre un profil
YAML -> deduplication (etat local) -> alerte Telegram avec lien pour postuler.
Concu pour tourner en tache planifiee ou depuis un orchestrateur (n8n,
Power Automate) : sortie --json sur stdout, code retour = nouvelles offres.

Sources sans cle API : Arbeitnow (Europe) et Remotive (remote). Adzuna
(France/Suisse) s'active si ADZUNA_APP_ID / ADZUNA_APP_KEY sont presents.
Enrichissement LLM optionnel (ANTHROPIC_API_KEY) : classement final et
angle d'attaque de candidature par offre.

Usage :
  python jobscout.py scan               # scan + tableau console
  python jobscout.py scan --notify      # + alerte Telegram des NOUVELLES offres
  python jobscout.py scan --json        # sortie machine-readable
  python jobscout.py reset              # oublie les offres deja vues
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
PROFILE_PATH = ROOT / "profile.yaml"
STATE_PATH = ROOT / "data" / "seen.json"
TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0 (JobScout personal job-alert agent)"}


# ── utilitaires ──────────────────────────────────────────────────────────────

def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower())


# ── sources ──────────────────────────────────────────────────────────────────

def fetch_arbeitnow(pages: int = 3) -> list[dict]:
    jobs = []
    for page in range(1, pages + 1):
        try:
            data = _get(f"https://api.arbeitnow.com/api/job-board-api?page={page}")
        except Exception as exc:
            print(f"[arbeitnow] page {page} en echec: {exc}", file=sys.stderr)
            break
        for j in data.get("data", []):
            jobs.append({
                "id": f"arbeitnow:{j.get('slug')}",
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("location", "") + (" (remote)" if j.get("remote") else ""),
                "url": j.get("url", ""),
                "tags": j.get("tags", []) + j.get("job_types", []),
                "description": j.get("description", ""),
                "source": "arbeitnow",
            })
    return jobs


def fetch_remotive(queries: list[str]) -> list[dict]:
    jobs = []
    for query in queries:
        try:
            data = _get("https://remotive.com/api/remote-jobs?search="
                        + urllib.parse.quote(query) + "&limit=50")
        except Exception as exc:
            print(f"[remotive] '{query}' en echec: {exc}", file=sys.stderr)
            continue
        for j in data.get("jobs", []):
            jobs.append({
                "id": f"remotive:{j.get('id')}",
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("candidate_required_location", "") + " (remote)",
                "url": j.get("url", ""),
                "tags": j.get("tags", []),
                "description": j.get("description", ""),
                "source": "remotive",
            })
    return jobs


def fetch_adzuna(queries: list[str], countries: list[str]) -> list[dict]:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        return []
    jobs = []
    for country in countries:
        for query in queries:
            url = (f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
                   f"?app_id={app_id}&app_key={app_key}&results_per_page=25"
                   f"&what={urllib.parse.quote(query)}&content-type=application/json")
            try:
                data = _get(url)
            except Exception as exc:
                print(f"[adzuna:{country}] '{query}' en echec: {exc}", file=sys.stderr)
                continue
            for j in data.get("results", []):
                jobs.append({
                    "id": f"adzuna:{j.get('id')}",
                    "title": j.get("title", ""),
                    "company": (j.get("company") or {}).get("display_name", ""),
                    "location": (j.get("location") or {}).get("display_name", ""),
                    "url": j.get("redirect_url", ""),
                    "tags": [],
                    "description": j.get("description", ""),
                    "source": f"adzuna:{country}",
                })
    return jobs


# ── scoring ──────────────────────────────────────────────────────────────────

def score_job(job: dict, profile: dict) -> tuple[int, list[str]]:
    title = _norm(job["title"])
    tags = _norm(" ".join(job.get("tags", [])))
    body = _norm(job.get("description", ""))[:4000]
    haystack = f"{title} {tags} {body}"

    for word in profile.get("exclude", []):
        if _norm(word) in haystack:
            return -1, [f"exclu: {word}"]

    if not any(_norm(w) in haystack for w in profile.get("must_have_any", [])):
        return -1, ["aucun mot-cle indispensable"]

    total = 0
    reasons = []
    for word, weight in profile.get("keywords", {}).items():
        w = _norm(word)
        hits = 0
        if w in title:
            hits += 3
        if w in tags:
            hits += 2
        if w in body:
            hits += 1
        if hits:
            total += hits * int(weight)
            reasons.append(f"{word}(+{hits * int(weight)})")

    loc = _norm(job.get("location", ""))
    for place, bonus in profile.get("locations", {}).items():
        if _norm(place) in loc:
            total += int(bonus)
            reasons.append(f"lieu {place}(+{bonus})")
            break
    return total, reasons


# ── enrichissement LLM optionnel ─────────────────────────────────────────────

def llm_brief(matches: list[dict], profile: dict) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not matches:
        return None
    offers = "\n".join(
        f"- [{m['score']}] {m['title']} — {m['company']} ({m['location']}) {m['url']}"
        for m in matches[:10]
    )
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 600,
        "messages": [{
            "role": "user",
            "content": (
                "Profil candidat : " + profile.get("headline", "") + "\n"
                "Offres detectees aujourd'hui :\n" + offers + "\n\n"
                "En francais : classe les 3 meilleures pour ce profil et donne, "
                "pour chacune, l'angle d'attaque de candidature en une phrase."
            ),
        }],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["content"][0]["text"]
    except Exception as exc:
        print(f"[llm] enrichissement indisponible: {exc}", file=sys.stderr)
        return None


# ── alertes ──────────────────────────────────────────────────────────────────

def telegram_send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents", file=sys.stderr)
        return False
    body = json.dumps({"chat_id": chat, "text": text[:4000],
                       "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception as exc:
        print(f"[telegram] envoi en echec: {exc}", file=sys.stderr)
        return False


# ── boucle principale ────────────────────────────────────────────────────────

def load_state() -> set[str]:
    if STATE_PATH.exists():
        try:
            return set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_state(seen: set[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def scan(args: argparse.Namespace) -> int:
    profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    queries = profile.get("search_queries", ["automation engineer"])

    raw = []
    raw += fetch_arbeitnow()
    raw += fetch_remotive(queries)
    raw += fetch_adzuna(queries, profile.get("adzuna_countries", ["fr", "ch"]))
    jobs, ids = [], set()
    for job in raw:                       # une meme offre revient une fois par requete
        if job["id"] not in ids:
            ids.add(job["id"])
            jobs.append(job)

    threshold = int(profile.get("min_score", 8))
    matches = []
    for job in jobs:
        total, reasons = score_job(job, profile)
        if total >= threshold:
            matches.append({**job, "score": total, "reasons": reasons,
                            "description": ""})
    matches.sort(key=lambda m: m["score"], reverse=True)

    seen = load_state()
    fresh = [m for m in matches if m["id"] not in seen]
    seen.update(m["id"] for m in matches)
    save_state(seen)

    brief = llm_brief(fresh, profile) if args.notify or args.json else None

    if args.json:
        print(json.dumps({
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "scanned": len(jobs), "matches": len(matches), "new": len(fresh),
            "results": fresh, "llm_brief": brief,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"{len(jobs)} offres scannees | {len(matches)} au-dessus du seuil "
              f"{threshold} | {len(fresh)} NOUVELLES")
        fresh_ids = {m["id"] for m in fresh}
        for m in (fresh if args.new_only else matches)[:args.top]:
            marker = "NEW " if m["id"] in fresh_ids else "    "
            print(f"{marker}[{m['score']:>3}] {m['title']} — {m['company']}"
                  f" ({m['location']}) {m['source']}")
            print(f"      {m['url']}")

    if args.notify and fresh:
        lines = [f"JobScout — {len(fresh)} nouvelle(s) offre(s)"]
        for m in fresh[:8]:
            lines.append(f"\n[{m['score']}] {m['title']}\n{m['company']} — "
                         f"{m['location']}\n{m['url']}")
        if brief:
            lines.append("\n--- Lecture IA ---\n" + brief)
        telegram_send("\n".join(lines))

    return min(len(fresh), 99)


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="JobScout — veille d'offres d'emploi")
    sub = parser.add_subparsers(dest="command", required=True)
    p_scan = sub.add_parser("scan", help="collecte, score, alerte")
    p_scan.add_argument("--notify", action="store_true", help="alerte Telegram des nouvelles offres")
    p_scan.add_argument("--json", action="store_true", help="sortie JSON machine-readable")
    p_scan.add_argument("--new-only", action="store_true", help="n'affiche que les nouvelles offres")
    p_scan.add_argument("--top", type=int, default=15, help="nombre d'offres affichees")
    sub.add_parser("reset", help="oublie les offres deja vues")
    args = parser.parse_args()

    if args.command == "reset":
        save_state(set())
        print("etat remis a zero")
        return 0
    return scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
