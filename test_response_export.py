"""Readable CSV exports and safe migration of older aggregate results."""
import copy
import csv
import io
import json
import re
import unittest
from unittest.mock import patch

from dropbox.files import WriteMode
import dropbox_store as storage
import judgement_biases_interview as app
import response_export as export
from test_dropbox_store import FakeDropbox
from test_judgement_biases_interview import ready_state, finish_probe


def completed():
    state = app.advance(ready_state(), 'ans1', 'Initial main answer, with\nnewlines é')
    state = finish_probe(state, 'probe1', 'Main feedback reply')
    state = app.advance(state, 'ans2', 'Initial review answer', '5')
    return finish_probe(state, 'probe2', 'Review feedback reply')


def legacy_row(state):
    review = app.selected_review(state)
    return {
        'ResponseId': state['response_id'], 'StartDate': state['started_at'],
        'EndDate': state['completed_at'], 'Finished': '1',
        'answerqn': str(state['assignment']['answerqn']),
        'review_case': str(review['case']), 'second_case': str(review['case']),
        'review_qid': review['qid'], 'Q318': 'redundant choice code',
        **state['answers'], **app.timing.export_columns(state),
        'rating_label': next(message['content'] for message in state['transcript']
                             if message['role'] == 'user' and message['step'] == 'rating'),
        'transcript_json': json.dumps(state['transcript']),
        'bot_interviews_json': json.dumps(app.ai.public_interviews(state)),
        'custom_column': 'keep this research annotation',
    }


class ResponseExportTests(unittest.TestCase):
    def test_fields_preserve_saved_questions_and_isolate_all_ten_supplied_responses(self):
        for case in range(1, 11):
            state = ready_state()
            first = next(number for number in app.DATA['branches'] if number != case)
            review = next(item for item in app.DATA['branches'][first]['reviews'] if item['case'] == case)
            state = ready_state({'answerqn': first, 'second_case': case, 'review_qid': review['qid']})
            state = app.advance(state, 'ans1', 'My first answer')
            state = finish_probe(state, 'probe1', 'My comment')
            state = app.advance(state, 'ans2', 'My explanation of the supplied response', '6')
            state = finish_probe(state, 'probe2', 'My review comment')
            row = app.export_row(state)
            self.assertEqual(row['main_case_number'], first)
            self.assertEqual(row['review_case_number'], case)
            self.assertEqual(row['main_first_reply'], 'My first answer')
            self.assertEqual(row['main_confirm_or_correct'], 'confirm')
            self.assertEqual(row['main_final_answer'], 'My first answer')
            self.assertEqual(row['review_assessment'], 'Wrong')
            self.assertIn('How well do you think', row['review_question'])
            self.assertIn(app.CONTENT['ans2_prompt'], row['review_question'])
            self.assertTrue(row['review_chatgpt_response'])
            self.assertNotIn(row['review_chatgpt_response'], row['review_question'])
            self.assertNotIn('How well do you think', row['review_chatgpt_response'])
            self.assertFalse(any(re.fullmatch(r'(?:QID\d+|Q\d+|Ch\d+)', field) for field in row))
            self.assertFalse(any('challenge' in field or field.endswith('_json') for field in row))
            with patch.dict(app.CONTENT, {'case_question': 'LATER QUESTION EDIT'}):
                self.assertEqual(app.export_row(state), row)

    def test_migration_preserves_answers_feedback_timings_and_custom_columns(self):
        state = completed()
        old = legacy_row(state)
        old['SubjectId'] = state['subject_id']
        new = app.export_row(state)
        header, rows = export.migrate_rows(list(old), [old], {key: str(value) for key, value in new.items()})
        for key, value in new.items():
            self.assertEqual(str(rows[0][key]), str(value), key)
        self.assertEqual(rows[0]['custom_column'], old['custom_column'])
        self.assertNotIn('Q318', header)
        self.assertNotIn('transcript_json', header)
        self.assertNotIn('ResponseId', header)
        self.assertNotIn('name', header)
        self.assertNotIn('email', header)
        # A mixed-schema file preserves an already-migrated value on another row.
        mixed = {**old, **{key: str(value) for key, value in new.items()}}
        mixed['main_feedback'] = 'Already migrated feedback'
        _, rows = export.migrate_rows(list(mixed), [mixed], new)
        self.assertEqual(rows[0]['main_feedback'], 'Already migrated feedback')

    def test_completion_merge_cleans_the_actual_existing_csv_without_losing_rows(self):
        state = completed()
        previous = copy.deepcopy(state)
        previous['response_id'] = 'R_previous'
        previous['answers']['name'] = 'Previous Student'
        previous['answers']['email'] = 'previous@example.org'
        previous['answers']['Ans1_final'] = 'An earlier corrected final answer'
        previous['answers']['Ans1_confirmation'] = 'correct'
        old = legacy_row(previous)
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=list(old))
        writer.writeheader()
        writer.writerow(old)
        client = FakeDropbox()
        store = storage.DropboxStore(client, lambda: state)
        token, saved = store.create()
        store._subject_id(
            {'name': saved['answers']['name'], 'email': saved['answers']['email']},
            saved['subject_id'])
        client.files_upload(output.getvalue().encode('utf-8-sig'), store.csv_path, WriteMode.add)
        store.sync_completed_csv(token, saved, app.export_row(saved))
        reader = csv.DictReader(io.StringIO(client.files[store.csv_path][1].decode('utf-8-sig')))
        rows = list(reader)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row['SubjectId'] for row in rows}), 2)
        earlier = next(row for row in rows if row['main_final_answer'] == 'An earlier corrected final answer')
        self.assertEqual(earlier['main_final_answer'], 'An earlier corrected final answer')
        self.assertEqual(earlier['main_confirm_or_correct'], 'correct')
        self.assertEqual(earlier['custom_column'], old['custom_column'])
        self.assertNotIn('Q318', reader.fieldnames)
        self.assertNotIn('Ans1', reader.fieldnames)
        self.assertNotIn('ResponseId', reader.fieldnames)
        self.assertNotIn('name', reader.fieldnames)
        self.assertNotIn('email', reader.fieldnames)
        subjects = list(csv.DictReader(io.StringIO(
            client.files[store.subject_csv_path][1].decode('utf-8-sig'))))
        self.assertEqual({row['SubjectId'] for row in subjects},
                         {row['SubjectId'] for row in rows})
        current = next(row for row in subjects if row['email'] == 'student@hec.edu')
        self.assertEqual(current['name'], 'Test Student')

    def test_malformed_legacy_transcript_leaves_the_existing_csv_intact(self):
        state = completed()
        client = FakeDropbox()
        store = storage.DropboxStore(client, lambda: state)
        token, saved = store.create()
        original = b'ResponseId,transcript_json\r\nR_previous,invalid-json\r\n'
        client.files_upload(original, store.csv_path, WriteMode.add)
        with self.assertRaises(storage.StorageError):
            store.sync_completed_csv(token, saved, app.export_row(saved))
        self.assertEqual(client.files[store.csv_path][1], original)
        self.assertEqual(store.load(token)['stage'], 'complete')
        self.assertFalse(store.load(token).get('csv_synced'))
