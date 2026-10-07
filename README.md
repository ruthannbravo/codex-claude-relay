# Codex ⇄ Claude relay

A small Python script that lets two AI coding assistants, OpenAI's **Codex** and Anthropic's **Claude**, work on the same task in the same Git repository. They take turns through a written hand-off note, so neither one starts from scratch and you don't have to re-explain the task.

![Backup mode: when one runs out of usage, the other carries on](docs/images/backup.png)

## Three modes

| Mode | Command | What happens | Stops when |
| --- | --- | --- | --- |
| **Backup** (default) | `relay.py run --task "..."` | One assistant works through the whole task, updating the note as it goes. If it runs out of usage, the relay records that and starts the other one, which picks up from the note. `--start claude` swaps who goes first. | Done · needs you · both out of usage (it prints both reset times) · any other error |
| **Make and check** | `relay.py run --task "..." --check 3 --calls 8` | Each round Claude does the work and may run live tests within the call budget. Codex then reviews it read-only and ends with approve, revise or ask-user. "Revise" goes back to Claude with the review. | Approved · needs you · round limit (1–5) · call budget exceeded or misreported · any error |
| **Take turns** | `relay.py run --task "..." --take-turns 4` | They alternate, one focused step each. | Done · needs you · turn limit (1–8) · any error |

Plus `relay.py init` to set up a project (it asks you 6 quick questions), `relay.py answer` to answer questions the assistants left for you, `relay.py pass --to codex --task "..." --note "..."` to hand off by hand at the end of a normal chat, `relay.py status` to see who's working and the current note, and `--dry-run` on any `run` to see exactly what each assistant would be told, with no calls.

![Make and check](docs/images/make-and-check.png)

## When the assistants need you

Some things only you can settle: what you want, your taste, a fact about your business. The assistants are told not to guess these. They list them under "Questions for you" (at most 3, in plain English, each with a suggested answer and a one-line reason), and a vague task like "make it better" gets its questions before any work starts. The reviewer does the same, including for anything the other assistant only *assumed* about what you want.

The relay then brings the questions to you:

```
Codex has 2 questions for you before carrying on:

1. The log-in link was removed to keep people focused on signing up. Keep it removed, or bring it back?
   Suggested: bring it back, because members landing here need a way in.
   Your answer (Enter = go with the suggestion):

2. Should the phone number be required?
   Your answer (Enter = let the assistants decide):

Thanks! Saved as your decisions. Continuing…
```

Your answers are saved as final `[user]` decisions that neither assistant can overturn. If no one is at the keyboard, the relay lists the questions, saves them in `.relay/questions.json` and stops; `relay.py answer` asks them one by one when you're back and carries on from there. Type `skip` to leave one to the assistants.

**In Claude desktop or another chat app:** ask Claude to run the relay for you. When it stops with questions, Claude shows them as clickable choices (the suggestion first, with its reason) and passes your answers back with `relay.py answer --answer "..." --answer "..."`. `relay.py init` puts that instruction in `CLAUDE.md`.

## How it keeps context

Codex and Claude each start every session with a blank memory. Neither can see the other's conversation. So the relay never relies on memory: everything that matters is written into files that both of them read first. Think of a shift change at a hospital. The next nurse doesn't remember the last shift, but the patient's chart says everything.

![How the relay keeps context](docs/images/context.png)

| Layer | File | What it holds | Changes |
| --- | --- | --- | --- |
| **Rulebook** | `AGENTS.md` (Codex reads it automatically) plus a one-line `CLAUDE.md` pointing to it | What the project is, how to test it, rules, your preferences | Rarely |
| **Task note** | `.relay/baton.md` | What I did · What's next · Watch out for · **Decisions and why** · **Tried, didn't work** | After every step |
| **Diary** | your progress file plus Git commit messages | Where things stand across days, and why each change was made | Every session |
| **Your answers** | added to the task note with `relay.py pass --note "..."` | Decisions only you can make | When asked |

Rules that keep it from leaking:

- **Write as you go.** Assistants update the note after every meaningful step, not just at the end, so running out of usage loses at most one step.
- **Only add, never wipe.** "Decisions and why" and "Tried, didn't work" are copied forward every time. Hand-overs and `pass` add to the existing note instead of replacing it, and if an assistant stops without writing anything, the relay keeps the old note and adds what it can see (the limit message and any half-finished files).
- **One rulebook for both.** Put shared instructions in `AGENTS.md`. Codex reads it automatically, and a one-line `CLAUDE.md` sends Claude there, so the two never follow different rules.
- **Keep the start-up reading short.** Every session re-reads its notes from scratch, and that reading counts toward your usage. The relay's own additions are small (about 500 tokens of instructions plus the note). The risk is your own files growing. In the project this was built in, the notes every session read first had grown to about 55,000 tokens; moving the history into an archive and keeping a one-page "where things stand" brought that down to about 1,900. Keep `AGENTS.md` and your progress file to roughly a page each, and point to longer records instead of including them.
- **A second pair of eyes.** In make and check, the reviewer judges the work against your own files (`review_criteria`), which catches drift from what you asked.

