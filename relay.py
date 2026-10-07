#!/usr/bin/env python3
"""Codex <-> Claude relay: two coding assistants share one task through a written hand-off note.

Run it from inside any Git repository:

  relay.py run --task "..."                     backup: Codex works; if it runs out of usage, Claude carries on
  relay.py run --task "..." --start claude      backup, Claude first
  relay.py run                                  pick up a baton that says Status: continue
  relay.py run --task "..." --check 3 --calls 8 make and check: Claude works (up to 8 live test calls in total),
                                                Codex reviews read-only each round until it approves
  relay.py run --task "..." --take-turns 4      take turns: alternate one step each, at most 4 turns
  relay.py run ... --dry-run                    print what each assistant would be told; no model call
  relay.py init                                 create AGENTS.md, CLAUDE.md, relay.json and a progress file, then
                                                ask you 6 quick questions about the project (Enter skips one)
  relay.py answer                               answer the questions the assistants left for you, then carry on
  relay.py look                                 screenshot the pages in relay.json "look" at desktop and phone size
  relay.py status                               who holds the lock, and the current baton
  relay.py pass --to codex --task "..." --note "..."   hand off by hand at the end of an interactive chat

Shared state lives in .relay/ in the repository: baton.md (the hand-off note), log.md (one line per event),
reviews/ (make-and-check reviews) and transcripts/ (everything each assistant printed; add it to .gitignore).
Optional settings come from relay.json at the repository root; see README.md.

When an assistant needs you (your goals, taste or a fact only you know), the relay shows its questions. At a
keyboard you answer there and it carries on; otherwise it saves them and stops until you run relay.py answer.

Every session claims a lock in the Git directory so only one assistant edits at a time, and gets a call
ledger (RELAY_CALL_BUDGET) that your test runner can check before each live model call. Outside make and check
the ledger allows no calls. Nothing retries, loops forever or runs on a schedule. Subscription sign-ins only:
Claude runs with ANTHROPIC_* and provider overrides removed, Codex with OPENAI_API_KEY removed and a ChatGPT
login required.
"""
import argparse, datetime, html, json, os, re, shutil, subprocess, sys, tempfile, uuid
from pathlib import Path

def git_root():
    try: return Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    except (subprocess.CalledProcessError, FileNotFoundError): raise SystemExit('Run the relay from inside a Git repository')

ROOT = git_root()
STATE = ROOT/'.relay'
BATON = STATE/'baton.md'
LOG = STATE/'log.md'
REVIEWS = STATE/'reviews'
TRANSCRIPTS = STATE/'transcripts'
QUESTIONS = STATE/'questions.json'
SHOTS = STATE/'screenshots'
RELAY_SCRIPT = Path(__file__).resolve()
AGENTS = ('codex', 'claude')
STATUSES = ('continue', 'done', 'ask-user')
MAX_TURNS, MAX_ROUNDS = 8, 5
TURN_TIMEOUT = 45*60      # one step in take-turns mode, and one review
TASK_TIMEOUT = 3*60*60    # one whole-task session
MAX_PAUSES = 3            # question stops in one run, so a run always ends
# How the CLIs report a spent subscription allowance. Checked in the last 10 lines of a session that failed,
# and the last 5 lines of one that exited cleanly, so a file the assistant read earlier cannot trigger it.
OUT_OF_USAGE = re.compile(r'usage limit|hit your (usage )?limit|limit reached|limit will reset|quota exceeded|out of (usage|credits)', re.I)
DEFAULTS = {'notes': ['AGENTS.md', 'CLAUDE.md', 'README.md'], 'checkpoint': None, 'tests': [], 'live_command': None,
            'review_criteria': [], 'claude_model': 'sonnet', 'codex_model': None, 'blind_hide': [],
            'keep_chats': False, 'look': [], 'lock_name': 'relay-writer-lock'}

def settings():
    path = ROOT/'relay.json'
    config = dict(DEFAULTS)
    if path.exists(): config.update(json.loads(path.read_text()))
    config['notes'] = [n for n in config['notes'] if (ROOT/n).exists()]
    return config

CONFIG = settings()

# ---- small helpers -------------------------------------------------------------------------------------------
def now(): return datetime.datetime.now().astimezone().isoformat(timespec='minutes')
def unique(): return f'{datetime.datetime.now():%Y-%m-%d-%H%M%S}-{uuid.uuid4().hex[:8]}'
def other(agent): return AGENTS[1 - AGENTS.index(agent)]
def git(*args): return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
def rel(path): return os.path.relpath(path, ROOT)

def lock_path():
    return (ROOT/git('rev-parse', '--git-common-dir')).resolve()/CONFIG['lock_name']

def claim(path, owner):
    try: path.mkdir()
    except FileExistsError: raise RuntimeError('workspace already claimed; run status and stop the other writer first')
    token = uuid.uuid4().hex
    (path/'owner.json').write_text(json.dumps({'owner': owner, 'token': token, 'started': now()}, indent=2))
    return token

def release(path, token):
    record = json.loads((path/'owner.json').read_text())
    if record['token'] != token: raise RuntimeError('wrong lock token; lock unchanged')
    (path/'owner.json').unlink(); path.rmdir()

def create_budget(limit):
    path = TRANSCRIPTS/f'{unique()}-budget'
    path.mkdir(parents=True)
    (path/'budget.json').write_text(json.dumps({'limit': limit}))
    return path

def budget_used(path):
    return sum(p.is_dir() and re.fullmatch(r'call-\d+', p.name) is not None for p in path.iterdir())

def read_baton():
    if not BATON.exists(): return None
    text = BATON.read_text()
    field = lambda name: ((re.search(rf'^{name}:\s*(.+)$', text, re.M) or [None, ''])[1]).strip()
    return {'text': text, 'status': field('Status').lower(), 'to': field('To').lower(), 'task': field('Task')}

def write_baton(task, sender, to, status, did, next_step, watch=''):
    STATE.mkdir(parents=True, exist_ok=True)
    BATON.write_text(f'# Relay baton\n\nStatus: {status}\nFrom: {sender}\nTo: {to}\nTask: {task}\nUpdated: {now()}\n\n'
                     f"## What I did\n{did}\n\n## What's next\n{next_step}\n\n## Watch out for\n{watch or 'Nothing new.'}\n\n"
                     "## Decisions and why\nNone yet.\n\n## Tried, didn't work\nNothing yet.\n")

def record_decision(text):
    """Add a [user] decision under Decisions and why. User decisions are final for both assistants."""
    note = BATON.read_text()
    line = f'- [user] {text.strip()} ({now()})'
    if '## Decisions and why' in note:
        head, rest = note.split('## Decisions and why', 1)
        body, sep, tail = rest.partition('\n## ')
        body = body.replace('\nNone yet.', '')
        note = f"{head}## Decisions and why{body.rstrip()}\n{line}\n" + (f'\n## {tail}' if sep else '')
    else:
        note = note.rstrip('\n') + f'\n\n## Decisions and why\n{line}\n'
    BATON.write_text(note)

