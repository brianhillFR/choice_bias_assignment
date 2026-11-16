"""Streamlit choice-biases assignment with one AI comment and a locked final answer for each case.

Run: streamlit run judgement_biases_interview.py
Export: python judgement_biases_interview.py --export-csv responses.csv
Dependencies are listed in requirements.txt. Case texts and interface labels
are loaded from assignment_content.toml beside this script.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import time

import ai_interview as ai
import answer_timing as timing
import response_export
import csv
import hmac
import io
import json
import os
from pathlib import Path
import random
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    import tomli as tomllib

from dropbox_store import StorageError, StaleResponse, configured_store, setting
from email_lookup import resume_existing_response
from response_identity import (attach_identity, check_update, email_key, new_subject_id,
                               progress_key, response_key, resume_token, valid_subject_id)

# Source content, not executable instructions. Never sent to a language model.
# Survey text lives beside this script; paths do not depend on the working directory.
CONTENT_PATH = Path(__file__).resolve().with_name("assignment_content.toml")


def load_content(path=CONTENT_PATH):
    """Read editable UTF-8 text as data; never execute content-file instructions."""
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


CONTENT = load_content()
UI = CONTENT["ui"]

# Choice-assignment routing from Choice_assignment.qsf. Each branch draws one
# review from the other nine cases. The final fixed question in each source
# block asks who wrote the response; the copied JBA workflow intentionally
# omits that additional Qualtrics-only question.
CHOICE_ROUTING_SPECS = {
    1: ('BL_9Nf3KBoBZlTqZM2', 'QID339', [(328, 10), (329, 9), (331, 8), (333, 6), (334, 5), (335, 4), (336, 3), (337, 2), (332, 7)]),
    2: ('BL_0Nw14zTcNKfNTYa', 'QID496', [(486, 10), (487, 9), (488, 8), (490, 6), (491, 5), (492, 4), (493, 3), (489, 7), (495, 1)]),
    3: ('BL_eu1mc9wiNSZwTSm', 'QID508', [(498, 10), (499, 9), (500, 8), (502, 6), (503, 5), (504, 4), (501, 7), (507, 1), (506, 2)]),
    4: ('BL_6Y8fotboSWYjS8C', 'QID520', [(510, 10), (511, 9), (512, 8), (514, 6), (515, 5), (513, 7), (519, 1), (518, 2), (517, 3)]),
    5: ('BL_8zW1a9gtBJPiHsy', 'QID532', [(522, 10), (523, 9), (524, 8), (526, 6), (525, 7), (531, 1), (530, 2), (529, 3), (528, 4)]),
    6: ('BL_3IdaVBdCpJlaGW2', 'QID544', [(534, 10), (535, 9), (536, 8), (537, 7), (543, 1), (542, 2), (541, 3), (540, 4), (539, 5)]),
    7: ('BL_6GcbCoLu7kFdt1Y', 'QID556', [(546, 10), (547, 9), (548, 8), (555, 1), (554, 2), (553, 3), (552, 4), (551, 5), (550, 6)]),
    8: ('BL_cvgmM1WqI0DSUIe', 'QID568', [(558, 10), (559, 9), (567, 1), (566, 2), (565, 3), (564, 4), (563, 5), (562, 6), (561, 7)]),
    9: ('BL_bkDYEGLGCu25UA6', 'QID580', [(570, 10), (579, 1), (578, 2), (577, 3), (576, 4), (575, 5), (574, 6), (573, 7), (572, 8)]),
    10: ('BL_8ce0UHiJgXRKbSm', 'QID592', [(591, 1), (590, 2), (589, 3), (588, 4), (587, 5), (586, 6), (585, 7), (584, 8), (583, 9)]),
}

ROUTING = {
    first: {
        'block_id': block_id,
        'text_qid': text_qid,
        'text_tag': text_qid.replace('QID', 'Q'),
        'reviews': [
            {'qid': f'QID{qid}', 'tag': f'Q{qid}', 'case': case,
             'choice_order': ['4', '5', '6']}
            for qid, case in reviews
        ],
    }
    for first, (block_id, text_qid, reviews) in CHOICE_ROUTING_SPECS.items()
}

# Preserve the existing flow/export interface while sourcing every stimulus externally.
DATA = {
    "source_sha256": '045f97f6ae5a9129e484353ac9ed926dcd196900086b30229d07b75e7ef5a918',
    "intro": CONTENT["intro"],
    "ans1_prompt": CONTENT["ans1_prompt"],
    "cases": {int(key): value for key, value in CONTENT["cases"].items()},
    "review_texts": {int(key): value for key, value in CONTENT["review_sections"].items()},
    "branches": {
        first: {
            **branch,
            "text_prompt": CONTENT["ans2_prompt"],
            "reviews": [
                {**review, "choices": CONTENT["rating_choices"]}
                for review in branch["reviews"]
            ],
        }
        for first, branch in ROUTING.items()
    },
}

STAGES = ("identity", "instructions", "ans1", "probe1", "ans2", "probe2", "complete")


class PlainText(HTMLParser):
    """Keep source wording and paragraph boundaries without executing source HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(source):
    import re
    parser = PlainText()
    parser.feed(source)
    text = "".join(parser.parts).replace("\xa0", " ")
    return re.sub(r"\n\s*\n(?:\s*\n)+", "\n\n", text).strip()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def create_assignment(rng=None):
    """Uniform first case; each of the other nine has exactly a 1/9 review chance."""
    rng = rng or random.SystemRandom()
    first = rng.choice(sorted(DATA["cases"]))
    eligible_cases = sorted(set(DATA["cases"]) - {first})
    second = rng.choice(eligible_cases)
    review = next(q for q in DATA["branches"][first]["reviews"] if q["case"] == second)
    return {"answerqn": first, "review_qid": review["qid"],
            "second_case": second, "review_case": second}


