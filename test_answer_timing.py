"""Answer timing, reconnects and exports; no live API or Dropbox calls."""
import copy
import json
import unittest
from unittest.mock import patch

import answer_timing as timing
import ai_interview as ai
import judgement_biases_interview as app
from response_identity import check_update
from test_judgement_biases_interview import ready_state, fake_bot


def stamp(seconds):
    return '2026-10-21T10:%02d:%02d+00:00' % divmod(seconds, 60)


class AnswerTimingTests(unittest.TestCase):
    def test_durable_screen_times_exclude_ai_wait_and_survive_reconnect(self):
        with patch.object(app, 'utc_now', return_value=stamp(0)):
            state = ready_state()
        self.assertEqual(state['current_timing']['step'], 'Ans1')
        with patch.object(app, 'utc_now', return_value=stamp(30)):
            answered = app.advance(state, 'ans1', 'My original answer')
        check_update(state, answered)
        self.assertEqual(answered['timings']['Ans1']['seconds'], 30)
        self.assertNotIn('current_timing', answered)
        claimed = ai.claim_generation(answered, 'probe1', app.bot_context(answered, 'probe1'))
        with patch.object(ai, 'now', return_value=stamp(120)):
            reply_screen = ai.apply_generation(claimed, 'probe1', claimed['bot_interviews']['probe1']['pending']['id'],
                                               fake_bot(claimed['bot_interviews']['probe1']))
        self.assertEqual(reply_screen['current_timing']['started_at'], stamp(120))
        # A new browser/server restores the same clock from JSON, without a
        # separate timer save or reconstructing it from the assignment start.
        resumed = json.loads(json.dumps(reply_screen))
        timing.start_step(resumed, stamp(150))
        with patch.object(app, 'utc_now', return_value=stamp(160)):
            final_screen = app.advance(resumed, 'probe1', 'My comment')
        check_update(resumed, final_screen)
        self.assertEqual(final_screen['timings']['Ans1_bot_reply']['seconds'], 40)
        with patch.object(app, 'utc_now', return_value=stamp(180)):
            next_case = app.advance(final_screen, 'probe1', {'action': 'confirm'})
        check_update(final_screen, next_case)
        self.assertEqual(next_case['timings']['Ans1_final']['seconds'], 20)
        self.assertEqual(next_case['current_timing']['step'], 'Ans2')
        self.assertEqual(next_case['current_timing']['started_at'], stamp(180))

    def test_invalid_submission_preserves_clock_and_recorded_times_are_locked(self):
        state = ready_state()
        before = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            app.advance(state, 'ans1', '   ')
        self.assertEqual(state, before)
        answered = app.advance(state, 'ans1', 'My answer')
        altered = copy.deepcopy(answered)
        altered['timings']['Ans1']['seconds'] = 999
        altered['revision'] += 1
        with self.assertRaisesRegex(ValueError, 'timings'):
            check_update(answered, altered)

    def test_legacy_screen_uses_explicit_fallback_and_historical_times_stay_blank(self):
        state = ready_state()
        state.pop('current_timing')
        self.assertEqual(app.export_row(state)['main_first_reply_seconds'], '')
        timing.start_step(state, stamp(10), basis='browser_session')
        timing.start_step(state, stamp(20), basis='browser_session')
        with patch.object(app, 'utc_now', return_value=stamp(35)):
            answered = app.advance(state, 'ans1', 'My answer')
        row = app.export_row(answered)
        self.assertEqual(row['main_first_reply_seconds'], 25)
        self.assertEqual(row['main_first_reply_started_at'], stamp(10))
        self.assertEqual(row['main_first_reply_submitted_at'], stamp(35))
        self.assertEqual(row['main_first_reply_timing_basis'], 'browser_session')
        self.assertEqual(row['review_first_reply_seconds'], '')
        self.assertEqual(row['main_reply_to_feedback_seconds'], '')
