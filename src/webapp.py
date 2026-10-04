from pathlib import Path
import json
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from .db import init_db, add_job, list_jobs, find_duplicate, get_job, save_prep, get_prep, set_status, review_queue, submitted_count_today
from .scorer import score_job
from .tailoring import prepare_application, prep_to_dict, fingerprint_job

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "data" / "profile.json").read_text(encoding="utf-8"))
app = FastAPI(title="Job Application Agent v0.5")
init_db()


def esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_prep(job_id):
    job = get_job(job_id)
    prep_row = get_prep(job_id)
    if not job or not prep_row:
        return "<p>No application prep found.</p>"
    prep = json.loads(prep_row["prep_json"])
    answers = "".join(f"<tr><td><b>{esc(k.replace('_',' ').title())}</b></td><td>{esc(v)}</td></tr>" for k,v in prep["answers"].items())
    flags = "".join(f"<li>{esc(x)}</li>" for x in prep["risk_flags"]) or "<li>None detected</li>"
    emphasis = ", ".join(prep["emphasis"]) or "general security"
    return f"""
    <div class='card'><h2>Application prep — #{job_id}</h2>
    <p><b>{esc(job['company'])}</b> · {esc(job['title'])} · <a href='{esc(job['url'])}' target='_blank'>Open job</a></p>
    <p><b>Resume variant:</b> {esc(prep['resume_variant'])}</p>
    <p><b>Emphasis:</b> {esc(emphasis)}</p>
    <p><b>Matched JD keywords:</b> {esc(', '.join(prep['matched_keywords']) or 'None')}</p>
    <p><b>Missing / unverified keywords:</b> {esc(', '.join(prep['missing_keywords']) or 'None')}</p>
    <h3>Suggested summary</h3><p>{esc(prep['suggested_summary'])}</p>
    <h3>Application answers</h3><table>{answers}</table>
    <h3>Risk flags</h3><ul>{flags}</ul>
    <form method='post' action='/jobs/{job_id}/status' class='inline'><button name='status' value='APPROVED'>Approve for submission</button><button name='status' value='REJECTED'>Reject</button></form>
    </div>"""