def add_to_baton(to, status, heading, text):
    """Point the existing note at someone and append a section, keeping everything already in it."""
    note = re.sub(r'^Status:.*$', f'Status: {status}', BATON.read_text(), count=1, flags=re.M)
    note = re.sub(r'^To:.*$', f'To: {to}', note, count=1, flags=re.M)
    note = re.sub(r'^Updated:.*$', f'Updated: {now()}', note, count=1, flags=re.M)
    BATON.write_text(note.rstrip('\n') + f'\n\n## {heading}\n{text.strip()}\n')

def log(line):
    STATE.mkdir(parents=True, exist_ok=True)
    if not LOG.exists(): LOG.write_text('# Relay log\n\nOne line per event. Newest last.\n\n')
    with LOG.open('a') as f: f.write(f'- {now()} {line}\n')

def stop(reason, ok=True):
    log('relay stopped: ' + reason)
    print(('Relay stopped: ' if ok else 'Relay stopped early: ') + reason)
    print(f'Full details: {rel(TRANSCRIPTS)}/')
    return 0 if ok else 1

def parse_verdict(text):
    lines = text.strip().splitlines()
    found = re.fullmatch(r'Verdict: (approve|revise|ask-user)', lines[-1]) if lines else None
    if not found or sum(line.startswith('Verdict:') for line in lines) != 1: return None
    return found[1]

# ---- what each assistant is told -------------------------------------------------------------------------------
def rules(live_calls=0):
    notes = ', '.join(CONFIG['notes']) or 'the README'
    tests = ('Check your work with: ' + '; '.join(CONFIG['tests']) + ' (run from the repository root, without cd).') if CONFIG['tests'] \
        else 'Check your work with the project\'s own tests.'
    checkpoint = f" Update the progress notes in {CONFIG['checkpoint']}." if CONFIG['checkpoint'] else ''
    calls = (f"You may make at most {live_calls} live model calls, only through {CONFIG['live_command']}. The inherited "
             "RELAY_CALL_BUDGET ledger enforces a shared cap across every invocation; do not unset it or bypass the runner. "
             "Count every attempt, including failures.") if live_calls else 'Make no live model calls.'
    return f"""First read {notes} and {rel(BATON)} if it exists.
The relay already holds the writer lock for you: do not claim or release it.
Rules: preserve existing untracked work; stay on the current Git branch; never configure an API key or fallback;
no pushing or publishing. {calls}
If the task is unclear in a way only the user can settle (their goals, audience, taste or a fact about their
business) and the notes don't answer it, don't guess: before changing anything, ask under Questions for you.
{tests}{look_rule()} Commit your work in small scoped commits (if Git is read-only in your sandbox, leave changes
uncommitted and say so under Watch out for).{checkpoint}"""

def look_rule():
    if not CONFIG['look']: return ''
    return (f' To see the page as people will, run: python3 {RELAY_SCRIPT} look (exactly as written). It saves desktop and phone '
            f'screenshots in {rel(SHOTS)}/; open the PNG files to look at them. Check how it looks before saying it is done.')

def screenshots_note():
    shots = sorted(SHOTS.glob('*.png')) if SHOTS.exists() else []
    if not shots: return ''
    return ('\nScreenshots of the page as it is now, taken by the relay (you may open these even though they are in .relay/): '
            + ', '.join(rel(s) for s in shots) + '. Look at them: judge what people will actually see, at desktop and phone size. '
            'In the phone pictures the page is the left 390 pixels; the grey strip on the right is only padding from the screenshot tool.')

def baton_shape(agent, task, extra=''):
    return f"""# Relay baton

Status: continue | done | ask-user
From: {agent}
To: {other(agent)}
Task: {task}
{extra}Updated: <now>

## What I did
## What's next
## Watch out for
## Decisions and why
## Tried, didn't work
## Questions for you

Keep everything already under Decisions and why and Tried, didn't work: copy it forward and add to it, never
drop it. That is how the next assistant knows what was chosen on purpose and what not to try again.
Start each decision with who made it: [user] or [codex]/[claude]. [user] decisions are final. An assistant's
decision may be challenged, but only with evidence: say what you found, change it, and keep the old line
marked "(replaced: ...)". Mark every claim about the work as (checked: how you checked it) or (assumed). Before
relying on anything (assumed) from an earlier note, check it yourself; earlier notes are leads, not facts.
Questions for you is for what only the user can settle: their goals, taste, a fact about their business, or anything
you would otherwise mark (assumed) about what they want. Don't settle those yourself. At most 3, numbered, in plain
English a non-programmer can answer (no file names, line numbers or code); under each, if you have a view, a line "Suggest: <answer>, because <one-line reason>". The
relay puts them to the user, so only ask what matters, and ask everything you need in one go; write "None." if there
are none. Don't ask again about anything the user has already decided (their lines under Decisions and why), and never
suggest an answer that undoes one of their decisions: if you think one should change, say why and ask.
Use Status: done only when the whole task is finished and checked. Use Status: ask-user when you can't sensibly go on
until the user answers. Otherwise use continue."""

def backup_prompt(agent, task, picking_up):
    start = (f'{other(agent).capitalize()} was working on this and ran out of usage. Read the baton first and carry on '
             'from where it stopped; check git status and git diff for unfinished changes it left.') if picking_up \
        else 'If the baton is for this task, it is an earlier hand-off: continue from it.'
    return f"""You are {agent.capitalize()}, working on this repository as part of a Codex <-> Claude relay.
Task: {task}

{start}
{rules()}
Work through the whole task, not just one step. Your usage allowance may run out at any moment, so after each
meaningful step rewrite {rel(BATON)} so {other(agent).capitalize()} could pick up from there:

{baton_shape(agent, task)}"""

def turn_prompt(agent, task, turn, turns):
    return f"""You are {agent.capitalize()} on turn {turn} of at most {turns} in a Codex <-> Claude relay on this repository.
Task for the whole relay: {task}

{rules()}
Do one focused, useful step toward the task, then end your turn by rewriting {rel(BATON)} in exactly this shape:

{baton_shape(agent, task)}"""

def maker_prompt(agent, task, rnd, rounds, remaining, review):
    feedback = f'Address this independent review of the previous round first:\n\n{review}\n\n' if review else ''
    return f"""You are {agent.capitalize()}, the maker in round {rnd} of at most {rounds} of a make-and-check relay.
Task: {task}

{feedback}{rules(remaining)}
Do the work for this round and check it. An independent reviewer will read your changes and the baton next, so
say plainly what you changed, what you tested and what the results were (quote real outputs; do not overstate).
End by rewriting {rel(BATON)} in this shape, including the Calls used line:

{baton_shape(agent, task, 'Calls used: <live model calls you made this round>' + chr(10))}
Add a ## Results section with the real outputs."""

