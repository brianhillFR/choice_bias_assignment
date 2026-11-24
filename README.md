# Choice Biases Assignment

A Streamlit assignment that reproduces the active Qualtrics routing: two different
cases drawn from ten, an original answer, an assessment of a supplied ChatGPT
response, and one AI objection after each text answer followed by a student-written final answer. The first page asks for an email address and confirmation. Any valid email domain
is accepted. New respondents then enter their name and access code on a second
page and read the instructions before beginning. Returning respondents confirm
only their email to resume their saved step. Email format is checked; mailbox ownership is not
verified. Each other case has exactly a 1/9 chance of becoming the review
case. Its number is saved as review_case in the assignment JSON and review_case_number in the CSV.

- `assignment_content.toml`: all question wording, cases, supplied responses, and interface labels.
- `judgement_biases_interview.py`: assignment flow, validation, local storage, and CSV export.
- `dropbox_store.py`: persistent Dropbox storage, email lookup, and completed CSV merging.
- `email_lookup.py`: email lookup during live updates, including older Dropbox objects.
- `response_export.py`: readable CSV columns and conversion of older result rows.
- `answer_timing.py`: elapsed answer-screen times and timestamps.
- `response_identity.py`: shared email lookup keys and rules locking saved answers.
- `ai_interview.py`: model calls and the durable challenge/reply/final-answer protocol.
- `bot_config.toml`: editable bot instructions, model settings, and ten plausible wrong positions.
- `.streamlit/secrets.toml.example`: configuration template containing placeholders only.

Edit assignment wording in `assignment_content.toml`. Text between triple single
quotes (`'''`) accepts ordinary line breaks and blank lines. Preserve placeholder
names such as `{number}`, numbered keys, and the existing HTML formatting.

## Run locally

Use Python 3.11 or 3.12 for a new deployment (the app also works on Python 3.9).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

For a local trial, edit `.streamlit/secrets.toml` to contain just:

```toml
ASSIGNMENT_PASSWORD = "choose-your-password"
STORAGE_BACKEND = "sqlite"
KEY_OPENAI = "your-openai-api-key"
```

Then run:

```bash
streamlit run judgement_biases_interview.py
```

## Configure the AI interview

Add `KEY_OPENAI` to Streamlit Secrets (the API key itself never belongs in Git):

```toml
KEY_OPENAI = "your-openai-api-key"
```

