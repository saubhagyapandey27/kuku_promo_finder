"""Single-process web boundary; saved browsing and parsing never construct a router."""
import hashlib
import importlib.util
import io
import json
import os
import secrets
import threading
import time
import zipfile
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field, SecretStr
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from src.llm.credentials import JobCredentials
from src.config import Config, load_env
from src.load import load_bytes_upload
from src.parse import parse_show
from src import jobs

load_env()
ROOT = Path(__file__).resolve().parents[1]
PRIVATE = os.getenv('PF_PRIVATE') == '1'
REMOTE = os.getenv('PF_REMOTE') == '1'
PASSCODE = os.getenv('APP_PASSCODE', '')
if REMOTE and PRIVATE and not PASSCODE:
    raise RuntimeError('Remote private review requires APP_PASSCODE')
if REMOTE and not os.getenv('PF_SESSION_SECRET'):
    raise RuntimeError('Remote service requires PF_SESSION_SECRET')
working_area = next((p for p in ROOT.parents if p.name == 'Rough Intermediate Files'), ROOT.parent / 'Rough Intermediate Files')
WORK = Path(os.getenv('PF_RUNS_DIR', str(working_area / 'web-runtime'))).resolve()
if WORK == ROOT or ROOT in WORK.parents:
    raise RuntimeError('PF_RUNS_DIR must be outside the submission')
WORK.mkdir(parents=True, exist_ok=True)
RESULTS = Path(os.getenv('PF_RESULTS_DIR', str(ROOT / 'outputs'))).resolve()
app = FastAPI(docs_url=None, redoc_url=None)
# Cross-site GitHub Pages -> Render needs SameSite=None; Secure. The local
# same-origin proxy keeps the original strict cookie.
_cross_site = REMOTE and bool(os.getenv('PF_ALLOWED_ORIGIN'))
app.add_middleware(SessionMiddleware, secret_key=os.getenv('PF_SESSION_SECRET') or secrets.token_hex(32),
                   https_only=bool(REMOTE), same_site='none' if _cross_site else 'strict', max_age=86400)
registry = jobs.JobRegistry()
lock = threading.Lock()
inputs = {}
owned_jobs = {}
starts = {}

@app.exception_handler(RequestValidationError)
async def invalid_request(req, exc):
    # Never echo request values: a malformed job request can contain credentials.
    return JSONResponse(status_code=422, content={'detail':'Invalid request fields; check input token, profile, credentials and positive spend cap.'})
catalog = {'demo': (ROOT / 'demo', 'Original synthetic demo · saved replay', None)}
if PRIVATE and RESULTS.is_dir():
    catalog['accepted'] = (RESULTS, 'Accepted · final operational recovery', None)

