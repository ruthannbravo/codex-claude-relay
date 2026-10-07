#!/usr/bin/env python3
"""Stand-in for the codex and claude CLIs, so the real relay can be tested end to end with no model calls.

Linked as `codex` and `claude` on PATH. $FAKE_PLAN is a comma list of behaviours, one per session, consumed in
order (the call count lives in $FAKE_STATE): work-then-out, out, finish, finish-after-input, ask, continue, error, make<N>
(reserve N ledger calls through budget/relay_budget.py), blind (a blind review that reports whether the maker's
note was visible), verdict-<approve|revise|ask-user>, ask-q (two questions
for the user, one with a suggestion), verdict-revise-q (a review with a question for the user).
"""
import json, os, re, sys
from pathlib import Path
me, args = Path(sys.argv[0]).name, sys.argv[1:]
if me == 'codex' and args[:2] == ['login', 'status']: print('Logged in using ChatGPT'); sys.exit(0)
if me == 'claude' and args[:2] == ['auth', 'status']: print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'})); sys.exit(0)
counter = Path(os.environ['FAKE_STATE']); n = int(counter.read_text()) if counter.exists() else 0; counter.write_text(str(n + 1))
step = os.environ['FAKE_PLAN'].split(',')[n]
prompt = next(a for a in args if len(a) > 200)
Path(os.environ['FAKE_STATE'] + f'.prompt{n}').write_text(prompt)
Path(os.environ['FAKE_STATE'] + f'.args{n}').write_text(' '.join(a for a in args if a != prompt))
task = re.search(r'^Task(?: for the whole relay)?: (.+)$', prompt, re.M)[1]
other = 'claude' if me == 'codex' else 'codex'
def baton(status, did, extra='', sections=''):
    Path('.relay').mkdir(exist_ok=True)
    old = Path('.relay/baton.md').read_text() if Path('.relay/baton.md').exists() else ''
    kept = re.search(r'^## Decisions and why\n.*?(?=^## |\Z)', old, re.M | re.S)  # copied forward, as real assistants are told to
    if kept and '## Decisions and why' not in sections: sections = kept[0].rstrip() + '\n\n' + sections
    Path('.relay/baton.md').write_text(f'# Relay baton\n\nStatus: {status}\nFrom: {me}\nTo: {other}\nTask: {task}\n{extra}'
                                       f"Updated: now\n\n## What I did\n{did}\n\n## What's next\nmore\n\n## Watch out for\nnothing\n\n{sections}")
print('reading notes: usage limit policy mentioned in a file the assistant read')
print('\n'.join(f'working on step {i}' for i in range(12)))
if step == 'work-then-out': Path('notes.txt').write_text('half done\n'); print("ERROR: You've hit your usage limit. Try again at 9:00 PM."); sys.exit(1)
if step == 'out': print('Claude AI usage limit reached. Your limit will reset at 10pm.'); sys.exit(0)
if step == 'error': print('ERROR: something else broke'); sys.exit(1)
if step == 'finish-after-input': sys.stdin.read(); step = 'finish'  # like codex exec, which reads stdin to the end first
if step == 'finish': Path('notes.txt').write_text('finished\n'); baton('done', 'finished the task'); sys.exit(0)
if step == 'ask': baton('ask-user', 'needs a decision'); sys.exit(0)
QUESTIONS = '## Questions for you\n1. Should the log-in link stay?\n2. Which colour for the button?\n   Suggest: blue, because it matches the logo\n'
if step == 'ask-q': baton('ask-user', 'needs two answers', sections=QUESTIONS); sys.exit(0)
if step == 'continue': baton('continue', 'one step'); sys.exit(0)
if step == 'make0-words': baton('continue', 'reviewed, no calls', 'Calls used: 0 live model calls\n'); sys.exit(0)
if step.startswith('make') and step[4:].isdigit():
    sys.path.insert(0, os.environ['RELAY_REPO'] + '/budget'); from relay_budget import reserve_relay_call
    used = 0
    for _ in range(int(step[4:])):
        try: reserve_relay_call(); used += 1
        except RuntimeError: break
    baton('continue', f'made a change with {used} calls', f'Calls used: {used}\n'); sys.exit(0)
if step == 'make0-notes':  # writes its findings into the progress file as well as the note
    Path('docs').mkdir(exist_ok=True)
    Path('docs/progress.md').write_text(Path('docs/progress.md').read_text() + 'MAKER FINDINGS: footer link broken\n')
    baton('continue', 'reviewed', 'Calls used: 0\n'); sys.exit(0)
if step == 'blind':
    seen = Path('.relay/baton.md').exists()
    notes = Path('docs/progress.md').exists() and 'MAKER FINDINGS' in Path('docs/progress.md').read_text()
    text = f'Blind finding: the footer link is broken (index.html:12). Report visible during blind look: {seen}. Maker notes visible: {notes}'
    if '-o' in args: Path(args[args.index('-o') + 1]).write_text(text)
    else: print(text)
    sys.exit(0)
if step == 'verdict-revise-q':
    text = '1. Summary: nearly there.\n\n**Questions for you**\n1. Is the phone number really required?\n   - Suggest: make it optional, because nothing uses it\n\nVerdict: revise'
    Path(args[args.index('-o') + 1]).write_text(text); sys.exit(0)
if step.startswith('verdict-'):
    text = f'1. A finding with evidence.\nVerdict: {step[8:]}'
    if '-o' in args: Path(args[args.index('-o') + 1]).write_text(text)
    else: print(text)
    sys.exit(0)
sys.exit(3)
