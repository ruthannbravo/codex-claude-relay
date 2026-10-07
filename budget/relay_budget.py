"""Call reserve_relay_call() before every live model call in your test runner.

Inside a relay session RELAY_CALL_BUDGET points at a shared ledger; each call reserves one slot with an atomic
mkdir, so parallel or repeated runs share one cap. Outside the relay (variable unset) it does nothing.
"""
import json, os
from pathlib import Path

def reserve_relay_call(directory=None):
    directory = os.environ.get('RELAY_CALL_BUDGET') if directory is None else directory
    if directory is None: return
    if not directory: raise RuntimeError('RELAY_CALL_BUDGET is set but empty')
    limit = json.loads((Path(directory)/'budget.json').read_text())['limit']
    if not isinstance(limit, int) or limit < 0: raise RuntimeError('Invalid relay call budget')
    for i in range(limit):
        try: (Path(directory)/f'call-{i}').mkdir(); return
        except FileExistsError: continue
    raise RuntimeError('Relay call budget used up; no call made')