`OPENAI_API_KEY` is also accepted as a fallback. Existing Dropbox and access-code
settings remain the same. The implementation uses the standard OpenAI Python
SDK and [Responses API](https://developers.openai.com/api/docs/guides/text?lang=python).
The default model is `gpt-4.1-mini`. Change `model`, `max_output_tokens`, and
`additional_api_kwargs` in `bot_config.toml` if needed.

The opponent's role, tone, rules and private case guidance are passed through the
Responses API's higher-priority `instructions` parameter on every call. The
student's answer is passed separately as a `user` message; no additional system
message is required. Model settings are snapshotted per case when its first bot
request is claimed, so retries keep that case's original model and instructions.

The private instructions are editable in `bot_config.toml`. Text between triple
single quotes (`'''`) accepts ordinary line breaks and blank lines, so you can
edit paragraphs directly without writing escaped newline sequences.
`question_context_template` may use only `{question}` and `{wrong_position}`;
the loader rejects other placeholders.

The configurable instructions are:

- `interview_outline`, `general_instructions`, and `question_context_template`
  come from the **Draft AI interview instructions** chat. They define the
  authoritative opponent who consistently supports the supplied wrong position.
- `cases."1"` through `cases."10"` contain the ten full `wrong_position` texts
  from **Generate Heuristic Answers**. This is the only case-specific field
  copied into the model request.
- `correct_answer`, `correct_reasoning`, and `objection_guidance` remain
  instructor metadata. They are never copied into the saved bot snapshot or
  sent to OpenAI and therefore cannot steer the generated feedback.
- `reply_request_instruction` tells the AI to end with a question inviting a
  student comment of no more than 150 words. Preserve its `{max_words}` placeholder.
  The app enforces that limit for the student comment.

For each case the app enforces this sequence:

1. Save the original student answer (and, for the review, the original rating).
2. Call the model once for a specific objection; save its message before display.
3. Save the student's comment (required, maximum 150 words).
4. Open a separate final-answer page with the earlier conversation collapsed
   but available to expand. Prefill the editor with the original student answer.
5. Require a choice to keep that original answer or change it in the editor.
6. Permanently save the student's final answer, then move to the next case or
   completion. There is no AI synthesis or other model call on this page.

There is one model call per case in a successful run, two per full assignment.
Refreshes and already-saved turns do not trigger another call. A short saved
request lease prevents other tabs from generating the same reply concurrently.
If generation is interrupted, a retry becomes available after the lease expires
(default 120 seconds). Failed requests require an explicit retry. If a model
reply was generated but its save failed, the same browser retains it for a save
retry without another API call.

The app reuses the OpenAI client to keep HTTP connections warm. Each bot call
also records `kind`, `duration_seconds` (the API request time), and the returned
token `usage` in `bot_interviews.<stage>.calls`. Older saved calls may not have
a duration. For token totals, sum each call once across both cases; cached input
tokens are already included in `input_tokens`.

Dropbox saves reuse the revision from the latest download or successful upload.
Each browser session retains its last confirmed response across screen reruns,
including after the student spends several minutes writing. There is no timed
expiry that would force an extra download just before saving. Email lookup, a
new browser session, an explicit load, or a failed save reads Dropbox again.
Snapshots are isolated by browser, account and folder; store objects are not
retained across live code updates.

Every validated step still waits for its JSON save before continuing. Conditional
uploads reject concurrent changes even when a screen uses a cached snapshot, so
another tab cannot overwrite an answer or move backwards. A tab may show older
progress until it submits, refreshes the browser, or checks a pending bot reply.
A conflicting save clears its snapshot so the next rerun reads Dropbox.
Completion-only CSV merging remains in place.

Returning-email lookup downloads only the hashed index and response JSON. The
legacy-index setup marker is checked only for missing email entries. Registration
uses the revisions returned by successful uploads instead of downloading those
same files again; legacy recovery and concurrent registrations still read the
winning remote records when necessary.

The first email-entry screen makes no Dropbox or model requests. If that screen
itself takes several seconds to appear, investigate the deployment's cold start,
browser connection, and hosting separately from response-storage performance.

The app records elapsed answer-screen timings in the private response JSON's
`timings` object and exports them as individual CSV columns. For each case, these
are `main_first_reply_seconds`, `main_reply_to_feedback_seconds`,
`main_final_answer_seconds`, and the matching `review_...` columns. Each has `_started_at`, `_submitted_at`, and `_timing_basis`
columns as well. Timestamps are UTC; durations are seconds to three decimal places.

These are elapsed reading/thinking/writing times, not keystroke or active-tab
measurements. A timer starts when the server prepares that answer screen and ends
at a validated submission, before its save. It includes breaks, disconnected
time and a small amount of screen-transition overhead. AI generation time is
excluded from the student's comment timer and is recorded separately in bot-call
`duration_seconds`. Start times are persisted in the previous normal checkpoint,
so reconnecting does not reset them or require an extra Dropbox request.

For an older in-progress screen without a persisted start, timing begins in the
browser session after the update (`timing_basis = browser_session`). Other timings
use `server_checkpoint`. Already submitted answers have blank timing columns if
no time was recorded; the app does not reconstruct times retrospectively.

Prompts and case guidance are snapshotted for each case when its first bot call
is claimed, so an active case keeps its original instructions after a config
edit. New interviews use the edited configuration. The model receives the case,
the relevant question and student contributions, and private guidance; the app
does not automatically send the student's name or email.

The private Dropbox/local response JSON retains those snapshots for analysis.
Respondents are not offered a JSON download. CSV exports contain readable
columns, with one row per respondent. The answer CSV contains only `SubjectId`
for identity; name and email are confined to the separate identity log. It also
contains start/completion dates and completion status. Each case has these fields, with
`main_` or `review_` prefixes:

- `case_number`, `question`, `first_reply`
- `feedback`, `reply_to_feedback`
- `confirm_or_correct` (`confirm` or `correct`), `final_answer`

The review case also has `review_assessment` (the displayed assessment label,
rather than a choice code) and `review_chatgpt_response` (the supplied answer the
student was correcting). The question columns retain the displayed case/question
context and the review correction instruction, while the supplied ChatGPT answer
is separated into its own field. Saved transcript wording takes priority over
later edits to the assignment file. The timing columns described above remain.

Qualtrics question codes, duplicate answer aliases, transcript/interview JSON,
private prompts and API diagnostics are excluded from the CSV. Full private JSON
records continue to retain the original routing, dialogue and model metadata.
When the next completed response is merged into an older results CSV, recognised
legacy columns are converted to this layout. Earlier respondents and unrelated
custom columns are preserved. If a legacy transcript is malformed, the merge
stops with the existing CSV intact; the completed response JSON remains saved.

Existing responses keep submitted answers and completed interviews. Unfinished
responses waiting for the former synthesis step resume directly on the student
final-answer page without a model call. Any older AI proposal stays in the
saved history; the final editor starts from the student's original answer.
A response is complete only after the second student final answer is submitted,
at which point it is merged into Dropbox's `answers.csv`.

## Connect Dropbox

This connects **the deployed Python app directly to Dropbox**. Connecting Dropbox
to a chat assistant does not supply credentials to a Streamlit deployment.

1. Open the [Dropbox App Console](https://www.dropbox.com/developers/apps) and
   choose **Create app**. Choose **Scoped access**, then **App folder**. Give the
   app a unique name. App-folder access is sufficient; Full Dropbox access is
   unnecessary.
2. On **Permissions**, enable `files.content.read`, `files.content.write`, and
   `files.metadata.read`, then save/submit the changes **before authorizing**.
3. On **Settings**, copy the **App key** and **App secret**. Do not commit either
   credential or your refresh token to the repository.
4. On Windows, run the dependency-free PowerShell helper:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dropbox_oauth.ps1
   ```

   Alternatively, if Python and the project dependencies are installed, run:

   ```bash
   python scripts/dropbox_oauth.py
   ```

   Enter the app key and secret (the secret input is hidden). Open the printed
   authorization URL, approve access, and paste the returned authorization code
   into the terminal. The helper prints `DROPBOX_REFRESH_TOKEN` for your settings.
   Run this helper locally; credentials should not be pasted into a chat.
5. Fill in the `.streamlit/secrets.toml.example` template using your real values
   in `.streamlit/secrets.toml`, or use the same settings directly in the cloud:

   ```toml
   ASSIGNMENT_PASSWORD = "choose-your-assignment-password"
   STORAGE_BACKEND = "dropbox"
   DROPBOX_APP_KEY = "your-app-key"
   DROPBOX_APP_SECRET = "your-app-secret"
   DROPBOX_REFRESH_TOKEN = "your-refresh-token"
   DROPBOX_ROOT_PATH = "/choice_bias_assignment"
   DROPBOX_CSV_NAME = "answers.csv"
    DROPBOX_SUBJECT_CSV_NAME = "subjects.csv"
   ```

With an App folder app, the root above is **inside the app's dedicated Dropbox
folder**, usually `Apps/<your Dropbox app name>/choice_bias_assignment/`.
It is an API path, not a local computer path.

The app writes one response JSON per respondent, using a random response key. Every validated step updates that file. Dropbox is the authoritative
store in this mode, so a browser can resume after the Streamlit server restarts.
Conflicting updates from stale tabs are rejected. A failed save keeps the current
step open; there is no silent fallback to temporary local storage.
At registration, the app gives the subject a random, unique eight-digit
`SubjectId` and records `SubjectId`, name and email in `subjects.csv`. Updates to
that single identity log use Dropbox revisions, so concurrent registrations
cannot receive the same code. Once both answers and both AI exchanges, including
final decisions, are complete, the app merges one completed-response row into
`answers.csv` in the same Dropbox folder. It
creates that file if necessary, preserves existing rows and columns, and matches
rows by `SubjectId` to avoid duplicates when a respondent refreshes or retries.
Concurrent updates use Dropbox file revisions and are retried without dropping
other submissions. If the CSV update fails, the completed JSON remains saved and
the completion screen offers a retry. A marker in the JSON prevents further CSV
work on subsequent visits after a successful merge.

Set `DROPBOX_CSV_NAME` or `DROPBOX_SUBJECT_CSV_NAME` to use different filenames
in the configured folder. The two names must be different. The answer file's only
identity field is `SubjectId`; it never contains a name, email or internal
`ResponseId`. It also includes `main_case_number`, `review_case_number`, both
questions, feedback exchanges and final answers. On the next completed merge,
identifiable legacy rows are assigned codes and the old identity columns are
removed. JSON files remain the authoritative individual records. The manual
export command below is still available when a researcher explicitly requests a
combined export.

The helper requests offline authorization to obtain a refresh token; a manually
generated temporary access token is insufficient for this configuration. See
[Dropbox's offline OAuth guide](https://dropbox.tech/developers/using-oauth-2-0-with-offline-access)
and the [official Python SDK](https://dropbox-sdk-python.readthedocs.io/en/latest/api/dropbox.html).

## Keep the deployed app awake

The **Keep Streamlit awake** GitHub Action runs every six hours, at 00:17, 06:17,
12:17 and 18:17 UTC. It uses Chromium to visit
https://choice-biases-assignment.streamlit.app/, clicks **Yes, get this app back up!** only
when the app is sleeping, and verifies that the email field and **Confirm email**
button appear. It leaves the connection open briefly, then closes it. It never
fills or submits the email form, enters a survey, or calls the AI. It runs on
GitHub, so the owner's computer can be off.

Open the repository's **Actions → Keep Streamlit awake → Run workflow** to test
it immediately. Failed runs are reported as failed jobs; the script does not
mistake an HTTP response, login wall or loading page for a ready app. No app
reboot or empty commit is used, so active respondents are not interrupted.

The schedule is in `.github/workflows/streamlit-keepalive.yml`; the browser check
is `scripts/wake_streamlit.py`. It installs only its own Playwright dependency,
not the survey's Dropbox/OpenAI dependencies. The workflow has read-only
repository permissions and does not receive the assignment password, Dropbox
credentials or OpenAI key. To change the deployed address, set a repository
Actions **variable** named `STREAMLIT_APP_URL` to its root Streamlit URL.

If the Streamlit app requires a viewer login, an anonymous GitHub browser cannot
wake it. The job will report that instead of changing app privacy. For that
case, `STREAMLIT_BROWSER_STATE` can be supplied as an encrypted repository Actions
**secret**, containing Playwright storage-state JSON for a dedicated, authorised
viewer session. Treat it as a login credential, keep it out of Git, and renew it
if it expires. Leave it unset for an app that is accessible without Streamlit
viewer authentication. The survey's own access code is not needed.

This is a browser-visit workaround for Community Cloud hibernation, not an uptime
guarantee. GitHub scheduled runs can be delayed or dropped; Actions must remain
enabled and have runner usage available. The workflow runs on the default branch.
See [Streamlit's hibernation documentation](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app#app-hibernation)
and [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Deploy on Streamlit Community Cloud

1. Sign in at [Streamlit Community Cloud](https://share.streamlit.io/) with the
   GitHub account that can access this repository. Authorize access to this
   private repository if requested.
2. Choose **Create app** / **Deploy an app** and select:
   - Repository: your new choice-biases assignment repository
   - Branch: `main`
   - Main file: `judgement_biases_interview.py`
3. In **Advanced settings**, choose Python 3.11 or 3.12 and paste your complete
   real TOML settings from the Dropbox section into **Secrets**.
4. Deploy. The service installs `requirements.txt` automatically. For later
   changes, update Secrets in the app's settings and restart if needed.
5. Complete a test interview. Confirm a JSON response file appears in Dropbox,
   with `stage` equal to `complete`, `csv_synced` equal to `true`, and both AI exchanges and final-answer
   fields saved. Confirm exactly one row for that `SubjectId` in `answers.csv`
   and one matching identity row in `subjects.csv`. Reboot the app
   and reopen the private resume URL to check that the completed response stays
   completed. Only then distribute the assignment URL.

Repository privacy and app viewer access are separate settings: configure the
app's sharing/viewer settings for the students who will use it. The shared
access code is required for first registration. Returning participants confirm
only their email; the form does not verify mailbox ownership. The same email
always resumes its existing response, including a
completed one. A person using a different email is treated as a new respondent.
Local SQLite is available for development; select Dropbox
for cloud deployment.
No storage credentials or actual student results belong in GitHub.

Official instructions: [deploy an app](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
and [manage secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management).

## After deploying a code update

If the Cloud log reports `DropboxStore` has no attribute `resume_by_email`,
the entry page and an imported storage module are from different code versions.
The email lookup adapter accepts the older Dropbox object without reloading
shared modules. A full **Manage app → ⋮ → Reboot app** also clears imported
modules from memory; simply refreshing the browser does not restart the server.
See [Streamlit's reboot instructions](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/reboot-your-app).

Deleting response JSON files cannot remove a Python method. A separate message,
"The saved response for this email could not be found", means an email lookup
still points to a deleted response. The app does not silently replace that saved
response. A fully cleaned test folder containing only `answers.csv` and the
`_email_index/ready.json` marker accepts new registrations normally.

## Resume after disconnecting

Return to the ordinary app URL, enter the same email address, and click
**Confirm email**. Returning participants do not need their name or access code.
The same email (ignoring letter case and surrounding spaces) opens the saved
response with the same cases and next unanswered step. A completed response
opens its completion receipt. There is no need to keep the private resume link.

For an email with no existing registration, the next page asks for the name
and access code. Merely confirming a new email does not create a survey or
allocate cases. The second page lets the participant change the email before
registering. A lookup failure stays on the email page rather than starting over.

Dropbox keeps a private, hashed email lookup in `_email_index/` inside the
configured folder. On the first use after this update it indexes existing JSON
responses that already contain an email, preserving their original response IDs
and private links. If there are older duplicate responses for one email, it
selects the furthest progressed record. SQLite maintains an equivalent index in
its database. An interrupted registration recovers its saved case allocation.

Saved answers and case assignments are immutable in the storage layer. Browser
Back, an old link, or a stale browser tab cannot reopen an earlier answer for
editing. Only submitted steps are recovered; text that was never submitted may
need to be entered again after a disconnect. The completion-only `answers.csv`
merge and its duplicate protection continue to work as before.

## Export responses

With the same secrets configured locally:

```bash
mkdir -p exports
python judgement_biases_interview.py --export-csv exports/responses.csv
```

This reads **all** responses from the configured backend, including partial
responses. Filter `Finished == 1` for completed submissions. The Dropbox mode
reads remote JSON files, including paginated folders; it does not depend on a
local database. The `exports/` folder is ignored by Git.

## Verify

```bash
python -m unittest -v
```

Tests check all 90 ordered case pairings and all rating values, the AI dialogue protocol,
API request construction, the 150-word comment limit, final submission and locking,
private-guidance redaction, retry/recovery, original-source wording,
required fields, immutable answers, email-based local and Dropbox resume,
concurrent email registration, legacy backfill, stale-update conflicts,
remote save failures, completion-only CSV merging, concurrent submissions,
duplicate-free retries, pagination, and Streamlit interactions. Model calls and Dropbox are mocked in the tests; no paid API calls are made.
Live authorization and model behavior must be checked using the deployment
smoke test above. Original-source comparisons skip when the attachments are
unavailable; set `QSF_SOURCE` and `DOCX_SOURCE` to supply them.

See [RUNNING.txt](RUNNING.txt) for the original flow and export details.
