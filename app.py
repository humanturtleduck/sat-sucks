"""Upload a SAT question-bank PDF -> parsed questions -> practice page.

Run:  pip install -r requirements.txt && python app.py    then open http://127.0.0.1:5000
"""
import json, os, re, shutil, time, uuid
from werkzeug.exceptions import HTTPException
from flask import Flask, abort, jsonify, request, send_from_directory
from sat_parser import parse_pdf

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SAMPLE = os.path.join(ROOT, "samples", "questionbank-export-2026-9-27.pdf")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


def ingest(sid, pdf, name):
    d = os.path.join(DATA, sid)
    os.makedirs(d, exist_ok=True)
    data = parse_pdf(pdf, os.path.join(d, "assets"))
    data.update(source=name, id=sid)
    with open(os.path.join(d, "questions.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return data


def prune(max_age=24 * 3600):
    """Hosts have small disks: delete uploaded sets older than a day (the sample cache is kept)."""
    if not os.path.isdir(DATA):
        return
    for name in os.listdir(DATA):
        d = os.path.join(DATA, name)
        if name != "sample" and os.path.isdir(d) and time.time() - os.path.getmtime(d) > max_age:
            shutil.rmtree(d, ignore_errors=True)


@app.get("/")
def index():
    return send_from_directory(os.path.join(ROOT, "docs"), "index.html")


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or f.stream.read(5) != b"%PDF-":
        return jsonify(error="Please choose a PDF file."), 400
    f.stream.seek(0)
    prune()
    sid = uuid.uuid4().hex[:12]
    os.makedirs(os.path.join(DATA, sid))
    path = os.path.join(DATA, sid, "source.pdf")
    f.save(path)
    try:
        return jsonify(ingest(sid, path, os.path.basename(f.filename or "upload.pdf")))
    except Exception as e:  # malformed / encrypted PDFs
        return jsonify(error=f"Could not read this PDF ({type(e).__name__})."), 422


@app.get("/api/sample")
def sample():
    cache = os.path.join(DATA, "sample", "questions.json")
    if not os.path.exists(cache):
        if not os.path.exists(SAMPLE):
            return jsonify(error="The sample PDF is missing."), 404
        ingest("sample", SAMPLE, os.path.basename(SAMPLE))
    with open(cache, encoding="utf-8") as fh:
        return jsonify(json.load(fh))


@app.get("/assets/<sid>/<path:name>")
def assets(sid, name):
    if not re.fullmatch(r"[0-9a-f]{12}|sample", sid):
        abort(404)
    return send_from_directory(os.path.join(DATA, sid, "assets"), name)


@app.errorhandler(413)
def too_big(_):
    return jsonify(error="That file is larger than 25 MB."), 413


@app.errorhandler(Exception)
def fail(e):
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    app.logger.exception("Unhandled error")
    return jsonify(error=f"Server error ({type(e).__name__}). See the terminal for details."), 500


if __name__ == "__main__":
    # Local: 127.0.0.1:5000. On a host, set HOST=0.0.0.0 (PORT is usually provided), or use gunicorn (see Procfile).
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
