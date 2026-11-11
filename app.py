"""Web server: python app.py  →  http://127.0.0.1:5000"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import threading
import uuid

from flask import Flask, Response, jsonify, render_template, request

from hidden_jobs.config import settings
from hidden_jobs.pipeline import SearchRequest, run_search, save_result
from hidden_jobs.util import Progress

app = Flask(__name__)
JOBS: dict[str, dict] = {}   # job_id -> {"progress": Progress, "result": dict|None}


def _split(value) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = str(value or "").replace("\n", ",").split(",")
    return [x.strip() for x in items if x.strip()]


@app.get("/")
def index():
    return render_template("index.html", has_maps=bool(settings.google_maps_api_key),
                           has_search=bool(settings.serper_api_key or settings.brave_api_key),
                           provider=settings.search_provider,
                           max_places=settings.max_places_requests, max_search=settings.max_search_requests)


@app.post("/api/search")
def start_search():
    data = request.get_json(force=True)
    req = SearchRequest(
        cities=_split(data.get("cities")), roles=_split(data.get("roles")),
        level=data.get("level", "normal"), use_maps=bool(data.get("use_maps", True)),
        use_dorking=bool(data.get("use_dorking", True)), use_synonyms=bool(data.get("use_synonyms", True)),
        extra_categories=_split(data.get("extra_categories")),
        only_extra_categories=bool(data.get("only_extra_categories", False)),
    )
    if not req.cities or not req.roles:
        return jsonify({"error": "צריך להזין לפחות עיר אחת ותפקיד אחד"}), 400
    if data.get("max_places"):
        settings.max_places_requests = int(data["max_places"])
    if data.get("max_search"):
        settings.max_search_requests = int(data["max_search"])

    job_id = uuid.uuid4().hex[:10]
    progress = Progress()
    JOBS[job_id] = {"progress": progress, "result": None}

    def worker():
        try:
            result = asyncio.run(run_search(req, settings, progress))
            save_result(result, settings, job_id)
            JOBS[job_id]["result"] = result
            progress.set_stage("done", "הסתיים")
        except Exception as e:  # noqa: BLE001
            progress.error = str(e)
            progress.add_log(f"❌ {e}")
        finally:
            progress.done = True

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/status/<job_id>")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    out = job["progress"].to_dict()
    out["has_result"] = job["result"] is not None
    return jsonify(out)


@app.get("/api/result/<job_id>")
def result(job_id: str):
    job = JOBS.get(job_id)
    if job and job["result"] is not None:
        return Response(json.dumps(job["result"], ensure_ascii=False), mimetype="application/json")
    path = settings.results_dir / f"{job_id}.json"
    if path.exists():
        return Response(path.read_text(encoding="utf-8"), mimetype="application/json")
    return jsonify({"error": "not ready"}), 404


@app.get("/api/result/<job_id>.csv")
def result_csv(job_id: str):
    job = JOBS.get(job_id)
    res = job["result"] if job else None
    if res is None:
        path = settings.results_dir / f"{job_id}.json"
        if not path.exists():
            return jsonify({"error": "not ready"}), 404
        res = json.loads(path.read_text(encoding="utf-8"))
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel shows Hebrew correctly
    cols = ["company", "title", "url", "location", "role", "matched_term", "source", "domain", "company_city"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(res["jobs"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=jobs_{job_id}.csv"})


@app.get("/api/history")
def history():
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(settings.results_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            items.append({"job_id": p.stem, "cities": r["request"]["cities"], "roles": r["request"]["roles"],
                          "matches": len(r["jobs"])})
        except Exception:  # noqa: BLE001
            continue
    return jsonify(items)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
