"""Run with python -m unittest -v; Streamlit UI tests use its official AppTest."""
import copy
import csv
import io
import json
import os
from pathlib import Path
import random
import re
import tempfile
import unittest
from unittest.mock import patch

import judgement_biases_interview as app


def ready_state(assignment=None):
    state = app.new_state(assignment)
    state['subject_id'] = '12345678'
    state = app.advance(state, 'identity', {'name': 'Test Student', 'email': 'student@hec.edu'})
    return app.advance(state, 'instructions', None)


def confirm_email(ui, email='student@hec.edu'):
    ui.text_input(key='participant_email').set_value(email)
    ui.button(key='confirm_email').click().run()
    if not ui.error:
        ui.run()
    return ui


def sign_in(ui, email='student@hec.edu', name='Test Student', code=''):
    if any(field.key == 'participant_email' for field in ui.text_input):
        confirm_email(ui, email)
    if ui.error and not any(field.key == 'participant_name' for field in ui.text_input):
        return ui
    if any(field.key == 'participant_name' for field in ui.text_input):
        ui.text_input(key='participant_name').set_value(name)
        ui.text_input(key='entry_code').set_value(code)
        ui.button(key='register_continue').click().run()
        if not ui.error:
            ui.run()
    return ui


def enter_details_and_begin(ui):
    sign_in(ui)
    ui.button[0].click().run()
    ui.run()
    return ui


def fake_bot(record):
    assert record['pending']['kind'] == 'challenge', 'Only one objection may be generated.'
    return {'provider': 'openai', 'model': 'test-model', 'response_id': record['pending']['id'], 'usage': {},
            'text': 'That conclusion seems too quick to me. Your argument overlooks an important distinction. Do you agree?'}


def finish_probe(state, stage, reply, store=None, token=None):
    def save(updated):
        nonlocal state
        if store is not None:
            store.commit(token, state, updated)
        state = updated
    save(app.ai.claim_generation(state, stage, app.bot_context(state, stage)))
    save(app.ai.apply_generation(state, stage, state['bot_interviews'][stage]['pending']['id'],
                                 fake_bot(state['bot_interviews'][stage])))
    save(app.advance(state, stage, reply))
    save(app.advance(state, stage, {'action': 'confirm'}))
    return state


def confirm_in_ui(ui):
    # Streamlit 1.50 AppTest retains stale widgets across chained model reruns.
    # Resume in a new browser at the durable final-answer checkpoint.
    from streamlit.testing.v1 import AppTest
    token = ui.query_params['response']
    current = AppTest.from_file(app.__file__)
    current.query_params['response'] = token
    current.run()
    sign_in(current)
    current.radio[0].set_value('confirm')
    current.button[0].click().run()
    resumed = AppTest.from_file(app.__file__)
    resumed.query_params['response'] = token
    resumed.run()
    sign_in(resumed)
    return resumed


