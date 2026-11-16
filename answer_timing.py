"""Elapsed answer-screen times, persisted in ordinary response checkpoints."""
from datetime import datetime, timezone

STEPS = ('Ans1', 'Ans1_bot_reply', 'Ans1_final',
         'Ans2', 'Ans2_bot_reply', 'Ans2_final')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def step_key(state):
    stage = state['stage']
    if stage in ('ans1', 'ans2'):
        return 'Ans1' if stage == 'ans1' else 'Ans2'
    if stage in ('probe1', 'probe2'):
        prefix = 'Ans1' if stage == 'probe1' else 'Ans2'
        record = state.get('bot_interviews', {}).get(stage, {})
        phase = record.get('phase')
        if phase == 'reply':
            return prefix + '_bot_reply'
        if phase in ('confirm', 'synthesis_pending', 'synthesis_generating') or (
                phase == 'generation_failed' and record.get('retry_kind') == 'synthesis'):
            return prefix + '_final'
    return None


def start_step(state, at=None, basis='server_checkpoint'):
    """Mark a newly prepared screen; callers include it in the normal save."""
    step = step_key(state)
    if step is not None and state.get('current_timing', {}).get('step') != step:
        state['current_timing'] = {'step': step, 'started_at': at or utc_now(), 'basis': basis}
    return state


def finish_step(previous, updated, at=None):
    """Close a validated answer's timer before saving, then prepare the next one."""
    submitted_at = at or utc_now()
    step = step_key(previous)
    timer = previous.get('current_timing', {})
    if step is not None and timer.get('step') == step:
        elapsed = (datetime.fromisoformat(submitted_at) -
                   datetime.fromisoformat(timer['started_at'])).total_seconds()
        updated.setdefault('timings', {})[step] = {
            'started_at': timer['started_at'], 'submitted_at': submitted_at,
            'seconds': round(max(0, elapsed), 3), 'basis': timer['basis'],
        }
    updated.pop('current_timing', None)
    return start_step(updated, submitted_at)


def export_columns(state):
    columns = {}
    for step in STEPS:
        record = state.get('timings', {}).get(step, {})
        for key in ('seconds', 'started_at', 'submitted_at'):
            columns[step + '_' + key] = record.get(key, '')
        columns[step + '_timing_basis'] = record.get('basis', '')
    return columns
