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

    def test_an_assistant_never_waits_for_typed_input(self):
        # Started from a script, the relay's own input can be an open pipe that never ends; codex exec would wait on it forever.
        read_end, write_end = os.pipe(); self.addCleanup(os.close, write_end)
        try:
            result = subprocess.run([sys.executable, str(REPO/'relay.py'), 'run', '--task', 'Tidy'], cwd=self.work, stdin=read_end,
                                    capture_output=True, text=True, timeout=30, env={**self.env, 'FAKE_PLAN': 'finish-after-input'})
        finally: os.close(read_end)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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

    # Questions for the user
    def questions(self): return json.loads((self.work/'.relay/questions.json').read_text())
    def decisions(self): return self.baton().split('## Decisions and why', 1)[1].split('\n## ', 1)[0]

    def test_with_no_one_at_the_keyboard_the_questions_are_listed_and_saved(self):
        self.assertEqual(self.relay(['ask-q'], 'run', '--task', 'Tidy'), 0)
        self.assertIn('Codex has 2 questions for you', self.output)
        self.assertIn('Suggested: blue, because it matches the logo', self.output)
        self.assertIn('waiting for your answers: run python3 relay.py answer', self.output)
        self.assertEqual(self.questions()['resume'], {'args': ['run', '--start', 'codex'], 'to': 'codex'})

    def test_questions_are_read_in_the_ways_assistants_write_them(self):
        sys.path.insert(0, str(REPO)); import relay
        inline = '## Questions for you\n1. Keep the quote? Suggest: leave it out, because it is unverified.\n2. Bring back the menu links?\n'
        self.assertEqual(relay.parse_questions(inline), [{'question': 'Keep the quote?', 'suggest': 'leave it out, because it is unverified.'},
                                                         {'question': 'Bring back the menu links?', 'suggest': ''}])
        bold = '**Must fix**\n1. Contrast.\n\n4. Questions for you:\n1. Required phone?\n   - **Suggest:** optional, because unused\n\nVerdict: revise'
        self.assertEqual(relay.parse_questions(bold), [{'question': 'Required phone?', 'suggest': 'optional, because unused'}])
        self.assertEqual(relay.parse_questions('## Questions for you\nNone.\n\n## Results\n1. ok'), [])

    def test_a_bare_ask_user_becomes_a_question_from_whats_next(self):
        self.assertEqual(self.relay(['ask'], 'run', '--task', 'Tidy'), 0)
        self.assertEqual(self.questions()['questions'], [{'question': 'more', 'suggest': '', 'from': 'codex'}])

    def test_answer_records_final_decisions_and_carries_on(self):
        self.relay(['ask-q', 'finish'], 'run', '--task', 'Tidy')
        self.assertEqual(self.relay(['ask-q', 'finish'], 'answer', '--answer', 'Yes, keep it', '--answer', 'suggested'), 0)
        self.assertIn('- [user] Should the log-in link stay? Answer: Yes, keep it', self.decisions())
        self.assertIn('Answer: blue, because it matches the logo (the suggested answer)', self.decisions())
        self.assertIn('task done', self.output)
        self.assertEqual(self.sessions(), 2)
        self.assertFalse((self.work/'.relay/questions.json').exists())

    def test_answer_needs_one_answer_per_question(self):
        self.relay(['ask-q'], 'run', '--task', 'Tidy')
        self.assertNotEqual(self.relay([], 'answer', '--answer', 'only one'), 0)
        self.assertEqual(self.relay([], 'answer'), 1)  # no keyboard and no answers: it lists them and stops

    def test_at_a_keyboard_the_questions_are_asked_there_and_it_carries_on(self):
        import pty
        main, typed = pty.openpty(); self.addCleanup(os.close, main)
        os.write(main, b'Bring it back\n\n')  # the second answer: Enter, so the suggestion
        result = subprocess.run([sys.executable, str(REPO/'relay.py'), 'run', '--task', 'Tidy'], cwd=self.work, stdin=typed, capture_output=True,
                                text=True, timeout=60, env={**self.env, 'FAKE_PLAN': 'ask-q,finish'})
        os.close(typed)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Thanks! Saved as your decisions. Continuing', result.stdout)
        self.assertIn('Answer: Bring it back', self.decisions())
        self.assertIn('(the suggested answer)', self.decisions())
        self.assertEqual(self.sessions(), 2)

    def test_a_reviewers_questions_come_to_the_user_before_the_next_round(self):
        self.configure()
        self.assertEqual(self.relay(['make0', 'blind', 'verdict-revise-q'], 'run', '--task', 'Tune', '--check', '2'), 0)
        self.assertEqual(self.questions()['questions'], [{'question': 'Is the phone number really required?', 'suggest': 'make it optional, because nothing uses it', 'from': 'codex'}])
        self.assertEqual(self.questions()['resume']['args'], ['run', '--check', '1', '--calls', '0', '--maker', 'claude', '--from-round', '2'])
        self.assertEqual(self.relay(['make0', 'blind', 'verdict-revise-q', 'make0', 'blind', 'verdict-approve'], 'answer', '--answer', 'skip'), 0)
        self.assertIn('Left to the assistants to decide: Is the phone number really required?', self.baton())
        self.assertIn('Round 2: Claude working', self.output)  # the count carries on after your answers
        self.assertIn('approved by codex in round 2', self.output)

    def test_both_assistants_questions_come_in_one_stop(self):
        self.configure()
        self.assertEqual(self.relay(['make0-q', 'blind', 'verdict-revise-q'], 'run', '--task', 'Tune', '--check', '2'), 0)
        self.assertEqual(self.sessions(), 3)  # the maker's questions waited for the review instead of stopping the run
        self.assertIn('Claude and Codex have 3 questions for you', self.output)
        self.assertIn('(asked by Codex)', self.output)
        self.assertEqual([q['from'] for q in self.questions()['questions']], ['claude', 'claude', 'codex'])

    def test_the_assistants_are_told_not_to_reopen_your_decisions(self):
        self.relay(['finish'], 'run', '--task', 'Tidy')
        self.assertIn('never\nsuggest an answer that undoes one of their decisions', self.prompt(0))
        self.assertIn('ask everything you need in one go', self.prompt(0))

    # Seeing the page
    def fake_chrome(self):
        chrome = self.tmp/'chrome'
        chrome.write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in --screenshot=*) echo png > "${a#--screenshot=}";; esac; done\n')
        chrome.chmod(0o755); self.env['RELAY_CHROME'] = str(chrome)

    def test_look_screenshots_each_page_at_desktop_and_phone_size(self):
        self.fake_chrome(); self.configure(look=['index.html'])
        self.assertEqual(self.relay([], 'look'), 0)
        self.assertEqual(sorted(p.name for p in (self.work/'.relay/screenshots').iterdir()), ['index-html-desktop.png', 'index-html-phone.png'])

    def test_reviewers_get_the_screenshots_and_the_maker_may_take_them(self):
        self.fake_chrome(); self.configure(look=['index.html'])
        self.relay(['make0', 'blind', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '1')
        self.assertIn('relay.py look', self.prompt(0))
        self.assertIn('.relay/screenshots/index-html-phone.png', self.prompt(1))
        self.assertIn('the grey strip on the right is only padding', self.prompt(1))
        self.assertIn('--image=', (self.tmp/'state.args1').read_text())

    def test_the_lock_name_can_match_a_projects_own(self):
        self.configure(lock_name='agent-writer-lock')
        (self.work/'.git/agent-writer-lock').mkdir()
        self.assertEqual(self.relay(['finish'], 'run', '--task', 'Tidy'), 1)
        self.assertIn('already claimed', self.output)

    def test_reviews_are_short_and_record_changes_of_mind(self):
        self.configure()
        self.relay(['make0', 'blind', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '1')
        for words in ('Summary:', 'Must fix: at most 3', 'Questions for you', 'Changed my mind', 'Reading the report is\nnot evidence'):
            self.assertIn(words, self.prompt(2))

    def test_sessions_are_not_saved_as_chats_unless_asked(self):
        self.configure()
        self.relay(['make0', 'blind', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '1')
        self.assertIn('--no-session-persistence', (self.tmp/'state.args0').read_text())
        self.assertIn('--ephemeral', (self.tmp/'state.args1').read_text())
        self.configure(keep_chats=True)
        self.relay(['make0', 'blind', 'verdict-approve'] * 2, 'run', '--task', 'Tune', '--check', '1')
        self.assertNotIn('--no-session-persistence', (self.tmp/'state.args3').read_text())

    def test_an_unclear_task_is_asked_about_before_any_work(self):
        self.relay(['finish'], 'run', '--task', 'Make it better')
        self.assertIn("don't guess: before changing anything, ask under Questions for you", self.prompt(0))

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
        self.assertEqual(self.relay(['make2', 'blind', 'verdict-revise', 'make1', 'blind', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '3', '--calls', '3'), 0)
        self.assertIn('approved by codex in round 2; 3 of 3', self.output)
        self.assertIn('A finding with evidence', self.prompt(3))
        commits = subprocess.run(['git', 'log', '--format=%s'], cwd=self.work, text=True, capture_output=True).stdout
        self.assertIn('round 1 review by Codex (revise)', commits)
        self.assertIn('round 2 review by Codex (approve)', commits)

    def test_the_ledger_refuses_calls_over_the_budget(self):
        self.configure()
        self.assertEqual(self.relay(['make5', 'blind', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '1', '--calls', '2'), 0)
        self.assertIn('Calls used: 2', self.baton())
        self.assertIn('2 of 2 live calls used', self.output)

    def test_the_reviewer_is_read_only(self):
        self.configure()
        self.relay(['make0', 'blind', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '1')
        self.assertIn('You are read-only', self.prompt(1))

    def test_a_review_without_a_verdict_stops(self):
        self.configure()
        self.assertEqual(self.relay(['make0', 'blind', 'verdict-maybe'], 'run', '--task', 'Tune', '--check', '1'), 1)
        self.assertIn('without a verdict', self.output)

    def test_calls_need_a_live_command_and_a_claude_maker(self):
        self.assertNotEqual(self.relay([], 'run', '--task', 'Tune', '--check', '1', '--calls', '2', '--dry-run'), 0)
        self.configure()
        self.assertNotEqual(self.relay([], 'run', '--task', 'Tune', '--check', '1', '--calls', '2', '--maker', 'codex', '--dry-run'), 0)

    def test_a_call_count_with_words_after_it_is_accepted(self):
        # Codex wrote "Calls used: 0 live model calls" in a real run; the number is what counts.
        self.configure()
        self.assertEqual(self.relay(['make0-words', 'blind', 'verdict-approve'], 'run', '--task', 'Review', '--check', '1'), 0)
        self.assertIn('approved by codex', self.output)

    # Against groupthink: a blind first look, findings carried into the comparison, tagged decisions
    def test_the_reviewer_looks_blind_first_with_the_report_hidden(self):
        self.configure()
        self.assertEqual(self.relay(['make0', 'blind', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '1'), 0)
        self.assertIn('BLIND REVIEW', self.prompt(1))
        self.assertIn('Report visible during blind look: False', self.prompt(2))  # its own blind findings, passed in
        self.assertIn('problems only you found', self.prompt(2))
        self.assertTrue((self.work/'.relay/baton.md').exists())  # the note is put back afterwards
        review = next((self.work/'.relay/reviews').iterdir()).read_text()
        self.assertIn('## Blind review (before reading the report)', review)

    def test_no_blind_skips_the_extra_look(self):
        self.configure()
        self.assertEqual(self.relay(['make0', 'verdict-approve'], 'run', '--task', 'Tune', '--check', '1', '--no-blind'), 0)
        self.assertEqual(self.sessions(), 2)
        self.assertNotIn('BLIND REVIEW', self.prompt(1))

    def test_assistants_tag_decisions_and_mark_claims(self):
        self.relay(['finish'], 'run', '--task', 'Tidy')
        self.assertIn('[user] decisions are final', self.prompt(0))
        self.assertIn('(assumed)', self.prompt(0))

    def test_a_user_decision_is_recorded_as_final(self):
        self.relay([], 'pass', '--to', 'codex', '--task', 'Tidy', '--note', 'Started', '--decision', 'Keep the short wording')
        self.relay([], 'pass', '--to', 'claude', '--note', 'More', '--decision', 'No new colours')
        note = (self.work/'.relay/baton.md').read_text()
        decisions = note.split('## Decisions and why', 1)[1].split('\n## ', 1)[0]
        self.assertIn('- [user] Keep the short wording', decisions)
        self.assertIn('- [user] No new colours', decisions)
        self.assertNotIn('None yet.', decisions)

    def test_the_makers_progress_notes_are_hidden_during_the_blind_look_and_put_back(self):
        (self.work/'docs').mkdir(); (self.work/'docs/progress.md').write_text('# Progress\n')
        subprocess.run(['git', 'add', '.'], cwd=self.work, check=True); subprocess.run(['git', 'commit', '-qm', 'notes'], cwd=self.work, check=True)
        self.configure(checkpoint='docs/progress.md')
        self.assertEqual(self.relay(['make0-notes', 'blind', 'verdict-approve'], 'run', '--task', 'Review', '--check', '1'), 0)
        self.assertIn('Maker notes visible: False', self.prompt(2))
        self.assertIn('skip its write-ups (docs/progress.md)', self.prompt(1))
        self.assertIn('MAKER FINDINGS', (self.work/'docs/progress.md').read_text())  # restored afterwards

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
        self.assertIn('1. What is this project, and who is it for?', self.output)  # listed for whoever runs it to ask
        self.assertIn('init --answer "..."', self.output)
        (self.work/'AGENTS.md').write_text('mine\n'); self.relay([], 'init')
        self.assertEqual((self.work/'AGENTS.md').read_text(), 'mine\n')
        self.assertEqual((self.work/'.gitignore').read_text().count('.relay/transcripts/'), 1)

    def test_init_asks_about_the_project_and_can_be_run_again(self):
        self.relay([], 'init', '--answer', 'A calm site for me and others', '--answer', 'I look at it on desktop and my phone')
        agents = (self.work/'AGENTS.md').read_text()
        self.assertTrue(agents.startswith('# Notes for AI assistants\n\n<!-- relay:about'))
        self.assertIn('- What it is and who it is for: A calm site for me and others', agents)
        self.assertIn('- Must never change or break: not said yet (ask the owner if it matters)', agents)
        self.assertIn('relay.py answer --answer', agents)  # how Claude or Codex passes your answers back
        self.relay([], 'init', '--answer', 'A calmer site')
        agents = (self.work/'AGENTS.md').read_text()
        self.assertEqual(agents.count('relay:about ('), 1)
        self.assertIn('A calmer site', agents); self.assertNotIn('for me and others', agents)

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


class Install(unittest.TestCase):
    """install.sh in a throwaway home folder, from the local relay.py, with no keyboard."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp/'home'; self.home.mkdir()
        self.env = {'HOME': str(self.home), 'SHELL': '/bin/zsh', 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin:' + str(Path(sys.executable).parent),
                    'RELAY_SOURCE': str(REPO/'relay.py'), 'RELAY_TEACH': 'no'}
        self.relay_cmd = self.home/'.local/bin/relay'

    def install(self, **env):
        return subprocess.run(['sh', str(REPO/'install.sh')], env={**self.env, **env}, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def relay(self, *args, cwd=None):
        return subprocess.run([str(self.relay_cmd), *args], cwd=cwd or self.tmp, env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def project(self):
        work = self.tmp/'project'; work.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=work, check=True)
        return work

    def test_install_gives_a_relay_command_that_works_in_any_project(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Installed the relay: codex-claude-relay', result.stdout)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', (self.home/'.zshrc').read_text())  # the Terminal will find it
        self.assertIn('relay teach', result.stdout)  # no keyboard: it says how to teach the apps instead of asking
        work = self.project()
        setup = self.relay('init', '--answer', 'A calm site', cwd=work)
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        agents = (work/'AGENTS.md').read_text()
        self.assertIn('A calm site', agents)
        self.assertIn('relay answer --answer', agents)  # installed: messages say `relay`, not `python3 relay.py`

    def test_installing_twice_is_harmless(self):
        self.install(); self.install()
        self.assertEqual((self.home/'.zshrc').read_text().count('codex-claude-relay'), 1)
        self.assertEqual(self.relay('version').returncode, 0)

    def test_a_broken_download_changes_nothing(self):
        self.install()
        before = (self.home/'.relay/relay.py').read_text()
        bad = self.tmp/'bad.py'; bad.write_text('<html>Not found</html')
        self.assertNotEqual(self.install(RELAY_SOURCE=str(bad)).returncode, 0)
        self.assertEqual((self.home/'.relay/relay.py').read_text(), before)

    def test_outside_a_project_it_says_how_to_start_one(self):
        self.install()
        result = self.relay('init')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('git init', result.stderr)

    def test_teach_adds_one_removable_note_and_keeps_your_own_instructions(self):
        (self.home/'.claude').mkdir(); (self.home/'.claude/CLAUDE.md').write_text('My own rules.\n')
        self.install(RELAY_TEACH='yes'); self.relay('teach')
        claude, codex = (self.home/'.claude/CLAUDE.md').read_text(), (self.home/'.codex/AGENTS.md').read_text()
        self.assertTrue(claude.startswith('My own rules.'))
        self.assertEqual(claude.count('<!-- codex-claude-relay -->'), 1)
        self.assertIn('Set up the relay for this project', codex)
        self.assertIn(str(self.relay_cmd), codex)
        self.relay('teach', '--remove')
        self.assertEqual((self.home/'.claude/CLAUDE.md').read_text(), 'My own rules.\n')

    def test_uninstall_removes_only_what_it_added(self):
        (self.home/'.zshrc').write_text('alias ll="ls -l"\n')
        self.install(RELAY_TEACH='yes')
        work = self.project(); self.relay('init', cwd=work)
        result = self.relay('uninstall', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.relay_cmd.exists())
        self.assertFalse((self.home/'.relay').exists())
        self.assertEqual((self.home/'.zshrc').read_text(), 'alias ll="ls -l"\n')
        self.assertNotIn('codex-claude-relay', (self.home/'.codex/AGENTS.md').read_text())
        self.assertTrue((work/'AGENTS.md').exists())  # your projects are left alone


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