def blind_prompt(agent, task, rnd):
    criteria = ', '.join(CONFIG['review_criteria'] or CONFIG['notes']) or 'the README'
    return f"""You are {agent.capitalize()}, reviewing round {rnd} of a make-and-check relay on this repository. BLIND REVIEW.
Task: {task}

Another assistant has just worked on this task. You will not see its report yet, on purpose: form your own view
first, so you are not steered by how it framed things. Do not open anything in .relay/, do not read its
commit messages for its explanations, and skip its write-ups ({', '.join(notes_files()) or 'progress notes'}) even
in git diff or git log -p: look only at the work itself (git status, git diff, git log for which files changed, and
the files). You are read-only: do not edit files or make model calls.{run_tests()}

{screenshots_note()}
Judge the work against the task and the project's own criteria ({criteria}). List what is right, what is wrong or
missing, and anything you are unsure about, each with evidence (file and line). Anything only the user can settle
(their goals, taste or business facts) is a question for them, not something to decide. Do not give a verdict."""

def run_tests():
    if not CONFIG['tests']: return ''
    return (' To prove a finding, run the tests: ' + '; '.join(CONFIG['tests']) + ' (exactly as written, from the repository '
            'root, without cd). Other commands may be refused, so check anything else by reading the code and working it out.')

def checker_prompt(agent, task, rnd, blind_findings=None):
    criteria = ', '.join(CONFIG['review_criteria'] or CONFIG['notes']) or 'the README'
    return f"""You are {agent.capitalize()}, the independent reviewer in round {rnd} of a make-and-check relay on this repository.
Task: {task}

You are read-only: do not edit files or make model calls.{run_tests()} Read {rel(BATON)} (the maker's report), then check the
actual work: git log, git diff, the files and any saved results it names. Judge it against the task and the
project's own criteria ({criteria}). Verify claims yourself rather than trusting the report; quote evidence.{screenshots_note()}
{compare(blind_findings)}
Your final message is saved as the review, and the user may read it, so keep it short and plain:
1. Summary: two or three sentences anyone could follow, no jargon.
2. Must fix: at most 3 items, most important first, each with evidence and the correction needed. Anything that goes
   against one of the user's decisions (their [user] lines in the report) belongs here.
3. Could also improve: optional, one line each.
4. Questions for you: what only the user can settle (their goals, taste, business facts), including any claim in the
   report marked (assumed) about what they want that you can't verify. Don't settle these yourself. At most 3,
   numbered, in plain English a non-programmer can answer (no file names, line numbers or code), each followed by "Suggest: <answer>, because <one-line reason>" if you have a view; "None." if none.
   The relay puts them to the user before the next round.
End with exactly one of these as the last line, and nothing after it:
"Verdict: approve", "Verdict: revise" or "Verdict: ask-user".
Use approve only when the task is met and checked. Use ask-user when what's left depends on the user's answers."""

def compare(blind_findings):
    if not blind_findings: return ''
    return f"""
Before reading the report, you reviewed the work blind and wrote this:

{blind_findings}

Now compare, in a short "Compared with the report" section: one line each for problems only you found, problems only
the maker reported, and anything you disagree on. Re-check each against the code before deciding; agreement between
two assistants is not evidence. Then a "Changed my mind" section: each blind finding you dropped or changed after
reading the report, with the new evidence that changed it (file and line, or a test result). Reading the report is
not evidence: with no new evidence, keep the finding. Write "None." if nothing changed.
"""

# ---- running one assistant -------------------------------------------------------------------------------------
def command_for(agent, prompt, last_message, live_calls=False, read_only=False):
    if agent == 'claude':
        model = ['--model', CONFIG['claude_model']] if CONFIG['claude_model'] else []
        if read_only:
            # The project's own tests are allowed so Claude can prove a finding, as Codex can in its read-only sandbox.
            tools = ['Read', 'Glob', 'Grep', *[f'Bash({t}:*)' for t in CONFIG['tests']], 'Bash(git diff:*)', 'Bash(git log:*)',
                     'Bash(git show:*)', 'Bash(git status:*)']
            return ['claude', '-p', prompt, '--safe-mode', *model, *no_chat(agent), '--permission-mode', 'default', '--allowedTools', *tools]
        tests = [f'Bash({t}:*)' for t in CONFIG['tests']]
        live = [f"Bash({CONFIG['live_command']}:*)"] if live_calls and CONFIG['live_command'] else []
        look = [f'Bash(python3 {RELAY_SCRIPT} look:*)'] if CONFIG['look'] else []
        tools = ['Read', 'Edit', 'Write', 'Glob', 'Grep', *tests, *live, *look, 'Bash(git status:*)', 'Bash(git diff:*)',
                 'Bash(git log:*)', 'Bash(git add:*)', 'Bash(git commit:*)']
        return ['claude', '-p', prompt, '--safe-mode', *model, *no_chat(agent), '--permission-mode', 'acceptEdits', '--allowedTools', *tools]
    model = ['-m', CONFIG['codex_model']] if CONFIG['codex_model'] else []
    images = [f'--image={s}' for s in sorted(SHOTS.glob('*.png'))] if read_only and SHOTS.exists() else []
    return ['codex', 'exec', *no_chat(agent), *images, '-C', str(ROOT), *model, '-s', 'read-only' if read_only else 'workspace-write', '-o', last_message, prompt]

def no_chat(agent):
    """Each session is a fresh, separate conversation (that is what keeps the blind review blind), but it need not be
    saved as a chat in the apps' history: the relay keeps its own record in .relay/. Set keep_chats to keep them."""
    if CONFIG['keep_chats']: return []
    return ['--no-session-persistence'] if agent == 'claude' else ['--ephemeral']

