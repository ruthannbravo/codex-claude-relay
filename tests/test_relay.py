"""End-to-end tests: the real relay runs in a throwaway Git repository against stand-in codex and claude programs.
No model calls. Run: python3 -m unittest discover tests
"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

class Relay(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = self.tmp/'work'; self.work.mkdir()
        bin_dir = self.tmp/'bin'; bin_dir.mkdir()
        for name in ('codex', 'claude'): (bin_dir/name).symlink_to(REPO/'tests'/'fake_agent.py')
        git = lambda *a: subprocess.run(['git', *a], cwd=self.work, check=True, capture_output=True)
        git('init', '-q', '-b', 'main'); git('config', 'user.email', 'test@example.com'); git('config', 'user.name', 'Test')
        (self.work/'README.md').write_text('# Test project\n'); (self.work/'.gitignore').write_text('.relay/transcripts/\n')
        git('add', '.'); git('commit', '-qm', 'start')
        self.env = {**os.environ, 'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', 'FAKE_STATE': str(self.tmp/'state'), 'RELAY_REPO': str(REPO)}

    def relay(self, plan, *args):
        result = subprocess.run([sys.executable, str(REPO/'relay.py'), *args], cwd=self.work, text=True, capture_output=True,
                                env={**self.env, 'FAKE_PLAN': ','.join(plan)})
        self.output = result.stdout + result.stderr
        return result.returncode

    def sessions(self): return int((self.tmp/'state').read_text()) if (self.tmp/'state').exists() else 0
    def prompt(self, n): return (self.tmp/f'state.prompt{n}').read_text()
    def baton(self): return (self.work/'.relay/baton.md').read_text()
    def log(self): return (self.work/'.relay/log.md').read_text()
    def locked(self): return any(p.name == 'relay-writer-lock' for p in (self.work/'.git').iterdir())

    # Backup
    def test_one_assistant_finishes_alone(self):
        self.assertEqual(self.relay(['finish'], 'run', '--task', 'Tidy'), 0)
        self.assertEqual(self.sessions(), 1)
        self.assertIn('task done', self.output)
        self.assertFalse(self.locked())

    def test_when_codex_runs_out_claude_takes_over_with_the_relay_note(self):
        self.assertEqual(self.relay(['work-then-out', 'finish'], 'run', '--task', 'Tidy'), 0)
        self.assertIn('ran out of usage', self.prompt(1))
        self.assertIn("codex ran out of usage: ERROR: You've hit your usage limit", self.log())
        self.assertIn('Handing the task to Claude', self.output)

    def test_both_out_of_usage_stops_with_both_messages(self):
        self.assertEqual(self.relay(['work-then-out', 'out'], 'run', '--task', 'Tidy'), 1)
        self.assertIn('both assistants are out of usage', self.output)
        self.assertIn('10pm', self.output)
        self.assertIn('To: codex', self.baton())
        self.assertIn('## Relay note', self.baton())

    def test_other_errors_do_not_switch_assistants(self):
        self.assertEqual(self.relay(['error', 'finish'], 'run', '--task', 'Tidy'), 1)
        self.assertEqual(self.sessions(), 1)

    def test_limit_words_read_earlier_do_not_trigger_a_switch(self):
        self.assertEqual(self.relay(['continue', 'finish'], 'run', '--task', 'Tidy'), 0)
        self.assertEqual(self.sessions(), 1)
        self.assertIn('before finishing', self.output)

    def test_ask_user_ends_the_run(self):
        self.assertEqual(self.relay(['ask'], 'run', '--task', 'Tidy'), 0)
        self.assertIn('the user needs to decide', self.output)

    def test_a_continuing_baton_is_picked_up_without_a_task(self):
        self.relay(['work-then-out', 'out'], 'run', '--task', 'Carry on')
        self.assertEqual(self.relay(['used', 'used', 'finish'], 'run'), 0)  # the step counter carries on from the first run
        self.assertIn('Task: Carry on', self.prompt(2))
        self.assertIn('You are Codex', self.prompt(2))

    def test_another_writer_blocks_the_relay(self):
        (self.work/'.git'/'relay-writer-lock').mkdir()
        self.assertEqual(self.relay(['finish'], 'run', '--task', 'Tidy'), 1)
        self.assertEqual(self.sessions(), 0)

    def test_sessions_outside_make_and_check_get_a_zero_call_ledger(self):
        self.assertEqual(self.relay(['make3'], 'run', '--task', 'Tidy'), 0)
        self.assertIn('with 0 calls', self.baton())

    # Make and check
    def configure(self, **settings):
        (self.work/'relay.json').write_text(json.dumps({'live_command': 'python3 run_evals.py', **settings}))

    def test_check_revise_then_approve_and_reviews_are_committed(self):
        self.configure()
        self.assertEqual(self.relay(['make2', 'verdict-revise', 'make1', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '3', '--calls', '3'), 0)
        self.assertIn('approved by codex in round 2; 3 of 3', self.output)
        self.assertIn('A finding with evidence', self.prompt(2))
        commits = subprocess.run(['git', 'log', '--format=%s'], cwd=self.work, text=True, capture_output=True).stdout
        self.assertIn('round 1 review by Codex (revise)', commits)
        self.assertIn('round 2 review by Codex (approve)', commits)

    def test_the_ledger_refuses_calls_over_the_budget(self):
        self.configure()
        self.assertEqual(self.relay(['make5', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '1', '--calls', '2'), 0)
        self.assertIn('Calls used: 2', self.baton())
        self.assertIn('2 of 2 live calls used', self.output)

    def test_the_reviewer_is_read_only(self):
        self.configure()
        self.relay(['make0', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '1')
        self.assertIn('You are read-only', self.prompt(1))

    def test_a_review_without_a_verdict_stops(self):
        self.configure()
        self.assertEqual(self.relay(['make0', 'verdict-maybe'], 'run', '--task', 'Tune', '--check', '1'), 1)
        self.assertIn('without a verdict', self.output)

    def test_calls_need_a_live_command_and_a_claude_maker(self):
        self.assertNotEqual(self.relay([], 'run', '--task', 'Tune', '--check', '1', '--calls', '2', '--dry-run'), 0)
        self.configure()
        self.assertNotEqual(self.relay([], 'run', '--task', 'Tune', '--check', '1', '--calls', '2', '--maker', 'codex', '--dry-run'), 0)

    def test_a_call_count_with_words_after_it_is_accepted(self):
        # Codex wrote "Calls used: 0 live model calls" in a real run; the number is what counts.
        self.configure()
        self.assertEqual(self.relay(['make0-words', 'verdict-approve'], 'run', '--task', 'Review', '--check', '1'), 0)
        self.assertIn('approved by codex', self.output)

    # Take turns
    def test_take_turns_alternates_and_stops_at_the_limit(self):
        self.assertEqual(self.relay(['continue'] * 3, 'run', '--task', 'Build', '--take-turns', '3'), 0)
        self.assertEqual(self.sessions(), 3)
        self.assertIn('You are Claude on turn 2', self.prompt(1))
        self.assertIn('turn limit reached', self.output)

    def test_limits_are_bounded(self):
        for args in (['--take-turns', '0'], ['--take-turns', '9'], ['--check', '0'], ['--check', '6']):
            self.assertNotEqual(self.relay([], 'run', '--task', 'x', *args, '--dry-run'), 0)

    # Manual hand-off and dry run
    def test_pass_and_status(self):
        self.assertEqual(self.relay([], 'pass', '--to', 'codex', '--task', 'Fix it', '--note', 'Half done'), 0)
        self.relay([], 'status')
        self.assertIn('To: codex', self.output)
        self.assertIn('Writer: nobody', self.output)

    def test_dry_run_makes_no_calls(self):
        self.assertEqual(self.relay([], 'run', '--task', 'x', '--dry-run'), 0)
        self.assertEqual(self.sessions(), 0)
        self.assertFalse((self.work/'.relay/log.md').exists())

    # Keeping context
    def test_init_creates_the_context_files_and_never_overwrites(self):
        (self.work/'CLAUDE.md').write_text('My own Claude notes.\n')
        self.assertEqual(self.relay([], 'init'), 0)
        self.assertIn('## Rules', (self.work/'AGENTS.md').read_text())
        self.assertEqual((self.work/'CLAUDE.md').read_text(), 'My own Claude notes.\n')
        self.assertIn('tip', self.output)
        self.assertEqual(json.loads((self.work/'relay.json').read_text())['checkpoint'], 'docs/progress.md')
        self.assertIn('.relay/transcripts/', (self.work/'.gitignore').read_text())
        (self.work/'AGENTS.md').write_text('mine\n'); self.relay([], 'init')
        self.assertEqual((self.work/'AGENTS.md').read_text(), 'mine\n')
        self.assertEqual((self.work/'.gitignore').read_text().count('.relay/transcripts/'), 1)

    def test_assistants_are_told_to_carry_decisions_forward(self):
        self.relay(['finish'], 'run', '--task', 'Tidy')
        self.assertIn("## Tried, didn't work", self.prompt(0))
        self.assertIn('never\ndrop it', self.prompt(0))

    def test_a_hand_over_keeps_decisions_already_in_the_note(self):
        self.relay([], 'pass', '--to', 'codex', '--task', 'Tidy', '--note', 'Started')
        baton = self.work/'.relay/baton.md'
        baton.write_text(baton.read_text().replace('None yet.', 'Kept British spelling because the client is in London.'))
        # Codex picks the note up, runs out before touching it; then Claude runs out too.
        self.assertEqual(self.relay(['out', 'out'], 'run'), 1)
        self.assertIn('Kept British spelling', baton.read_text())
        self.assertIn('## Relay note', baton.read_text())

    def test_pass_adds_to_the_note_instead_of_replacing_it(self):
        self.relay([], 'pass', '--to', 'codex', '--task', 'Tidy', '--note', 'First note')
        self.relay([], 'pass', '--to', 'claude', '--note', 'Sam says: use the short version', '--status', 'continue')
        note = (self.work/'.relay/baton.md').read_text()
        self.assertIn('First note', note)
        self.assertIn('Sam says: use the short version', note)
        self.assertIn('To: claude', note)


class Budget(unittest.TestCase):
    def test_python_and_node_helpers_share_one_ledger(self):
        sys.path.insert(0, str(REPO/'budget')); from relay_budget import reserve_relay_call
        ledger = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, ledger, True)
        (ledger/'budget.json').write_text(json.dumps({'limit': 2}))
        reserve_relay_call(str(ledger))
        node = subprocess.run(['node', '-e', f"import('{(REPO/'budget'/'relay_budget.mjs').as_uri()}').then(m => {{ m.reserveRelayCall('{ledger}'); try {{ m.reserveRelayCall('{ledger}'); console.log('allowed') }} catch {{ console.log('refused') }} }})"],
                              text=True, capture_output=True)
        self.assertEqual(node.stdout.strip(), 'refused')
        with self.assertRaises(RuntimeError): reserve_relay_call(str(ledger))

if __name__ == '__main__': unittest.main()
