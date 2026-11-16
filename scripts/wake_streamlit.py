"""Visit only the assignment entry screen; never enter or submit survey data."""
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

DEFAULT_URL = 'https://choice-biases-assignment.streamlit.app/'
WAKE_BUTTON = re.compile(r'^Yes,?\s*get this app back up!?$', re.IGNORECASE)
ACCESS_HEADING = re.compile(r'You do not have access to this app or it does not exist', re.IGNORECASE)


def app_url(value):
    """Prevent a participant resume link or credentials from entering the job."""
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or
            not parsed.hostname.endswith('.streamlit.app') or
            parsed.username or parsed.password or parsed.port or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('STREAMLIT_APP_URL must be the https://NAME.streamlit.app/ entry URL, without a participant link.')
    return value


def browser_state(value):
    if not value.strip():
        return None
    try:
        state = json.loads(value)
    except ValueError as error:
        raise ValueError('STREAMLIT_BROWSER_STATE must contain valid Playwright storage-state JSON.') from error
    if not isinstance(state, dict) or not isinstance(state.get('cookies', []), list) or not isinstance(state.get('origins', []), list):
        raise ValueError('STREAMLIT_BROWSER_STATE must contain a Playwright storage-state object.')
    return state


def entry_labels():
    content = Path(__file__).resolve().parents[1] / 'assignment_content.toml'
    ui = tomllib.loads(content.read_text(encoding='utf-8'))['ui']
    return ui['email_label'], ui['email_confirm_button']


def wait_for_entry(page, labels, timeout_seconds=240, clock=time.monotonic, transient_errors=()):
    """Allow the app to boot inside a Cloud wrapper iframe as well as directly."""
    deadline = clock() + timeout_seconds
    started = clock()
    wake_clicked = False
    while clock() < deadline:
        access_denied = False
        for frame in page.frames:
            try:
                email = frame.get_by_role('textbox', name=labels[0], exact=True).first
                confirm = frame.get_by_role('button', name=labels[1], exact=True).first
                if email.is_visible() and confirm.is_visible():
                    return wake_clicked
                wake = frame.get_by_role('button', name=WAKE_BUTTON).first
                if not wake_clicked and wake.is_visible():
                    wake.click(timeout=5000)
                    wake_clicked = True
                access_denied |= frame.get_by_role('heading', name=ACCESS_HEADING).first.is_visible()
            except transient_errors:
                # Navigation can remove a wrapper iframe while the app wakes.
                continue
        if access_denied and clock() - started >= 10:
            raise RuntimeError('Streamlit returned an access/not-found page. Check STREAMLIT_APP_URL. '
                               'For a private app, configure the STREAMLIT_BROWSER_STATE GitHub secret '
                               'with a session authorised to view it; the job does not change app sharing.')
        page.wait_for_timeout(1000)
    raise RuntimeError('The assignment email-entry screen did not become ready within %s seconds. ' % timeout_seconds +
                       'Check the app URL, viewer access and Streamlit deployment logs.')


def main():
    from playwright.sync_api import Error as BrowserError, sync_playwright
    url = app_url(os.environ.get('STREAMLIT_APP_URL', DEFAULT_URL))
    state = browser_state(os.environ.get('STREAMLIT_BROWSER_STATE', ''))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=state)
            page = context.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            clicked = wait_for_entry(page, entry_labels(), transient_errors=(BrowserError,))
            # Maintain the actual Streamlit browser connection briefly. Recheck
            # readiness before reporting success, without filling any input.
            page.wait_for_timeout(15000)
            wait_for_entry(page, entry_labels(), timeout_seconds=30, transient_errors=(BrowserError,))
        finally:
            browser.close()
    message = ('Woke the sleeping app and verified its email-entry screen.' if clicked else
               'App was already awake; verified its email-entry screen.')
    print(message)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write(message + '\n\nNo survey data was entered and no model calls were made.\n')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not log browser storage, cookies, page bodies or participant data.
        detail = str(error) if isinstance(error, (ValueError, RuntimeError, ImportError)) else 'Browser request failed; check viewer access and deployment logs.'
        print('Wake-up check failed: ' + detail, file=sys.stderr)
        sys.exit(1)
