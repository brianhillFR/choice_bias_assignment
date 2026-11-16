"""Readable response CSV fields and migration of older Qualtrics-shaped rows."""
import json
import re

import answer_timing as timing

TIMING_NAMES = {
    'Ans1': 'main_first_reply', 'Ans1_bot_reply': 'main_reply_to_feedback', 'Ans1_final': 'main_final_answer',
    'Ans2': 'review_first_reply', 'Ans2_bot_reply': 'review_reply_to_feedback', 'Ans2_final': 'review_final_answer',
}


def saved_question(state, step, fallback=''):
    return next((message['content'] for message in state.get('transcript', [])
                 if message.get('role') == 'assistant' and message.get('step') == step), fallback)


def split_review_prompt(text):
    """Separate the supplied response from the question, retaining saved wording.

    Start after the ChatGPT introduction, since the case itself may contain
    quotation marks. The source encloses the whole supplied response in quotes.
    """
    introduction = re.search(r'(?m)^[^\n]*\bChatGPT\b[^\n]*', text)
    if introduction is None:
        return text, ''
    tail = text[introduction.end():]
    quoted = re.fullmatch(r'\s*["“](.*)["”](.*)', tail, flags=re.DOTALL)
    if quoted is None:
        return text, ''
    response, assessment_question = quoted.groups()
    question = '\n\n'.join(part.strip() for part in
                           (text[:introduction.start()], assessment_question) if part.strip())
    return question, response.strip()


def export_response(state, main_question='', review_question='', assessment='', supplied_response=''):
    answers = state.get('answers', {})
    assignment = state.get('assignment', {})
    row = {
        'SubjectId': state.get('subject_id', ''), 'StartDate': state.get('started_at', ''),
        'EndDate': state.get('completed_at') or '', 'Finished': int(state.get('stage') == 'complete'),
    }
    main_question = saved_question(state, 'ans1', main_question)
    review_question, captured_response = split_review_prompt(saved_question(state, 'rating', review_question))
    correction_question = saved_question(state, 'ans2')
    if correction_question and correction_question not in review_question:
        review_question = '\n\n'.join(filter(None, (review_question, correction_question)))
    for prefix, old, stage, case, question in (
        ('main', 'Ans1', 'probe1', assignment.get('answerqn', ''), main_question),
        ('review', 'Ans2', 'probe2', assignment.get('review_case', assignment.get('second_case', '')), review_question),
    ):
        record = state.get('bot_interviews', {}).get(stage, {})
        row.update({
            prefix + '_case_number': case,
            prefix + '_question': question,
            prefix + '_first_reply': answers.get(old, ''),
            prefix + '_feedback': answers.get(old + '_bot_challenge', record.get('challenge', '')),
            prefix + '_reply_to_feedback': answers.get(old + '_bot_reply', answers.get(old + '_reasons', record.get('student_reply', ''))),
            prefix + '_confirm_or_correct': answers.get(old + '_confirmation', record.get('decision', '')),
            prefix + '_final_answer': answers.get(old + '_final', record.get('final_answer', '')),
        })
    row['review_assessment'] = assessment
    row['review_chatgpt_response'] = captured_response or supplied_response
    for field, value in timing.export_columns(state).items():
        # Match longest first: Ans1_bot_reply must be matched before Ans1.
        old = next(key for key in sorted(TIMING_NAMES, key=len, reverse=True) if field.startswith(key + '_'))
        row[TIMING_NAMES[old] + field[len(old):]] = value
    return row


def _json_field(row, key, expected):
    raw = row.get(key)
    if not raw:
        return expected()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        # Do not remove a legacy transcript if it cannot be migrated safely.
        raise ValueError('An existing CSV row has invalid ' + key + '; repair it before merging.') from error
    if not isinstance(value, expected):
        raise ValueError('An existing CSV row has an invalid ' + key + ' value.')
    return value


def legacy_column(name):
    # These identity columns must never survive in the answers CSV. ResponseId
    # is an old internal identifier; SubjectId is now the only identity field.
    if name.casefold() in ('responseid', 'name', 'email'):
        return True
    if re.fullmatch(r'(?:QID\d+|Q\d+|Ch\d+)', name, flags=re.IGNORECASE):
        return True
    if name in ('answerqn', 'second_case', 'review_case', 'review_qid', 'review_export_tag',
                'Ans2_qid', 'Ans2_export_tag', 'rating_choice_id', 'rating_label',
                'stage', 'source_sha256', 'transcript_json', 'bot_interviews_json'):
        return True
    return name in ('Ans1', 'Ans2') or any(name == old + suffix for old in ('Ans1', 'Ans2')
        for suffix in ('_reasons', '_bot_challenge', '_bot_reply', '_proposed', '_final',
                       '_confirmation', '_correction')) or any(
        name == old + suffix for old in TIMING_NAMES
        for suffix in ('_seconds', '_started_at', '_submitted_at', '_timing_basis'))


def migrate_rows(header, rows, new_row):
    """Clean legacy and identifying columns without losing answer fields."""
    legacy = any(legacy_column(name) for name in header)
    if not legacy:
        return list(dict.fromkeys(list(new_row) + header)), rows
    extras = [name for name in header if name not in new_row and not legacy_column(name)]
    output = []
    for original in rows:
        state = {
            'subject_id': original.get('SubjectId', ''),
            'response_id': original.get('ResponseId', ''),
            'started_at': original.get('StartDate', ''), 'completed_at': original.get('EndDate', ''),
            'stage': 'complete' if str(original.get('Finished', '')).lower() in ('1', 'true') else '',
            'assignment': {'answerqn': original.get('answerqn', ''),
                           'review_case': original.get('review_case') or original.get('second_case', '')},
            'answers': original,
            'transcript': _json_field(original, 'transcript_json', list),
            'bot_interviews': _json_field(original, 'bot_interviews_json', dict),
            'timings': {},
        }
        for step in timing.STEPS:
            state['timings'][step] = {key: original.get(step + '_' + key, '')
                                      for key in ('seconds', 'started_at', 'submitted_at')}
            state['timings'][step]['basis'] = original.get(step + '_timing_basis', '')
        converted = export_response(state, assessment=original.get('rating_label', ''))
        # Mixed files may already contain the new fields on some rows. Prefer
        # their nonempty values; do not replace them with empty legacy columns.
        converted.update({key: value for key, value in original.items()
                          if key in new_row and value not in ('', None)})
        converted.update({key: original.get(key, '') for key in extras})
        output.append(converted)
    return list(new_row) + extras, output