def subscription_env(agent):
    env = os.environ.copy()
    if agent == 'claude':
        for key in list(env):
            if key.startswith('ANTHROPIC_') or key in ('CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
                env.pop(key)
        auth = json.loads(subprocess.check_output(['claude', 'auth', 'status', '--json'], env=env, text=True))
        if not (auth.get('loggedIn') and auth.get('authMethod') == 'claude.ai' and not auth.get('apiKeySource')):
            raise RuntimeError('Claude needs a claude.ai subscription sign-in; no API fallback')
        return env
    env.pop('OPENAI_API_KEY', None)
    status = subprocess.run(['codex', 'login', 'status'], env=env, text=True, capture_output=True)
    if 'ChatGPT' not in status.stdout + status.stderr: raise RuntimeError('Codex needs a ChatGPT sign-in; no API fallback')
    return env

def session(agent, command, label, timeout, budget=None):
    """Run one assistant once while holding the lock. Returns (exit code, or None on timeout; transcript path)."""
    token = claim(lock_path(), agent)
    try:
        TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
        transcript = TRANSCRIPTS/f'{unique()}-{label}-{agent}.txt'
        env = subscription_env(agent)
        env['RELAY_CALL_BUDGET'] = str(budget if budget is not None else create_budget(0))
        with transcript.open('x') as out:
            code = subprocess.run(command, cwd=ROOT, env=env, timeout=timeout, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT).returncode
    except subprocess.TimeoutExpired:
        code = None
    finally:
        release(lock_path(), token)
    return code, transcript

def out_of_usage(transcript, lines=10):
    tail = transcript.read_text(errors='replace').splitlines()[-lines:]
    return next((line.strip()[:200] for line in reversed(tail) if OUT_OF_USAGE.search(line)), None)

def pick_up(task, start):
    baton = read_baton()
    if baton and baton['status'] == 'continue' and baton['to'] in AGENTS and not task: return baton['task'], baton['to']
    if not task: raise SystemExit('Give --task, or leave a baton with Status: continue to pick up')
    return task, start

def busy(): return lock_path().exists()

# ---- looking at the page ----------------------------------------------------------------------------------------
def find_chrome():
    for path in (os.environ.get('RELAY_CHROME'), '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                 '/Applications/Chromium.app/Contents/MacOS/Chromium', shutil.which('google-chrome'), shutil.which('chromium'),
                 shutil.which('chromium-browser'), shutil.which('chrome')):
        if path and Path(path).exists(): return path

def look(quiet=False):
    """Screenshot each page in relay.json "look" at desktop and phone size into .relay/screenshots/. Returns the files."""
    if not CONFIG['look']:
        if not quiet: print('Add the pages to look at to relay.json, e.g. "look": ["index.html"] or "look": ["http://localhost:3000"]')
        return []
    chrome = find_chrome()
    if not chrome:
        log('look: no Chrome or Chromium found, so no screenshots')
        if not quiet: print('No Chrome or Chromium found: install one, or set RELAY_CHROME to its path')
        return []
    SHOTS.mkdir(parents=True, exist_ok=True)
    for old in SHOTS.glob('*.png'): old.unlink()
    made = []
    for page in CONFIG['look']:
        url = page if re.match(r'https?://', page) else (ROOT/page).resolve().as_uri()
        name = re.sub(r'[^A-Za-z0-9]+', '-', re.sub(r'^https?://', '', page)).strip('-')[:60] or 'page'
        with tempfile.TemporaryDirectory() as tmp:
            # Chrome won't make a window narrower than 500px, so the phone view is the page in a 390px-wide frame.
            frame = Path(tmp)/'phone.html'
            frame.write_text(f'<!doctype html><body style="margin:0;background:#777"><iframe src="{html.escape(url)}" '
                             'style="border:0;width:390px;height:1600px;display:block;background:#fff"></iframe>')
            for label, target, size in (('desktop', url, '1280,1000'), ('phone', frame.as_uri(), '500,1600')):
                out = SHOTS/f'{name}-{label}.png'
                subprocess.run([chrome, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--allow-file-access-from-files',
                                '--virtual-time-budget=3000', f'--window-size={size}', f'--screenshot={out}', target],
                               capture_output=True, timeout=60)  # a separate --user-data-dir makes headless Chrome hang on macOS
                if out.exists(): made.append(out)
    log(f'look: {len(made)} screenshot(s) in {rel(SHOTS)}/')
    if not quiet:
        for shot in made: print('saved', rel(shot))
        if made: print('The phone view is the left 390 pixels of each phone picture.')
    return made

# ---- questions for the user -------------------------------------------------------------------------------------
QUESTIONS_HEADING = re.compile(r'^(?:#{1,6}\s*|\*\*|\d+\.\s*\**)Questions for you:?(?:\*\*)?:?[ \t]*(.*)$', re.M)
SECTION_END = re.compile(r'^(?:#{1,6}\s|\*\*[^*\n]+\*\*:?\s*$|Verdict:)', re.M)

def parse_questions(text):
    """Numbered questions under a 'Questions for you' heading, each with an optional 'Suggest:' line."""
    heading = None
    for heading in QUESTIONS_HEADING.finditer(text): pass  # the last one: the newest note or review
    if not heading: return []
    body = heading[1] + '\n' + text[heading.end():]
    end = SECTION_END.search(body, 1)
    questions = []
    for line in body[:end.start() if end else None].splitlines():
        hint = re.match(r'\s*(?:[-*]\s*)?(?:\*\*)?Suggest(?:ed|ion)?(?:\*\*)?\s*:(?:\*\*)?\s*(.+)', line, re.I)
        item = re.match(r'\s*\d+[.)]\s+(.+)', line)
        if hint and questions: questions[-1]['suggest'] = hint[1].strip()
        elif item:  # the suggestion is sometimes written on the same line as the question
            parts = re.split(r'\s+(?:\*\*)?Suggest(?:ed|ion)?(?:\*\*)?\s*:(?:\*\*)?\s*', item[1].strip(), maxsplit=1, flags=re.I)
            questions.append({'question': parts[0].strip(), 'suggest': parts[1].strip() if len(parts) > 1 else ''})
        elif line.strip() and questions and not questions[-1]['suggest']: questions[-1]['question'] += ' ' + line.strip()
    return [q for q in questions if not re.fullmatch(r'(none|n/?a)\.?', q['question'], re.I)]

def questions_in(baton, asker=None):
    """The questions an assistant left in the baton; a bare ask-user status becomes one question from What's next."""
    questions = [{**q, 'from': asker} if asker else q for q in parse_questions(baton['text'])]
    if not questions and baton['status'] == 'ask-user':
        found = re.search(r"^## What's next\s*\n(.*?)(?=^## |\Z)", baton['text'], re.M | re.S)
        questions = [{'question': ' '.join((found[1] if found else '').split()) or f'The assistant needs a decision from you; see {rel(BATON)}.', 'suggest': ''}]
        if asker: questions[0]['from'] = asker
    return questions

def show_question(n, q, mixed=False):
    print(f'{n}. {q["question"]}' + (f'   (asked by {q["from"].capitalize()})' if mixed and q.get('from') else ''))
    if q['suggest']: print(f'   Suggested: {q["suggest"]}')

def ask_user(questions, asked_by, resume):
    """Put the questions to the user. At a keyboard: ask now, save the answers as [user] decisions and return True.
    Otherwise save them for `relay.py answer`, which records the answers and re-runs `resume`; return False."""
    STATE.mkdir(parents=True, exist_ok=True)
    QUESTIONS.write_text(json.dumps({'asked_by': asked_by, 'asked': now(), 'questions': questions, 'resume': resume}, indent=2) + '\n')
    log(f'{asked_by} asked the user {len(questions)} question(s)')
    who = sorted({q.get('from') or asked_by for q in questions})
    mixed = len(who) > 1
    print(f'\n{" and ".join(w.capitalize() for w in who)} {"have" if mixed else "has"} {len(questions)} '
          f'question{"s" if len(questions) != 1 else ""} for you before carrying on:\n')
    if sys.stdin.isatty():
        try:
            answers = []
            for n, q in enumerate(questions, 1):
                show_question(n, q, mixed)
                answers.append(input(f'   Your answer (Enter = {"go with the suggestion" if q["suggest"] else "let the assistants decide"}): '))
                print()
            save_answers(questions, answers)
            print('Thanks! Saved as your decisions. Continuing…', flush=True)
            return True
        except EOFError: print()
    for n, q in enumerate(questions, 1): show_question(n, q, mixed); print()
    print('Answer them with: python3 relay.py answer   (it asks them one by one, then carries on)')
    return False

def save_answers(questions, answers):
    """Record each answer as a final [user] decision. Empty or "suggested" takes the suggestion; "skip" leaves it to the assistants."""
    for q, given in zip(questions, answers):
        given = given.strip()
        if given.lower() in ('', 'suggested') and q['suggest']: record_decision(f'{q["question"]} Answer: {q["suggest"]} (the suggested answer)')
        elif given.lower() in ('', 'skip', 'suggested'): record_decision(f'Left to the assistants to decide: {q["question"]}')
        else: record_decision(f'{q["question"]} Answer: {given}')
    note = BATON.read_text()
    note = re.sub(r"(^## Questions for you\s*\n).*?(?=^## |\Z)", r'\1Answered: see Decisions and why.\n\n', note, count=1, flags=re.M | re.S)
    BATON.write_text(note)
    QUESTIONS.unlink(missing_ok=True)
    log(f'the user answered {len(answers)} question(s)')

def carry_on(to):
    """After the user's answers, point the baton at whoever continues."""
    note = re.sub(r'^Status:.*$', 'Status: continue', BATON.read_text(), count=1, flags=re.M)
    BATON.write_text(re.sub(r'^To:.*$', f'To: {to}', note, count=1, flags=re.M))

def answer(given):
    if not QUESTIONS.exists(): print('No questions are waiting for you.'); return 0
    saved = json.loads(QUESTIONS.read_text()); questions = saved['questions']
    if given:
        if len(given) != len(questions): raise SystemExit(f'There are {len(questions)} questions: give one --answer for each, in order ("suggested" or "skip" are fine)')
        save_answers(questions, given)
    else:
        if not sys.stdin.isatty():
            for n, q in enumerate(questions, 1): show_question(n, q)
            raise SystemExit('Answer in a terminal, or pass one --answer "..." per question, in order')
        print(f'{saved["asked_by"].capitalize()} asked:\n')
        answers = []
        for n, q in enumerate(questions, 1):
            show_question(n, q)
            answers.append(input(f'   Your answer (Enter = {"go with the suggestion" if q["suggest"] else "let the assistants decide"}): ')); print()
        save_answers(questions, answers)
    resume = saved.get('resume')
    if not resume: print('Saved as your decisions.'); return 0
    carry_on(resume['to'])
    print('Saved as your decisions. Carrying on…', flush=True)
    return main(resume['args'])

def pause(questions, asked_by, resume, pauses):
    """Returns None to carry on, or the exit code to stop with."""
    if pauses > MAX_PAUSES: return stop(f'{MAX_PAUSES} question stops in one run; read {rel(BATON)} and run again', ok=False)
    if ask_user(questions, asked_by, resume): return None
    return stop('waiting for your answers: run python3 relay.py answer')

# ---- mode 1: backup --------------------------------------------------------------------------------------------
def run_backup(task, start, dry_run):
    task, agent = pick_up(task, start)
    if dry_run:
        print(f'--- {agent} starts; {other(agent)} takes over only if {agent} runs out of usage\n{backup_prompt(agent, task, False)}\n')
        print('Dry run: no model calls, nothing claimed or logged.'); return 0
    branch, picking_up, ran_out, pauses = git('branch', '--show-current'), False, {}, 0
    log(f'relay started (backup): {agent} first, branch {branch}, task: {task}')
    while True:
        if busy(): return stop('workspace already claimed; run status and stop the other writer first', ok=False)
        before = BATON.read_text() if BATON.exists() else ''
        with tempfile.TemporaryDirectory() as tmp:
            print(f'{agent.capitalize()} working on the task… ', end='', flush=True)
            code, transcript = session(agent, command_for(agent, backup_prompt(agent, task, picking_up), str(Path(tmp)/'last.txt')), 'backup', TASK_TIMEOUT)
        if code is None: print('stopped'); return stop(f'{agent} ran past {TASK_TIMEOUT//3600} hours; check git status', ok=False)
        if git('branch', '--show-current') != branch: print('stopped'); return stop(f'{agent} changed branch', ok=False)
        limit = out_of_usage(transcript) if code else out_of_usage(transcript, lines=5)
        if limit:
            print('ran out of usage', flush=True)
            ran_out[agent] = limit
            log(f'{agent} ran out of usage: {limit}')
            finished = hand_over(task, agent, before, limit)
            if finished: return stop('task done' if finished == 'done' else f"the user needs to decide; see What's next in {rel(BATON)}")
            if other(agent) in ran_out:
                return stop('both assistants are out of usage. ' + ' / '.join(f'{a.capitalize()}: {m.rstrip(".")}' for a, m in ran_out.items())
                            + '. Run again once either resets; it picks up from the baton.', ok=False)
            print(f'Handing the task to {other(agent).capitalize()}.', flush=True)
            agent, picking_up = other(agent), True
            continue
        if code: print('failed'); return stop(f'{agent} stopped with an error (not a usage limit); see {rel(transcript)}', ok=False)
        print('finished', flush=True)
        baton = read_baton()
        if not baton or baton['text'] == before: return stop(f'{agent} finished without updating the baton; see {rel(transcript)}', ok=False)
        log(f'{agent} → {baton["status"]} (HEAD {git("rev-parse", "--short", "HEAD")})')
        questions = questions_in(baton, agent)
        if questions:
            pauses += 1
            done = baton['status'] == 'done'
            stopped = pause(questions, agent, None if done else {'args': ['run', '--start', agent], 'to': agent}, pauses)
            if stopped is not None: return stopped
            if not done: carry_on(agent); picking_up = False; continue
        if baton['status'] == 'done': return stop('task done')
        return stop(f'{agent} ended its session before finishing; run again to continue from the baton')

def hand_over(task, agent, before, limit):
    """Point the baton at the other assistant and record what the relay can see. Returns done/ask-user if already final."""
    baton = read_baton()
    note = (f'\n\n## Relay note\n{agent.capitalize()} ran out of usage at {now()} ({limit}). Uncommitted changes at that moment:\n'
            f"```\n{git('status', '--short') or 'none'}\n```\nCheck them with git diff before continuing.\n")
    if baton and baton['text'] != before and baton['task'] == task and baton['status'] in ('done', 'ask-user'):
        BATON.write_text(baton['text'].rstrip('\n') + note); return baton['status']
    if baton and baton['task'] == task:  # whether or not it was updated this session, keep what it already says
        add_to_baton(other(agent), 'continue', 'Relay note', note.split('## Relay note\n', 1)[1])
    else:
        write_baton(task, agent, other(agent), 'continue', f'{agent.capitalize()} ran out of usage before writing a hand-off.',
                    'Read the uncommitted changes and the progress notes, then carry on with the task.', note.strip())
    return None

# ---- mode 2: make and check ------------------------------------------------------------------------------------
def notes_files():
    """The maker's own write-ups: the progress file and anything listed in blind_hide."""
    return [n for n in [CONFIG['checkpoint'], *CONFIG['blind_hide']] if n]

def hide_note_changes(stash):
    """Set aside unsaved changes to the notes files (the work itself stays visible). Returns what to put back."""
    put_back = []
    for name in notes_files():
        path = ROOT/name
        if not path.exists() or not git('status', '--porcelain', '--', name): continue
        copy = stash/name.replace('/', '__')
        shutil.copy2(path, copy)
        tracked = subprocess.run(['git', 'cat-file', '-e', f'HEAD:{name}'], cwd=ROOT, capture_output=True).returncode == 0
        if tracked: path.write_text(git('show', f'HEAD:{name}') + '\n')
        else: path.unlink()
        put_back.append((copy, path))
    return put_back

def blind_session(checker, task, rnd):
    """Review the work with the maker's note and its unsaved notes-file changes set aside, so they can't steer the first look."""
    stash = Path(tempfile.mkdtemp())
    hidden = stash/'baton.md'
    BATON.replace(hidden)
    put_back = hide_note_changes(stash)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            last = Path(tmp)/'last.txt'
            code, transcript = session(checker, command_for(checker, blind_prompt(checker, task, rnd), str(last), read_only=True), f'round-{rnd}-blind', TURN_TIMEOUT)
            text = (last.read_text() if last.exists() and last.read_text().strip() else transcript.read_text(errors='replace')).strip()
    finally:
        hidden.replace(BATON)
        for copy, path in put_back: shutil.copy2(copy, path)
    return code, transcript, text

def run_check(task, rounds, budget, maker, dry_run, blind=True, first=1):
    if not 1 <= rounds <= MAX_ROUNDS: raise SystemExit(f'--check must be between 1 and {MAX_ROUNDS}')
    if budget < 0: raise SystemExit('--calls cannot be negative')
    if budget and not CONFIG['live_command']: raise SystemExit('--calls needs "live_command" in relay.json (the only command allowed to make live calls)')
    if budget and maker == 'codex': raise SystemExit('--calls needs Claude as the maker: Codex\'s sandbox usually cannot reach a model CLI')
    task, _ = pick_up(task, maker)
    checker, review, used, pauses, waiting = other(maker), '', 0, 0, []
    final = first + rounds - 1
    if dry_run:
        print(f'--- round {first} maker: {maker}\n{maker_prompt(maker, task, first, final, budget, "")}\n')
        if blind: print(f'--- round 1 blind review: {checker} (read-only, report hidden)\n{blind_prompt(checker, task, 1)}\n')
        print(f'--- round 1 checker: {checker} (read-only)\n{checker_prompt(checker, task, 1, "<the blind findings>" if blind else None)}\n')
        print('Dry run: no model calls, nothing claimed or logged.'); return 0
    branch = git('branch', '--show-current')
    log(f'relay started (make and check): {maker} makes, {checker} checks, up to {rounds} rounds, {budget} live calls, branch {branch}, task: {task}')
    ledger = create_budget(budget)
    resume = lambda rnd: {'args': ['run', '--check', str(max(1, final - rnd + 1)), '--calls', str(budget - used), '--maker', maker,
                                   *([] if blind else ['--no-blind']), '--from-round', str(rnd)], 'to': maker}
    for rnd in range(first, final + 1):
        while True:  # the maker's turn, again after any questions it asked
            if busy(): return stop(f'round {rnd}: workspace already claimed', ok=False)
            before = BATON.read_text() if BATON.exists() else ''
            with tempfile.TemporaryDirectory() as tmp:
                print(f'Round {rnd}: {maker.capitalize()} working… ', end='', flush=True)
                command = command_for(maker, maker_prompt(maker, task, rnd, final, budget - used, review), str(Path(tmp)/'last.txt'), live_calls=budget - used > 0)
                code, transcript = session(maker, command, f'round-{rnd}-make', TASK_TIMEOUT, budget=ledger)
            if code is None: print('stopped'); return stop(f'round {rnd}: {maker} ran past {TASK_TIMEOUT//3600} hours', ok=False)
            if code: print('failed'); return stop(f'round {rnd}: {maker} stopped with an error ({out_of_usage(transcript) or "see " + rel(transcript)})', ok=False)
            print('done', flush=True)
            baton = read_baton()
            if not baton or baton['text'] == before: return stop(f'round {rnd}: {maker} finished without updating the baton', ok=False)
            if git('branch', '--show-current') != branch: return stop(f'round {rnd}: {maker} changed branch', ok=False)
            reported = re.search(r'^Calls used:\s*(\d+)\b', baton['text'], re.M)
            if not reported: return stop(f'round {rnd}: {maker} did not report Calls used', ok=False)
            measured = budget_used(ledger)
            if int(reported[1]) != measured - used: return stop(f'round {rnd}: reported calls ({reported[1]}) disagree with the ledger ({measured - used})', ok=False)
            used = measured
            log(f'round {rnd}: {maker} made (calls {reported[1]}, total {used} of {budget}; HEAD {git("rev-parse", "--short", "HEAD")})')
            questions = questions_in(baton, maker)
            if baton['status'] != 'ask-user':  # not blocking: ask them together with the reviewer's, after the review
                waiting = questions; break
            pauses += 1
            stopped = pause(questions, maker, resume(rnd), pauses)
            if stopped is not None: return stopped
            carry_on(maker)
        if CONFIG['look']: look(quiet=True)
        blind_findings = None
        if blind:
            print(f'Round {rnd}: {checker.capitalize()} reviewing blind… ', end='', flush=True)
            code, transcript, blind_findings = blind_session(checker, task, rnd)
            if code is None: print('stopped'); return stop(f'round {rnd}: the blind review ran past {TURN_TIMEOUT//60} minutes', ok=False)
            if code: print('failed'); return stop(f'round {rnd}: the blind review stopped with an error ({out_of_usage(transcript) or "see " + rel(transcript)})', ok=False)
            print('done', flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            last = Path(tmp)/'last.txt'
            print(f'Round {rnd}: {checker.capitalize()} comparing with the report… ' if blind else f'Round {rnd}: {checker.capitalize()} checking… ', end='', flush=True)
            code, transcript = session(checker, command_for(checker, checker_prompt(checker, task, rnd, blind_findings), str(last), read_only=True), f'round-{rnd}-check', TURN_TIMEOUT)
            text = (last.read_text() if last.exists() and last.read_text().strip() else transcript.read_text(errors='replace')).strip()
        if code is None: print('stopped'); return stop(f'round {rnd}: the review ran past {TURN_TIMEOUT//60} minutes', ok=False)
        if code: print('failed'); return stop(f'round {rnd}: the review stopped with an error ({out_of_usage(transcript) or "see " + rel(transcript)})', ok=False)
        verdict = parse_verdict(text)
        print(verdict or 'no verdict', flush=True)
        REVIEWS.mkdir(parents=True, exist_ok=True)
        saved = REVIEWS/f'{unique()}-round-{rnd}-{checker}.md'
        blind_part = f'## Blind review (before reading the report)\n\n{blind_findings}\n\n## Review after comparing\n\n' if blind_findings else ''
        saved.write_text(f'# Round {rnd} review by {checker.capitalize()}\n\nTask: {task}\n\n{blind_part}{text}\n')
        status = {'approve': 'done', 'revise': 'continue'}.get(verdict, 'ask-user')
        updated = re.sub(r'^Status:.*$', f'Status: {status}', BATON.read_text(), count=1, flags=re.M)
        updated = re.sub(r'^To:.*$', f'To: {maker}', updated, count=1, flags=re.M)
        BATON.write_text(updated.rstrip('\n') + f'\n\n## Independent review (round {rnd}, {checker})\n{text}\n')
        log(f'round {rnd}: {checker} → {verdict or "no verdict"} ({rel(saved)})')
        save_review(rnd, checker, verdict, saved)
        review = text
        if not verdict: return stop(f'round {rnd}: the review ended without a verdict; read {rel(saved)}', ok=False)
        questions = [{**q, 'from': checker} for q in parse_questions(text)]
        if verdict == 'ask-user' and not questions:
            questions = [{'question': f'The reviewer needs a decision from you; read {rel(saved)}.', 'suggest': '', 'from': checker}]
        seen = {q['question'] for q in questions}
        questions = [q for q in waiting if q['question'] not in seen] + questions  # one stop for both assistants' questions
        waiting = []
        if questions:
            pauses += 1
            stopped = pause(questions, checker, None if verdict == 'approve' else resume(rnd + 1), pauses)
            if stopped is not None: return stopped
            if verdict != 'approve': carry_on(maker)
            review += '\n\nThe user has answered the questions above: see their decisions in the baton.'
        if verdict == 'approve': return stop(f'approved by {checker} in round {rnd}; {used} of {budget} live calls used')
    return stop(f'round limit reached ({final}) without approval; {used} of {budget} live calls used')

def save_review(rnd, checker, verdict, saved):
    """Commit the review and the baton, and nothing else, so the last review is never left unsaved."""
    paths = [rel(BATON), rel(saved)]
    try:
        git('add', '-f', '--', *paths)
        git('commit', '-q', '-m', f'Relay: round {rnd} review by {checker.capitalize()} ({verdict or "no verdict"})', '--', *paths)
    except subprocess.CalledProcessError as error:
        log(f'round {rnd}: could not commit the review ({error}); it is saved at {paths[1]}')

# ---- mode 3: take turns ----------------------------------------------------------------------------------------
def run_turns(task, turns, start, dry_run, first=1):
    if not 1 <= turns <= MAX_TURNS: raise SystemExit(f'--take-turns must be between 1 and {MAX_TURNS}')
    task, agent = pick_up(task, start)
    if dry_run:
        for turn in range(first, first + turns): print(f'--- turn {turn}: {agent}\n{turn_prompt(agent, task, turn, first + turns - 1)}\n'); agent = other(agent)
        print('Dry run: no model calls, nothing claimed or logged.'); return 0
    branch = git('branch', '--show-current')
    log(f'relay started (take turns): up to {turns} turns, {agent} first, branch {branch}, task: {task}')
    pauses, last = 0, first + turns - 1
    for turn in range(first, last + 1):
        if busy(): return stop(f'turn {turn}: workspace already claimed', ok=False)
        before = BATON.read_text() if BATON.exists() else ''
        with tempfile.TemporaryDirectory() as tmp:
            print(f'Turn {turn}: {agent.capitalize()} working… ', end='', flush=True)
            code, transcript = session(agent, command_for(agent, turn_prompt(agent, task, turn, last), str(Path(tmp)/'last.txt')), f'turn-{turn}', TURN_TIMEOUT)
        if code is None: print('stopped'); return stop(f'turn {turn}: {agent} ran past {TURN_TIMEOUT//60} minutes', ok=False)
        print('done' if not code else 'failed', flush=True)
        if code: return stop(f'turn {turn}: {agent} exited with code {code}; see {rel(transcript)}', ok=False)
        baton = read_baton()
        if not baton or baton['text'] == before: return stop(f'turn {turn}: {agent} finished without writing a baton', ok=False)
        if baton['status'] not in STATUSES: return stop(f'turn {turn}: baton status "{baton["status"]}" not understood', ok=False)
        if git('branch', '--show-current') != branch: return stop(f'turn {turn}: {agent} changed branch', ok=False)
        log(f'turn {turn}: {agent} → {baton["status"]} (HEAD {git("rev-parse", "--short", "HEAD")})')
        questions = questions_in(baton, agent)
        if questions:
            pauses += 1
            done = baton['status'] == 'done'
            resume = {'args': ['run', '--take-turns', str(max(1, last - turn)), '--start', agent, '--from-round', str(turn + 1)], 'to': agent}
            stopped = pause(questions, agent, None if done else resume, pauses)
            if stopped is not None: return stopped
            if not done: carry_on(agent); continue  # whoever asked carries on with the answers
        if baton['status'] == 'done': return stop('task done')
        agent = other(agent)
    return stop(f'turn limit reached ({last}); run again to continue from the baton')

# ---- setting up a project -------------------------------------------------------------------------------------
AGENTS_TEMPLATE = '''# Notes for AI assistants

Codex reads this file automatically; Claude reads it through CLAUDE.md. Keep it short and current.
Both assistants start every session with no memory of earlier ones: what is written here is what they know.

## How to check work
<the exact test and build commands, run from the repository root>

## Rules
- <things that must never change, e.g. "don't edit the grading rubric">
- <how you want changes made, e.g. "small commits, plain-English messages">

## Where things stand
See {checkpoint} for current progress, and .relay/baton.md for the task in hand.
'''

CLAUDE_TEMPLATE = '''Read AGENTS.md first: it holds this project's notes for AI assistants, shared with Codex.

If you run relay.py for the user and it stops with questions for them, read .relay/questions.json and put each
question to the user with your question tool: the suggested answer first, marked as recommended, with its reason.
Then pass their answers, in order, with: python3 relay.py answer --answer "..." --answer "..." ("suggested" or "skip" work too).
'''
SETUP = [('What is this project, and who is it for?', 'What it is and who it is for'),
         ('How do you check it works? (a command, or "I look at it on my phone")', 'How the owner checks work'),
         ('What must never change or break?', 'Must never change or break'),
         ('What does "good" look like to you? Words, examples you like, things you dislike.', 'What good looks like'),
         ('Which decisions must always come to you? (wording, colours, anything public or that costs money)', 'Decisions that always go to the owner'),
         ('Anything the AIs should never do?', 'Never do')]
ABOUT = ('<!-- relay:about (your answers to relay.py init; run it again to change them) -->', '<!-- /relay:about -->')

def about_you(given):
    """Ask the six setup questions (or take them from --answer) and keep the answers at the top of AGENTS.md."""
    if given: answers = given + [''] * (len(SETUP) - len(given))
    elif sys.stdin.isatty():
        print('\nA few quick questions, so both assistants know what you want. Press Enter to skip any.\n')
        try: answers = [input(f'{n}. {q}\n   ') for n, (q, _) in enumerate(SETUP, 1)]
        except EOFError: return
    else:
        print('tip     run relay.py init in a terminal to answer 6 quick questions about the project'); return
    lines = [f'- {label}: {a.strip() or "not said yet (ask the owner if it matters)"}' for (_, label), a in zip(SETUP, answers)]
    block = f'{ABOUT[0]}\n## About this project and its owner\n' + '\n'.join(lines) + \
            "\nThese are the owner's own answers: treat them as [user] decisions.\n" + ABOUT[1]
    path = ROOT/'AGENTS.md'
    text = path.read_text()
    if ABOUT[0] in text: text = re.sub(re.escape(ABOUT[0]) + '.*?' + re.escape(ABOUT[1]), lambda _: block, text, flags=re.S)
    elif text.startswith('# '): title, _, rest = text.partition('\n'); text = f'{title}\n\n{block}\n{rest}'
    else: text = f'{block}\n\n{text}'
    path.write_text(text)
    print('\nsaved   your answers at the top of AGENTS.md (run relay.py init again to change them)')

def init(given=()):
    """Create the files that carry context between assistants, then ask about the project. Never overwrites a file."""
    made, kept = [], []
    def create(name, text):
        path = ROOT/name
        if path.exists(): kept.append(name); return
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text); made.append(name)
    create('AGENTS.md', AGENTS_TEMPLATE.format(checkpoint='docs/progress.md'))
    create('CLAUDE.md', CLAUDE_TEMPLATE)
    create('docs/progress.md', f'# Progress\n\n{datetime.date.today()}: set up the Codex ⇄ Claude relay.\n')
    create('relay.json', json.dumps({**DEFAULTS, 'notes': ['AGENTS.md'], 'checkpoint': 'docs/progress.md'}, indent=2) + '\n')
    ignore = ROOT/'.gitignore'
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    if '.relay/transcripts/' in lines: kept.append('.gitignore entry')
    else: ignore.write_text('\n'.join([*lines, '.relay/transcripts/']) + '\n'); made.append('.gitignore entry for .relay/transcripts/')
    claude = (ROOT/'CLAUDE.md').read_text()
    for name in made: print('created', name)
    for name in kept: print('kept   ', name, '(already there)')
    if 'AGENTS.md' not in claude: print('tip     add a line to CLAUDE.md telling Claude to read AGENTS.md, so both assistants share one set of notes')
    about_you(list(given))
    print('Next: add your test commands to AGENTS.md if you have any, then try: relay.py run --task "..." --dry-run')
    return 0

# ---- command line ----------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description='Codex <-> Claude relay'); sub = ap.add_subparsers(dest='action', required=True)
    sub.add_parser('status')
    i = sub.add_parser('init'); i.add_argument('--answer', action='append', default=[], help='answers to the setup questions, in order')
    q = sub.add_parser('answer'); q.add_argument('--answer', action='append', default=[], metavar='TEXT',
                                                 help='one per waiting question, in order; "suggested" or "skip" work too')
    p = sub.add_parser('pass'); p.add_argument('--to', choices=AGENTS, required=True); p.add_argument('--note', required=True)
    p.add_argument('--next', default='Read the note above and continue the task.'); p.add_argument('--task')
    p.add_argument('--status', choices=STATUSES, default='continue')
    p.add_argument('--decision', action='append', default=[], metavar='TEXT', help='record a final decision of yours, e.g. --decision "Keep the short wording"')
    r = sub.add_parser('run'); r.add_argument('--task'); r.add_argument('--start', choices=AGENTS, default='codex')
    r.add_argument('--take-turns', type=int, metavar='N'); r.add_argument('--check', type=int, metavar='ROUNDS')
    r.add_argument('--calls', type=int, default=0, metavar='N'); r.add_argument('--maker', choices=AGENTS, default='claude')
    r.add_argument('--no-blind', action='store_true', help='make and check: skip the blind first look (cheaper, more anchoring)')
    r.add_argument('--dry-run', action='store_true')
    r.add_argument('--from-round', type=int, default=1, help=argparse.SUPPRESS)  # set when a run carries on after your answers
    sub.add_parser('look')
    a = ap.parse_args(argv)
    if a.action == 'init': return init(a.answer)
    if a.action == 'answer': return answer(a.answer)
    if a.action == 'look': return 0 if look() or not CONFIG['look'] else 1
    if a.action == 'status':
        owner = lock_path()/'owner.json'
        print('Writer:', owner.read_text().strip() if owner.exists() else ('incomplete lock, inspect it' if busy() else 'nobody'))
        baton = read_baton(); print(baton['text'] if baton else 'No baton yet.'); return 0
    if a.action == 'pass':
        baton = read_baton(); task = a.task or (baton or {}).get('task')
        if not task: raise SystemExit('The first hand-off needs --task')
        if baton and baton['task'] == task: add_to_baton(a.to, a.status, f'Note added by hand ({now()})', f'{a.note}\n\nNext: {a.next}')
        else: write_baton(task, other(a.to), a.to, a.status, a.note, a.next)
        for d in a.decision: record_decision(d)
        log(f'hand-off by hand: → {a.to} ({a.status})')
        print(f'Baton passed to {a.to}.'); return 0
    try:
        if a.check is not None: return run_check(a.task, a.check, a.calls, a.maker, a.dry_run, blind=not a.no_blind, first=a.from_round)
        if a.take_turns is not None: return run_turns(a.task, a.take_turns, a.start, a.dry_run, first=a.from_round)
        return run_backup(a.task, a.start, a.dry_run)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        return stop(f'could not start an assistant: {error}', ok=False)

if __name__ == '__main__': sys.exit(main())