class RoutingTests(unittest.TestCase):
    def test_answer_and_bot_lease_share_one_storage_revision(self):
        state = ready_state()
        updated, request_id = app.prepare_submission(state, 'ans1', 'First answer')
        self.assertEqual(updated['revision'], state['revision'] + 1)
        self.assertEqual(updated['stage'], 'probe1')
        self.assertEqual(app.ai.phase(updated, 'probe1'), 'challenge_generating')
        self.assertEqual(updated['bot_interviews']['probe1']['pending']['id'], request_id)
        self.assertEqual(updated['answers']['Ans1'], 'First answer')

    def test_all_90_paths_and_all_ratings(self):
        paths = set()
        for first, branch in app.DATA['branches'].items():
            self.assertEqual({q['case'] for q in branch['reviews']}, set(range(1, 11)) - {first})
            for review in branch['reviews']:
                paths.add((first, review['case']))
                for rating in review['choice_order']:
                    state = ready_state({'answerqn': first, 'second_case': review['case'], 'review_qid': review['qid']})
                    state = app.advance(state, 'ans1', 'First answer')
                    state = finish_probe(state, 'probe1', 'First reasons')
                    state = app.advance(state, 'ans2', 'Correction', rating)
                    state = finish_probe(state, 'probe2', 'Second reasons')
                    row = app.export_row(state)
                    self.assertEqual(row['Finished'], 1)
                    self.assertEqual(row['review_case_number'], review['case'])
                    self.assertEqual(row['SubjectId'], '12345678')
                    self.assertNotIn('name', row)
                    self.assertNotIn('email', row)
                    self.assertNotIn('ResponseId', row)
                    self.assertEqual(row['main_first_reply'], 'First answer')
                    self.assertEqual(row['review_first_reply'], 'Correction')
                    self.assertNotIn(branch['text_tag'], row)
                    self.assertEqual(row['review_assessment'], app.plain_text(review['choices'][rating]['Display']))
                    self.assertEqual(row['main_reply_to_feedback'], 'First reasons')
                    self.assertEqual(row['review_reply_to_feedback'], 'Second reasons')
                    self.assertEqual(len(state['transcript']), 12)
                    self.assertEqual(sum(len(record['calls']) for record in state['bot_interviews'].values()), 2)
                    self.assertEqual(state['transcript'][-1]['kind'], 'final_answer')
                    with self.assertRaises(ValueError):
                        app.advance(state, 'probe2', 'repeat')
        self.assertEqual(len(paths), 90)

    def test_random_draws_use_branch_subset(self):
        class ControlledRNG:
            def __init__(self, first, index):
                self.first, self.index, self.calls = first, index, 0
            def choice(self, pool):
                self.calls += 1
                if self.calls == 1:
                    if pool != list(range(1, 11)):
                        raise AssertionError(pool)
                    return self.first
                return pool[self.index]
        for first in range(1, 11):
            for index in range(9):
                assignment = app.create_assignment(ControlledRNG(first, index))
                review = sorted(app.DATA['branches'][first]['reviews'], key=lambda q: q['case'])[index]
                self.assertEqual(assignment['review_qid'], review['qid'])
                self.assertNotEqual(first, assignment['second_case'])
        self.assertEqual(app.create_assignment(random.Random(123)), app.create_assignment(random.Random(123)))

    def test_validation_does_not_mutate_answers(self):
        state = ready_state()
        before = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            app.advance(state, 'ans1', '  ')
        with self.assertRaises(ValueError):
            app.advance(state, 'ans2', 'Out of order', '4')
        self.assertEqual(state, before)
        state = app.advance(state, 'ans1', 'Answer')
        state = finish_probe(state, 'probe1', 'Reasons')
        with self.assertRaises(ValueError):
            app.advance(state, 'ans2', 'Correction', None)
        self.assertEqual(state['stage'], 'ans2')

    def test_persistence_and_stale_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'responses.sqlite3'
            store = app.Store(path)
            token, state = store.create()
            for stage, answer in [('identity', {'name': 'Test Student', 'email': 'student@hec.edu'}), ('instructions', None)]:
                updated = app.advance(state, stage, answer)
                store.commit(token, state, updated)
                state = updated
            new = app.advance(state, 'ans1', 'Multiline\nUnicode é, "quoted"')
            store.commit(token, state, new)
            self.assertEqual(app.Store(path).load(token), new)
            self.assertIsNone(store.load('unknown'))
            with self.assertRaises(app.StaleResponse):
                store.commit(token, state, new)
            row = next(csv.DictReader(io.StringIO(app.csv_export(store.all_responses()))))
            self.assertEqual(row['main_first_reply'], new['answers']['Ans1'])
            self.assertEqual(row['Finished'], '0')
            self.assertEqual(row['review_first_reply'], '')
            self.assertEqual(len(list(csv.DictReader(io.StringIO(app.csv_export([]))))), 0)

    def test_qsf_fidelity_when_source_available(self):
        path = Path(os.environ.get('QSF_SOURCE', r'C:\Users\hill\Downloads\Choice_assignment.qsf'))
        if not path.exists():
            self.skipTest('Set QSF_SOURCE to run the original-source comparison')
        import hashlib
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), app.DATA['source_sha256'])
        elements = json.loads(path.read_text())['SurveyElements']
        qs = {e['Payload']['QuestionID']: e['Payload'] for e in elements if e['Element'] == 'SQ'}
        blocks = next(e['Payload'] for e in elements if e['Element'] == 'BL')
        flow = next(e['Payload'] for e in elements if e['Element'] == 'FL')['Flow']
        self.assertEqual(app.DATA['intro'], qs['QID1']['QuestionText'])
        self.assertIn(app.plain_text(app.CONTENT['case_question']), app.plain_text(qs['QID2']['QuestionText']))
        self.assertEqual(blocks['0']['Options']['Randomization']['Advanced']['FixedOrder'], ['QID1'])
        self.assertNotIn(next(b['ID'] for b in blocks.values() if b['Description'] == 'who?'),
                         [node.get('ID') for node in flow])
        for node in flow:
            if node['Type'] != 'Branch':
                continue
            first = int(node['BranchLogic']['0']['0']['RightOperand'])
            branch = app.DATA['branches'][first]
            self.assertEqual(node['Flow'][0]['ID'], branch['block_id'])
            original = next(b for b in blocks.values() if b['ID'] == branch['block_id'])
            advanced = original['Options']['Randomization']['Advanced']
            self.assertEqual(advanced['TotalRandSubset'], 1)
            self.assertFalse(original['Options']['Randomization']['EvenPresentation'])
            self.assertEqual([q['qid'] for q in branch['reviews']], advanced['RandomSubSet'])
            self.assertEqual(branch['text_qid'], advanced['FixedOrder'][-2])
            self.assertEqual(branch['text_tag'], qs[branch['text_qid']]['DataExportTag'])
            for review in branch['reviews']:
                source = qs[review['qid']]
                self.assertEqual(review['tag'], source['DataExportTag'])
                self.assertEqual(review['choices'], source['Choices'])
                self.assertEqual(review['case'], int(re.search(r'case (\d+)', source['QuestionText']).group(1)))
                self.assertIn('How well do you think', app.plain_text(source['QuestionText']))

    def test_identity_validation_and_saved_intake(self):
        original = app.new_state()
        for name, email in [('', 'a@example.com'), ('Student', 'not an email'),
                            ('Student', 'a@'), ('Student', 'a@example'),
                            ('Student', 'a@@example.com'), ('Student', 'a..b@example.com')]:
            with self.assertRaises(ValueError):
                app.advance(original, 'identity', {'name': name, 'email': email})
            self.assertEqual(original['answers'], {})
        state = app.advance(original, 'identity', {'name': '  Student Name  ', 'email': ' Student@HEC.EDU '})
        self.assertEqual(state['answers'], {'name': 'Student Name', 'email': 'student@hec.edu'})
        self.assertEqual(state['stage'], 'instructions')
        with self.assertRaises(ValueError):
            app.advance(state, 'ans1', 'skip instructions')
        self.assertEqual(app.advance(state, 'instructions', None)['stage'], 'ans1')
        for email in ('a@gmail.com', 'a@example.org', 'a@sub.hec.edu', 'a+tag@company.co.uk'):
            self.assertEqual(app.validate_identity('Student', email)['email'], email)

    def test_case_screen_order_for_every_pair(self):
        for first, branch in app.DATA['branches'].items():
            for review in branch['reviews']:
                state = ready_state({'answerqn': first, 'second_case': review['case'], 'review_qid': review['qid']})
                parts = app.case_prompt_parts(state)
                if app.CONTENT['screens']['ans1_opening'] in app.plain_text(app.DATA['ans1_prompt']):
                    self.assertEqual(parts.pop(0), app.CONTENT['screens']['ans1_opening'])
                self.assertEqual(parts[0], f'First, consider case {first}.')
                self.assertEqual(parts[1], 'As a reminder, this is the case:')
                self.assertEqual(parts[2], app.DATA['cases'][first])
                self.assertTrue(parts[3].startswith('Please answer the following question'))
                review_parts = app.case_prompt_parts(state, review=True)
                self.assertEqual(review_parts[0], f'Now consider case {review["case"]}.')
                self.assertEqual(review_parts[2], app.DATA['cases'][review['case']])
                self.assertEqual(review_parts[3], 'As for the previous case, the question is:')
                self.assertTrue(review_parts[-1].startswith('The following response to the question has been provided by ChatGPT.'))

    def test_docx_cases_when_source_available(self):
        import zipfile
        import xml.etree.ElementTree as ET
        path = Path(os.environ.get('DOCX_SOURCE', r'C:\Users\hill\HEC PARIS Dropbox\Brian Hill\Recherche\HEC\coursHEC\BehEco\current\assignments\Choice bias assignment master.docx'))
        if not path.exists():
            self.skipTest('Set DOCX_SOURCE to run the original-case comparison')
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read('word/document.xml'))
        source_text = ' '.join(text.strip() for text in root.itertext() if text.strip())
        for marker in ('decrease wages and salaries 7 percent', 'can of fictitious insecticide',
                       'offer gifts that the receivers', 'held in small denominations',
                       'pennies per day', 'rebate on their tuition fees', 'Taxi drivers',
                       'violent storm and a strike', 'two dinner sets', 'two legal cases'):
            self.assertIn(marker, source_text)
        self.assertEqual(set(app.DATA['cases']), set(range(1, 11)))


