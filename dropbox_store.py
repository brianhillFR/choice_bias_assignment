"""Dropbox store: one JSON per response, de-identified results and an identity log."""
from copy import deepcopy
import csv
import io
import json
import re
import secrets
from functools import lru_cache

from response_export import migrate_rows

from response_identity import (attach_identity, check_update, email_key, new_subject_id,
                               progress_key, response_key, resume_token, valid_subject_id)


class StorageError(RuntimeError):
    """A configured persistent store could not complete an operation."""


class StaleResponse(ValueError):
    """Another tab has already advanced this response."""


def setting(name, default=None):
    import os
    if name in os.environ:
        return os.environ[name]
    try:
        import streamlit as st
        return st.secrets.get(name, default)
    except (ImportError, FileNotFoundError):
        return default


@lru_cache(maxsize=1)
def _client(key, secret, refresh_token):
    import dropbox
    return dropbox.Dropbox(app_key=key, app_secret=secret,
                           oauth2_refresh_token=refresh_token,
                           timeout=20, max_retries_on_error=2, max_retries_on_rate_limit=1)


def configured_store(new_state):
    names = ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN')
    values = [str(setting(name, '')).strip() for name in names]
    if not all(values):
        raise StorageError('Set DROPBOX_APP_KEY, DROPBOX_APP_SECRET and DROPBOX_REFRESH_TOKEN in Streamlit secrets.')
    return DropboxStore(_client(*values), new_state,
                        str(setting('DROPBOX_ROOT_PATH', '/choice_bias_assignment')),
                        str(setting('DROPBOX_CSV_NAME', 'answers.csv')),
                        str(setting('DROPBOX_SUBJECT_CSV_NAME', 'subjects.csv')))


def path_error(error, tag):
    """Classify SDK tagged errors without treating connection errors as not-found."""
    union = getattr(error, 'error', None)
    if not getattr(union, 'is_path', lambda: False)():
        return False
    path = union.get_path()
    # UploadError.path contains an UploadWriteFailed wrapping WriteError.
    path = getattr(path, 'reason', path)
    return getattr(path, 'is_' + tag, lambda: False)()


