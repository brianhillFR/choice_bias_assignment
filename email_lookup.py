"""Email lookup compatible with Dropbox objects retained during a live app update."""
from dropbox_store import StorageError, StaleResponse
from response_identity import email_key, resume_token, valid_subject_id


def resume_existing_response(store, email):
    """Handle a pre-update Dropbox object without reloading shared modules."""
    lookup = getattr(store, 'resume_by_email', None)
    if callable(lookup):
        return lookup(email)
    if callable(getattr(store, '_email_path', None)):
        return resume_dropbox_by_email(store, email)
    raise StorageError('The assignment update needs a server restart. Please contact the instructor.')


def resume_dropbox_by_email(store, email):
    """Return an existing registration; unknown emails never allocate cases."""
    from dropbox.files import WriteMode
    index_path = store._email_path(email)
    indexed = store._read(index_path)
    if indexed is None and store._bootstrap_email_index() is not False:
        # Existing indexed emails need no setup-marker download. A missing
        # lookup still triggers legacy backfill, which may create this entry.
        # Older store implementations return None, so reread after those too.
        indexed = store._read(index_path)
    if indexed is None:
        return None
    registration = indexed[1]
    token = resume_token(registration['response_key'])
    state = store.load(token)
    if state is None:
        initial = registration.get('initial_state')
        if initial is None:
            raise StorageError('The saved response for this email could not be found.')
        if email_key(initial['answers'].get('email', '')) != email_key(email):
            raise StorageError('The saved email lookup does not match this response.')
        try:
            store._upload(store.response_path(token), initial, WriteMode.add)
        except StaleResponse:
            pass  # Another browser recovered the same registration first.
        state = store.load(token)
        if state is None:
            raise StorageError('Could not confirm the saved response. Please retry.')
    if email_key(state['answers'].get('email', '')) != email_key(email):
        raise StorageError('The saved email lookup does not match this response.')
    if not state['answers'].get('name') or state['stage'] == 'identity':
        return None
    if 'initial_state' in registration:
        try:
            store._upload(index_path, {'response_key': registration['response_key']},
                         WriteMode.update(indexed[0].rev))
        except StaleResponse:
            pass
    ensure_identity = getattr(store, 'ensure_subject_identity', None)
    if not valid_subject_id(state.get('subject_id')) and callable(ensure_identity):
        state = ensure_identity(token, state)
    return token, state
