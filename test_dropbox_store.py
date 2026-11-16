"""Exercise remote persistence with SDK-shaped responses; no live account needed."""
import json
import csv
import copy
import io
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from dropbox.exceptions import ApiError
from dropbox.files import (DownloadError, FileMetadata, LookupError, UploadError,
                           UploadWriteFailed, WriteConflictError, WriteError)
import dropbox_store as storage
import judgement_biases_interview as app
from email_lookup import resume_existing_response
from test_judgement_biases_interview import ready_state, enter_details_and_begin, sign_in, confirm_email, finish_probe, fake_bot, confirm_in_ui


def conflict():
    return ApiError('request', UploadError.path(UploadWriteFailed(
        reason=WriteError.conflict(WriteConflictError.file))), None, None)


class FakeDropbox:
    def __init__(self):
        self.files = {}
        self.fail_write = False
        self.fail_read = False
        self.race = False
        self.counter = 0
        self.upload_paths = []
        self.before_upload = None
        self.fail_csv = False
        self.fail_marker = False

    def files_upload(self, content, path, mode, **kwargs):
        self.upload_paths.append(path)
        if self.fail_write or (self.fail_csv and path.endswith('.csv')) or (
            self.fail_marker and path.endswith('.json') and json.loads(content).get('csv_synced')
        ):
            raise ConnectionError('simulated offline')
        if self.before_upload:
            callback, self.before_upload = self.before_upload, None
            callback(path)
        current = self.files.get(path)
        if mode.is_add() and current:
            raise conflict()
        if mode.is_update() and (not current or current[0].rev != mode.get_update() or self.race):
            raise conflict()
        self.counter += 1
        metadata = FileMetadata(name=path.rsplit('/', 1)[-1], id='id:test',
                                rev=format(self.counter, '010x'), size=len(content),
                                client_modified=datetime.now(), server_modified=datetime.now(), path_lower=path)
        self.files[path] = (metadata, content)
        return metadata

    def files_download(self, path):
        if self.fail_read:
            raise ConnectionError('simulated offline')
        if path not in self.files:
            raise ApiError('request', DownloadError.path(LookupError.not_found), None, None)
        metadata, content = self.files[path]
        return metadata, SimpleNamespace(content=content, close=lambda: None)

    def files_list_folder(self, root):
        entries = [meta for path, (meta, content) in self.files.items() if path.rsplit('/', 1)[0] == root]
        self.page_entries = entries
        # Force pagination so export cannot silently drop later responses.
        return SimpleNamespace(entries=entries[:1], has_more=len(entries) > 1, cursor='next')

    def files_list_folder_continue(self, cursor):
        return SimpleNamespace(entries=self.page_entries[1:], has_more=False)


class PreUpdateDropboxStore:
    """Simulate the older class instance reported in the Cloud log."""
    def __init__(self, current):
        self.current = current

    def __getattr__(self, name):
        if name == 'resume_by_email':
            raise AttributeError("'DropboxStore' object has no attribute 'resume_by_email'")
        return getattr(self.current, name)


