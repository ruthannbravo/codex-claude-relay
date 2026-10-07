# Codex ⇄ Claude relay

Let two AI coding helpers, **Codex** (by OpenAI) and **Claude** (by Anthropic), work on the same project together. They pass a written note back and forth, like runners passing a baton, so neither starts from scratch and you never have to re-explain the job.

![Two AI helpers. One job. One shared note.](docs/images/1-overview.png)

## What it does

**If one runs out of usage, the other carries on.** Both helpers come with usage limits. When one hits its limit mid-job, the other reads the note and picks up where it stopped.

![If one runs out, the other carries on.](docs/images/2-backup.png)

**One does the work, the other checks it.** The checker looks at the work on its own first, before it reads the other one's report. That way it forms its own opinion instead of just agreeing.

![One does the work. The other checks it.](docs/images/3-check.png)

**When only you can decide, it asks you.** Your taste, your goals, facts about your business: the helpers ask instead of guessing. You get a short question with a suggested answer, and your answer is final.

![When only you can decide, it asks you.](docs/images/4-asks.png)

**It looks at your page, not just the code.** For websites and apps, it takes pictures at computer and phone size after each round, and the checker judges what people will actually see.

![It looks at your page, not just the code.](docs/images/5-look.png)

**We tested whether they just agree with each other.** When the checker read the other one's report first, it sometimes stopped asking questions and even approved unfinished work. When it looked first, it kept its own view every time. So looking first is on by default. [Read the tests](docs/tests.md).

![Do they just agree with each other? We tested it.](docs/images/6-tested.png)

## Getting started

![Getting started](docs/images/7-start.png)

