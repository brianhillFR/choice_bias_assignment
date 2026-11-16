"""Shared response lookup and rules for keeping submitted answers immutable."""
import hashlib
import json
import re
import secrets

STAGE_ORDER = ('identity', 'instructions', 'ans1', 'probe1', 'ans2', 'probe2', 'complete')


def new_subject_id():
    """Return an eight-digit, non-sequential identifier.

    Uniqueness is enforced by the storage backend, which retries if this
    candidate is already present in the subject log.
    """
    return str(secrets.randbelow(90_000_000) + 10_000_000)


def valid_subject_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]{8}', value) is not None


def response_key(token):
    # New resume handles reference the same random key as older private links.
    # This lets existing JSON responses resume without copying or reassigning them.
    if re.fullmatch(r'resume_[0-9a-f]{64}', token):
        return token[len('resume_'):]
    return hashlib.sha256(token.encode()).hexdigest()


def resume_token(key):
    if not re.fullmatch(r'[0-9a-f]{64}', key):
        raise ValueError('Invalid saved response reference.')
    return 'resume_' + key


def email_key(email):
    return hashlib.sha256(email.strip().casefold().encode('utf-8')).hexdigest()


def attach_identity(state, identity):
    """Fill missing entry details; resuming never changes a saved name or answer."""
    saved_email = state['answers'].get('email')
    if saved_email and email_key(saved_email) != email_key(identity['email']):
        raise ValueError('This response belongs to a different email address.')
    saved_subject = state.get('subject_id')
    incoming_subject = identity.get('subject_id')
    if saved_subject and incoming_subject and saved_subject != incoming_subject:
        raise ValueError('This response belongs to a different subject ID.')
    updated = json.loads(json.dumps(state))
    for key in ('name', 'email'):
        if not updated['answers'].get(key):
            updated['answers'][key] = identity[key]
    if not updated.get('subject_id') and incoming_subject:
        if not valid_subject_id(incoming_subject):
            raise ValueError('Subject IDs must contain exactly eight digits.')
        updated['subject_id'] = incoming_subject
    if updated['stage'] == 'identity':
        updated['stage'] = 'instructions'
    if updated != state:
        updated['revision'] += 1
        if state['stage'] == 'complete':
            updated['csv_synced'] = False
    return updated


def check_update(current, updated):
    """Reject answer edits, case reassignment, and backwards stage changes."""
    if updated['response_id'] != current['response_id'] or updated['assignment'] != current['assignment']:
        raise ValueError('The saved response and case assignment cannot be changed.')
    if updated.get('subject_id') and not valid_subject_id(updated['subject_id']):
        raise ValueError('Subject IDs must contain exactly eight digits.')
    if current.get('subject_id') and updated.get('subject_id') != current['subject_id']:
        raise ValueError('The subject ID cannot be changed.')
    if any(updated['answers'].get(key) != value for key, value in current['answers'].items()):
        raise ValueError('Previously submitted answers cannot be changed.')
    if STAGE_ORDER.index(updated['stage']) < STAGE_ORDER.index(current['stage']):
        raise ValueError('You cannot return to an earlier step.')
    if any(updated.get('timings', {}).get(key) != value
           for key, value in current.get('timings', {}).items()):
        raise ValueError('Previously recorded answer timings cannot be changed.')
    for stage, saved in current.get('bot_interviews', {}).items():
        next_record = updated.get('bot_interviews', {}).get(stage, {})
        for key in ('_config', '_context', 'challenge', 'student_reply', 'proposed_answer',
                    'confirmation_question', 'decision', 'final_answer'):
            if key in saved and next_record.get(key) != saved[key]:
                raise ValueError('Previously saved interview turns cannot be changed.')
        for key in ('messages', 'calls'):
            if next_record.get(key, [])[:len(saved.get(key, []))] != saved.get(key, []):
                raise ValueError('Previously saved interview turns cannot be changed.')
    if updated['revision'] != current['revision'] + 1:
        raise ValueError('Invalid response revision. Reload to continue.')


def progress_key(state):
    return (STAGE_ORDER.index(state.get('resume_stage', state['stage'])),
            state['revision'], state['started_at'])