`relay.py init` sets all of this up in a new project: a starter `AGENTS.md` to fill in, the `CLAUDE.md` pointer, a progress file, `relay.json` and the `.gitignore` entry. It never overwrites a file you already have.

What doesn't carry over: anything that was said but never written down. If it matters, put it in `AGENTS.md` or the note.

## Avoiding groupthink

When one AI reads another's notes, it tends to see the problem the same way, agree too easily and miss what the first one missed. The relay pushes back on that:

- **Blind review (make and check, on by default).** Before the reviewer sees the other AI's report, the relay moves the report out of the project and the reviewer looks at the work on its own. Then it gets its blind findings back, reads the report, and has to list what only it found, what only the other found, and where they disagree, re-checking each against the code. Agreement between two AIs doesn't count as evidence. Unsaved changes the other AI made to its own write-ups (your progress file, plus anything in `blind_hide` in `relay.json`) are set aside during the blind look too, and put back afterwards. Both passes are saved in the review. `--no-blind` skips the extra look (cheaper, more anchoring).
- **Decisions say who made them.** Each line under "Decisions and why" starts with `[user]`, `[codex]` or `[claude]`. Yours are final. An AI's can be challenged, but only with evidence, and the old line is kept and marked as replaced. Record yours with `relay.py pass --to claude --note "..." --decision "Keep the short wording"`.
- **Checked or assumed.** Every claim in the note is marked `(checked: how)` or `(assumed)`, and the next AI must check anything assumed before relying on it. Earlier notes are leads, not facts.
- **Two different models.** Codex and Claude come from different companies with different blind spots, and they're judged against your files and real tests, not each other's opinion.
- **Questions instead of assumptions.** Anything only you can settle goes to you as a question, so neither assistant quietly settles it and the other doesn't inherit the guess.
- **Changes of mind need evidence.** After the blind look, the reviewer must list every blind finding it dropped or changed after reading the report, and what new evidence changed it. Reading the report doesn't count.
- **Swap who goes first.** Whoever starts sets the frame: use `--start claude` or `--maker codex` on some runs.

## How it works

![Under the hood](docs/images/architecture.png)

- **One editor at a time.** Every session claims a lock in the Git directory (`.git/relay-writer-lock`). If anyone else holds it, the relay refuses to start.
- **The hand-off note.** `.relay/baton.md` says what was done, what's next, what to watch out for, which decisions were made and why, and what was tried and didn't work. Its `Status:` line (`continue`, `done` or `ask-user`) decides whether the relay continues.
- **The call ledger.** Every session gets a `RELAY_CALL_BUDGET` directory. Your test runner calls `reserveRelayCall()` (Node) or `reserve_relay_call()` (Python) from [`budget/`](budget/) before each live model call. Each call takes one slot with an atomic `mkdir`, so repeated and parallel runs share one cap. Outside make and check the budget is zero. In make and check the relay also checks the maker's reported `Calls used:` against the ledger and stops if they disagree.
- **Usage-limit detection.** When a session ends, the relay looks for a usage-limit message in its last few lines only, so text the assistant read earlier can't trigger a switch.
- **Receipts.** The terminal shows one line per step. Everything each assistant printed goes to `.relay/transcripts/`, `.relay/log.md` keeps a one-line history, and each make-and-check review is committed on its own.

![What you see](docs/images/terminal.png)

## Set up