class DropboxTests(unittest.TestCase):
    def setUp(self):
        self.bot_patch = patch.object(app.ai, 'generate', side_effect=fake_bot)
        self.bot_patch.start()
        self.addCleanup(self.bot_patch.stop)
        self.client = FakeDropbox()
        self.store = storage.DropboxStore(self.client, ready_state)

    def test_resume_after_restart_and_export_pagination(self):
        token, before = self.store.create()
        after = app.advance(before, 'ans1', 'Réponse, with\nnewlines')
        self.store.commit(token, before, after)
        restarted = storage.DropboxStore(self.client, app.new_state)
        self.assertEqual(restarted.load(token), after)
        self.assertIsNone(restarted.load('unknown'))
        restarted.create()
        self.assertEqual(len(restarted.all_responses()), 2)
        self.assertNotIn(token, next(iter(self.client.files)))
        self.assertNotIn(token, json.dumps(after))

    def test_stale_tab_and_remote_write_race(self):
        token, before = self.store.create()
        after = app.advance(before, 'ans1', 'Answer')
        self.store.commit(token, before, after)
        with self.assertRaises(storage.StaleResponse):
            self.store.commit(token, before, after)
        self.client.race = True
        with self.assertRaises(storage.StaleResponse):
            self.store.commit(token, after, app.ai.claim_generation(after, 'probe1', app.bot_context(after, 'probe1')))
        self.assertEqual(self.store.load(token), after)

    def test_loaded_revision_avoids_downloads_during_successive_conditional_saves(self):
        token, before = self.store.create()
        restarted = storage.DropboxStore(self.client, app.new_state)
        with patch.object(self.client, 'files_download', wraps=self.client.files_download) as download:
            loaded = restarted.load(token)
            after = app.advance(loaded, 'ans1', 'Answer')
            restarted.commit(token, loaded, after)
            claimed = app.ai.claim_generation(after, 'probe1', app.bot_context(after, 'probe1'))
            restarted.commit(token, after, claimed)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(restarted.load(token), claimed)
            self.assertEqual(download.call_count, 2)

    def test_browser_cache_reuses_confirmed_save_until_an_explicit_read(self):
        cache = {}
        self.store.use_session_cache(cache)
        token, before = self.store.create()
        rerun = storage.DropboxStore(self.client, app.new_state)
        rerun.use_session_cache(cache)
        with patch.object(self.client, 'files_download', wraps=self.client.files_download) as download:
            loaded = rerun.load_for_screen(token)
            self.assertEqual(download.call_count, 0)
            # Returned state must never alias the snapshot used for locking.
            loaded['answers']['name'] = 'Accidentally modified widget state'
            self.assertEqual(rerun.load_for_screen(token), before)
            self.assertEqual(download.call_count, 0)
            self.assertEqual(rerun.load(token), before)  # Explicit reads remain authoritative.
            self.assertEqual(download.call_count, 1)
            after = app.advance(before, 'ans1', 'Saved answer')
            rerun.commit(token, before, after)
            next_screen = storage.DropboxStore(self.client, app.new_state)
            next_screen.use_session_cache(cache)
            self.assertEqual(next_screen.load_for_screen(token), after)
            self.assertEqual(download.call_count, 1)

    def test_registration_and_returning_email_avoid_redundant_downloads(self):
        store = storage.DropboxStore(self.client, app.new_state)
        store._bootstrap_email_index()
        with patch.object(self.client, 'files_download', wraps=self.client.files_download) as download:
            token, state = store.start_or_resume({'name': 'Student', 'email': 'student@example.org'})
            # Missing hashed lookup and legacy marker; successful uploads return
            # the authoritative metadata, without read-after-write round trips.
            self.assertEqual(download.call_count, 3)  # Email lookup, marker and subjects.csv.
            download.reset_mock()
            lookup = storage.DropboxStore(self.client, app.new_state)
            self.assertEqual(lookup.resume_by_email('student@example.org'), (token, state))
            self.assertEqual(download.call_count, 2)  # Hashed lookup and JSON only.
            self.assertFalse(any(call.args[0].endswith('ready.json') for call in download.call_args_list))

    def test_known_new_email_skips_repeating_the_lookup_before_atomic_registration(self):
        store = storage.DropboxStore(self.client, app.new_state)
        store._bootstrap_email_index()
        with patch.object(self.client, 'files_download', wraps=self.client.files_download) as download:
            token, state = store.start_or_resume(
                {'name': 'Student', 'email': 'student@example.org'}, known_missing=True)
        self.assertEqual(download.call_count, 1)  # Only the subject log is checked.
        self.assertEqual(self.client.upload_paths.count(store.response_path(token)), 1)
        self.assertEqual(store.load(token), state)

    def test_subject_log_retries_a_concurrent_code_collision(self):
        first = {'name': 'First Student', 'email': 'first@example.org'}
        second = {'name': 'Second Student', 'email': 'second@example.org'}
        winner = []

        def concurrent_registration(path):
            self.assertEqual(path, self.store.subject_csv_path)
            winner.append(storage.DropboxStore(self.client, app.new_state)._subject_id(second))

        self.client.before_upload = concurrent_registration
        with patch.object(storage, 'new_subject_id',
                          side_effect=['12345678', '12345678', '87654321']):
            code = self.store._subject_id(first)
        self.assertEqual(winner, ['12345678'])
        self.assertEqual(code, '87654321')
        rows = list(csv.DictReader(io.StringIO(
            self.client.files[self.store.subject_csv_path][1].decode('utf-8-sig'))))
        self.assertEqual({row['SubjectId'] for row in rows}, {'12345678', '87654321'})
        self.assertEqual({row['email'] for row in rows},
                         {'first@example.org', 'second@example.org'})

    def test_registration_reuses_one_logged_subject_id_for_the_same_email(self):
        identity = {'name': 'Student', 'email': 'student@example.org'}
        store = storage.DropboxStore(self.client, app.new_state)
        with patch.object(storage, 'new_subject_id', return_value='12345678'):
            token, state = store.start_or_resume(identity)
        again, resumed = storage.DropboxStore(self.client, app.new_state).start_or_resume(
            {'name': 'Changed Name', 'email': 'STUDENT@EXAMPLE.ORG'})
        self.assertEqual(again, token)
        self.assertEqual(resumed['subject_id'], '12345678')
        rows = list(csv.DictReader(io.StringIO(
            self.client.files[self.store.subject_csv_path][1].decode('utf-8-sig'))))
        self.assertEqual(rows, [{
            'SubjectId': '12345678', 'name': 'Student', 'email': 'student@example.org'
        }])

    def test_email_screen_performs_no_remote_requests_before_submission(self):
        from streamlit.testing.v1 import AppTest
        with patch.dict(os.environ, {'STORAGE_BACKEND': 'dropbox'}), patch.object(
            storage, 'configured_store', side_effect=AssertionError('Entry should not initialise Dropbox')
        ):
            ui = AppTest.from_file(app.__file__).run()
            self.assertFalse(ui.exception)
            self.assertEqual(ui.text_input(key='participant_email').label, app.UI['email_label'])

    def test_browser_cache_conflict_and_failed_save_force_fresh_read(self):
        self.store.use_session_cache({})
        token, before = self.store.create()
        other = storage.DropboxStore(self.client, app.new_state)
        winner = app.advance(before, 'ans1', 'Other tab wins')
        other.commit(token, other.load(token), winner)
        self.assertEqual(self.store.load_for_screen(token), before)
        with self.assertRaises(storage.StaleResponse):
            self.store.commit(token, before, app.advance(before, 'ans1', 'Stale answer'))
        with patch.object(self.client, 'files_download', wraps=self.client.files_download) as download:
            self.assertEqual(self.store.load_for_screen(token), winner)
            self.assertEqual(download.call_count, 1)
            claimed = app.ai.claim_generation(winner, 'probe1', app.bot_context(winner, 'probe1'))
            self.client.fail_write = True
            with self.assertRaises(storage.StorageError):
                self.store.commit(token, winner, claimed)
            self.client.fail_write = False
            self.assertEqual(self.store.load_for_screen(token), winner)
            self.assertEqual(download.call_count, 2)

    def test_browser_cache_is_not_shared_across_browsers_accounts_or_folders(self):
        cache = {}
        self.store.use_session_cache(cache)
        token, before = self.store.create()
        with patch.object(self.client, 'files_download', wraps=self.client.files_download) as download:
            fresh_browser = storage.DropboxStore(self.client, app.new_state)
            fresh_browser.use_session_cache({})
            self.assertEqual(fresh_browser.load_for_screen(token), before)
            self.assertEqual(download.call_count, 1)
            changed_folder = storage.DropboxStore(self.client, app.new_state, root='/other')
            changed_folder.use_session_cache(cache)
            self.assertIsNone(changed_folder.load_for_screen(token))
            self.assertEqual(download.call_count, 2)
        changed_account = storage.DropboxStore(FakeDropbox(), app.new_state)
        changed_account.use_session_cache(cache)
        self.assertIsNone(changed_account.load_for_screen(token))

    def test_check_again_reads_remote_generation_even_with_a_recent_snapshot(self):
        token, before = self.store.create()
        answer = app.advance(before, 'ans1', 'Answer')
        self.store.commit(token, before, answer)
        claimed = app.ai.claim_generation(answer, 'probe1', app.bot_context(answer, 'probe1'))
        self.store.commit(token, answer, claimed)
        other = storage.DropboxStore(self.client, app.new_state)
        current = other.load(token)
        replied = app.ai.apply_generation(current, 'probe1', current['bot_interviews']['probe1']['pending']['id'],
                                          fake_bot(current['bot_interviews']['probe1']))
        other.commit(token, current, replied)
        self.assertEqual(self.store.load_for_screen(token), replied)

    def test_ui_transitions_only_write_and_quick_reruns_make_no_dropbox_requests(self):
        from streamlit.testing.v1 import AppTest
        with patch.dict(os.environ, {'ASSIGNMENT_PASSWORD': '', 'STORAGE_BACKEND': 'dropbox'}), patch.object(
            storage, 'configured_store', side_effect=lambda factory: storage.DropboxStore(self.client, factory)
        ):
            ui = AppTest.from_file(app.__file__).run()
            sign_in(ui)
            with patch.object(self.client, 'files_download', wraps=self.client.files_download) as download:
                ui.button[0].click().run()
                ui.run()
                self.assertFalse(ui.exception)
                self.assertEqual(download.call_count, 0)
                # The answer and generation lease share one checkpoint; the
                # generated objection is saved in a second checkpoint.
                writes = len(self.client.upload_paths)
                ui.text_area[0].set_value('My original answer')
                ui.button[0].click().run()
                ui.run()
                self.assertFalse(ui.exception)
                self.assertEqual(download.call_count, 0)
                self.assertEqual(len(self.client.upload_paths) - writes, 2)
                self.assertEqual(len(ui.chat_input), 1)
                writes = len(self.client.upload_paths)
                ui.chat_input[0].set_value('My comment on that objection').run()
                ui.run()
                self.assertFalse(ui.exception)
                self.assertEqual(download.call_count, 0)
                self.assertEqual(len(self.client.upload_paths) - writes, 1)
                # A plain screen rerun must not perform reads or writes.
                writes = len(self.client.upload_paths)
                ui.run()
                self.assertEqual(download.call_count, 0)
                self.assertEqual(len(self.client.upload_paths), writes)

    def test_identity_resume_can_refresh_an_older_revision_in_the_same_store(self):
        store = storage.DropboxStore(self.client, app.new_state)
        token, before = store.create()
        _, resumed = store.start_or_resume({'name': 'Student', 'email': 'new@example.org'}, token_hint=token)
        self.assertGreater(resumed['revision'], before['revision'])
        after = app.advance(resumed, 'instructions', None)
        store.commit(token, resumed, after)
        self.assertEqual(store.load(token), after)

    def test_cached_revision_rejects_another_tabs_remote_write(self):
        token, before = self.store.create()
        other = storage.DropboxStore(self.client, app.new_state)
        other_before = other.load(token)
        winner = app.advance(other_before, 'ans1', 'The other tab submitted first.')
        other.commit(token, other_before, winner)
        with self.assertRaises(storage.StaleResponse):
            self.store.commit(token, before, app.advance(before, 'ans1', 'Stale answer'))
        self.assertEqual(self.store.load(token), winner)

    def test_cached_state_still_prevents_answer_edits_even_if_previous_is_modified(self):
        token, before = self.store.create()
        saved = app.advance(before, 'ans1', 'Locked answer')
        self.store.commit(token, before, saved)
        modified = copy.deepcopy(saved)
        modified['answers']['Ans1'] = 'Attempted replacement'
        claimed = app.ai.claim_generation(modified, 'probe1', app.bot_context(modified, 'probe1'))
        with self.assertRaises(ValueError):
            self.store.commit(token, modified, claimed)
        self.assertEqual(self.store.load(token), saved)

    def test_failures_do_not_advance_and_are_not_missing_links(self):
        token, before = self.store.create()
        self.client.fail_write = True
        with self.assertRaises(storage.StorageError):
            self.store.commit(token, before, app.advance(before, 'ans1', 'Answer'))
        self.assertEqual(self.store.load(token), before)
        self.client.fail_read = True
        with self.assertRaises(storage.StorageError):
            self.store.load(token)

    def complete_response(self):
        token, state = self.store.create()
        self.store._subject_id(
            {'name': state['answers']['name'], 'email': state['answers']['email']},
            state['subject_id'])
        for stage, answer, rating in [('ans1', 'First answer', None), ('probe1', 'First reasons', None),
                                     ('ans2', 'Réponse, with\nnewlines', '5'), ('probe2', 'Second reasons', None)]:
            if stage in app.ai.PROBES:
                state = finish_probe(state, stage, answer, self.store, token)
                continue
            updated = app.advance(state, stage, answer, rating)
            self.store.commit(token, state, updated)
            state = updated
        return token, state

    def csv_rows(self):
        return list(csv.DictReader(io.StringIO(self.client.files[self.store.csv_path][1].decode('utf-8-sig'))))

    def subject_rows(self):
        return list(csv.DictReader(io.StringIO(
            self.client.files[self.store.subject_csv_path][1].decode('utf-8-sig'))))

    def test_csv_only_at_completion_preserves_existing_rows_and_columns(self):
        from dropbox.files import WriteMode
        token, state = self.complete_response()
        self.assertNotIn(self.store.csv_path, self.client.upload_paths)
        self.client.files_upload(b'ResponseId,custom_column\r\nR_previous,keep me\r\n',
                                 self.store.csv_path, mode=WriteMode.add)
        saved = self.store.sync_completed_csv(token, state, app.export_row(state))
        rows = self.csv_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['custom_column'], 'keep me')
        self.assertEqual(rows[1]['review_first_reply'], 'Réponse, with\nnewlines')
        self.assertEqual(rows[1]['SubjectId'], state['subject_id'])
        self.assertNotIn('email', rows[1])
        self.assertNotIn('name', rows[1])
        self.assertNotIn('ResponseId', rows[1])
        self.assertEqual(self.subject_rows(), [{
            'SubjectId': state['subject_id'], 'name': 'Test Student', 'email': 'student@hec.edu'
        }])
        self.assertEqual(rows[1]['review_case_number'], str(state['assignment']['second_case']))
        self.assertTrue(self.store.load(token)['csv_synced'])
        writes = len(self.client.upload_paths)
        self.store.sync_completed_csv(token, saved, app.export_row(saved))
        self.assertEqual(len(self.client.upload_paths), writes)
        self.assertEqual(len(self.store.all_responses()), 1)  # Aggregate is not a JSON response.

    def test_csv_concurrent_finish_preserves_other_submission(self):
        from dropbox.files import WriteMode
        token, state = self.complete_response()
        self.client.files_upload(b'SubjectId\r\n11111111\r\n', self.store.csv_path, mode=WriteMode.add)
        self.store._subject_id(
            {'name': state['answers']['name'], 'email': state['answers']['email']},
            state['subject_id'])
        def other_submission(path):
            self.assertEqual(path, self.store.csv_path)
            meta = self.client.files[path][0]
            self.client.files_upload(b'SubjectId\r\n11111111\r\n22222222\r\n', path,
                                     mode=WriteMode.update(meta.rev))
        self.client.before_upload = other_submission
        self.store.sync_completed_csv(token, state, app.export_row(state))
        self.assertEqual({row['SubjectId'] for row in self.csv_rows()},
                         {'11111111', '22222222', state['subject_id']})

    def test_csv_failure_and_marker_failure_recover_without_duplicate(self):
        token, state = self.complete_response()
        self.client.fail_csv = True
        with self.assertRaises(storage.StorageError):
            self.store.sync_completed_csv(token, state, app.export_row(state))
        self.assertEqual(self.store.load(token)['stage'], 'complete')
        self.assertFalse(self.store.load(token).get('csv_synced'))
        self.client.fail_csv = False
        self.client.fail_marker = True
        with self.assertRaises(storage.StorageError):
            self.store.sync_completed_csv(token, state, app.export_row(state))
        self.assertEqual(len(self.csv_rows()), 1)
        self.client.fail_marker = False
        saved = self.store.sync_completed_csv(token, state, app.export_row(state))
        self.assertTrue(saved['csv_synced'])
        self.assertEqual(len(self.csv_rows()), 1)

    def test_csv_rejects_partial_response(self):
        token, state = self.store.create()
        with self.assertRaises(ValueError):
            self.store.sync_completed_csv(token, state, app.export_row(state))
        self.assertFalse(any(path.endswith('.csv') for path in self.client.upload_paths))

    def test_ui_completes_remote_json_then_csv_once(self):
        from streamlit.testing.v1 import AppTest
        with patch.dict(os.environ, {'ASSIGNMENT_PASSWORD': '', 'STORAGE_BACKEND': 'dropbox'}), patch.object(
            storage, 'configured_store', side_effect=lambda factory: storage.DropboxStore(self.client, factory)
        ):
            ui = AppTest.from_file(app.__file__).run()
            enter_details_and_begin(ui)
            token = ui.query_params['response']
            ui.text_area[0].set_value('First answer')
            ui.button[0].click().run()
            ui.chat_input[0].set_value('First reasons').run()
            ui = confirm_in_ui(ui)
            review = app.selected_review(self.store.load(token))
            ui.radio[0].set_value(review['choice_order'][0])
            ui.text_area[0].set_value('Second answer')
            ui.button[0].click().run()
            self.assertNotIn(self.store.csv_path, self.client.upload_paths)
            self.assertEqual(self.store.load(token)['stage'], 'probe2')
            # Resume in a clean browser before finalisation, also exercising restart recovery.
            ui = AppTest.from_file(app.__file__)
            ui.query_params['response'] = token
            ui.run()
            sign_in(ui)
            ui.chat_input[0].set_value('Second reasons').run()
            ui = confirm_in_ui(ui)
            ui.run()
            self.assertFalse(ui.exception)
            self.assertTrue(ui.success)
            self.assertTrue(self.store.load(token)['csv_synced'])
            self.assertEqual(len(self.csv_rows()), 1)
            writes = len(self.client.upload_paths)
            ui.run()
            self.assertEqual(len(self.client.upload_paths), writes)

    def test_email_lookup_handles_the_pre_update_class_in_the_cloud_log(self):
        store = storage.DropboxStore(self.client, app.new_state)
        legacy = PreUpdateDropboxStore(store)
        with self.assertRaisesRegex(AttributeError, 'resume_by_email'):
            legacy.resume_by_email('student@example.org')
        self.assertIsNone(resume_existing_response(legacy, 'student@example.org'))
        token, before = store.start_or_resume({'name': 'Student', 'email': 'student@example.org'})
        after = app.advance(before, 'instructions', None)
        store.commit(token, before, after)
        writes = len(self.client.upload_paths)
        self.assertEqual(resume_existing_response(legacy, ' STUDENT@EXAMPLE.ORG '), (token, after))
        self.assertEqual(len(self.client.upload_paths), writes)
        self.assertEqual(store.load(token), after)

    def test_cleaned_test_folder_with_only_marker_and_csv_allows_new_email(self):
        from dropbox.files import WriteMode
        store = storage.DropboxStore(self.client, app.new_state)
        store._bootstrap_email_index()
        self.client.files_upload(b'ResponseId\r\nR_old_test\r\n', store.csv_path, mode=WriteMode.add)
        existing = dict(self.client.files)
        self.assertIsNone(resume_existing_response(PreUpdateDropboxStore(store), 'new@example.org'))
        self.assertEqual(self.client.files, existing)
        self.assertEqual(store.all_responses(), [])
        token, state = store.start_or_resume({'name': 'New Student', 'email': 'new@example.org'})
        self.assertEqual(store.load(token), state)
        self.assertEqual(len(store.all_responses()), 1)

    def test_legacy_lookup_tolerates_concurrent_recovery_of_an_interrupted_registration(self):
        from dropbox.files import WriteMode
        store = storage.DropboxStore(self.client, app.new_state)
        token, state = store.start_or_resume({'name': 'Student', 'email': 'student@example.org'})
        index_path = store._email_path('student@example.org')
        metadata = self.client.files[index_path][0]
        store._upload(index_path, {'response_key': storage.response_key(token), 'initial_state': state},
                      WriteMode.update(metadata.rev))
        response_path = store.response_path(token)
        del self.client.files[response_path]
        def other_browser_recovers(path):
            self.assertEqual(path, response_path)
            self.client.files_upload(json.dumps(state).encode(), path, mode=WriteMode.add)
        self.client.before_upload = other_browser_recovers
        self.assertEqual(resume_existing_response(PreUpdateDropboxStore(store), 'student@example.org'), (token, state))
        self.assertEqual(len(store.all_responses()), 1)

    def test_email_page_resumes_with_a_cached_store_missing_the_new_method(self):
        from streamlit.testing.v1 import AppTest
        store = storage.DropboxStore(self.client, app.new_state)
        token, before = store.start_or_resume({'name': 'Returning Student', 'email': 'return@example.org'})
        with patch.dict(os.environ, {'ASSIGNMENT_PASSWORD': 'code', 'STORAGE_BACKEND': 'dropbox'}), patch.object(
            storage, 'configured_store', side_effect=lambda factory: PreUpdateDropboxStore(
                storage.DropboxStore(self.client, factory))
        ):
            ui = AppTest.from_file(app.__file__).run()
            confirm_email(ui, 'RETURN@example.org')
            self.assertFalse(ui.exception)
            self.assertFalse(ui.error)
            self.assertEqual(ui.query_params['response'], token)
            self.assertEqual(store.load(token), before)
            self.assertEqual(len(ui.text_input), 0)

    def test_email_only_lookup_never_creates_unknown_responses_or_changes_saved_data(self):
        store = storage.DropboxStore(self.client, app.new_state)
        self.assertIsNone(store.resume_by_email('unknown@example.org'))
        self.assertEqual(store.all_responses(), [])
        token, state = store.start_or_resume({'name': 'Student', 'email': 'student@example.org'})
        def must_not_create():
            raise AssertionError('Email lookup must reuse the existing response')
        lookup = storage.DropboxStore(self.client, must_not_create)
        writes = len(self.client.upload_paths)
        self.assertEqual(lookup.resume_by_email(' STUDENT@EXAMPLE.ORG '), (token, state))
        self.assertEqual(len(self.client.upload_paths), writes)
        self.assertEqual(len(store.all_responses()), 1)
        del self.client.files[store.response_path(token)]
        with self.assertRaises(storage.StorageError):
            lookup.resume_by_email('student@example.org')
        self.assertEqual(store.all_responses(), [])

    def test_returning_browser_needs_only_email_even_when_registration_requires_code(self):
        from streamlit.testing.v1 import AppTest
        with patch.dict(os.environ, {'ASSIGNMENT_PASSWORD': 'code', 'STORAGE_BACKEND': 'dropbox'}), patch.object(
            storage, 'configured_store', side_effect=lambda factory: storage.DropboxStore(self.client, factory)
        ):
            ui = AppTest.from_file(app.__file__).run()
            sign_in(ui, email='return@example.org', code='code')
            ui.button[0].click().run()
            ui.run()
            ui.text_area[0].set_value('My saved first answer')
            ui.button[0].click().run()
            token = ui.query_params['response']
            before = self.store.load(token)
            fresh = AppTest.from_file(app.__file__).run()
            self.assertEqual([field.label for field in fresh.text_input], ['Email address'])
            confirm_email(fresh, ' RETURN@EXAMPLE.ORG ')
            self.assertFalse(fresh.exception)
            self.assertEqual(len(fresh.text_input), 0)
            self.assertEqual(fresh.chat_input[0].key, 'probe1_input')
            self.assertEqual(self.store.load(token), before)
            self.assertEqual(len(self.store.all_responses()), 1)

    def test_email_resume_every_stage_without_private_link(self):
        store = storage.DropboxStore(self.client, app.new_state)
        identity = {'name': 'Student Name', 'email': 'student@example.org'}
        token, state = store.start_or_resume(identity)
        assignment, response_id = copy.deepcopy(state['assignment']), state['response_id']
        for stage, answer in [('instructions', None), ('ans1', 'First answer'), ('probe1', 'First reasons'),
                              ('ans2', 'Correction'), ('probe2', 'Second reasons')]:
            again, resumed = storage.DropboxStore(self.client, app.new_state).start_or_resume(
                {'name': 'Changed name on return', 'email': 'STUDENT@EXAMPLE.ORG'})
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
        self.assertNotIn(store.csv_path, self.client.upload_paths)
        state = store.sync_completed_csv(token, state, app.export_row(state))
        _, resumed = storage.DropboxStore(self.client, app.new_state).start_or_resume(identity)
        self.assertEqual(resumed, state)
        self.assertEqual(resumed['stage'], 'complete')
        self.assertEqual(resumed['answers']['name'], 'Student Name')
        self.assertEqual(len(store.all_responses()), 1)
        self.assertEqual(len(self.csv_rows()), 1)

    def test_email_backfill_reuses_existing_json_and_old_link(self):
        old_token, original = self.store.create()  # Existing pre-index response with email.
        updated = app.advance(original, 'ans1', 'Already saved answer')
        self.store.commit(old_token, original, updated)
        token, state = self.store.start_or_resume({'name': 'Returning name', 'email': 'student@HEC.edu'})
        self.assertEqual(state, updated)
        self.assertEqual(self.store.load(old_token), self.store.load(token))
        self.assertEqual(len(self.store.all_responses()), 1)

    def test_concurrent_email_registration_claims_one_response(self):
        store = storage.DropboxStore(self.client, app.new_state)
        identity = {'name': 'Student', 'email': 'student@example.org'}
        store._bootstrap_email_index()
        winner = []
        def concurrent_claim(path):
            self.assertEqual(path, store._email_path(identity['email']))
            winner.append(store.start_or_resume(identity))
        self.client.before_upload = concurrent_claim
        token, state = store.start_or_resume(identity)
        self.assertEqual((token, state), winner[0])
        self.assertEqual(len(store.all_responses()), 1)

    def test_registration_preserves_progress_when_another_browser_recovers_first(self):
        store = storage.DropboxStore(self.client, app.new_state)
        store._bootstrap_email_index()
        identity = {'name': 'Student', 'email': 'student@example.org'}
        winner = []

        def after_index_claim(path):
            self.assertEqual(path, store._email_path(identity['email']))

            def recover_before_response_upload(path):
                other = storage.DropboxStore(self.client, app.new_state)
                token, initial = other.resume_by_email(identity['email'])
                advanced = app.advance(initial, 'instructions', None)
                other.commit(token, initial, advanced)
                winner.append((token, advanced))
            self.client.before_upload = recover_before_response_upload

        self.client.before_upload = after_index_claim
        token, state = store.start_or_resume(identity)
        self.assertEqual((token, state), winner[0])
        self.assertEqual(state['stage'], 'ans1')
        self.assertEqual(len(store.all_responses()), 1)

    def test_registration_recovers_after_response_write_failure(self):
        store = storage.DropboxStore(self.client, app.new_state)
        identity = {'name': 'Student', 'email': 'student@example.org'}
        store._bootstrap_email_index()
        def fail_response_write(path):
            # Claim succeeds; emulate a crash before writing the response JSON.
            def fail_next_write(path):
                raise ConnectionError('simulated stop before response creation')
            self.client.before_upload = fail_next_write
        self.client.before_upload = fail_response_write
        with self.assertRaises(storage.StorageError):
            store.start_or_resume(identity)
        indexed = store._read(store._email_path(identity['email']))[1]
        self.client.fail_write = False
        def must_not_rerandomize():
            raise AssertionError('Recovery must reuse the saved initial assignment')
        token, state = storage.DropboxStore(self.client, must_not_rerandomize).resume_by_email(' STUDENT@EXAMPLE.ORG ')
        self.assertEqual(state['assignment'], indexed['initial_state']['assignment'])
        self.assertEqual(state['response_id'], indexed['initial_state']['response_id'])
        self.assertEqual(len(store.all_responses()), 1)

    def test_missing_saved_response_does_not_reset_the_survey(self):
        store = storage.DropboxStore(self.client, app.new_state)
        identity = {'name': 'Student', 'email': 'student@example.org'}
        token, state = store.start_or_resume(identity)
        indexed = store._read(store._email_path(identity['email']))[1]
        self.assertNotIn('initial_state', indexed)
        del self.client.files[store.response_path(token)]
        with self.assertRaises(storage.StorageError):
            store.start_or_resume(identity)
        self.assertEqual(len(store.all_responses()), 0)

    def test_remote_storage_rejects_answer_edits_and_backwards_stage(self):
        token, state = self.store.create()
        updated = app.advance(state, 'ans1', 'Locked answer')
        self.store.commit(token, state, updated)
        state = updated
        for change in ('answer', 'rewind'):
            updated = copy.deepcopy(state)
            updated['revision'] += 1
            if change == 'answer':
                updated['answers']['Ans1'] = 'Overwritten'
            else:
                updated['stage'] = 'ans1'
            with self.assertRaises(ValueError):
                self.store.commit(token, state, updated)
        self.assertEqual(self.store.load(token), state)

    def test_partially_configured_credentials_fail_closed(self):
        with patch.object(storage, 'setting', side_effect=lambda name, default=None: 'key' if name == 'DROPBOX_APP_KEY' else default):
            with self.assertRaises(storage.StorageError):
                storage.configured_store(app.new_state)

    def test_ui_preserves_stage_after_failed_remote_save(self):
        from streamlit.testing.v1 import AppTest
        with patch.dict(os.environ, {'ASSIGNMENT_PASSWORD': '', 'STORAGE_BACKEND': 'dropbox'}), patch.object(
            storage, 'configured_store', side_effect=lambda factory: storage.DropboxStore(self.client, factory)
        ):
            ui = AppTest.from_file(app.__file__).run()
            enter_details_and_begin(ui)
            token = ui.query_params['response']
            ui.text_area[0].set_value('Retain my answer')
            self.client.fail_write = True
            ui.button[0].click().run()
            self.assertFalse(ui.exception)
            self.assertTrue(ui.error)
            self.assertEqual(ui.text_area[0].value, 'Retain my answer')
            self.assertEqual(self.store.load(token)['stage'], 'ans1')
            self.client.fail_write = False
            ui.button[0].click().run()
            self.assertEqual(self.store.load(token)['stage'], 'probe1')
            self.assertEqual(self.store.load(token)['answers']['Ans1'], 'Retain my answer')
            self.client.fail_write = True
            ui.chat_input[0].set_value('Keep these reasons too').run()
            self.assertFalse(ui.exception)
            self.assertTrue(ui.error)
            self.assertEqual(self.store.load(token)['stage'], 'probe1')
            self.assertTrue(any(code.value == 'Keep these reasons too' for code in ui.code))
            self.client.fail_write = False
            ui.run()
            ui.button(key='probe1_retry').click().run()
            ui = confirm_in_ui(ui)
            self.assertEqual(self.store.load(token)['stage'], 'ans2')
            self.assertEqual(self.store.load(token)['answers']['Ans1_reasons'], 'Keep these reasons too')


if __name__ == '__main__':
    unittest.main()
