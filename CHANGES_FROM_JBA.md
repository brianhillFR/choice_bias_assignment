# Changes from the judgement-biases assignment

This file records every intentional difference from the canonical local reference app at
`C:\Users\hill\Documents\GitHub\judgement_biases_ass`.

## Assignment content and routing

- The displayed assignment title is **Choice Biases Assignment**.
- The six judgement-bias cases were replaced with the ten choice-bias cases in
  `Choice_assignment.qsf` and `Choice bias assignment master.docx`.
- The initial case is now selected uniformly from cases 1–10 rather than 1–6.
- The review case is selected uniformly from the other nine cases rather than the
  other five, giving 90 ordered case pairs instead of 30.
- Qualtrics block IDs, question IDs, export tags, and each branch's nine-question
  review pool were replaced by the active routing from `Choice_assignment.qsf`.
- The stored source fingerprint is the SHA-256 hash of that QSF:
  `045f97f6ae5a9129e484353ac9ed926dcd196900086b30229d07b75e7ef5a918`.
- The initial-answer and review wording was replaced with the corresponding wording
  from the choice-assignment QSF. No other participant-facing workflow was redesigned.
- The QSF's additional author-identification question is not included. The reference
  Streamlit assignment does not include its Qualtrics counterpart, so omitting it
  preserves the requested JBA Streamlit structure.

## Supplied ChatGPT responses and feedback guidance

- The six judgement-bias supplied responses were replaced by ten plausible,
  deliberately wrong responses prepared by the other requested agent and handed off
  in `coordination/choice_bias_wrong_answers.json`.
- `bot_config.toml` now contains ten case-specific private entries. Each has the
  instructor-master mechanism and reasoning, the supplied plausible-wrong position,
  and guidance explaining the diagnostic flaw so that feedback can challenge the
  student's answer consistently.
- Bot-config validation now requires cases 1–10 instead of cases 1–6.
- The obsolete copied `bot_config_old.toml` was removed so the new folder contains no
  alternative judgement-bias answer set.

## Storage and deployment preparation

- The default dedicated Dropbox folder and example secret use
  `/choice_bias_assignment` instead of `/judgement_biases_ass`, preventing results for
  the two assignments from being mixed. The identity log remains separate from the
  answer CSV exactly as in the reference app.
- The keep-awake helper and workflow use the future placeholder URL
  `https://choice-biases-assignment.streamlit.app/`. Set the repository variable
  `STREAMLIT_APP_URL` to the actual URL after deployment.
- README and RUNNING documentation now describe ten cases, 90 ordered pairs, the new
  Dropbox folder, and a future choice-assignment repository.
- No Git repository was initialized, and nothing was pushed or deployed.

## Verification updates

- Routing tests now cover all 90 ordered case pairs and all three rating values.
- QSF fidelity tests use the supplied choice QSF and verify its hash, branch/block
  mapping, review pools, export tags, rating choices, and case numbers.
- DOCX fidelity tests use the supplied master and verify distinctive source content
  for each of the ten cases.
- Response-export tests exercise all ten supplied responses.
- AppTest query-parameter and form interactions were updated for the installed
  Streamlit testing API; this changes tests only, not participant behavior.

## Deliberately unchanged behavior

The entry code, email-first registration, name capture, email-based resume, durable
per-step saving, random subject IDs, separate subject identity log, answer CSV schema,
stale-tab protection, timing capture, two AI feedback exchanges, final-answer locking,
Dropbox/local backends, export tools, and Streamlit UI sequence are inherited from the
judgement-biases app without intentional behavioral changes.

## Source provenance

The uploaded project reference ZIP was inspected and used as provenance. It contains
the Behavioral Economics export/history and transfer bundle but not a deployable JBA
`app.py`/Streamlit source tree. Therefore the copied Python and support files came from
the canonical local reference app at
`C:\Users\hill\Documents\GitHub\judgement_biases_ass`; the assignment content came
from the supplied QSF and master DOCX.
