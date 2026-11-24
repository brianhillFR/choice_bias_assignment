"""One AI objection per case, followed by a student comment and locked final answer."""
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import secrets
from string import Formatter
import time

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    import tomli as tomllib

from dropbox_store import setting
from answer_timing import start_step

CONFIG_PATH = Path(__file__).resolve().with_name('bot_config.toml')
PROBES = ('probe1', 'probe2')
COMMENT_MAX_WORDS = 150
SAFE_CONTEXT_FIELDS = {'question', 'wrong_position'}
SAFE_CONTEXT_TEMPLATE = '''
Current question:
{question}

Position you must defend:
{wrong_position}
'''
SNAPSHOT_KEYS = (
    'model', 'api_key_secret', 'max_output_tokens', 'timeout_seconds',
    'generation_lease_seconds', 'additional_api_kwargs', 'interview_outline',
    'general_instructions', 'question_context_template',
    'reply_request_instruction',
)


class BotError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def load_config(path=CONFIG_PATH):
    config = tomllib.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('provider') != 'openai':
        raise BotError('Set provider to openai in bot_config.toml.')
    for key in ('model', 'interview_outline', 'general_instructions',
                'question_context_template'):
        if not str(config.get(key, '')).strip():
            raise BotError('Missing bot setting: ' + key)
    fields = {field for _, field, _, _ in Formatter().parse(config['question_context_template']) if field}
    if fields - SAFE_CONTEXT_FIELDS:
        raise BotError('question_context_template may use only {question} and {wrong_position}.')
    if set(config.get('cases', {})) != {str(number) for number in range(1, 11)} or any(
        not case.get('wrong_position', '').strip() for case in config['cases'].values()
    ):
        raise BotError('bot_config.toml must provide a wrong position for each of the ten cases.')
    if not isinstance(config.get('additional_api_kwargs', {}), dict):
        raise BotError('additional_api_kwargs must be a TOML table.')
    return config


def phase(state, stage):
    record = state.get('bot_interviews', {}).get(stage, {})
    current = record.get('phase', 'challenge_pending')
    # Older unfinished responses go directly to the student's final-answer page.
    if current in ('synthesis_pending', 'synthesis_generating') or (
        current == 'generation_failed' and record.get('retry_kind') == 'synthesis'
    ):
        return 'confirm'
    return current


def _case(state, stage):
    return state['assignment']['answerqn' if stage == 'probe1' else 'second_case']


def claim_generation(state, stage, context, config=None):
    if state['stage'] != stage or stage not in PROBES:
        raise ValueError('This interview is no longer the current step.')
    current = phase(state, stage)
    existing = state.get('bot_interviews', {}).get(stage, {})
    if current.endswith('_generating'):
        if existing['pending']['expires_at'] > time.time():
            raise ValueError('The bot is already preparing this reply. Please wait.')
        kind = current.removesuffix('_generating')
    elif current == 'generation_failed':
        kind = existing['retry_kind']
    elif current == 'challenge_pending':
        kind = 'challenge'
    else:
        raise ValueError('The bot is waiting for your response.')
    if kind != 'challenge':
        raise ValueError('The AI exchange is complete. Please submit your final answer.')
    updated = deepcopy(state)
    record = updated.setdefault('bot_interviews', {}).setdefault(stage, {'messages': []})
    if '_config' not in record:
        config = config or load_config()
        snapshot = deepcopy({key: config[key] for key in SNAPSHOT_KEYS if key in config})
        snapshot['wrong_position'] = config['cases'][str(_case(state, stage))]['wrong_position']
        record['_config'] = snapshot
        record['_context'] = deepcopy(context)
        record['config_sha256'] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        record['case'] = _case(state, stage)
    record['phase'] = kind + '_generating'
    record['pending'] = {
        'id': secrets.token_hex(16), 'kind': kind,
        'expires_at': time.time() + int(record['_config'].get('generation_lease_seconds', 120)),
    }
    record.pop('retry_kind', None)
    updated['revision'] += 1
    return updated


def generation_failed(state, stage, request_id):
    updated = deepcopy(state)
    record = updated['bot_interviews'][stage]
    if record['pending']['id'] != request_id:
        raise ValueError('A newer bot request is already in progress.')
    record['retry_kind'] = record['pending']['kind']
    record['phase'] = 'generation_failed'
    record.pop('pending')
    updated['revision'] += 1
    return updated


def apply_generation(state, stage, request_id, result):
    if state['stage'] != stage or phase(state, stage) != 'challenge_generating':
        raise ValueError('This bot reply has already been saved.')
    updated = deepcopy(state)
    record = updated['bot_interviews'][stage]
    if record['pending']['id'] != request_id or record['pending']['kind'] != 'challenge':
        raise ValueError('A newer bot request is already in progress.')
    text = result.get('text', '').strip()
    if not text:
        raise BotError('The bot returned an empty response. Please retry.')
    prefix = 'Ans1' if stage == 'probe1' else 'Ans2'
    record['challenge'] = text
    record['phase'] = 'reply'
    updated['answers'][prefix + '_bot_challenge'] = text
    record['messages'].append({'role': 'assistant', 'kind': 'challenge', 'content': text, 'time': now()})
    call = {key: result.get(key) for key in ('provider', 'model', 'response_id', 'usage', 'duration_seconds')}
    call['kind'] = 'challenge'
    record.setdefault('calls', []).append(call)
    record.pop('pending')
    updated['transcript'].append({'role': 'assistant', 'step': stage, 'kind': 'challenge', 'content': text, 'time': now()})
    updated['revision'] += 1
    return start_step(updated, now())


