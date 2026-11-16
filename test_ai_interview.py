"""AI interview protocol tests; all model responses are mocked, with no API spend."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ai_interview as ai
import judgement_biases_interview as app
import dropbox_store as storage
from response_identity import check_update
from test_judgement_biases_interview import ready_state, fake_bot, sign_in, enter_details_and_begin
from test_dropbox_store import FakeDropbox


class FakeResponses:
    def __init__(self, values):
        self.values, self.calls = list(values), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text, status = self.values.pop(0)
        return SimpleNamespace(output_text=text, status=status, model=kwargs['model'], id='api-test',
                               usage=SimpleNamespace(model_dump=lambda: {'input_tokens': 12, 'output_tokens': 25}))


class BotTests(unittest.TestCase):
    def original(self):
        return app.advance(ready_state(), 'ans1', 'My original explanation.')

    def claimed(self, state):
        return ai.claim_generation(state, 'probe1', app.bot_context(state, 'probe1'))

    def apply(self, claimed):
        return ai.apply_generation(claimed, 'probe1', claimed['bot_interviews']['probe1']['pending']['id'],
                                   fake_bot(claimed['bot_interviews']['probe1']))

    def test_reply_then_correction_is_required_and_original_stays_locked(self):
        original = self.original()
        with self.assertRaises(ValueError):
            app.advance(original, 'probe1', 'skip the bot')
        state = self.apply(self.claimed(original))
        self.assertEqual(ai.phase(state, 'probe1'), 'reply')
        state = app.advance(state, 'probe1', 'I disagree; I keep my initial position.')
        self.assertEqual(state['stage'], 'probe1')
        self.assertEqual(len(state['bot_interviews']['probe1']['calls']), 1)
        self.assertEqual(ai.phase(state, 'probe1'), 'confirm')
        for answer in ('Yes', {'action': None}, {'action': 'correct', 'final_answer': ' '}):
            with self.assertRaises(ValueError):
                app.advance(state, 'probe1', answer)
        final = app.advance(state, 'probe1', {'action': 'correct', 'final_answer': 'My complete corrected final answer.'})
        self.assertEqual(final['stage'], 'ans2')
        self.assertEqual(final['answers']['Ans1'], original['answers']['Ans1'])
        self.assertEqual(final['answers']['Ans1_final'], 'My complete corrected final answer.')
        self.assertEqual(final['answers']['Ans1_confirmation'], 'correct')
        with self.assertRaises(ValueError):
            app.advance(final, 'probe1', {'action': 'confirm'})

    def test_generation_claim_blocks_duplicate_and_expired_request_is_replaced(self):
        claimed = self.claimed(self.original())
        pending = claimed['bot_interviews']['probe1']['pending']
        with self.assertRaises(ValueError):
            self.claimed(claimed)
        with patch.object(ai.time, 'time', return_value=pending['expires_at'] + 1):
            replacement = self.claimed(claimed)
        self.assertNotEqual(replacement['bot_interviews']['probe1']['pending']['id'], pending['id'])
        with self.assertRaises(ValueError):
            ai.apply_generation(replacement, 'probe1', pending['id'], fake_bot(claimed['bot_interviews']['probe1']))
        self.assertEqual(replacement['answers']['Ans1'], claimed['answers']['Ans1'])

    def test_failure_retry_uses_same_config_snapshot(self):
        claimed = self.claimed(self.original())
        record = claimed['bot_interviews']['probe1']
        failed = ai.generation_failed(claimed, 'probe1', record['pending']['id'])
        self.assertEqual(ai.phase(failed, 'probe1'), 'generation_failed')
        replacement = self.claimed(failed)
        self.assertEqual(replacement['bot_interviews']['probe1']['_config'], record['_config'])
        self.assertEqual(replacement['answers'], claimed['answers'])

    def test_one_api_call_uses_case_and_answer_then_student_writes_final(self):
        state = self.claimed(self.original())
        api = FakeResponses([('One specific objection. Please comment in at most 150 words.', 'completed')])
        client = SimpleNamespace(responses=api)
        record = state['bot_interviews']['probe1']
        first = ai.generate(record, client=client)
        self.assertIn(record['_config']['case_guidance']['wrong_position'], api.calls[0]['instructions'])
        self.assertIn(state['answers']['Ans1'], api.calls[0]['input'][0]['content'])
        self.assertIn('150 words', api.calls[0]['instructions'])
        self.assertNotIn(state['answers']['email'], json.dumps(api.calls[0]))
        self.assertFalse(api.calls[0]['store'])
        self.assertNotIn('synthesis_instructions', record['_config'])
        state = ai.apply_generation(state, 'probe1', record['pending']['id'], first)
        state = app.advance(state, 'probe1', 'I reject that objection.')
        self.assertEqual(ai.phase(state, 'probe1'), 'confirm')
        with self.assertRaises(ValueError):
            self.claimed(state)
        final = app.advance(state, 'probe1', {'action': 'correct', 'final_answer': 'My own final position.'})
        self.assertEqual(final['answers']['Ans1_final'], 'My own final position.')
        self.assertNotIn('Ans1_proposed', final['answers'])
        self.assertEqual(len(api.calls), 1)

    def test_client_reuses_connection_and_separates_credentials_and_timeouts(self):
        ai._openai_client.cache_clear()
        self.addCleanup(ai._openai_client.cache_clear)
        with patch('openai.OpenAI') as factory:
            first = ai._openai_client('test-key', 45)
            self.assertIs(ai._openai_client('test-key', 45), first)
            self.assertEqual(factory.call_count, 1)
            ai._openai_client('other-test-key', 45)
            ai._openai_client('test-key', 60)
            self.assertEqual(factory.call_count, 3)

    def test_api_duration_and_usage_are_saved_with_the_call(self):
        state = self.claimed(self.original())
        record = state['bot_interviews']['probe1']
        client = SimpleNamespace(responses=FakeResponses([('Specific objection.', 'completed')]))
        with patch.object(ai.time, 'perf_counter', side_effect=[100, 102.25]):
            result = ai.generate(record, client=client)
        updated = ai.apply_generation(state, 'probe1', record['pending']['id'], result)
        call = updated['bot_interviews']['probe1']['calls'][0]
        self.assertEqual(call['duration_seconds'], 2.25)
        self.assertEqual(call['kind'], 'challenge')
        self.assertEqual(call['usage']['input_tokens'], 12)
        self.assertEqual(call['usage']['output_tokens'], 25)

    def test_bad_or_truncated_output_does_not_finish_interview(self):
        for text, status in [('', 'completed'), ('partial', 'incomplete')]:
            state = self.claimed(self.original())
            with self.assertRaises(ai.BotError):
                ai.generate(state['bot_interviews']['probe1'], client=SimpleNamespace(responses=FakeResponses([(text, status)])))
            self.assertEqual(ai.phase(state, 'probe1'), 'challenge_generating')

    def test_comment_word_limit_and_confirmation_use_the_original_answer(self):
        state = self.apply(self.claimed(self.original()))
        before = copy.deepcopy(state)
        with self.assertRaisesRegex(ValueError, '150 words'):
            app.advance(state, 'probe1', ' '.join(['word'] * 151))
        self.assertEqual(state, before)
        final_page = app.advance(state, 'probe1', ' '.join(['word'] * 150))
        with self.assertRaises(ValueError):
            app.advance(final_page, 'probe1', {'action': 'confirm', 'final_answer': 'Edited text'})
        final = app.advance(final_page, 'probe1', {'action': 'confirm'})
        self.assertEqual(final['answers']['Ans1_final'], final['answers']['Ans1'])
        altered = copy.deepcopy(final)
        altered['revision'] += 1
        altered['answers']['Ans1_final'] = 'Rewritten after submission'
        with self.assertRaises(ValueError):
            check_update(final, altered)

    def test_legacy_synthesis_states_resume_on_final_page_without_a_model_call(self):
        state = self.apply(self.claimed(self.original()))
        state = app.advance(state, 'probe1', 'My saved comment.')
        for old_phase in ('synthesis_pending', 'synthesis_generating', 'generation_failed', 'confirm'):
            legacy = copy.deepcopy(state)
            record = legacy['bot_interviews']['probe1']
            record['phase'] = old_phase
            record['retry_kind'] = 'synthesis'
            record['pending'] = {'id': 'old-request', 'kind': 'synthesis', 'expires_at': 0}
            record['proposed_answer'] = 'An older AI proposal is not the student final answer.'
            saved = copy.deepcopy(legacy)
            self.assertEqual(ai.phase(legacy, 'probe1'), 'confirm')
            with self.assertRaises(ValueError):
                self.claimed(legacy)
            with self.assertRaises(ai.BotError):
                ai.generate(record, client=SimpleNamespace(responses=FakeResponses([])))
            final = app.advance(legacy, 'probe1', {'action': 'confirm'})
            check_update(legacy, final)
            self.assertEqual(final['answers']['Ans1_final'], state['answers']['Ans1'])
            self.assertEqual(final['bot_interviews']['probe1']['calls'], record['calls'])
            self.assertEqual(legacy, saved)

    def test_private_answer_key_and_prompts_are_not_in_student_download_or_csv(self):
        config = ai.load_config()
        for case in config['cases'].values():
            case['correct_answer'] = 'PRIVATE_KEY_SENTINEL'
        state = ai.claim_generation(self.original(), 'probe1', app.bot_context(self.original(), 'probe1'), config=config)
        state = self.apply(state)
        self.assertIn('PRIVATE_KEY_SENTINEL', json.dumps(state))
        self.assertNotIn('PRIVATE_KEY_SENTINEL', json.dumps(ai.student_download(state)))
        self.assertNotIn('PRIVATE_KEY_SENTINEL', app.csv_export([state]))
        self.assertTrue(ai.public_interviews(state)['probe1']['messages'])

    def test_saved_bot_message_cannot_be_changed(self):
        state = self.apply(self.claimed(self.original()))
        altered = copy.deepcopy(state)
        altered['revision'] += 1
        altered['bot_interviews']['probe1']['messages'][0]['content'] = 'A rewritten objection'
        with self.assertRaises(ValueError):
            check_update(state, altered)


class BotUITests(unittest.TestCase):
    def test_comment_opens_collapsed_history_and_student_final_editor_without_ai_call(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'ASSIGNMENT_PASSWORD': '', 'JUDGEMENT_DB': str(Path(folder) / 'test.sqlite3'), 'STORAGE_BACKEND': 'sqlite'
        }), patch.object(ai, 'generate', side_effect=fake_bot) as model:
            ui = AppTest.from_file(app.__file__).run()
            enter_details_and_begin(ui)
            ui.text_area[0].set_value('My original answer.')
            ui.button[0].click().run()
            self.assertEqual(model.call_count, 1)
            ui.chat_input[0].set_value('My comment on the AI objection.').run()
            token = ui.query_params['response']
            store = app.Store(app.database_path())
            self.assertEqual(ai.phase(store.load(token), 'probe1'), 'confirm')
            page = AppTest.from_file(app.__file__)
            page.query_params['response'] = token
            page.run()
            sign_in(page)
            self.assertFalse(page.exception)
            self.assertEqual(len(page.chat_input), 0)
            self.assertEqual(page.text_area(key='probe1_final_answer').value, 'My original answer.')
            history = next(block for block in page.expander if block.label == app.UI['conversation_review'])
            self.assertFalse(history.proto.expanded)
            self.assertTrue(any(message.value == 'My comment on the AI objection.' for message in history.markdown))
            self.assertEqual(model.call_count, 1)
            page.radio[0].set_value('correct')
            page.text_area(key='probe1_final_answer').set_value('My revised and final answer.')
            page.button[0].click().run()
            self.assertFalse(page.exception)
            saved = store.load(token)
            self.assertEqual(saved['stage'], 'ans2')
            self.assertEqual(saved['answers']['Ans1'], 'My original answer.')
            self.assertEqual(saved['answers']['Ans1_final'], 'My revised and final answer.')
            self.assertEqual(model.call_count, 1)
            altered = copy.deepcopy(saved)
            altered['revision'] += 1
            altered['answers']['Ans1_final'] = 'An impermissible later change'
            with self.assertRaises(ValueError):
                store.commit(token, saved, altered)

    def test_failed_save_reuses_api_result_without_another_model_call(self):
        from streamlit.testing.v1 import AppTest
        client = FakeDropbox()
        store = storage.DropboxStore(client, app.new_state)
        def generate_then_fail_save(record):
            result = fake_bot(record)
            client.fail_write = True
            return result
        with patch.dict(os.environ, {'ASSIGNMENT_PASSWORD': '', 'STORAGE_BACKEND': 'dropbox'}), patch.object(
            storage, 'configured_store', side_effect=lambda factory: storage.DropboxStore(client, factory)
        ), patch.object(ai, 'generate', side_effect=generate_then_fail_save) as model:
            ui = AppTest.from_file(app.__file__).run()
            enter_details_and_begin(ui)
            ui.text_area[0].set_value('My answer')
            ui.button[0].click().run()
            self.assertFalse(ui.exception)
            self.assertTrue(ui.error)
            token = ui.query_params['response']
            self.assertEqual(ai.phase(store.load(token), 'probe1'), 'challenge_generating')
            self.assertEqual(model.call_count, 1)
            client.fail_write = False
            ui.button(key='probe1_retry_save_bot').click().run()
            self.assertFalse(ui.exception)
            self.assertEqual(ai.phase(store.load(token), 'probe1'), 'reply')
            self.assertEqual(model.call_count, 1)

    def test_api_failure_waits_for_explicit_retry(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'ASSIGNMENT_PASSWORD': '', 'JUDGEMENT_DB': str(Path(folder) / 'test.sqlite3'), 'STORAGE_BACKEND': 'sqlite'
        }), patch.object(ai, 'generate', side_effect=ai.BotError('Temporary failure')) as model:
            ui = AppTest.from_file(app.__file__).run()
            enter_details_and_begin(ui)
            ui.text_area[0].set_value('My answer')
            ui.button[0].click().run()
            token = ui.query_params['response']
            self.assertFalse(ui.exception)
            self.assertEqual(ai.phase(app.Store(app.database_path()).load(token), 'probe1'), 'generation_failed')
            ui.run()
            self.assertEqual(model.call_count, 1)
            model.side_effect = fake_bot
            ui.button(key='probe1_retry_bot').click().run()
            self.assertFalse(ui.exception)
            self.assertEqual(model.call_count, 2)
            self.assertEqual(ai.phase(app.Store(app.database_path()).load(token), 'probe1'), 'reply')


if __name__ == '__main__':
    unittest.main()