class DropboxStore:
    def __init__(self, client, new_state, root='/choice_bias_assignment', csv_name='answers.csv',
                 subject_csv_name='subjects.csv'):
        root = '/' + root.strip('/')
        if root == '/' or any(part in ('.', '..', '') for part in root[1:].split('/')):
            raise StorageError('DROPBOX_ROOT_PATH must name a dedicated folder, e.g. /choice_bias_assignment.')
        if '/' in csv_name or '\\' in csv_name or not csv_name.endswith('.csv'):
            raise StorageError('DROPBOX_CSV_NAME must be a CSV filename, e.g. answers.csv.')
        if ('/' in subject_csv_name or '\\' in subject_csv_name or
                not subject_csv_name.endswith('.csv') or subject_csv_name == csv_name):
            raise StorageError('DROPBOX_SUBJECT_CSV_NAME must name a separate CSV file, e.g. subjects.csv.')
        self.client, self.new_state, self.root = client, new_state, root
        self.csv_path = root + '/' + csv_name
        self.subject_csv_path = root + '/' + subject_csv_name
        self._known_responses = {}

    def use_session_cache(self, cache):
        """Share confirmed snapshots across reruns of this browser only.

        Keep store instances short-lived so a live update cannot retain an old
        class. Changes of account or folder discard the browser's old snapshots.
        """
        scope = ('confirmed-v2', id(self.client), self.root, self.csv_path)
        if cache.get('scope') != scope:
            cache.clear()
            cache.update(scope=scope, responses={})
        self._known_responses = cache['responses']

    def _remember(self, path, metadata, state):
        self._known_responses[path] = (metadata, deepcopy(state))

    def _forget(self, path):
        self._known_responses.pop(path, None)

    def load_for_screen(self, token):
        return self.load_for_screen_trusted(token)

    def load_for_screen_trusted(self, token, trusted_generation_ids=()):
        """Reuse this browser's confirmed progress until a fresh read is needed.

        Time spent writing an answer does not make a snapshot unsafe: every
        submission still uses Dropbox's atomic revision check. A new browser,
        explicit load, missing snapshot, or failed save requires a remote read.
        """
        path = self.response_path(token)
        cached = self._known_responses.get(path)
        if cached is not None:
            state = cached[1]
            # Someone else's bot generation may have finished since this tab's
            # last read. The "check again" button must see that immediately.
            generating = {record.get('pending', {}).get('id')
                          for record in state.get('bot_interviews', {}).values()
                          if record.get('phase', '').endswith('_generating')}
            generating.discard(None)
            if not generating or generating.issubset(set(trusted_generation_ids)):
                return deepcopy(state)
        return self.load(token)

    def response_path(self, token):
        return self.root + '/' + response_key(token) + '.json'

    def _read(self, path):
        try:
            metadata, response = self.client.files_download(path)
            try:
                state = json.loads(response.content)
            finally:
                response.close()
            return metadata, state
        except Exception as error:
            if path_error(error, 'not_found'):
                return None
            raise StorageError('Could not read the saved response from Dropbox. Please retry.') from error

    def _upload(self, path, state, mode):
        return self._upload_bytes(path, json.dumps(state, ensure_ascii=False).encode('utf-8'), mode)

    def _upload_bytes(self, path, payload, mode):
        try:
            return self.client.files_upload(payload, path, mode=mode,
                                            autorename=False, strict_conflict=True, mute=True)
        except Exception as error:
            if path_error(error, 'conflict'):
                raise StaleResponse('This response was updated in another tab. Reload to continue.') from error
            raise StorageError('Dropbox could not confirm the save. Keep your answer and reload before retrying.') from error

    def _email_path(self, email):
        return self.root + '/_email_index/' + email_key(email) + '.json'

    def _subject_id(self, identity, preferred=''):
        """Find or atomically allocate one unique code in subjects.csv."""
        from dropbox.files import WriteMode
        for attempt in range(5):
            metadata = None
            try:
                metadata, response = self.client.files_download(self.subject_csv_path)
                try:
                    reader = csv.DictReader(io.StringIO(response.content.decode('utf-8-sig')))
                    header = reader.fieldnames or []
                    rows = list(reader)
                finally:
                    response.close()
            except Exception as error:
                if not path_error(error, 'not_found'):
                    raise StorageError('Could not read subjects.csv. Please retry registration.') from error
                header, rows = ['SubjectId', 'name', 'email'], []
            required = {'SubjectId', 'name', 'email'}
            if not required.issubset(header):
                raise StorageError('subjects.csv must contain SubjectId, name and email columns.')
            ids = [row.get('SubjectId', '') for row in rows]
            if any(not valid_subject_id(value) for value in ids) or len(ids) != len(set(ids)):
                raise StorageError('subjects.csv contains an invalid or duplicate SubjectId.')
            matches = [row for row in rows
                       if row.get('email', '').strip().casefold() == identity['email'].strip().casefold()]
            if matches:
                code = matches[0]['SubjectId']
                if len(matches) > 1 or any(row['SubjectId'] != code for row in matches):
                    raise StorageError('subjects.csv contains duplicate entries for this email.')
                if preferred and preferred != code:
                    raise StorageError('The saved subject ID conflicts with subjects.csv.')
                return code
            if preferred:
                if not valid_subject_id(preferred):
                    raise StorageError('The saved subject ID must contain exactly eight digits.')
                if preferred in ids:
                    raise StorageError('The saved subject ID is already assigned to another subject.')
                code = preferred
            else:
                code = new_subject_id()
                while code in ids:
                    code = new_subject_id()
            rows.append({'SubjectId': code, 'name': identity['name'], 'email': identity['email']})
            output = io.StringIO(newline='')
            writer = csv.DictWriter(output, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
            mode = WriteMode.update(metadata.rev) if metadata else WriteMode.add
            try:
                self._upload_bytes(self.subject_csv_path, output.getvalue().encode('utf-8-sig'), mode)
                return code
            except StaleResponse:
                continue
        raise StorageError('Other registrations are updating subjects.csv. Please retry.')

    def ensure_subject_identity(self, token, state):
        """Backfill a legacy response and its subject-log row without changing identity."""
        identity = {'name': state['answers'].get('name', ''),
                    'email': state['answers'].get('email', '')}
        if not all(identity.values()):
            return state
        for attempt in range(3):
            identity['subject_id'] = self._subject_id(identity, state.get('subject_id', ''))
            updated = attach_identity(state, identity)
            if updated == state:
                return state
            try:
                self.commit(token, state, updated)
                return updated
            except StaleResponse:
                state = self.load(token)
                if state is None:
                    raise StorageError('The saved response for this email could not be found.')
        raise StorageError('This response is being updated in another browser. Please retry.')

    def _bootstrap_email_index(self):
        """One-time backfill allows pre-index JSON records to resume by email."""
        from dropbox.files import WriteMode
        marker = self.root + '/_email_index/ready.json'
        if self._read(marker):
            return False
        best = {}
        for key, state in self._response_records():
            email = state['answers'].get('email')
            if email:
                hashed = email_key(email)
                if hashed not in best or progress_key(state) > progress_key(best[hashed][1]):
                    best[hashed] = (key, state)
        for hashed, (key, state) in best.items():
            try:
                self._upload(self.root + '/_email_index/' + hashed + '.json',
                             {'response_key': key}, WriteMode.add)
            except StaleResponse:
                pass  # Another browser already registered this email.
        try:
            self._upload(marker, {'version': 1}, WriteMode.add)
        except StaleResponse:
            pass
        return True

    def resume_by_email(self, email):
        from email_lookup import resume_dropbox_by_email
        return resume_dropbox_by_email(self, email)

    def start_known_missing(self, identity, token_hint=None):
        return self.start_or_resume(identity, token_hint=token_hint, known_missing=True)

    def start_or_resume(self, identity, token_hint=None, known_missing=False):
        """Claim one response per email without duplicating or resetting answers."""
        from dropbox.files import WriteMode
        index_path = self._email_path(identity['email'])
        # The email-entry screen has just completed this lookup for a new
        # participant. Go straight to the atomic add; a concurrent registration
        # still wins safely and is read in the conflict handler below.
        indexed = None if known_missing else self._read(index_path)
        if indexed is None and not known_missing and self._bootstrap_email_index():
            # Legacy backfill may just have found this email's original response.
            indexed = self._read(index_path)
        created = False
        allocated_subject_id = None
        if indexed is None:
            hint = self.load(token_hint) if token_hint else None
            if hint and not hint['answers'].get('email'):
                registration = {'response_key': response_key(token_hint)}
            else:
                key = response_key(secrets.token_urlsafe(32))
                registration = {'response_key': key,
                                'initial_state': attach_identity(self.new_state(), identity)}
            try:
                # The successful upload already returns its authoritative revision.
                # Keep the initial state in the index until the JSON is confirmed.
                metadata = self._upload(index_path, registration, WriteMode.add)
                indexed = metadata, registration
                created = True
            except StaleResponse:
                indexed = self._read(index_path)
        if indexed is None:
            raise StorageError('Could not confirm the saved email lookup. Please retry.')
        registration = indexed[1]
        token = resume_token(registration['response_key'])
        path = self.response_path(token)
        saved = None
        if created and 'initial_state' in registration:
            allocated_subject_id = self._subject_id(identity)
            identified = dict(identity, subject_id=allocated_subject_id)
            registration['initial_state'] = attach_identity(registration['initial_state'], identified)
            # We just claimed this random response key. Add mode still protects
            # against a collision or another browser recovering the registration.
            try:
                initial = registration['initial_state']
                saved = self._upload(path, initial, WriteMode.add), initial
            except StaleResponse:
                pass
        if saved is None:
            saved = self._read(path)
        if saved is None:
            if 'initial_state' not in registration:
                raise StorageError('The saved response for this email could not be found.')
            try:
                initial = registration['initial_state']
                saved = self._upload(path, initial, WriteMode.add), initial
            except StaleResponse:
                saved = self._read(path)
        for attempt in range(3):
            if saved is None:
                raise StorageError('Could not confirm the saved response. Please retry.')
            metadata, state = saved
            if 'initial_state' in registration:
                # Never use the recovery snapshot to reset an existing JSON.
                try:
                    self._upload(index_path, {'response_key': registration['response_key']},
                                 WriteMode.update(indexed[0].rev))
                except StaleResponse:
                    pass
                registration.pop('initial_state')
            identified = dict(identity)
            identified['subject_id'] = state.get('subject_id') or allocated_subject_id
            if not valid_subject_id(identified['subject_id']):
                identified['subject_id'] = self._subject_id(identity, identified['subject_id'] or '')
            updated = attach_identity(state, identified)
            if updated == state:
                self._remember(path, metadata, state)
                return token, state
            check_update(state, updated)
            try:
                metadata = self._upload(path, updated, WriteMode.update(metadata.rev))
                self._remember(path, metadata, updated)
                return token, updated
            except StaleResponse:
                saved = self._read(path)
        raise StorageError('This response is being updated in another browser. Please retry.')

    def create(self):
        from dropbox.files import WriteMode
        token = secrets.token_urlsafe(32)
        state = self.new_state()
        # Dropbox creates missing ancestor folders when uploading a file.
        path = self.response_path(token)
        metadata = self._upload(path, state, WriteMode.add)
        self._remember(path, metadata, state)
        return token, state

    def load(self, token):
        path = self.response_path(token)
        # Explicit loads always read Dropbox, including email-based resume.
        try:
            result = self._read(path)
        except StorageError:
            self._forget(path)
            raise
        if result is None:
            self._forget(path)
            return None
        metadata, state = result
        self._remember(path, metadata, state)
        return state

    def commit(self, token, previous, updated):
        from dropbox.files import WriteMode
        path = self.response_path(token)
        result = self._known_responses.get(path)
        if (result is None or result[1]['revision'] != previous['revision'] or
                result[1]['response_id'] != previous['response_id']):
            try:
                result = self._read(path)
            except StorageError:
                self._forget(path)
                raise
        if result is None:
            self._forget(path)
            raise StaleResponse('This response no longer exists. Please contact the instructor.')
        metadata, current = result
        if (current['revision'] != previous['revision'] or
                current['response_id'] != previous['response_id']):
            self._forget(path)
            raise StaleResponse('This response was updated in another tab. Reload to continue.')
        check_update(current, updated)
        # Dropbox atomically rejects a save if any other tab has changed the
        # remote revision since load/the last successful upload.
        try:
            metadata = self._upload(path, updated, WriteMode.update(metadata.rev))
        except (StorageError, StaleResponse):
            self._forget(path)
            raise
        self._remember(path, metadata, updated)

    def sync_completed_csv(self, token, state, row):
        """Merge just this completed row, retrying concurrent aggregate writes."""
        from dropbox.files import WriteMode
        if state['stage'] != 'complete':
            raise ValueError('Only completed responses can enter the results CSV.')
        if not valid_subject_id(state.get('subject_id')):
            state = self.ensure_subject_identity(token, state)
        if not valid_subject_id(state.get('subject_id')):
            raise StorageError('This completed response has no valid SubjectId. Please contact the instructor.')
        if state.get('csv_synced'):
            return state
        row = dict(row)
        row['SubjectId'] = state['subject_id']
        new_row = {key: '' if value is None else str(value) for key, value in row.items()}
        for attempt in range(5):
            metadata = None
            try:
                metadata, response = self.client.files_download(self.csv_path)
                try:
                    reader = csv.DictReader(io.StringIO(response.content.decode('utf-8-sig')))
                    header = reader.fieldnames or []
                    rows = list(reader)
                finally:
                    response.close()
            except Exception as error:
                if not path_error(error, 'not_found'):
                    raise StorageError('Could not read answers.csv. Your completed response is saved as JSON; retry finalising.') from error
                header, rows = [], []
            if header and not ({'SubjectId', 'ResponseId'} & set(header)):
                raise StorageError('The existing results CSV needs a SubjectId or legacy ResponseId column before it can be merged safely.')
            # Give identifiable legacy rows codes before the migration removes
            # their names, emails and internal ResponseIds.
            for existing in rows:
                if not existing.get('SubjectId') and existing.get('email'):
                    existing['SubjectId'] = self._subject_id(
                        {'name': existing.get('name', ''), 'email': existing['email']})
                if (not existing.get('SubjectId') and
                        existing.get('ResponseId') == state.get('response_id')):
                    existing['SubjectId'] = state['subject_id']
            try:
                header, rows = migrate_rows(header, rows, new_row)
            except ValueError as error:
                raise StorageError('Could not update the existing CSV layout. Your completed JSON is saved. ' + str(error)) from error
            matches = [i for i, existing in enumerate(rows)
                       if existing.get('SubjectId') == new_row['SubjectId']]
            if matches:
                rows[matches[0]].update(new_row)
                rows = [existing for i, existing in enumerate(rows) if i not in matches[1:]]
            else:
                rows.append(new_row)
            output = io.StringIO(newline='')
            writer = csv.DictWriter(output, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
            mode = WriteMode.update(metadata.rev) if metadata else WriteMode.add
            try:
                self._upload_bytes(self.csv_path, output.getvalue().encode('utf-8-sig'), mode)
                break
            except StaleResponse:
                continue
        else:
            raise StorageError('Other submissions are updating the results CSV. Your JSON is saved; retry finalising.')

        # Mark the JSON only after the aggregate save succeeds. Retries merge by
        # SubjectId, so a crash between the two writes cannot duplicate a row.
        updated = json.loads(json.dumps(state))
        updated['csv_synced'] = True
        updated['revision'] += 1
        try:
            self.commit(token, state, updated)
        except StaleResponse:
            current = self.load(token)
            if current and current.get('csv_synced'):
                return current
            raise
        return updated

    def _response_records(self):
        from dropbox.files import FileMetadata
        states = []
        try:
            page = self.client.files_list_folder(self.root)
            while True:
                for entry in page.entries:
                    if isinstance(entry, FileMetadata) and re.fullmatch(r'[0-9a-f]{64}\.json', entry.name):
                        result = self._read(entry.path_lower)
                        if result:
                            states.append((entry.name[:-5], result[1]))
                if not page.has_more:
                    break
                page = self.client.files_list_folder_continue(page.cursor)
        except StorageError:
            raise
        except Exception as error:
            if path_error(error, 'not_found'):
                return []
            raise StorageError('Could not list response files in Dropbox.') from error
        return sorted(states, key=lambda item: item[1]['started_at'])

    def all_responses(self):
        return [state for key, state in self._response_records()]