class EmailResumeTests(unittest.TestCase):
    def test_local_subject_ids_are_unique_and_identity_is_logged_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.Store(Path(directory) / 'test.sqlite3')
            with patch.object(app, 'new_subject_id',
                              side_effect=['12345678', '12345678', '87654321']):
                _, first = store.start_or_resume(
                    {'name': 'First Student', 'email': 'first@example.org'})
                _, second = store.start_or_resume(
                    {'name': 'Second Student', 'email': 'second@example.org'})
            self.assertEqual(first['subject_id'], '12345678')
            self.assertEqual(second['subject_id'], '87654321')
            with store.connect() as db:
                rows = list(db.execute(
                    'SELECT subject_id, name, email FROM subject_log ORDER BY email'))
            self.assertEqual(rows, [
                ('12345678', 'First Student', 'first@example.org'),
                ('87654321', 'Second Student', 'second@example.org'),
            ])
            exported = next(csv.DictReader(io.StringIO(app.csv_export([first]))))
            self.assertEqual(exported['SubjectId'], '12345678')
            self.assertNotIn('name', exported)
            self.assertNotIn('email', exported)
            self.assertNotIn('ResponseId', exported)

    def test_local_resume_all_stages_and_completed_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite3'
            identity = app.validate_identity('Student Name', ' Student@Example.COM ')
            store = app.Store(path)
            token, state = store.start_or_resume(identity)
            assignment, response_id = copy.deepcopy(state['assignment']), state['response_id']
            steps = [('instructions', None), ('ans1', 'First answer'), ('probe1', 'First reasons'),
                     ('ans2', 'Correction'), ('probe2', 'Second reasons')]
            for stage, answer in steps:
                self.assertEqual(state['stage'], stage)
                again, resumed = app.Store(path).start_or_resume({'name': 'A changed name', 'email': 'STUDENT@example.com'})
                self.assertEqual(again, token)
                self.assertEqual(resumed, state)
                self.assertEqual(resumed['assignment'], assignment)
                self.assertEqual(resumed['response_id'], response_id)
                if stage in app.ai.PROBES:
                    state = finish_probe(state, stage, answer, store, token)
                    continue
                updated = app.advance(state, stage, answer, '4' if stage == 'ans2' else None)
                store.commit(token, state, updated)
                state = updated
            _, completed = app.Store(path).start_or_resume(identity)
            self.assertEqual(completed['stage'], 'complete')
            self.assertEqual(completed['answers']['name'], 'Student Name')
            self.assertEqual(len(store.all_responses()), 1)
            with self.assertRaises(ValueError):
                app.advance(completed, 'ans1', 'Change first answer')

    def test_email_only_lookup_is_read_only_and_returns_the_saved_response(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.Store(Path(directory) / 'test.sqlite3')
            self.assertIsNone(store.resume_by_email('unknown@example.org'))
            self.assertEqual(store.all_responses(), [])
            token, state = store.start_or_resume({'name': 'Registered Student', 'email': 'student@example.org'})
            updated = app.advance(state, 'instructions', None)
            store.commit(token, state, updated)
            self.assertEqual(app.Store(store.path).resume_by_email(' STUDENT@EXAMPLE.ORG '), (token, updated))
            self.assertEqual(store.load(token), updated)
            self.assertEqual(len(store.all_responses()), 1)
            with store.connect() as db:
                db.execute('DELETE FROM participants')
            self.assertEqual(store.resume_by_email('student@example.org'), (token, updated))
            with store.connect() as db:
                db.execute('DELETE FROM responses')
            with self.assertRaises(app.StorageError):
                store.resume_by_email('student@example.org')

    def test_local_legacy_email_backfill_preserves_private_link(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.Store(Path(directory) / 'test.sqlite3')
            old_token, original = store.create()
            identified = app.advance(original, 'identity', {'name': 'Student', 'email': 'student@gmail.com'})
            store.commit(old_token, original, identified)
            token, resumed = store.start_or_resume({'name': 'Different name', 'email': 'student@gmail.com'})
            self.assertRegex(resumed['subject_id'], r'^\d{8}$')
            self.assertEqual(resumed['answers'], identified['answers'])
            self.assertEqual(resumed['assignment'], identified['assignment'])
            self.assertEqual(resumed['response_id'], identified['response_id'])
            self.assertEqual(store.load(old_token), store.load(token))
            updated = app.advance(resumed, 'instructions', None)
            store.commit(token, resumed, updated)
            self.assertEqual(store.load(old_token), updated)
            self.assertEqual(len(store.all_responses()), 1)

    def test_local_storage_rejects_saved_answer_edits_and_rewinds(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.Store(Path(directory) / 'test.sqlite3')
            token, state = store.start_or_resume({'name': 'Student', 'email': 'student@example.org'})
            for stage, answer in [('instructions', None), ('ans1', 'Locked answer')]:
                updated = app.advance(state, stage, answer)
                store.commit(token, state, updated)
                state = updated
            for change in ('answer', 'rewind', 'assignment'):
                updated = copy.deepcopy(state)
                updated['revision'] += 1
                if change == 'answer':
                    updated['answers']['Ans1'] = 'Overwritten'
                elif change == 'rewind':
                    updated['stage'] = 'ans1'
                else:
                    updated['assignment']['answerqn'] = 99
                with self.assertRaises(ValueError):
                    store.commit(token, state, updated)
            self.assertEqual(store.load(token), state)


class StreamlitTests(unittest.TestCase):
    def setUp(self):
        self.bot_patch = patch.object(app.ai, 'generate', side_effect=fake_bot)
        self.bot_patch.start()
        self.addCleanup(self.bot_patch.stop)

    def test_full_interview_and_refresh(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'JUDGEMENT_DB': str(Path(directory) / 'test.sqlite3'), 'ASSIGNMENT_PASSWORD': ''
        }):
            ui = AppTest.from_file(str(Path(app.__file__))).run()
            self.assertFalse(ui.exception)
            enter_details_and_begin(ui)
            token = ui.query_params['response']
            store = app.Store(app.database_path())
            assignment = store.load(token)['assignment']
            ui.text_area[0].set_value('My first answer')
            ui.button[0].click().run()
            self.assertFalse(ui.exception)
            self.assertEqual(len(ui.chat_input), 1)
            ui.chat_input[0].set_value('My first reasons').run()
            ui = confirm_in_ui(ui)
            self.assertEqual(len(ui.radio), 1)
            # Blank fields and no rating must not advance the state.
            ui.button[0].click().run()
            self.assertTrue(ui.error)
            self.assertEqual(store.load(token)['stage'], 'ans2')
            review = app.selected_review(store.load(token))
            ui.radio[0].set_value(review['choice_order'][1])
            ui.text_area[0].set_value('My correction')
            ui.button[0].click().run()
            self.assertFalse(ui.exception)
            self.assertEqual(len(ui.chat_input), 1)
            # Simulate a fresh browser process resuming the bearer link.
            resumed = AppTest.from_file(str(Path(app.__file__)))
            resumed.query_params['response'] = token
            resumed.run()
            sign_in(resumed)
            self.assertFalse(resumed.exception)
            self.assertEqual(store.load(token)['assignment'], assignment)
            self.assertEqual(resumed.chat_input[0].key, 'probe2_input')
            resumed.chat_input[0].set_value('My second reasons').run()
            resumed = confirm_in_ui(resumed)
            resumed.run()  # AppTest 1.50 retains stale nodes through an immediate rerun.
            self.assertFalse(resumed.exception)
            self.assertTrue(resumed.success)
            self.assertEqual(len(resumed.chat_input), 0)
            self.assertEqual(len(resumed.text_area), 0)
            self.assertEqual(store.load(token)['stage'], 'complete')
            saved = store.load(token)
            self.assertEqual(set(saved['timings']), set(app.timing.STEPS))
            row = app.export_row(saved)
            for step in app.timing.STEPS:
                self.assertGreaterEqual(row[app.response_export.TIMING_NAMES[step] + '_seconds'], 0)
                self.assertEqual(row[app.response_export.TIMING_NAMES[step] + '_timing_basis'], 'server_checkpoint')
            downloads = resumed.get('download_button')
            self.assertEqual([button.proto.label for button in downloads], [app.UI['download_csv']])
            resumed.run()
            self.assertEqual(len(store.all_responses()), 1)

    def test_email_confirm_page_then_name_code_page_and_generic_email_validation(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'JUDGEMENT_DB': str(Path(directory) / 'test.sqlite3'), 'ASSIGNMENT_PASSWORD': ''
        }):
            ui = AppTest.from_file(app.__file__).run()
            self.assertFalse(ui.exception)
            self.assertEqual([field.label for field in ui.text_input], ['Email address'])
            self.assertEqual(len(ui.text_area), 0)
            self.assertFalse(any(x.value == 'Assignment instructions' for x in ui.subheader))
            confirm_email(ui, 'not-an-email')
            self.assertTrue(ui.error)
            self.assertEqual([field.label for field in ui.text_input], ['Email address'])
            confirm_email(ui, ' Student@Example.COM ')
            self.assertFalse(ui.exception)
            self.assertEqual([field.label for field in ui.text_input], ['Full name', 'Access code'])
            self.assertEqual(app.Store(app.database_path()).all_responses(), [])
            ui.text_input(key='participant_name').set_value('Student Name')
            ui.button(key='register_continue').click().run()
            ui.run()
            self.assertFalse(ui.exception)
            token = ui.query_params['response']
            state = app.Store(app.database_path()).load(token)
            self.assertEqual(state['stage'], 'instructions')
            self.assertEqual(state['answers']['email'], 'student@example.com')
            self.assertTrue(any(x.value == 'Assignment instructions' for x in ui.subheader))
            ui.button[0].click().run()
            ui.run()
            chat = ui.chat_message[0]
            self.assertEqual(chat.subheader[0].value, f'Question 1: Case {state["assignment"]["answerqn"]}')
            expected = [app.quotation_markdown(part) if part == app.DATA['cases'][state['assignment']['answerqn']]
                        else part for part in app.case_prompt_parts(state)]
            self.assertEqual([x.value for x in chat.markdown], expected)

    def test_email_can_be_changed_before_registration_without_allocating_a_response(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'JUDGEMENT_DB': str(Path(directory) / 'test.sqlite3'), 'ASSIGNMENT_PASSWORD': 'code'
        }):
            ui = AppTest.from_file(app.__file__).run()
            confirm_email(ui, 'first@example.org')
            self.assertEqual(len(ui.text_input), 2)
            ui.button(key='change_email').click().run()
            ui.run()
            self.assertEqual([field.label for field in ui.text_input], ['Email address'])
            sign_in(ui, email='second@example.org', name='Second Student', code='code')
            self.assertFalse(ui.exception)
            store = app.Store(app.database_path())
            self.assertIsNone(store.resume_by_email('first@example.org'))
            self.assertEqual(len(store.all_responses()), 1)
            self.assertEqual(store.all_responses()[0]['answers']['email'], 'second@example.org')

    def test_email_lookup_failure_stays_on_email_page(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'JUDGEMENT_DB': str(Path(directory) / 'test.sqlite3'), 'ASSIGNMENT_PASSWORD': 'code',
            'STORAGE_BACKEND': 'dropbox'
        }), patch('dropbox_store.configured_store', side_effect=app.StorageError('Temporary storage failure')):
            ui = AppTest.from_file(app.__file__).run()
            confirm_email(ui)
            self.assertTrue(ui.error)
            self.assertEqual([field.label for field in ui.text_input], ['Email address'])
            self.assertFalse(ui.query_params)
            self.assertEqual(app.Store(app.database_path()).all_responses(), [])

    def test_legacy_resume_keeps_answers_and_case_assignment(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'JUDGEMENT_DB': str(Path(directory) / 'test.sqlite3'), 'ASSIGNMENT_PASSWORD': ''
        }):
            store = app.Store(app.database_path())
            token, original = store.create()
            old = ready_state(original['assignment'])
            old['response_id'] = original['response_id']
            old = app.advance(old, 'ans1', 'Previously saved answer')
            old['answers'].pop('name')
            old['answers'].pop('email')
            # Simulate an imported pre-upgrade response, not a current UI write.
            with store.connect() as db:
                db.execute('UPDATE responses SET payload = ?, revision = ? WHERE token_hash = ?',
                           (json.dumps(old), old['revision'], store.token_hash(token)))
            ui = AppTest.from_file(app.__file__)
            ui.query_params['response'] = token
            ui.run()
            self.assertFalse(ui.exception)
            sign_in(ui)
            self.assertFalse(ui.exception)
            saved = store.load(token)
            self.assertEqual(saved['stage'], 'probe1')
            self.assertEqual(saved['answers']['Ans1'], 'Previously saved answer')
            self.assertEqual(saved['assignment'], original['assignment'])
            self.assertEqual(len(ui.chat_input), 1)

    def test_fresh_browser_resumes_by_email_without_private_link(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'JUDGEMENT_DB': str(Path(directory) / 'test.sqlite3'), 'ASSIGNMENT_PASSWORD': 'code'
        }):
            first = AppTest.from_file(app.__file__).run()
            sign_in(first, email='person@example.org', code='code')
            first.button[0].click().run()
            first.run()
            first.text_area[0].set_value('My locked original answer')
            first.button[0].click().run()
            token = first.query_params['response']
            original = app.Store(app.database_path()).load(token)
            fresh = AppTest.from_file(app.__file__).run()
            self.assertFalse(fresh.query_params)
            confirm_email(fresh, ' PERSON@EXAMPLE.ORG ')
            self.assertFalse(fresh.exception)
            self.assertEqual(len(fresh.text_area), 0)
            self.assertEqual(fresh.chat_input[0].key, 'probe1_input')
            self.assertEqual(len(fresh.text_input), 0)
            resumed = app.Store(app.database_path()).load(fresh.query_params['response'])
            self.assertEqual(resumed, original)
            self.assertEqual(len(app.Store(app.database_path()).all_responses()), 1)

    def test_first_registration_requires_code_and_email_alone_recovers_bad_link(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'JUDGEMENT_DB': str(Path(directory) / 'test.sqlite3'), 'ASSIGNMENT_PASSWORD': 'test'
        }):
            ui = AppTest.from_file(str(Path(app.__file__))).run()
            sign_in(ui, code='wrong')
            self.assertTrue(ui.error)
            self.assertEqual(len(app.Store(app.database_path()).all_responses()), 0)
            sign_in(ui, code='test')
            self.assertFalse(ui.exception)
            self.assertEqual(ui.button[0].label, 'Begin assignment')
            original = app.Store(app.database_path()).all_responses()[0]
            ui.query_params['response'] = 'unknown'
            ui.run()
            self.assertEqual(ui.text_input(key='participant_email').label, 'Email address')
            sign_in(ui, code='test')
            self.assertFalse(ui.exception)
            saved = app.Store(app.database_path()).all_responses()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]['response_id'], original['response_id'])


if __name__ == '__main__':
    unittest.main()
