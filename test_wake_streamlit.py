"""Check the keepalive's browser decisions without installing or opening a browser."""
import re
import unittest

from scripts.wake_streamlit import app_url, browser_state, entry_labels, wait_for_entry


class Clock:
    def __init__(self):
        self.seconds = 0

    def __call__(self):
        return self.seconds


class Locator:
    def __init__(self, frame, role, name):
        self.frame, self.role, self.name = frame, role, name
        self.first = self

    def is_visible(self):
        if self.role == 'textbox':
            return self.frame.ready and self.name == 'Email address'
        if self.role == 'heading':
            return self.frame.denied
        if self.role == 'button' and isinstance(self.name, re.Pattern):
            return self.frame.sleeping and bool(self.name.fullmatch('Yes, get this app back up!'))
        return self.frame.ready and self.name == 'Confirm email'

    def click(self, **kwargs):
        if not isinstance(self.name, re.Pattern):
            raise AssertionError('The keepalive must never submit an entry/survey form.')
        self.frame.clicks += 1
        self.frame.ready = True
        self.frame.sleeping = False


class Frame:
    def __init__(self, ready=False, sleeping=False, denied=False):
        self.ready, self.sleeping, self.denied = ready, sleeping, denied
        self.clicks = 0

    def get_by_role(self, role, name, **kwargs):
        return Locator(self, role, name)


class Page:
    def __init__(self, frames, clock):
        self.frames, self.clock = frames, clock

    def wait_for_timeout(self, milliseconds):
        self.clock.seconds += milliseconds / 1000


class WakeStreamlitTests(unittest.TestCase):
    def test_awake_app_requires_no_clicks(self):
        clock, frame = Clock(), Frame(ready=True)
        self.assertFalse(wait_for_entry(Page([frame], clock), entry_labels(), clock=clock))
        self.assertEqual(frame.clicks, 0)

    def test_sleeping_app_in_wrapper_iframe_clicks_only_the_wake_button(self):
        clock, frame = Clock(), Frame(sleeping=True)
        self.assertTrue(wait_for_entry(Page([Frame(), frame], clock), entry_labels(), clock=clock))
        self.assertEqual(frame.clicks, 1)

    def test_private_or_missing_app_fails_with_actionable_message(self):
        clock = Clock()
        with self.assertRaisesRegex(RuntimeError, 'STREAMLIT_BROWSER_STATE'):
            wait_for_entry(Page([Frame(denied=True)], clock), entry_labels(), clock=clock)

    def test_other_pages_do_not_count_as_success(self):
        clock = Clock()
        with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
            wait_for_entry(Page([Frame()], clock), entry_labels(), timeout_seconds=3, clock=clock)

    def test_participant_links_and_other_sites_are_rejected(self):
        valid = 'https://jba-behecon-hec.streamlit.app/'
        self.assertEqual(app_url(valid), valid)
        for invalid in (valid + '?response=private-token', 'https://other.example/',
                        'http://example.streamlit.app/', 'https://example.streamlit.app.evil.example/',
                        'https://user:password@example.streamlit.app/'):
            with self.assertRaises(ValueError):
                app_url(invalid)

    def test_auth_state_is_optional_and_not_echoed_on_invalid_input(self):
        self.assertIsNone(browser_state(''))
        self.assertEqual(browser_state('{"cookies": [], "origins": []}'), {'cookies': [], 'origins': []})
        for invalid in ('private-cookie-do-not-log', '[]', '{"cookies": "secret"}'):
            with self.assertRaises(ValueError) as error:
                browser_state(invalid)
            self.assertNotIn('private-cookie', str(error.exception))
