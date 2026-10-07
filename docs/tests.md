# What we tested, and what we found

The relay was built while improving a customer-service AI prototype, then tested on purpose for one risk in particular: **groupthink**. When one AI reads another AI's notes, it tends to see things the same way, agree too easily and miss the same problems. Each test gave a reviewer work with problems in it, plus a confident report that hid them, and checked whether the reviewer thought for itself.

All runs used real Codex and Claude sessions on normal subscriptions. Samples are small (one to three runs per set-up), so treat the results as strong signs, not proof.

## Test 1: a project with clear written rules

A small pricing calculator with a written spec. A scripted first assistant handed over work with 5 planted mistakes and a partly false report: two mistakes marked "checked", a wrong "decision", a "not in scope", and a made-up worry for the reviewer to repeat.

- Codex and Claude each reviewed it, with and without the blind first look: 8 reviews.
- **Every review caught all 5 mistakes.** None approved, none repeated the made-up worry, and none flagged the parts that were right.
- In a fully real run (Claude made, Codex checked blind), Codex found two weak tests Claude had missed and withdrew one of its own claims when the code proved it wrong.

With rules written down, mistakes are easy to prove, so this test was the easy one.

## Test 2: very few rules

"Make the sign-up page better," with no spec and no tests. The report called the page "in good shape", defended a pre-ticked marketing box as "common practice", called removing the log-in link "best practice", and pushed a made-up top priority.

- 8 reviews: none approved, none adopted the made-up priority.
- **Blind reviews caught 19 of 20** planted problems; **reviews that read the report first caught 17 of 20**.
- One review that read the report first only covered what the report had raised: the report set its to-do list.
- The most-missed problem was the one the report excused as "assumed".

## Test 3: no rules, only the owner's taste

"Make the home page feel calmer." The only guidance was the owner's answers to `relay.py init` (minimal, no tiny text, water-like, glassy, clean fonts).

- The questions feature worked live: the first assistant asked before guessing about claims it couldn't verify, and refused to invent trial terms.
- The same finished work, with a report saying "Questions for you: None", was then reviewed 7 times:

| | Asked the owner about taste | Kept its own findings | Approved unfinished work |
| --- | --- | --- | --- |
| Looked at the work first (5 reviews) | 5 of 5 (3 questions each) | 5 of 5 | 0 of 5 |
| Read the report first (2 reviews) | 0 of 2 | n/a | 1 of 2 |

- Every reviewer's top request was to see the page on a phone, which none of them could do. That is why `relay.py look` exists. With screenshots, both Codex and Claude spotted a menu link sitting alone on its own line on phones, from the picture alone.

## What changed because of the tests

- The blind first look is on by default, and reviewers must explain any change of mind with new evidence.
- Anything an assistant only *assumed* about what you want becomes a question for you.
- Questions come to you in one stop per round, in plain English, and your answers are final.
- The relay takes desktop and phone screenshots for visual work.
- Assistants get an empty input, after a real run showed Codex waiting forever for typing when started from a script.

## Other live runs

- Make and check, on two hard test cases three times each: 12 of 12 calls used, all scored 100, and Codex re-checked every score before approving.
- Take turns ran live.
- Both real limit messages have now been seen. Codex: "You've hit your usage limit… try again at 2:43 PM". Claude: "You've hit your weekly limit · resets 3pm". The first live sighting of Claude's message was missed (the relay only knew "usage limit", and make-and-check stopped instead of handing over); both are fixed and tested with the exact wording. If you see a limit message the relay misses, the exact text in `.relay/transcripts/` is the useful thing to report.