@app.get("/", response_class=HTMLResponse)
def home():
    jobs = list_jobs()
    queue = review_queue()
    rows = ''.join(
        f"<tr><td>{r['id']}</td><td><b>{esc(r['title'])}</b></td><td>{esc(r['company'])}</td><td>{r['score'] or 0}/100</td>"
        f"<td>{esc(r['recommendation'])}</td><td>{esc(r['status'])}</td><td><a href='/jobs/{r['id']}'>Review</a> · <a href='{esc(r['url'])}' target='_blank'>Open</a></td></tr>"
        for r in jobs
    ) or '<tr><td colspan="7">No jobs yet.</td></tr>'
    skills = [s for group in PROFILE.get("skills", {}).values() for s in group]
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>Job Application Agent</title>
    <style>body{{font-family:Inter,Arial,sans-serif;margin:0;background:#f6f7fb;color:#16181d}}main{{max-width:1180px;margin:36px auto;padding:0 20px}}.card{{background:white;border:1px solid #e4e6eb;border-radius:14px;padding:20px;margin-bottom:18px;box-shadow:0 3px 12px rgba(0,0,0,.04)}}h1{{margin:0 0 6px}}h2{{margin-top:0}}.muted{{color:#68707d}}form.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}textarea{{grid-column:1/-1;min-height:180px}}input,textarea{{padding:10px;border:1px solid #cfd4dc;border-radius:8px;font:inherit;width:100%;box-sizing:border-box}}button{{padding:10px 14px;border:0;border-radius:8px;cursor:pointer;margin-right:8px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #eee;text-align:left;font-size:14px}}.skills span{{display:inline-block;background:#eef1f6;border-radius:20px;padding:5px 9px;margin:4px;font-size:12px}}.stat{{display:inline-block;margin-right:26px}}.inline{{display:inline}}</style></head><body><main>
    <div class='card'><h1>Job Application Agent v0.5</h1><div class='muted'>Targeted matching → application preparation → human review → tracking</div></div>
    <div class='card'><div class='stat'><b>{len(queue)}</b><br><span class='muted'>review queue</span></div><div class='stat'><b>{submitted_count_today()}/{PROFILE['targeting']['max_daily_applications']}</b><br><span class='muted'>submitted today</span></div><div class='stat'><b>₹{PROFILE['targeting']['minimum_ctc_lpa']}–₹{PROFILE['targeting']['preferred_ctc_lpa_range']['max']} LPA</b><br><span class='muted'>target range</span></div></div>
    <div class='card'><h2>Naukri browser</h2><p>Run <code>python -m src.cli naukri setup-credentials</code> once, then <code>python -m src.cli naukri login</code>. Scan with <code>python -m src.cli naukri scan</code>; prepare/apply from the queue with <code>python -m src.cli naukri apply --limit 10 --auto-submit</code>.</p></div><div class='card'><h2>Candidate profile</h2><p><b>{esc(PROFILE['name'])}</b> · {esc(PROFILE['headline'])} · {esc(PROFILE['location'])}</p><div class='skills'>{''.join(f'<span>{esc(s)}</span>' for s in skills)}</div></div>
    <div class='card'><h2>Add a job</h2><form class='grid' method='post' action='/jobs'><input name='company' placeholder='Company' required><input name='title' placeholder='Job title' required><input name='url' placeholder='Job URL' required><input name='description_file' placeholder='Optional local description file path'><textarea name='description' placeholder='Paste the job description here'></textarea><button type='submit'>Score &amp; prepare</button></form></div>
    <div class='card'><h2>Jobs</h2><table><thead><tr><th>#</th><th>Role</th><th>Company</th><th>Score</th><th>Recommendation</th><th>Status</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>
    </main></body></html>"""


@app.post("/jobs")
def create_job(company: str = Form(...), title: str = Form(...), url: str = Form(...), description: str = Form(""), description_file: str = Form("")):
    if description_file:
        try: description = Path(description_file).read_text(encoding="utf-8")
        except OSError: pass
    fp = fingerprint_job(company, title, url)
    if find_duplicate(company, title, url, fp):
        return RedirectResponse("/", status_code=303)
    result = score_job(title, description, company)
    job_id = add_job(company, title, url, description, result.score, result.recommendation, fp)
    if result.score >= PROFILE["targeting"]["minimum_match_score_for_review"]:
        prep = prepare_application(title, description, PROFILE, company)
        save_prep(job_id, json.dumps(prep_to_dict(prep), ensure_ascii=False))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int):
    job = get_job(job_id)
    if not job: return HTMLResponse("Not found", status_code=404)
    prep = get_prep(job_id)
    prep_html = render_prep(job_id) if prep else "<div class='card'><p>This job is below the review threshold. Application prep has not been generated.</p></div>"
    return HTMLResponse(f"<!doctype html><html><head><meta charset='utf-8'><title>Job #{job_id}</title><style>body{{font-family:Inter,Arial,sans-serif;margin:0;background:#f6f7fb}}main{{max-width:1000px;margin:36px auto;padding:0 20px}}.card{{background:white;border:1px solid #e4e6eb;border-radius:14px;padding:20px;margin-bottom:18px}}table{{width:100%;border-collapse:collapse}}td{{padding:9px;border-bottom:1px solid #eee}}button{{padding:10px 14px;border:0;border-radius:8px;margin-right:8px}}</style></head><body><main><div class='card'><a href='/'>← Dashboard</a><h1>{esc(job['title'])}</h1><p><b>{esc(job['company'])}</b> · Score <b>{job['score'] or 0}/100</b> · {esc(job['recommendation'])} · {esc(job['status'])}</p><p><a href='{esc(job['url'])}' target='_blank'>Open job in your browser</a></p><h3>Job description</h3><p style='white-space:pre-wrap'>{esc(job['description'])}</p><form method='post' action='/jobs/{job_id}/prepare'><button>Refresh application prep</button></form></div>{prep_html}<div class='card'><h2>Tracking</h2><form method='post' action='/jobs/{job_id}/status'><button name='status' value='APPROVED'>Approve</button><button name='status' value='SUBMITTED'>Mark submitted</button><button name='status' value='REJECTED'>Reject</button></form></div></main></body></html>")


@app.post("/jobs/{job_id}/prepare")
def prepare(job_id: int):
    job = get_job(job_id)
    if not job: return HTMLResponse("Not found", status_code=404)
    prep = prepare_application(job["title"], job["description"] or "", PROFILE, job["company"])
    save_prep(job_id, json.dumps(prep_to_dict(prep), ensure_ascii=False))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/status")
def change_status(job_id: int, status: str = Form(...)):
    try:
        set_status(job_id, status, "Updated from dashboard", PROFILE["targeting"]["max_daily_applications"])
    except ValueError as exc:
        return HTMLResponse(f"<p>{esc(exc)}</p><p><a href='/jobs/{job_id}'>Back</a></p>", status_code=400)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
