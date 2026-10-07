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
  relay.py init                                 create AGENTS.md, CLAUDE.md, relay.json and a progress file
  relay.py status                               who holds the lock, and the current baton
  relay.py pass --to codex --task "..." --note "..."   hand off by hand at the end of an interactive chat

Shared state lives in .relay/ in the repository: baton.md (the hand-off note), log.md (one line per event),
reviews/ (make-and-check reviews) and transcripts/ (everything each assistant printed; add it to .gitignore).
Optional settings come from relay.json at the repository root; see README.md.

Every session claims a lock in the Git directory so only one assistant edits at a time, and gets a call
ledger (RELAY_CALL_BUDGET) that your test runner can check before each live model call. Outside make and check
the ledger allows no calls. Nothing retries, loops forever or runs on a schedule. Subscription sign-ins only:
Claude runs with ANTHROPIC_* and provider overrides removed, Codex with OPENAI_API_KEY removed and a ChatGPT
login required.
"""
import argparse, datetime, json, os, re, subprocess, sys, tempfile, uuid
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
AGENTS = ('codex', 'claude')
STATUSES = ('continue', 'done', 'ask-user')
MAX_TURNS, MAX_ROUNDS = 8, 5
TURN_TIMEOUT = 45*60      # one step in take-turns mode, and one review
TASK_TIMEOUT = 3*60*60    # one whole-task session
# How the CLIs report a spent subscription allowance. Checked in the last 10 lines of a session that failed,
# and the last 5 lines of one that exited cleanly, so a file the assistant read earlier cannot trigger it.
OUT_OF_USAGE = re.compile(r'usage limit|hit your (usage )?limit|limit reached|limit will reset|quota exceeded|out of (usage|credits)', re.I)
DEFAULTS = {'notes': ['AGENTS.md', 'CLAUDE.md', 'README.md'], 'checkpoint': None, 'tests': [], 'live_command': None,
            'review_criteria': [], 'claude_model': 'sonnet', 'codex_model': None}

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
    return (ROOT/git('rev-parse', '--git-common-dir')).resolve()/'relay-writer-lock'

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
{tests} Commit your work in small scoped commits (if Git is read-only in your sandbox, leave changes
uncommitted and say so under Watch out for).{checkpoint}"""

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

Keep everything already under Decisions and why and Tried, didn't work: copy it forward and add to it, never
drop it. That is how the next assistant knows what was chosen on purpose and what not to try again.
Start each decision with who made it: [user] or [codex]/[claude]. [user] decisions are final. An assistant's
decision may be challenged, but only with evidence: say what you found, change it, and keep the old line
marked "(replaced: ...)". Mark every claim about the work as (checked: how you checked it) or (assumed). Before
relying on anything (assumed) from an earlier note, check it yourself; earlier notes are leads, not facts.
Use Status: done only when the whole task is finished and checked. Use Status: ask-user when the next step is a
decision that belongs to the user, and write the question under What's next. Otherwise use continue."""

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
first, so you are not steered by how it framed things. Do not open anything in .relay/ and do not read its
commit messages for its explanations; look only at the work itself: git status, git diff, git log for which files
changed, and the files. You are read-only: do not edit files or make model calls.

Judge the work against the task and the project's own criteria ({criteria}). List what is right, what is wrong or
missing, and anything you are unsure about, each with evidence (file and line). Do not give a verdict."""

def checker_prompt(agent, task, rnd, blind_findings=None):
    criteria = ', '.join(CONFIG['review_criteria'] or CONFIG['notes']) or 'the README'
    return f"""You are {agent.capitalize()}, the independent reviewer in round {rnd} of a make-and-check relay on this repository.
Task: {task}