You need Python 3.9+, Git, and both CLIs signed in with their subscriptions: [Codex CLI](https://github.com/openai/codex) (`codex login`, ChatGPT sign-in) and [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`, claude.ai sign-in).

```sh
curl -O https://raw.githubusercontent.com/ruthannbravo/codex-claude-relay/main/relay.py
python3 relay.py init          # creates AGENTS.md, CLAUDE.md, a progress file and relay.json (never overwrites),
                               # then asks 6 quick questions about the project; run it again to change your answers
python3 relay.py run --task "Describe the job" --dry-run
```

Optionally add `relay.json` at your repository root (see [`relay.example.json`](relay.example.json)):

| Key | Meaning | Default |
| --- | --- | --- |
| `notes` | Files each assistant reads first | `AGENTS.md`, `CLAUDE.md`, `README.md` (those that exist) |
| `checkpoint` | Progress file to keep up to date | none |
| `tests` | Test commands; Claude is allowed to run exactly these | none |
| `live_command` | The only command allowed to make live model calls (needed for `--calls`) | none |
| `review_criteria` | Files the reviewer judges against | the `notes` |
| `claude_model` / `codex_model` | Model for each CLI | `sonnet` / the CLI's default |
| `keep_chats` | Also save each session as a chat in the Claude and Codex apps. Off by default, because a run starts several separate sessions (they must be separate for the blind review to be blind) and the relay keeps its own record in `.relay/` | `false` |

Add `.relay/transcripts/` to your `.gitignore`.

## Guardrails

- **Subscriptions only.** Claude runs with `ANTHROPIC_*` and provider overrides removed and must be signed in with claude.ai. Codex runs with `OPENAI_API_KEY` removed and must be signed in with ChatGPT. There is no API-key fallback.
- **It always ends.** Nothing retries, loops forever or runs on a schedule. Backup mode uses at most one session of each assistant per run.
- **Short reviews.** Each review opens with a plain-English summary, then at most 3 must-fix items, optional extras, and questions for you.
- **Reviewers can't edit.** Codex reviews in its read-only sandbox. Claude reviews with read-only tools plus your `tests` commands, so both can prove a finding by running the tests.
- **No publishing.** Assistants are told never to push, and the relay stops if one switches branch.
- **Know the limits.** The ledger guards test runners that call it. It isn't a sandbox against deliberate changes to code or environment. Codex's workspace-write sandbox usually can't make Git commits, so it leaves changes for the next assistant and says so in the note.

## Results so far

![Real results](docs/images/scores.png)

**Groupthink test.** On a small pricing project with a written spec, a scripted first assistant handed over work with planted mistakes and a confident, partly false report: mistakes marked "checked", a wrong "decision", a "not in scope", and a made-up worry. Codex and Claude each reviewed it, blind and not blind (8 reviews in all). Every review caught every planted mistake, none approved, none repeated the made-up worry, and the blind look added findings. In a fully real run Codex found two weak tests Claude had missed, and withdrew one of its own claims when the code proved it wrong. Small sample, and a written spec makes mistakes easy to prove: expect more anchoring on judgement calls with no written standard, which is where blind review matters most.

A second test had almost no rules ("Make the sign-up page better", no spec, no tests) and a persuasive report: "in good shape", a pre-ticked marketing box defended as common practice, a removed log-in link called best practice, and a made-up top priority. Across 8 reviews none approved and none adopted the made-up priority. Blind reviews caught 19 of 20 planted problems; reviews that read the report first caught 17 of 20, and one of those only covered what the report had raised. Blind reviews also found more real problems nobody had planted. That is the anchoring the blind look is there to stop: the report sets the reviewer's agenda. Small samples (two runs per set-up), so treat these as signs, not proof.

A third test had no rules at all beyond the owner's answers to `relay.py init` ("Make the home page feel calmer", taste: minimal, water-like, glass, clean fonts). The new questions worked live: the maker asked before guessing about claims it couldn't verify, and refused to invent trial terms. Then the same finished work was reviewed six times. Every blind review (5 of 5) kept its findings ("Changed my mind: None") and left taste calls to the owner as questions (3 each). Both reviews that read the report first raised no questions at all, and Claude's approved the page. The report had already said "Questions for you: None", and the reviewer took that on. Small sample, but it's the clearest sign yet that the blind look matters most when there are no rules. Every reviewer's top request was to see the page on a phone, which none of them could do: if your work is visual, put a screenshot command in `tests`.

Built while improving a customer-service AI prototype. Make and check was run live twice: once on one test case (2 of 2 calls), then on two hard test cases three times each (12 of 12 calls). All seven results scored 100, and Codex re-checked every score itself before approving. Take turns was also run live. Backup mode is covered by the end-to-end tests. Codex's real limit message ("You've hit your usage limit… try again at 2:43 PM") has now been seen and recognised, during a review, where the relay stops rather than switching; a full backup hand-over hasn't happened live yet. If you see one, the exact limit message in `.relay/transcripts/` is the useful thing to report.

## Tests

```sh
python3 -m unittest discover tests
```

The tests run the real relay end to end in a throwaway Git repository against stand-in `codex` and `claude` programs ([`tests/fake_agent.py`](tests/fake_agent.py)), with no model calls.

![What you can use it for](docs/images/uses.png)

## License

MIT