@app.middleware('http')
async def boundary(request, call_next):
    if request.method == 'POST' and request.headers.get('origin'):
        from urllib.parse import urlparse
        if (urlparse(request.headers['origin']).netloc != request.headers.get('host') and
                request.headers['origin'] != os.getenv('PF_ALLOWED_ORIGIN')):
            return __import__('starlette.responses', fromlist=['JSONResponse']).JSONResponse({'detail':'Cross-origin write refused'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

# CORS for the GitHub Pages origin. Added last so it is outermost and answers
# preflight (OPTIONS) before the write-origin check. Explicit origin only (never
# a wildcard) because signed session cookies are sent with credentials.
_allowed_origin = os.getenv('PF_ALLOWED_ORIGIN')
if _allowed_origin:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware,
                       allow_origins=[_allowed_origin],
                       allow_credentials=True,
                       allow_methods=['GET', 'POST', 'OPTIONS'],
                       allow_headers=['*'])

def owner(req):
    if PASSCODE and not req.session.get('authed'):
        raise HTTPException(401, 'Private API requires sign in')
    if 'owner' not in req.session:
        req.session['owner'] = secrets.token_hex(24)
    return req.session['owner']

class Login(BaseModel):
    passcode: str

@app.post('/api/session')
def login(body: Login, req: Request):
    # Constant-time comparison; HTTP Basic-style operator credential, signed session cookie.
    if not PASSCODE or not secrets.compare_digest(body.passcode, PASSCODE):
        raise HTTPException(401, 'Incorrect access code')
    req.session.clear()
    req.session.update(authed=True, owner=secrets.token_hex(24))
    return {'authenticated': True}

def capabilities():
    cfg = Config.load('quality')
    missing = [k for k in cfg.required_credentials() if not os.getenv(k)]
    problems = cfg.final_contract_problems()
    visitor_reason = ('Visitor-key execution is disabled by the operator.' if os.getenv('PF_VISITOR_KEYS_ENABLED','1') != '1' else
                      'Remote jobs require persistent budget/run storage.' if REMOTE and os.getenv('PF_PERSISTENT_STORAGE') != '1' else
                      'Active profile routes are incomplete.' if problems else '')
    # Visitor jobs are OpenRouter-only (BYOK). Gemini is optional: the pipeline's
    # role chains fall back to OpenRouter models when a Gemini key is absent.
    required = sorted({sp.api_key_env or 'OPENROUTER_API_KEY'
                       for role in cfg.active_roles() for sp in cfg.role_prefs(role)
                       if sp.provider != 'gemini'})
    if set(required) - {'OPENROUTER_API_KEY','GEMINI_API_KEY'}:
        visitor_reason = 'This profile requires credentials unsupported by the visitor form.'
    enabled = os.getenv('PF_LIVE_ENABLED') == '1'
    persistent = not REMOTE or os.getenv('PF_PERSISTENT_STORAGE') == '1'
    reason = ('Operator has disabled live execution.' if not enabled else
              'Remote execution requires persistent budget/run storage.' if not persistent else
              'Remote live execution requires APP_PASSCODE authentication.' if REMOTE and not PASSCODE else
              'Active profile credentials are missing.' if missing else
              'Active profile routes are incomplete.' if problems else '')
    formats = ['txt', 'md', 'zip']
    for ext, module in [('pdf','pypdf'), ('docx','docx')]:
        if importlib.util.find_spec(module): formats.append(ext)
    return {'profiles':['quality'], 'formats':formats, 'live_enabled':not bool(reason),
            'live_reason':reason, 'max_upload_mb':10, 'visitor_enabled':not bool(visitor_reason),
            'visitor_reason':visitor_reason, 'profile_credentials':{'quality':required}}

@app.get('/api/health')
def health(): return {'status':'ok'}

@app.get('/api/config')
def config(req: Request):
    owner(req)
    return capabilities()

def entry(rid, who):
    e = catalog.get(rid)
    if not e or (e[2] is not None and e[2] != who): raise HTTPException(404, 'Saved run unavailable')
    return e

@app.get('/api/runs')
def runs(req: Request):
    who = owner(req)
    return [{'id':k,'label':v[1], 'files':{f:(v[0]/('promos.'+f)).is_file() for f in ['json','csv']}}
            for k,v in catalog.items() if v[2] is None or v[2] == who]

def payload(path):
    try:
        p = json.loads((path/'promos.json').read_text(encoding='utf-8-sig'))
        assert isinstance(p.get('promos'), list)
    except (OSError, ValueError, AssertionError): raise HTTPException(422, 'Saved JSON is missing or invalid; choose another run')
    # Explicit presentation allowlist: omit request diagnostics, source archives and ledger paths.
    result = {k:p[k] for k in ['show','posting_plan','coverage','note'] if k in p}
    result['cost'] = {k:p.get('cost',{}).get(k) for k in ['total_usd','cache_hits','budget_usd']}
    result['promos'] = []
    keys = ['promo_id','rank','title','segments','duration_s','opening_window','selected_transcript','hook','cliffhanger','why_it_works','biggest_weakness','scores','ranking','flags','longest_dialogue_impact_gap_s']
    for r in p['promos']:
        row = {k:r[k] for k in keys if k in r}
        e = r.get('eligibility',{})
        row['eligibility'] = {k:e[k] for k in ['assessment','ranking','fingerprint'] if k in e}
        row['proof_summary'] = [r.get('reason','') for r in e.get('readiness',{}).get('reviews',[]) if r.get('reason')]
        postcut = []
        for rv in e.get('readiness', {}).get('reviews', []):
            for w in (rv.get('postcut_response') or []):
                if w.get('quote'):
                    postcut.append({'block_id': w.get('block_id'), 'quote': w.get('quote')})
        if postcut:
            row['postcut'] = postcut
        result['promos'].append(row)
    result['sha256'] = hashlib.sha256((path/'promos.json').read_bytes()).hexdigest()
    notes = Path(os.getenv('PF_REVIEW_NOTES', str(working_area / 'web-review-notes.json')))
    if PRIVATE and path.resolve() == RESULTS and notes.is_file():
        derived = json.loads(notes.read_text(encoding='utf-8'))
        if derived.get('output_sha256') == result['sha256']:
            result['cost_scope'] = derived.get('cost_scope')
            review = derived.get('review_note',{})
            if review.get('promo_id') in {r['promo_id'] for r in result['promos']}:
                result['review_note'] = {**review, 'output_sha256':result['sha256']}
    return result

@app.get('/api/runs/{rid}')
def run(rid: str, req: Request): return payload(entry(rid, owner(req))[0])

@app.get('/api/runs/{rid}/files/{fmt}')
def download(rid: str, fmt: str, req: Request):
    path = entry(rid, owner(req))[0]
    if fmt not in ['json','csv']: raise HTTPException(404)
    file = path / ('promos.'+fmt)
    if not file.is_file(): raise HTTPException(404, 'Original file not available')
    return FileResponse(file, filename=file.name)

@app.post('/api/inputs/validate')
async def validate(req: Request):
    who = owner(req)
    if len(inputs) >= 100: raise HTTPException(429, 'Input capacity reached; restart the local service to clear temporary inputs')
    # Bound the entire multipart body before its parser allocates upload storage.
    size = 0
    chunks = []
    async for chunk in req.stream():
        size += len(chunk)
        if size > 10*1024*1024: raise HTTPException(413, 'Upload limit is 10 MB')
        chunks.append(chunk)
    req._body = b''.join(chunks)
    form = await req.form(max_files=50, max_fields=2)
    files = []
    expanded = 0
    try:
        for f in form.getlist('files'):
            data = await f.read()
            name = Path(f.filename).name
            if Path(name).suffix.lower().lstrip('.') not in capabilities()['formats']:
                raise ValueError('Unsupported file format')
            if name.lower().endswith(('.zip', '.docx')):
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    if len(z.infolist()) > 500: raise ValueError('ZIP limit is 500 entries')
                    expanded += sum(i.file_size for i in z.infolist())
            else: expanded += len(data)
            if expanded > 30*1024*1024: raise ValueError('Expanded input limit is 30 MB')
            files.append((name,data))
        texts = load_bytes_upload(files)
        show, report = parse_show(texts, None, 'Uploaded screenplay')
        if not show.episodes or not report.blocks or report.errors:
            raise ValueError(report.errors[0].msg if report.errors else 'No timestamped episodes found')
    except Exception as exc:
        raise HTTPException(422, 'Input could not be parsed: '+str(exc)[:250]) from None
    token = secrets.token_urlsafe(24)
    inputs[token] = (who, texts, time.time())
    return {'input_token':token, 'episodes':len(show.episodes), 'scenes':sum(len(e.scenes) for e in show.episodes),
            'blocks':report.blocks, 'report':report.model_dump()}

class Start(BaseModel):
    input_token: str
    budget: float = Field(gt=0, le=10, allow_inf_nan=False)
    profile: str = 'quality'
    openrouter_key: SecretStr | None = Field(default=None, max_length=4096, repr=False)
    gemini_key: SecretStr | None = Field(default=None, max_length=4096, repr=False)

@app.post('/api/jobs', status_code=202)
def start(body: Start, req: Request):
    who = owner(req)
    if req.url.scheme != 'https': raise HTTPException(400, 'Visitor credentials require HTTPS. Saved browsing and parsing remain available.')
    item = inputs.get(body.input_token)
    if not item or item[0] != who: raise HTTPException(404, 'Validate the current input first')
    if body.profile != 'quality': raise HTTPException(422, 'Unsupported profile')
    with lock:
        key = (who, body.input_token)
        if key in starts: return {'job_id':starts[key]}
        caps = capabilities()
        if not caps['visitor_enabled']: raise HTTPException(403, caps['visitor_reason'])
        values = {name: secret.get_secret_value().strip() for name,secret in
                  [('OPENROUTER_API_KEY',body.openrouter_key),('GEMINI_API_KEY',body.gemini_key)] if secret}
        required = caps['profile_credentials'][body.profile]
        if any(not values.get(k) for k in required):
            raise HTTPException(422, 'Enter all keys required by the selected profile: '+', '.join(required))
        if any(registry.alive(v[1]) for v in owned_jobs.values()): raise HTTPException(409, 'Another analysis is active; try again after it finishes')
        jid = secrets.token_urlsafe(24)
        credentials = JobCredentials({k:values[k] for k in required})
        values.clear()
        try:
            directory = registry.start(item[1], 'Uploaded screenplay', body.profile, None, body.budget, str(WORK / jid), credentials=credentials)
        except Exception:
            credentials.clear()
            raise HTTPException(500, 'Analysis could not be started; credentials were discarded') from None
        owned_jobs[jid] = (who, directory, time.time())
        starts[key] = jid
        return {'job_id':jid}

@app.get('/api/jobs/{jid}')
def status(jid: str, req: Request):
    who = owner(req)
    v = owned_jobs.get(jid)
    if not v or v[0] != who: raise HTTPException(404, 'Job unavailable; a service restart may have interrupted it')
    st = jobs.job_status(v[1])
    state = st.get('state')
    if state == 'running' and not registry.alive(v[1]): state = 'interrupted'
    _, phase = jobs.read_progress(v[1])
    result = {'job_id':jid, 'state':state, 'phase':phase or 'Preparing input', 'elapsed_s':round(time.time()-v[2]), 'cost_usd':jobs.run_cost(v[1])}
    if state == 'error':
        raw = st.get('error','')
        result['error'] = ('Provider preflight failed; check credentials, route availability and current prices.' if 'preflight' in raw.lower() else
                           'Spend cap reached; this input may have partial work saved.' if 'budget' in raw.lower() else
                           'Input parsing failed; validate the screenplay again.' if 'pars' in raw.lower() else
                           'Analysis failed; inspect the private local status file for provider or pipeline details.')
    if state in ['done','incomplete'] and (Path(v[1])/'promos.json').is_file():
        catalog[jid] = (Path(v[1]), 'Uploaded screenplay · saved analysis', who)
        result.update(run_id=jid, count=len(payload(Path(v[1]))['promos']))
    return result