You are read-only: do not edit files or make model calls. Read {rel(BATON)} (the maker's report), then check the
actual work: git log, git diff, the files and any saved results it names. Judge it against the task and the
project's own criteria ({criteria}). Verify claims yourself rather than trusting the report; quote evidence.
{compare(blind_findings)}
Your final message is saved as the review. Write the findings first (most important first, each with evidence and
the correction needed). End with exactly one of these as the last line, and nothing after it:
"Verdict: approve", "Verdict: revise" or "Verdict: ask-user".
Use approve only when the task is met and checked. Use ask-user when the remaining question is a judgement that
belongs to the user, and state the question."""

def compare(blind_findings):
    if not blind_findings: return ''
    return f"""
Before reading the report, you reviewed the work blind and wrote this:

{blind_findings}

Now compare. List separately: problems only you found, problems only the maker reported, and anything you
disagree on. Re-check each against the code before deciding; agreement between two assistants is not evidence.
Do not drop a blind finding just because the report doesn't mention it.
"""

# ---- running one assistant -------------------------------------------------------------------------------------
def command_for(agent, prompt, last_message, live_calls=False, read_only=False):
    if agent == 'claude':
        model = ['--model', CONFIG['claude_model']] if CONFIG['claude_model'] else []
        if read_only:
            tools = ['Read', 'Glob', 'Grep', 'Bash(git diff:*)', 'Bash(git log:*)', 'Bash(git show:*)', 'Bash(git status:*)']
            return ['claude', '-p', prompt, '--safe-mode', *model, '--permission-mode', 'default', '--allowedTools', *tools]
        tests = [f'Bash({t}:*)' for t in CONFIG['tests']]
        live = [f"Bash({CONFIG['live_command']}:*)"] if live_calls and CONFIG['live_command'] else []
        tools = ['Read', 'Edit', 'Write', 'Glob', 'Grep', *tests, *live, 'Bash(git status:*)', 'Bash(git diff:*)',
                 'Bash(git log:*)', 'Bash(git add:*)', 'Bash(git commit:*)']
        return ['claude', '-p', prompt, '--safe-mode', *model, '--permission-mode', 'acceptEdits', '--allowedTools', *tools]
    model = ['-m', CONFIG['codex_model']] if CONFIG['codex_model'] else []
    return ['codex', 'exec', '-C', str(ROOT), *model, '-s', 'read-only' if read_only else 'workspace-write', '-o', last_message, prompt]

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
            code = subprocess.run(command, cwd=ROOT, env=env, timeout=timeout, stdout=out, stderr=subprocess.STDOUT).returncode
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

# ---- mode 1: backup --------------------------------------------------------------------------------------------
def run_backup(task, start, dry_run):
    task, agent = pick_up(task, start)
    if dry_run:
        print(f'--- {agent} starts; {other(agent)} takes over only if {agent} runs out of usage\n{backup_prompt(agent, task, False)}\n')
        print('Dry run: no model calls, nothing claimed or logged.'); return 0
    branch, picking_up, ran_out = git('branch', '--show-current'), False, {}
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
        if baton['status'] == 'done': return stop('task done')
        if baton['status'] == 'ask-user': return stop(f"the user needs to decide; see What's next in {rel(BATON)}")
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
def blind_session(checker, task, rnd):
    """Review the work with the maker's note moved out of the repository, so it can't steer the first look."""
    hidden = Path(tempfile.mkdtemp())/'baton.md'
    BATON.replace(hidden)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            last = Path(tmp)/'last.txt'
            code, transcript = session(checker, command_for(checker, blind_prompt(checker, task, rnd), str(last), read_only=True), f'round-{rnd}-blind', TURN_TIMEOUT)
            text = (last.read_text() if last.exists() and last.read_text().strip() else transcript.read_text(errors='replace')).strip()
    finally:
        hidden.replace(BATON)
    return code, transcript, text

def run_check(task, rounds, budget, maker, dry_run, blind=True):
    if not 1 <= rounds <= MAX_ROUNDS: raise SystemExit(f'--check must be between 1 and {MAX_ROUNDS}')
    if budget < 0: raise SystemExit('--calls cannot be negative')
    if budget and not CONFIG['live_command']: raise SystemExit('--calls needs "live_command" in relay.json (the only command allowed to make live calls)')
    if budget and maker == 'codex': raise SystemExit('--calls needs Claude as the maker: Codex\'s sandbox usually cannot reach a model CLI')
    task, _ = pick_up(task, maker)
    checker, review, used = other(maker), '', 0
    if dry_run:
        print(f'--- round 1 maker: {maker}\n{maker_prompt(maker, task, 1, rounds, budget, "")}\n')
        if blind: print(f'--- round 1 blind review: {checker} (read-only, report hidden)\n{blind_prompt(checker, task, 1)}\n')
        print(f'--- round 1 checker: {checker} (read-only)\n{checker_prompt(checker, task, 1, "<the blind findings>" if blind else None)}\n')
        print('Dry run: no model calls, nothing claimed or logged.'); return 0
    branch = git('branch', '--show-current')
    log(f'relay started (make and check): {maker} makes, {checker} checks, up to {rounds} rounds, {budget} live calls, branch {branch}, task: {task}')
    ledger = create_budget(budget)
    for rnd in range(1, rounds + 1):
        if busy(): return stop(f'round {rnd}: workspace already claimed', ok=False)
        before = BATON.read_text() if BATON.exists() else ''
        with tempfile.TemporaryDirectory() as tmp:
            print(f'Round {rnd}: {maker.capitalize()} working… ', end='', flush=True)
            command = command_for(maker, maker_prompt(maker, task, rnd, rounds, budget - used, review), str(Path(tmp)/'last.txt'), live_calls=budget - used > 0)
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
        if baton['status'] == 'ask-user': return stop(f"the user needs to decide; see What's next in {rel(BATON)}")
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
        if verdict == 'approve': return stop(f'approved by {checker} in round {rnd}; {used} of {budget} live calls used')
        if verdict == 'ask-user': return stop(f'the reviewer needs the user to decide; read {rel(saved)}')
    return stop(f'round limit reached ({rounds}) without approval; {used} of {budget} live calls used')

def save_review(rnd, checker, verdict, saved):
    """Commit the review and the baton, and nothing else, so the last review is never left unsaved."""
    paths = [rel(BATON), rel(saved)]
    try:
        git('add', '-f', '--', *paths)
        git('commit', '-q', '-m', f'Relay: round {rnd} review by {checker.capitalize()} ({verdict or "no verdict"})', '--', *paths)
    except subprocess.CalledProcessError as error:
        log(f'round {rnd}: could not commit the review ({error}); it is saved at {paths[1]}')

# ---- mode 3: take turns ----------------------------------------------------------------------------------------
def run_turns(task, turns, start, dry_run):
    if not 1 <= turns <= MAX_TURNS: raise SystemExit(f'--take-turns must be between 1 and {MAX_TURNS}')
    task, agent = pick_up(task, start)
    if dry_run:
        for turn in range(1, turns + 1): print(f'--- turn {turn}: {agent}\n{turn_prompt(agent, task, turn, turns)}\n'); agent = other(agent)
        print('Dry run: no model calls, nothing claimed or logged.'); return 0
    branch = git('branch', '--show-current')
    log(f'relay started (take turns): up to {turns} turns, {agent} first, branch {branch}, task: {task}')
    for turn in range(1, turns + 1):
        if busy(): return stop(f'turn {turn}: workspace already claimed', ok=False)
        before = BATON.read_text() if BATON.exists() else ''
        with tempfile.TemporaryDirectory() as tmp:
            print(f'Turn {turn}: {agent.capitalize()} working… ', end='', flush=True)
            code, transcript = session(agent, command_for(agent, turn_prompt(agent, task, turn, turns), str(Path(tmp)/'last.txt')), f'turn-{turn}', TURN_TIMEOUT)
        if code is None: print('stopped'); return stop(f'turn {turn}: {agent} ran past {TURN_TIMEOUT//60} minutes', ok=False)
        print('done' if not code else 'failed', flush=True)
        if code: return stop(f'turn {turn}: {agent} exited with code {code}; see {rel(transcript)}', ok=False)
        baton = read_baton()
        if not baton or baton['text'] == before: return stop(f'turn {turn}: {agent} finished without writing a baton', ok=False)
        if baton['status'] not in STATUSES: return stop(f'turn {turn}: baton status "{baton["status"]}" not understood', ok=False)
        if git('branch', '--show-current') != branch: return stop(f'turn {turn}: {agent} changed branch', ok=False)
        log(f'turn {turn}: {agent} → {baton["status"]} (HEAD {git("rev-parse", "--short", "HEAD")})')
        if baton['status'] == 'done': return stop('task done')
        if baton['status'] == 'ask-user': return stop(f"the user needs to decide; see What's next in {rel(BATON)}")
        agent = other(agent)
    return stop(f'turn limit reached ({turns}); run again to continue from the baton')

# ---- setting up a project -------------------------------------------------------------------------------------
AGENTS_TEMPLATE = '''# Notes for AI assistants

Codex reads this file automatically; Claude reads it through CLAUDE.md. Keep it short and current.
Both assistants start every session with no memory of earlier ones: what is written here is what they know.

## What this project is
<one or two sentences>

## How to check work
<the exact test and build commands, run from the repository root>

## Rules
- <things that must never change, e.g. "don't edit the grading rubric">
- <how you want changes made, e.g. "small commits, plain-English messages">

## Preferences
- <style, wording, tools you prefer>

## Where things stand
See {checkpoint} for current progress, and .relay/baton.md for the task in hand.
'''

def init():
    """Create the files that carry context between assistants. Never overwrites anything."""
    made, kept = [], []
    def create(name, text):
        path = ROOT/name
        if path.exists(): kept.append(name); return
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text); made.append(name)
    create('AGENTS.md', AGENTS_TEMPLATE.format(checkpoint='docs/progress.md'))
    create('CLAUDE.md', 'Read AGENTS.md first: it holds this project\'s notes for AI assistants, shared with Codex.\n')
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
    print('Next: fill in AGENTS.md, then try: relay.py run --task "..." --dry-run')
    return 0

# ---- command line ----------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description='Codex <-> Claude relay'); sub = ap.add_subparsers(dest='action', required=True)
    sub.add_parser('status'); sub.add_parser('init')
    p = sub.add_parser('pass'); p.add_argument('--to', choices=AGENTS, required=True); p.add_argument('--note', required=True)
    p.add_argument('--next', default='Read the note above and continue the task.'); p.add_argument('--task')
    p.add_argument('--status', choices=STATUSES, default='continue')
    p.add_argument('--decision', action='append', default=[], metavar='TEXT', help='record a final decision of yours, e.g. --decision "Keep the short wording"')
    r = sub.add_parser('run'); r.add_argument('--task'); r.add_argument('--start', choices=AGENTS, default='codex')
    r.add_argument('--take-turns', type=int, metavar='N'); r.add_argument('--check', type=int, metavar='ROUNDS')
    r.add_argument('--calls', type=int, default=0, metavar='N'); r.add_argument('--maker', choices=AGENTS, default='claude')
    r.add_argument('--no-blind', action='store_true', help='make and check: skip the blind first look (cheaper, more anchoring)')
    r.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    if a.action == 'init': return init()
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
        if a.check is not None: return run_check(a.task, a.check, a.calls, a.maker, a.dry_run, blind=not a.no_blind)
        if a.take_turns is not None: return run_turns(a.task, a.take_turns, a.start, a.dry_run)
        return run_backup(a.task, a.start, a.dry_run)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        return stop(f'could not start an assistant: {error}', ok=False)

if __name__ == '__main__': sys.exit(main())