def record_student(state, stage, answer):
    current = phase(state, stage)
    if state['stage'] != stage:
        raise ValueError('This interview has already been submitted.')
    if current not in ('reply', 'confirm'):
        raise ValueError('Please wait for the bot before answering.')
    updated = deepcopy(state)
    record = updated['bot_interviews'][stage]
    prefix = 'Ans1' if stage == 'probe1' else 'Ans2'
    if current == 'reply':
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('Please enter your response to the bot.')
        if len(answer.split()) > COMMENT_MAX_WORDS:
            raise ValueError(f'Please keep your comment to {COMMENT_MAX_WORDS} words or fewer.')
        text = answer
        updated['answers'][prefix + '_bot_reply'] = text
        # Keep the original export alias for existing downstream analyses.
        updated['answers'][prefix + '_reasons'] = text
        record['student_reply'] = text
        record['phase'] = 'confirm'
        kind = 'student_reply'
    else:
        if not isinstance(answer, dict) or answer.get('action') not in ('confirm', 'correct'):
            raise ValueError('Please choose whether to keep your previous answer or change it.')
        original = state['answers'][prefix]
        final = answer.get('final_answer', original if answer['action'] == 'confirm' else '')
        if not isinstance(final, str) or not final.strip():
            raise ValueError('Please enter your complete final answer.')
        if answer['action'] == 'confirm':
            if final.strip() != original.strip():
                raise ValueError('Choose "Change my answer" if you have edited your previous answer.')
            final = original
        updated['answers'].update({prefix + '_final': final, prefix + '_confirmation': answer['action'],
                                   prefix + '_correction': final if answer['action'] == 'correct' else ''})
        text = final
        record['phase'] = 'done'
        record['decision'] = answer['action']
        record['final_answer'] = final
        record.pop('pending', None)
        record.pop('retry_kind', None)
        updated['stage'] = 'ans2' if stage == 'probe1' else 'complete'
        if updated['stage'] == 'complete':
            updated['completed_at'] = now()
        kind = 'final_answer'
    record['messages'].append({'role': 'user', 'kind': kind, 'content': text, 'time': now()})
    updated['transcript'].append({'role': 'user', 'step': stage, 'kind': kind, 'content': text, 'time': now()})
    updated['revision'] += 1
    return updated


def public_interviews(state):
    return {stage: {key: deepcopy(value) for key, value in record.items() if not key.startswith('_')}
            for stage, record in state.get('bot_interviews', {}).items()}


def student_download(state):
    result = deepcopy(state)
    result['bot_interviews'] = public_interviews(state)
    return result


@lru_cache(maxsize=4)
def _openai_client(key, timeout):
    """Reuse HTTP connections across calls; keys/timeouts separate credentials."""
    from openai import OpenAI
    return OpenAI(api_key=key, timeout=timeout, max_retries=0)


def generate(record, client=None):
    """One objection call; no student identity or final-answer generation."""
    config, context = record['_config'], record['_context']
    if record['pending']['kind'] != 'challenge':
        raise BotError('Only the AI objection can be generated. Submit your own final answer next.')
    if client is None:
        key = setting(config.get('api_key_secret', 'KEY_OPENAI')) or setting('OPENAI_API_KEY')
        if not key:
            raise BotError('Configure KEY_OPENAI in Streamlit Secrets to enable the interview bot.')
        client = _openai_client(key, int(config.get('timeout_seconds', 45)))
    # Older unfinished records may still contain the former full case snapshot.
    # Extract only its wrong position; never forward answer-key or diagnostic fields.
    wrong_position = config.get('wrong_position')
    if not wrong_position:
        wrong_position = config.get('case_guidance', {}).get('wrong_position')
    if not str(wrong_position or '').strip():
        raise BotError('The saved bot configuration has no wrong position.')
    template = config.get('question_context_template', SAFE_CONTEXT_TEMPLATE)
    fields = {field for _, field, _, _ in Formatter().parse(template) if field}
    if fields - SAFE_CONTEXT_FIELDS:
        template = SAFE_CONTEXT_TEMPLATE
    instructions = config['interview_outline'] + '\n\n' + config['general_instructions']
    instructions += '\n\n' + template.format(
        question=context['question'], wrong_position=wrong_position)
    request = config.get('reply_request_instruction',
                         'End by asking the student to comment in no more than {max_words} words.')
    instructions += '\n\n' + request.format(max_words=COMMENT_MAX_WORDS)
    payload = {'original_answer': context['original_answer'], 'review_assessment': context.get('review_assessment')}
    kwargs = deepcopy(config.get('additional_api_kwargs', {}))
    kwargs.update(model=config['model'], instructions=instructions,
                  input=[{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                  max_output_tokens=int(config.get('max_output_tokens', 1000)), store=False)
    try:
        started = time.perf_counter()
        response = client.responses.create(**kwargs)
        duration = round(time.perf_counter() - started, 3)
        if response.status != 'completed' or not response.output_text.strip():
            raise BotError('The bot did not finish its reply. Please retry.')
        return {'text': response.output_text, 'provider': 'openai', 'model': response.model,
                'response_id': response.id, 'duration_seconds': duration,
                'usage': response.usage.model_dump() if response.usage else {}}
    except BotError:
        raise
    except Exception as error:
        raise BotError('The bot could not complete this reply. Your saved answers are safe; please retry.') from error
