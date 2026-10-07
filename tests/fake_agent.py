#!/usr/bin/env python3
"""Stand-in for the codex and claude CLIs, so the real relay can be tested end to end with no model calls.

Linked as `codex` and `claude` on PATH. $FAKE_PLAN is a comma list of behaviours, one per session, consumed in
order (the call count lives in $FAKE_STATE): work-then-out, out, finish, ask, continue, error, make<N>
(reserve N ledger calls through budget/relay_budget.py), verdict-<approve|revise|ask-user>.
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
task = re.search(r'^Task(?: for the whole relay)?: (.+)$', prompt, re.M)[1]
other = 'claude' if me == 'codex' else 'codex'
def baton(status, did, extra=''):
    Path('.relay').mkdir(exist_ok=True)
    Path('.relay/baton.md').write_text(f'# Relay baton\n\nStatus: {status}\nFrom: {me}\nTo: {other}\nTask: {task}\n{extra}'
                                       f"Updated: now\n\n## What I did\n{did}\n\n## What's next\nmore\n\n## Watch out for\nnothing\n")
print('reading notes: usage limit policy mentioned in a file the assistant read')
print('\n'.join(f'working on step {i}' for i in range(12)))
if step == 'work-then-out': Path('notes.txt').write_text('half done\n'); print("ERROR: You've hit your usage limit. Try again at 9:00 PM."); sys.exit(1)
if step == 'out': print('Claude AI usage limit reached. Your limit will reset at 10pm.'); sys.exit(0)
if step == 'error': print('ERROR: something else broke'); sys.exit(1)
if step == 'finish': Path('notes.txt').write_text('finished\n'); baton('done', 'finished the task'); sys.exit(0)
if step == 'ask': baton('ask-user', 'needs a decision'); sys.exit(0)
if step == 'continue': baton('continue', 'one step'); sys.exit(0)
if step.startswith('make'):
    sys.path.insert(0, os.environ['RELAY_REPO'] + '/budget'); from relay_budget import reserve_relay_call
    used = 0
    for _ in range(int(step[4:])):
        try: reserve_relay_call(); used += 1
        except RuntimeError: break
    baton('continue', f'made a change with {used} calls', f'Calls used: {used}\n'); sys.exit(0)
if step.startswith('verdict-'):
    text = f'1. A finding with evidence.\nVerdict: {step[8:]}'
    if '-o' in args: Path(args[args.index('-o') + 1]).write_text(text)
    else: print(text)
    sys.exit(0)
sys.exit(3)