1. **Install both helpers** and sign in with your normal plans: [Codex](https://github.com/openai/codex) (with ChatGPT) and [Claude Code](https://docs.claude.com/en/docs/claude-code) (with Claude). You also need Python 3.9 or newer and Git, which most Macs already have.
2. **Download the relay into your project and answer 6 quick questions** about what the project is and what "good" looks like to you:
   ```sh
   curl -O https://raw.githubusercontent.com/ruthannbravo/codex-claude-relay/main/relay.py
   python3 relay.py init
   ```
3. **Give it a job:**
   ```sh
   python3 relay.py run --task "Make the home page feel calmer"
   ```
   Or skip the terminal and ask Claude (in the Claude app or Claude Code) to run the relay for you. It will show you any questions as clickable choices.

## Common questions

**Does it cost extra?** No. It uses the Codex and Claude plans you already pay for. It never uses paid API keys, even if you have one set up.

**Is my project safe?** Only one helper can change files at a time. The checker can't edit anything. The helpers are told never to publish or upload your work: that stays your decision.

**Do I need to be a programmer?** You need to be comfortable typing two or three commands, or you can ask Claude to run it for you. Everything the helpers ask you is in plain English.

**What does it not do?** It doesn't run on a schedule or loop forever: every run ends, and it tells you why. And it only knows what's written down, so anything that matters should be in your answers or the project notes.

---

## For developers

A single Python file with no dependencies. It runs the `codex` and `claude` CLIs in your Git repository and coordinates them through files.

### Modes

| Mode | Command | What happens | Stops when |
| --- | --- | --- | --- |
| **Backup** (default) | `relay.py run --task "..."` | One assistant works through the whole task, updating the note as it goes. If it runs out of usage, the relay records that and starts the other one, which picks up from the note. `--start claude` swaps who goes first. | Done · waiting for your answers · both out of usage (prints both reset times) · any other error |
| **Make and check** | `relay.py run --task "..." --check 3 --calls 8` | Each round the maker (Claude, or `--maker codex`) does the work and may make live test calls within the budget. The checker reviews read-only, blind first, and ends with approve, revise or ask-user. | Approved · waiting for your answers · round limit (1–5) · call budget exceeded or misreported · any error |
| **Take turns** | `relay.py run --task "..." --take-turns 4` | They alternate, one focused step each. | Done · waiting for your answers · turn limit (1–8) · any error |

Other commands: `init` (set up a project and ask the 6 questions; run it again to change your answers), `answer` (answer waiting questions, then carry on), `look` (screenshot your pages), `status` (who holds the lock, and the current note), `pass --to codex --task "..." --note "..." [--decision "..."]` (hand off by hand at the end of a normal chat). Add `--dry-run` to any `run` to print exactly what each assistant would be told, with no model calls.

### How context is kept

Each session starts with a blank memory, so everything that matters lives in files both assistants read first:

| Layer | File | What it holds |
| --- | --- | --- |
| Rulebook | `AGENTS.md` (Codex reads it automatically) and a `CLAUDE.md` pointing to it | What the project is, your setup answers, how to test it, rules |
| Task note | `.relay/baton.md` | What I did · What's next · Watch out for · Decisions and why · Tried, didn't work · Questions for you |
| Diary | your progress file and Git commit messages | Where things stand across days |

Notes are only added to, never wiped: decisions and dead ends are copied forward on every hand-off, and if an assistant stops without writing, the relay keeps the old note and adds what it can see. Keep `AGENTS.md` and your progress file to about a page each, since every session re-reads them and that reading counts toward your usage.

### Against groupthink

- **Blind first look** (make and check, on by default). The report and the maker's unsaved notes are set aside while the checker reviews the work alone. It then reads the report and lists what only it found, what only the maker reported, and any disagreements. A "Changed my mind" section must give new evidence for every finding it dropped. `--no-blind` skips this.
- **Decisions say who made them.** `[user]`, `[codex]` or `[claude]`. Yours are final; an assistant's can be challenged only with evidence, and the old line is kept.
- **Checked or assumed.** Every claim is marked `(checked: how)` or `(assumed)`. Anything assumed about what you want becomes a question for you.
- **Two different models,** judged against your files and real tests rather than each other's opinion. Swap who goes first with `--start` or `--maker`.

### Questions for you

Assistants list what only you can settle under "Questions for you": at most 3, in plain English, each with a suggested answer and a reason. In make and check, the maker's questions wait for the review, so both assistants' questions come in one stop. At a keyboard you answer in the terminal and the run carries on. Otherwise the relay saves them to `.relay/questions.json` and stops; `relay.py answer` asks them later, or takes `--answer "..."` once per question (`suggested` and `skip` work too) and resumes. Answers are recorded as `[user]` decisions, and assistants are told never to suggest undoing one.

### Seeing the page

Set `"look": ["index.html"]` (files or URLs) in `relay.json`. After each round of work the relay screenshots each page at 1280px and 390px wide into `.relay/screenshots/`. Claude reviewers open them; Codex reviewers get them attached with `--image`. The maker can run `relay.py look` itself. Needs Chrome or Chromium (or `RELAY_CHROME`). Pages that refuse to load in a frame get no phone picture.

### Settings (`relay.json`, optional)

See [`relay.example.json`](relay.example.json).

| Key | Meaning | Default |
| --- | --- | --- |
| `notes` | Files each assistant reads first | `AGENTS.md`, `CLAUDE.md`, `README.md` (those that exist) |
| `checkpoint` | Progress file to keep up to date | none |
| `tests` | Test commands; Claude may run exactly these, as maker or reviewer | none |
| `live_command` | The only command allowed to make live model calls (needed for `--calls`) | none |
| `review_criteria` | Files the reviewer judges against | the `notes` |
| `blind_hide` | Extra write-up files to set aside during the blind look | none |
| `claude_model` / `codex_model` | Model for each CLI | `sonnet` / the CLI's default |
| `look` | Pages to screenshot at desktop and phone size | none |
| `lock_name` | Name of the writer lock in the Git directory, to share your project's own lock | `relay-writer-lock` |
| `keep_chats` | Also save each session as a chat in the apps (sessions are kept separate so the blind look stays blind) | `false` |

Add `.relay/transcripts/` and `.relay/screenshots/` to your `.gitignore` (`init` adds the first).

### Under the hood

- **One editor at a time.** Every session claims a lock directory in the Git directory; if anyone else holds it, the relay refuses to start.
- **Call ledger.** Every session gets a `RELAY_CALL_BUDGET` directory. Your test runner calls `reserveRelayCall()` (Node) or `reserve_relay_call()` (Python) from [`budget/`](budget/) before each live model call; each call takes a slot with an atomic `mkdir`, so parallel runs share one cap. Outside make and check the limit is zero. The maker's reported `Calls used:` must match the ledger.
- **Usage-limit detection** reads only the last lines of a session, so a file that mentions limits can't trigger a switch.
- **Subscriptions only.** Claude runs with `ANTHROPIC_*` and provider overrides removed and must be signed in with claude.ai; Codex runs with `OPENAI_API_KEY` removed and must be signed in with ChatGPT.
- **Always ends.** No retries, schedules or endless loops; round, turn and question-stop limits.
- **Receipts.** One line per step in the terminal; full output in `.relay/transcripts/`; a one-line history in `.relay/log.md`; each review committed on its own.
- **Know the limits.** The ledger guards test runners that call it; it isn't a sandbox against deliberate changes. Codex's sandbox usually can't make Git commits, so it leaves changes for the next assistant and says so in the note.

### Tests

```sh
python3 -m unittest discover tests
```

45 end-to-end tests run the real relay in a throwaway Git repository against stand-in `codex` and `claude` programs ([`tests/fake_agent.py`](tests/fake_agent.py)), with no model calls. The README images are made from [`docs/images/source/cards.html`](docs/images/source/cards.html).

## License

MIT