def selected_review(state):
    branch = DATA["branches"][state["assignment"]["answerqn"]]
    return next(q for q in branch["reviews"] if q["qid"] == state["assignment"]["review_qid"])


def new_state(assignment=None):
    return {
        "subject_id": "",
        "response_id": "R_" + secrets.token_hex(12),
        "assignment": assignment or create_assignment(),
        "source_sha256": DATA["source_sha256"],
        "stage": "identity", "revision": 0,
        "started_at": utc_now(), "completed_at": None,
        "answers": {}, "transcript": [], "bot_interviews": {}, "timings": {},
    }


def validate_email(email):
    email = email.strip().lower() if isinstance(email, str) else ""
    local, separator, domain = email.rpartition("@")
    labels = domain.split(".")
    if (not separator or len(email) > 254 or len(local) > 64
            or not re.fullmatch(r"[a-z0-9!#$%&'*+/=?^_`{|}~.\-]+", local)
            or local.startswith(".") or local.endswith(".") or ".." in local
            or len(labels) < 2
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?", label) for label in labels)):
        raise ValueError(UI["email_invalid"])
    return email


def validate_identity(name, email):
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise ValueError(UI["name_required"])
    return {"name": name, "email": validate_email(email)}


def case_prompt_parts(state, review=False):
    """Shared display/transcript order; all wording remains in the content file."""
    number = selected_review(state)["case"] if review else state["assignment"]["answerqn"]
    screens = CONTENT["screens"]
    parts = []
    if not review and screens["ans1_opening"] in plain_text(DATA["ans1_prompt"]):
        parts.append(screens["ans1_opening"])
    parts.extend([
        screens["ans2_case_lead" if review else "ans1_case_lead"].format(number=number),
        screens["case_reminder"],
        DATA["cases"][number],
        screens["ans2_question_lead" if review else "ans1_question_lead"],
        plain_text(CONTENT["case_question"]),
    ])
    if review:
        parts.append(plain_text(CONTENT["review_sections"][str(number)]))
    return parts


def quotation_markdown(text):
    """Indent every paragraph and wrap the passage in one pair of quotes."""
    text = text.strip()
    if len(text) >= 2 and (text[0], text[-1]) in (("\"", "\""), ("“", "”")):
        text = text[1:-1]
    return "\n".join("> " + line for line in ("“" + text + "”").splitlines())


def render_review_text(st, source):
    """The source's outer quotes delimit the supplied ChatGPT response.

    Leave its introduction and the student's assessment question outside the
    quotation. Inner quotes, such as names of biases, stay within the passage.
    """
    text = plain_text(source)
    passage = re.fullmatch(r'(.*?)["“](.*)["”](.*)', text, flags=re.DOTALL)
    if passage is None:
        st.write(text)
        return
    introduction, response, question = passage.groups()
    if introduction.strip():
        st.write(introduction.strip())
    st.markdown(quotation_markdown(response))
    if question.strip():
        st.write(question.strip())


def render_case_prompt(st, state, review=False):
    number = selected_review(state)["case"] if review else state["assignment"]["answerqn"]
    st.subheader(UI["question_case_title"].format(question_number=2 if review else 1, number=number))
    parts = case_prompt_parts(state, review)
    for index, part in enumerate(parts):
        if part == DATA["cases"][number]:
            st.markdown(quotation_markdown(part))
        elif review and index == len(parts) - 1:
            render_review_text(st, CONTENT["review_sections"][str(number)])
        else:
            st.write(part)


def render_instructions(st, show_booklet=True):
    st.subheader(UI["instructions_title"])
    for key in ("task_instructions", "word_limit_instructions", "grading_instructions", "interview_instructions"):
        st.write(UI[key])
    if show_booklet:
        with st.expander(UI["cases_title"]):
            show_cases(st)


def advance(state, stage, answer, rating=None):
    # Record only validated submissions, before waiting for their normal save.
    updated = _advance(state, stage, answer, rating)
    return timing.finish_step(state, updated, utc_now())


def _advance(state, stage, answer, rating=None):
    """Validate and return a new state; previously committed steps cannot change."""
    if state["stage"] != stage or stage == "complete":
        raise ValueError("This step has already been submitted. Reload to continue.")
    if stage in ("identity", "instructions"):
        result = json.loads(json.dumps(state))
        if stage == "identity":
            identity = validate_identity(answer.get("name"), answer.get("email"))
            result["answers"].update(identity)
            result["stage"] = "instructions"
        else:
            result["stage"] = result.pop("resume_stage", "ans1")
        result["revision"] += 1
        return result
    if stage in ai.PROBES:
        return ai.record_student(state, stage, answer)
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Please enter an answer before continuing.")
    result = json.loads(json.dumps(state))
    answers = result["answers"]
    first = state["assignment"]["answerqn"]
    if stage == "ans1":
        answers["Ans1"] = answer
        prompt = "\n\n".join(case_prompt_parts(state))
    elif stage == "ans2":
        review = selected_review(state)
        if rating not in review["choice_order"]:
            raise ValueError("Please select an assessment of the supplied response.")
        branch = DATA["branches"][first]
        answers[review["tag"]] = rating
        answers[branch["text_tag"]] = answer
        answers["Ans2"] = answer  # Added stable alias across the six branches.
        result["transcript"].extend([
            {"role": "assistant", "step": "rating",
             "content": "\n\n".join(case_prompt_parts(state, review=True)), "time": utc_now()},
            {"role": "user", "step": "rating", "qid": review["qid"],
             "content": plain_text(review["choices"][rating]["Display"]), "time": utc_now()},
        ])
        prompt = branch["text_prompt"]
    result["transcript"].extend([
        {"role": "assistant", "step": stage, "content": prompt, "time": utc_now()},
        {"role": "user", "step": stage, "content": answer, "time": utc_now()},
    ])
    result["stage"] = STAGES[STAGES.index(stage) + 1]
    result["revision"] += 1
    if result["stage"] == "complete":
        result["completed_at"] = utc_now()
    return result


class Store:
    """SQLite commits survive browser refresh; revision checks reject stale tabs."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS responses "
                       "(token_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS participants "
                       "(email_hash TEXT PRIMARY KEY, response_key TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS subject_log "
                       "(subject_id TEXT PRIMARY KEY, email_hash TEXT UNIQUE NOT NULL, "
                       "name TEXT NOT NULL, email TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _subject_id(db, identity, preferred=''):
        """Find or atomically allocate the identity log's eight-digit code."""
        hashed = email_key(identity['email'])
        existing = db.execute(
            "SELECT subject_id FROM subject_log WHERE email_hash = ?", (hashed,)
        ).fetchone()
        if existing:
            if preferred and preferred != existing[0]:
                raise StorageError("The saved subject ID conflicts with the identity log.")
            return existing[0]
        if preferred and not valid_subject_id(preferred):
            raise StorageError("The saved subject ID must contain exactly eight digits.")
        while True:
            candidate = preferred or new_subject_id()
            try:
                db.execute("INSERT INTO subject_log VALUES (?, ?, ?, ?)",
                           (candidate, hashed, identity['name'], identity['email']))
                return candidate
            except sqlite3.IntegrityError:
                if preferred:
                    raise StorageError("The saved subject ID is already assigned to another subject.")

    @staticmethod
    def token_hash(token):
        return response_key(token)

    def resume_by_email(self, email):
        """Look up an existing registration without creating a new response."""
        key = email_key(email)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            indexed = db.execute("SELECT response_key FROM participants WHERE email_hash = ?", (key,)).fetchone()
            if indexed:
                target = indexed[0]
                row = db.execute("SELECT payload FROM responses WHERE token_hash = ?", (target,)).fetchone()
                if row is None:
                    raise StorageError("The saved response for this email could not be found.")
                state = json.loads(row[0])
            else:
                candidates = []
                for target, payload in db.execute("SELECT token_hash, payload FROM responses"):
                    candidate = json.loads(payload)
                    saved_email = candidate["answers"].get("email")
                    if saved_email and email_key(saved_email) == key:
                        candidates.append((target, candidate))
                if not candidates:
                    return None
                target, state = max(candidates, key=lambda item: progress_key(item[1]))
                db.execute("INSERT INTO participants VALUES (?, ?)", (key, target))
            if email_key(state['answers'].get('email', '')) != key:
                raise StorageError("The saved email lookup does not match this response.")
            if not state['answers'].get('name') or state['stage'] == 'identity':
                return None
            identity = {'name': state['answers']['name'], 'email': state['answers']['email']}
            identity['subject_id'] = self._subject_id(db, identity, state.get('subject_id', ''))
            updated = attach_identity(state, identity)
            if updated != state:
                check_update(state, updated)
                db.execute("UPDATE responses SET payload = ?, revision = ? WHERE token_hash = ?",
                           (json.dumps(updated, ensure_ascii=False), updated['revision'], target))
                state = updated
        return resume_token(target), state

    def start_or_resume(self, identity, token_hint=None, known_missing=False):
        """Atomically bind an email to one response, including legacy saved data."""
        key = email_key(identity["email"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            indexed = db.execute("SELECT response_key FROM participants WHERE email_hash = ?", (key,)).fetchone()
            candidates = []
            if indexed:
                row = db.execute("SELECT payload FROM responses WHERE token_hash = ?", indexed).fetchone()
                if row is None:
                    raise StorageError("The saved response for this email could not be found.")
                target, state = indexed[0], json.loads(row[0])
            else:
                for target, payload in db.execute("SELECT token_hash, payload FROM responses"):
                    candidate = json.loads(payload)
                    email = candidate["answers"].get("email")
                    if email and email_key(email) == key:
                        candidates.append((target, candidate))
                if candidates:
                    target, state = max(candidates, key=lambda item: progress_key(item[1]))
                else:
                    hint = None
                    if token_hint:
                        hint = db.execute("SELECT payload FROM responses WHERE token_hash = ?", (response_key(token_hint),)).fetchone()
                    if hint and not json.loads(hint[0])["answers"].get("email"):
                        target, state = response_key(token_hint), json.loads(hint[0])
                    else:
                        target, state = response_key(secrets.token_urlsafe(32)), new_state()
                        db.execute("INSERT INTO responses VALUES (?, ?, ?)",
                                   (target, json.dumps(state, ensure_ascii=False), state["revision"]))
                db.execute("INSERT INTO participants VALUES (?, ?)", (key, target))
            identified = dict(identity)
            identified['subject_id'] = self._subject_id(db, identity, state.get('subject_id', ''))
            updated = attach_identity(state, identified)
            if updated != state:
                check_update(state, updated)
                db.execute("UPDATE responses SET payload = ?, revision = ? WHERE token_hash = ?",
                           (json.dumps(updated, ensure_ascii=False), updated["revision"], target))
        return resume_token(target), updated

    def start_known_missing(self, identity, token_hint=None):
        return self.start_or_resume(identity, token_hint=token_hint, known_missing=True)

    def create(self):
        token = secrets.token_urlsafe(32)
        state = new_state()
        with self.connect() as db:
            db.execute("INSERT INTO responses VALUES (?, ?, ?)",
                       (self.token_hash(token), json.dumps(state, ensure_ascii=False), 0))
        return token, state

    def load(self, token):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM responses WHERE token_hash = ?",
                             (self.token_hash(token),)).fetchone()
        return json.loads(row[0]) if row else None

    def commit(self, token, previous, updated):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM responses WHERE token_hash = ?", (self.token_hash(token),)).fetchone()
            if row is None or json.loads(row[0])["revision"] != previous["revision"]:
                raise StaleResponse("This response was updated in another tab. Reload to continue.")
            check_update(json.loads(row[0]), updated)
            cursor = db.execute("UPDATE responses SET payload = ?, revision = ? "
                                "WHERE token_hash = ? AND revision = ?",
                                (json.dumps(updated, ensure_ascii=False), updated["revision"],
                                 self.token_hash(token), previous["revision"]))
            if cursor.rowcount != 1:
                raise StaleResponse("This response was updated in another tab. Reload to continue.")

    def all_responses(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM responses ORDER BY rowid")]


def database_path():
    return os.environ.get("JUDGEMENT_DB", str(Path(__file__).resolve().parent / "responses" / "assignment.sqlite3"))


def response_store():
    backend = str(setting("STORAGE_BACKEND", "sqlite")).lower()
    if backend == "dropbox":
        return configured_store(new_state)
    if backend == "sqlite":
        return Store(database_path())
    raise StorageError("STORAGE_BACKEND must be sqlite or dropbox.")


def export_row(state):
    """Readable columns for each case; historical question wording is retained."""
    review = selected_review(state)
    choice = state["answers"].get(review["tag"])
    current_label = plain_text(review["choices"][choice]["Display"]) if choice else ""
    assessment = next((message["content"] for message in state.get("transcript", [])
                       if message.get("role") == "user" and message.get("step") == "rating"), current_label)
    _, supplied = response_export.split_review_prompt(plain_text(CONTENT["review_sections"][str(review["case"])]))
    return response_export.export_response(
        state, main_question="\n\n".join(case_prompt_parts(state)),
        review_question="\n\n".join(case_prompt_parts(state, review=True)),
        assessment=assessment, supplied_response=supplied,
    )


def csv_export(states):
    # Header is stable even for an empty database or partial responses.
    rows = [export_row(state) for state in states]
    header = list(export_row(new_state()).keys())
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=header)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def bot_context(state, stage):
    main_step = "ans1" if stage == "probe1" else "rating"
    question = next((message["content"] for message in state["transcript"]
                     if message["role"] == "assistant" and message["step"] == main_step),
                    "\n\n".join(case_prompt_parts(state, review=stage == "probe2")))
    context = {"question": question, "original_answer": state["answers"]["Ans1" if stage == "probe1" else "Ans2"]}
    if stage == "probe2":
        review = selected_review(state)
        context["review_assessment"] = plain_text(review["choices"][state["answers"][review["tag"]]]["Display"])
    return context


def render_saved_case(st, state, stage):
    is_first = stage == "probe1"
    case = state["assignment"]["answerqn" if is_first else "second_case"]
    st.markdown(quotation_markdown(DATA["cases"][case]))
    if not is_first:
        render_review_text(st, DATA["review_texts"][case])
        review = selected_review(state)
        st.write(UI['assessment_prefix'] + plain_text(review["choices"][state["answers"][review["tag"]]]["Display"]))
    st.write(UI["previous_answer_label"])
    st.write(state["answers"]["Ans1" if is_first else "Ans2"])


def render_interview_messages(st, state, stage):
    for message in state.get("bot_interviews", {}).get(stage, {}).get("messages", []):
        avatar = UI["feedback_avatar"] if message["role"] == "assistant" else None
        with st.chat_message(message["role"], avatar=avatar):
            if message["role"] == "assistant":
                st.markdown(quotation_markdown(message["content"]))
            else:
                st.write(message["content"])


def generate_bot_turn(st, store, token, state, stage):
    """Claim before calling; save before showing; reuse an unsaved API result."""
    current = ai.phase(state, stage)
    record = state.get("bot_interviews", {}).get(stage, {})
    cache = st.session_state.setdefault("unsaved_bot_results", {})
    request_id = record.get("pending", {}).get("id")
    result = cache.get(request_id)
    owned = st.session_state.setdefault("owned_bot_requests", {})
    owns_pending = bool(request_id and owned.get(stage) == request_id)
    if current == "generation_failed":
        st.error(UI["bot_retry_message"])
        if not st.button(UI["bot_retry_button"], key=stage + "_retry_bot"):
            return
    elif current.endswith("_generating") and result is None and not owns_pending:
        if record["pending"]["expires_at"] > time.time():
            st.info(UI["bot_busy_message"])
            if st.button(UI["bot_check_button"], key=stage + "_check_bot"):
                st.rerun()
            return
        if not st.button(UI["bot_retry_button"], key=stage + "_retry_bot"):
            st.info(UI["bot_interrupted_message"])
            return
    try:
        if result is None:
            if not owns_pending:
                claimed = ai.claim_generation(state, stage, bot_context(state, stage))
                store.commit(token, state, claimed)
                state = claimed
                record = state["bot_interviews"][stage]
                request_id = record["pending"]["id"]
            try:
                with st.spinner(UI["bot_thinking_message"]):
                    result = ai.generate(record)
            except ai.BotError:
                failed = ai.generation_failed(state, stage, request_id)
                store.commit(token, state, failed)
                owned.pop(stage, None)
                st.error(UI["bot_retry_message"])
                if st.button(UI["bot_retry_button"], key=stage + "_retry_after_error"):
                    st.rerun()
                return
            cache[request_id] = result
        updated = ai.apply_generation(state, stage, request_id, result)
        store.commit(token, state, updated)
    except (ValueError, ai.BotError, StorageError) as error:
        st.error(str(error))
        if st.button(UI["bot_check_button"], key=stage + "_retry_save_bot"):
            st.rerun()
        return
    cache.pop(request_id, None)
    owned.pop(stage, None)
    st.rerun()


def prepare_submission(state, stage, answer, rating=None):
    """Combine an answer and its bot lease into one durable revision."""
    updated = advance(state, stage, answer, rating)
    request_id = None
    if updated['stage'] in ai.PROBES and ai.phase(updated, updated['stage']) == 'challenge_pending':
        updated = ai.claim_generation(updated, updated['stage'], bot_context(updated, updated['stage']))
        # Both changes are committed atomically, so together they are one
        # storage revision rather than two consecutive Dropbox uploads.
        updated['revision'] = state['revision'] + 1
        request_id = updated['bot_interviews'][updated['stage']]['pending']['id']
    return updated, request_id


def run_app():
    import streamlit as st
    st.set_page_config(page_title=UI['title'], page_icon="💬")
    st.title(UI['title'])

    token = st.query_params.get("response")

    def browser_store():
        store = response_store()
        # Never cache a store object globally or retain its class across updates.
        # Only confirmed response snapshots belong to this browser session.
        if callable(getattr(store, "use_session_cache", None)):
            store.use_session_cache(st.session_state.setdefault("dropbox_response_cache", {}))
        return store

    def open_response(response_token):
        # Entry is complete: clear unsaved widgets before opening this response.
        for key in ("ans1_draft", "ans2_draft", "probe1_unsaved", "probe2_unsaved",
                    "probe1_final_answer", "probe2_final_answer", "unsaved_bot_results",
                    "owned_bot_requests"):
            st.session_state.pop(key, None)
        st.session_state.pop("registration_email", None)
        st.session_state["authenticated_response"] = response_key(response_token)
        st.query_params["response"] = response_token
        st.rerun()

    active_key = st.session_state.get("authenticated_response")
    if not token or active_key != response_key(token):
        email = st.session_state.get("registration_email")
        if email is None:
            st.subheader(UI["email_entry_title"])
            st.write(UI["email_entry_instructions"])
            with st.form("email_entry_form"):
                entered_email = st.text_input(UI["email_label"], key="participant_email")
                submitted = st.form_submit_button(UI["email_confirm_button"], key="confirm_email", type="primary")
            if submitted:
                try:
                    email = validate_email(entered_email)
                    existing = resume_existing_response(browser_store(), email)
                except (ValueError, StorageError) as error:
                    st.error(str(error))
                else:
                    if existing is not None:
                        open_response(existing[0])
                    st.session_state["registration_email"] = email
                    st.rerun()
            return

        st.subheader(UI["identity_title"])
        st.caption(UI["confirmed_email"].format(email=email))
        password = setting("ASSIGNMENT_PASSWORD")
        if password is None:
            st.error(UI["password_setup_message"])
        else:
            with st.form("registration_form"):
                name = st.text_input(UI["name_label"], key="participant_name")
                entered = st.text_input(UI["password_label"], type="password", key="entry_code")
                submitted = st.form_submit_button(UI["entry_continue"], key="register_continue", type="primary")
            if submitted:
                try:
                    identity = validate_identity(name, email)
                    if not hmac.compare_digest(entered.encode(), str(password).encode()):
                        raise ValueError(UI["incorrect_code"])
                    store = browser_store()
                    start_known_missing = getattr(store, "start_known_missing", None)
                    if callable(start_known_missing):
                        token, state = start_known_missing(identity, token_hint=token)
                    else:  # A pre-update store object can finish after a hot deploy.
                        token, state = store.start_or_resume(identity, token_hint=token)
                except (ValueError, StorageError) as error:
                    st.error(str(error))
                else:
                    open_response(token)
        if st.button(UI["change_email_button"], key="change_email"):
            for key in ("registration_email", "participant_name", "entry_code"):
                st.session_state.pop(key, None)
            st.rerun()
        return

    try:
        store = browser_store()
        trusted_loader = getattr(store, "load_for_screen_trusted", None)
        if callable(trusted_loader):
            trusted = set(st.session_state.get("owned_bot_requests", {}).values())
            state = trusted_loader(token, trusted_generation_ids=trusted)
        else:
            state = getattr(store, "load_for_screen", store.load)(token)
    except StorageError as error:
        st.error(str(error))
        return
    if state is None:
        st.session_state.pop("authenticated_response", None)
        st.query_params.clear()
        st.rerun()

    # Only confirmed snapshots are displayed; conditional saves prevent rewinds.
    if state["source_sha256"] != DATA["source_sha256"]:
        st.error("This response belongs to a different assignment version. Please contact the instructor.")
        st.stop()
    stage = state["stage"]
    first = state["assignment"]["answerqn"]
    second = state["assignment"]["second_case"]
    st.caption(UI['resume_caption'])

    # Older responses have no checkpoint timer for the current page. Keep one
    # in this browser until its next ordinary save, without a new Dropbox call.
    timing_step = timing.step_key(state)
    if timing_step and state.get("current_timing", {}).get("step") != timing_step:
        starts = st.session_state.setdefault("answer_timing_starts", {})
        clock_key = response_key(token) + ":" + timing_step
        started_at = starts.setdefault(clock_key, utc_now())
        timing.start_step(state, started_at, basis="browser_session")

    def submit(answer, rating=None):
        try:
            updated, owned_request = prepare_submission(state, stage, answer, rating)
            store.commit(token, state, updated)
            if owned_request:
                st.session_state.setdefault("owned_bot_requests", {})[updated['stage']] = owned_request
        except (ValueError, StorageError) as exc:
            st.error(str(exc))
        else:
            st.rerun()

    if stage == "instructions":
        st.write(UI["instructions_again"])
        render_instructions(st)
        if st.button(UI["begin_button"], type="primary"):
            submit(None)
        return

    if stage != "complete":
        with st.expander(UI["instructions_review"]):
            render_instructions(st, show_booklet=False)

    if stage == "ans1":
        with st.chat_message("assistant", avatar=UI["feedback_avatar"]):
            render_case_prompt(st, state)
        with st.form("ans1_form"):
            answer = st.text_area(UI['answer_label'], height=240, key="ans1_draft")
            st.caption(UI['word_limit_caption'])
            if st.form_submit_button(UI['continue_button'], type="primary"):
                submit(answer)
    elif stage in ai.PROBES:
        case = first if stage == "probe1" else second
        original = state["answers"]["Ans1" if stage == "probe1" else "Ans2"]
        current = ai.phase(state, stage)
        if current == "confirm":
            st.subheader(UI["final_answer_title"].format(question_number=1 if stage == "probe1" else 2, number=case))
            with st.expander(UI["conversation_review"], expanded=False):
                render_saved_case(st, state, stage)
                render_interview_messages(st, state, stage)
            st.write(UI["final_answer_instructions"])
            with st.form(stage + "_final_form"):
                action = st.radio(UI["bot_confirmation_label"], ("confirm", "correct"), index=None,
                                  format_func=lambda value: UI["bot_confirm_option" if value == "confirm" else "bot_correct_option"])
                final = st.text_area(UI["final_answer_label"], value=original, height=260,
                                     key=stage + "_final_answer")
                st.caption(UI["word_limit_caption"])
                st.caption(UI["final_answer_locked_notice"])
                if st.form_submit_button(UI["bot_final_submit"], type="primary"):
                    submit({"action": action, "final_answer": final})
            return
        st.subheader(UI["question_case_title"].format(question_number=1 if stage == "probe1" else 2, number=case))
        with st.expander(UI['saved_answer_title']):
            render_saved_case(st, state, stage)
        render_interview_messages(st, state, stage)
        if current in ("challenge_pending", "generation_failed") or current == "challenge_generating":
            generate_bot_turn(st, store, token, state, stage)
            return
        if current == "reply":
            st.write(UI["bot_reply_invitation"].format(max_words=ai.COMMENT_MAX_WORDS))
            pending_key = stage + "_unsaved"
            if st.session_state.get(pending_key):
                st.info(UI["unsaved_reason_message"])
                st.code(st.session_state[pending_key], language=None)
                if st.button(UI["retry_save_button"], key=stage + "_retry"):
                    submit(st.session_state[pending_key])
            answer = st.chat_input(UI["bot_reply_placeholder"], key=stage + "_input")
            if answer is not None:
                st.session_state[pending_key] = answer
                submit(answer)
                st.code(answer, language=None)
    elif stage == "ans2":
        review = selected_review(state)
        branch = DATA["branches"][first]
        with st.chat_message("assistant", avatar=UI["feedback_avatar"]):
            render_case_prompt(st, state, review=True)
        with st.form("ans2_form"):
            rating = st.radio(UI['assessment_label'], review["choice_order"], index=None,
                              format_func=lambda key: plain_text(review["choices"][key]["Display"]))
            answer = st.text_area(branch["text_prompt"], height=240, key="ans2_draft")
            st.caption(UI['word_limit_caption'])
            if st.form_submit_button(UI['continue_button'], type="primary"):
                submit(answer, rating)
    else:
        if hasattr(store, "sync_completed_csv") and not state.get("csv_synced"):
            try:
                state = store.sync_completed_csv(token, state, export_row(state))
            except (StorageError, StaleResponse) as error:
                st.info(UI["csv_pending_message"])
                st.error(str(error))
                if st.button(UI["retry_finalize_button"]):
                    st.rerun()
                return
        st.success(UI['completion_message'])
        receipt = state.get("subject_id") or state["response_id"]
        st.caption(UI['submission_prefix'] + receipt)
        st.download_button(UI['download_csv'], csv_export([state]),
                           file_name=receipt + ".csv", mime="text/csv")


def show_cases(st):
    for number, text in sorted(DATA["cases"].items()):
        st.subheader(UI["case_title"].format(number=number))
        st.markdown(quotation_markdown(text))


if __name__ == "__main__":
    import sys
    if "--export-csv" in sys.argv:
        parser = argparse.ArgumentParser(description="Export saved assignment responses")
        parser.add_argument("--export-csv", required=True, type=Path)
        args = parser.parse_args()
        args.export_csv.write_text(csv_export(response_store().all_responses()), encoding="utf-8-sig")
        print(f"Exported responses to {args.export_csv}")
    else:
        run_app()
