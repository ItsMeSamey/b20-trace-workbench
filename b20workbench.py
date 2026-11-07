"""Offline B20 authored-protocol evaluator. No network or executable plugins."""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys

WAD = 10**18
U128 = 2**128 - 1
U64 = 2**64 - 1
U256 = 2**256 - 1
UPDATE = '0x2205df4534432b2f60654a3fdb48737ffdaf3e9edb1a498bd985bc026b15b055'
CANCEL = '0x883856335ba5f60c18b9817c4505d3c7d3f6223dcf39516b30c508c46a5e1cad'
LEGACY = '0x4dbe4840d7465bd162f67814cea0b519567a2e0e578bcde61e7f4ced361e5a3d'


class Invalid(ValueError):
    """Malformed document or case, not an onchain revert."""


def keys(obj, expected):
    if not isinstance(obj, dict) or set(obj) != set(expected):
        raise Invalid('missing or extra keys')


def identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise Invalid('nonempty string ID required')
    return value


def uint(value, maximum, minimum=0):
    if not isinstance(value, str) or re.fullmatch(r'0|[1-9][0-9]*', value) is None:
        raise Invalid('canonical unsigned decimal string required')
    # Avoid Python integer conversion limits for out-of-domain input.
    if len(value) > len(str(maximum)):
        raise Invalid('integer outside supported domain')
    number = int(value)
    if not minimum <= number <= maximum:
        raise Invalid('integer outside supported domain')
    return number


def unique(items):
    if len(items) != len(set(items)):
        raise Invalid('duplicate ID')


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise Invalid('duplicate JSON key')
        result[name] = value
    return result


def load(path):
    def bad_constant(value):
        raise Invalid('nonfinite JSON value')
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'),
                          object_pairs_hook=_pairs, parse_constant=bad_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Invalid('invalid UTF-8 JSON') from exc


def validate_state(case):
    keys(case, ('id', 'kind', 'initial', 'steps'))
    initial = case['initial']
    keys(initial, ('at', 'active', 'pending'))
    previous = uint(initial['at'], U64)
    uint(initial['active'], U128, 1)
    if initial['pending'] is not None:
        keys(initial['pending'], ('value', 'effective_at'))
        uint(initial['pending']['value'], U128, 1)
        uint(initial['pending']['effective_at'], U64)
    if not isinstance(case['steps'], list):
        raise Invalid('steps must be an array')
    ids = []
    fields = {'query': ('raw',), 'schedule': ('value', 'effective_at'),
              'instant': ('value',), 'cancel': (), 'legacy_reconstruct': ()}
    for step in case['steps']:
        if not isinstance(step, dict) or not isinstance(step.get('op'), str) or step['op'] not in fields:
            raise Invalid('unknown operation')
        keys(step, ('id', 'at', 'op') + fields[step['op']])
        ids.append(identifier(step['id']))
        at = uint(step['at'], U64)
        if at < previous:
            raise Invalid('timestamps must be nondecreasing')
        previous = at
        if step['op'] in ('schedule', 'instant'):
            uint(step['value'], U128, 1)
        if step['op'] == 'schedule' and uint(step['effective_at'], U64) <= at:
            raise Invalid('schedule must be future')
        if step['op'] == 'query':
            if not isinstance(step['raw'], list):
                raise Invalid('raw must be an array')
            for raw in step['raw']:
                uint(raw, U256)
    unique(ids)


def state(case):
    validate_state(case)
    active = int(case['initial']['active'])
    pending = deepcopy(case['initial']['pending'])
    result = {'id': case['id'], 'kind': 'state', 'status': 'ok', 'steps': []}
    for step in case['steps']:
        at = int(step['at'])
        if pending is not None and int(pending['effective_at']) <= at:
            active, pending = int(pending['value']), None
        op = step['op']
        reason = None
        conversions = []
        if op == 'legacy_reconstruct':
            reason = 'legacy_emitter_ambiguous'
        elif op == 'schedule':
            if pending is not None:
                reason = 'pending_schedule_conflict'
            else:
                pending = {'value': step['value'], 'effective_at': step['effective_at']}
        elif op == 'instant':
            if pending is not None:
                reason = 'pending_instant_conflict'
            else:
                active = int(step['value'])
        elif op == 'cancel':
            if pending is None:
                reason = 'no_live_pending_cancel'
            else:
                pending = None
        elif op == 'query':
            for raw in step['raw']:
                product = int(raw) * active
                if product > U256:
                    reason = 'product_overflow_unqualified'
                    break
                conversions.append({'raw': raw, 'ui': str(product // WAD)})
        if reason:
            result.update(status='unsupported', at_step=step['id'], reason=reason)
            return result
        result['steps'].append({'id': step['id'], 'active': str(active),
                                'pending': deepcopy(pending), 'conversions': conversions})
    return result


def abi(case):
    keys(case, ('id', 'kind', 'topics', 'data'))
    topics, data = case['topics'], case['data']
    if not isinstance(topics, list) or len(topics) != 1:
        raise Invalid('one unindexed event topic required')
    topic = topics[0]
    if not isinstance(topic, str) or re.fullmatch(r'0x[0-9a-fA-F]{64}', topic) is None:
        raise Invalid('invalid topic')
    if not isinstance(data, str) or re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', data) is None:
        raise Invalid('invalid data hex')
    topic = topic.lower()
    if topic == LEGACY:
        if len(data) != 66:
            raise Invalid('legacy event requires one uint256 word')
        return {'id': case['id'], 'kind': 'abi', 'status': 'unsupported',
                'reason': 'legacy_emitter_ambiguous'}
    definitions = {UPDATE: ('UIMultiplierUpdated', 3),
                   CANCEL: ('UIMultiplierUpdateCancelled', 2)}
    if topic not in definitions:
        raise Invalid('unknown event')
    name, count = definitions[topic]
    if len(data) != 2 + count * 64:
        raise Invalid('incorrect ABI word count')
    values = [int(data[2 + i * 64:2 + (i + 1) * 64], 16) for i in range(count)]
    encoded = '0x' + ''.join(f'{number:064x}' for number in values)
    return {'id': case['id'], 'kind': 'abi', 'status': 'ok', 'event': name,
            'values': [str(value) for value in values],
            'encoded': {'topics': [topic], 'data': encoded}}


def evaluate(document):
    keys(document, ('format', 'origin', 'cases'))
    if document['format'] != 'b20-workbench-v1' or document['origin'] != 'authored' or not isinstance(document['cases'], list):
        raise Invalid('unknown fixture document')
    ids = []
    for case in document['cases']:
        if not isinstance(case, dict) or case.get('kind') not in ('state', 'abi'):
            raise Invalid('case identity or kind missing')
        ids.append(identifier(case.get('id')))
    unique(ids)
    results = []
    for case in document['cases']:
        try:
            results.append(state(case) if case['kind'] == 'state' else abi(case))
        except Invalid:
            results.append({'id': case['id'], 'kind': case['kind'],
                            'status': 'invalid', 'reason': 'invalid_case'})
    return {'format': 'b20-workbench-results-v1', 'origin': 'authored', 'results': results}


def exact(left, right):
    """JSON equality that never conflates booleans, integers, floats or strings."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(exact(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(exact(a, b) for a, b in zip(left, right))
    return left == right


def validate_result(record):
    """Validate result structure, without treating it as a correct computation."""
    if not isinstance(record, dict):
        raise Invalid('result record must be an object')
    identifier(record.get('id'))
    kind, status = record.get('kind'), record.get('status')
    if kind not in ('state', 'abi') or status not in ('ok', 'invalid', 'unsupported'):
        raise Invalid('unknown result kind or status')
    if status == 'invalid':
        keys(record, ('id', 'kind', 'status', 'reason'))
        if record['reason'] != 'invalid_case':
            raise Invalid('unknown invalid-case reason')
        return
    if kind == 'abi':
        if status == 'unsupported':
            keys(record, ('id', 'kind', 'status', 'reason'))
            if record['reason'] != 'legacy_emitter_ambiguous':
                raise Invalid('unknown ABI unsupported reason')
            return
        keys(record, ('id', 'kind', 'status', 'event', 'values', 'encoded'))
        counts = {'UIMultiplierUpdated': 3, 'UIMultiplierUpdateCancelled': 2}
        if not isinstance(record['event'], str) or record['event'] not in counts:
            raise Invalid('unknown result event')
        count = counts[record['event']]
        if not isinstance(record['values'], list) or len(record['values']) != count:
            raise Invalid('invalid result ABI values')
        for value in record['values']:
            uint(value, U256)
        encoded = record['encoded']
        keys(encoded, ('topics', 'data'))
        if not isinstance(encoded['topics'], list) or len(encoded['topics']) != 1:
            raise Invalid('invalid result topic array')
        topic = encoded['topics'][0]
        if not isinstance(topic, str) or topic not in (UPDATE, CANCEL):
            raise Invalid('noncanonical result topic')
        if not isinstance(encoded['data'], str) or re.fullmatch(r'0x[0-9a-f]{' + str(count * 64) + '}', encoded['data']) is None:
            raise Invalid('noncanonical result ABI data')
        return
    required = ('id', 'kind', 'status', 'steps')
    if status == 'unsupported':
        required += ('at_step', 'reason')
    keys(record, required)
    if status == 'unsupported':
        identifier(record['at_step'])
        if record['reason'] not in ('pending_schedule_conflict', 'pending_instant_conflict',
                                    'no_live_pending_cancel', 'legacy_emitter_ambiguous',
                                    'product_overflow_unqualified'):
            raise Invalid('unknown state unsupported reason')
    if not isinstance(record['steps'], list):
        raise Invalid('result steps must be an array')
    ids = []
    for step in record['steps']:
        keys(step, ('id', 'active', 'pending', 'conversions'))
        ids.append(identifier(step['id']))
        uint(step['active'], U128, 1)
        if step['pending'] is not None:
            keys(step['pending'], ('value', 'effective_at'))
            uint(step['pending']['value'], U128, 1)
            uint(step['pending']['effective_at'], U64)
        if not isinstance(step['conversions'], list):
            raise Invalid('result conversions must be an array')
        for conversion in step['conversions']:
            keys(conversion, ('raw', 'ui'))
            uint(conversion['raw'], U256)
            uint(conversion['ui'], U256)
    unique(ids)


def result_records(document):
    keys(document, ('format', 'origin', 'results'))
    if document['format'] != 'b20-workbench-results-v1' or document['origin'] != 'authored' or not isinstance(document['results'], list):
        raise Invalid('unknown result document')
    for record in document['results']:
        validate_result(record)
    return document['results']


def differences(expected, actual, path='$'):
    if exact(expected, actual):
        return []
    if type(expected) is not type(actual):
        return [{'path': path, 'expected': expected, 'actual': actual, 'reason': 'type'}]
    if isinstance(expected, dict):
        output = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                output.append({'path': path + '.' + key, 'reason': 'missing' if key not in actual else 'extra'})
            else:
                output.extend(differences(expected[key], actual[key], path + '.' + key))
        return output
    if isinstance(expected, list) and len(expected) == len(actual):
        return [diff for i, (a, b) in enumerate(zip(expected, actual))
                for diff in differences(a, b, f'{path}[{i}]')]
    return [{'path': path, 'expected': expected, 'actual': actual, 'reason': 'value'}]


def compare(expected, actual):
    e, a = result_records(expected), result_records(actual)
    ec, ac = Counter(x['id'] for x in e), Counter(x['id'] for x in a)
    failures = []
    for label, counts in (('expected', ec), ('actual', ac)):
        for name, count in counts.items():
            if count > 1:
                failures.append({'id': name, 'reason': 'duplicate_' + label, 'count': count})
    em, am = {x['id']: x for x in e}, {x['id']: x for x in a}
    for name in sorted(set(em) | set(am)):
        if name not in em or name not in am:
            failures.append({'id': name, 'reason': 'missing' if name not in am else 'extra'})
        elif ec[name] == ac[name] == 1:
            diff = differences(em[name], am[name])
            if diff:
                failures.append({'id': name, 'reason': 'mismatch', 'differences': diff})
    return {'origin': 'authored', 'matches': not failures,
            'expected_records': len(e), 'actual_records': len(a), 'failures': failures}


def inspect_metadata(path):
    """Summarize one captured REST page, never promote it to exact raw state."""
    body = Path(path).read_bytes()
    document = load(path)
    if not isinstance(document, dict) or not isinstance(document.get('tokens'), list):
        raise Invalid('metadata tokens array missing')
    if 'next_page_token' in document and document['next_page_token'] is not None and not isinstance(document['next_page_token'], str):
        raise Invalid('invalid pagination cursor')
    fields = Counter()
    identities = []
    for token in document['tokens']:
        if not isinstance(token, dict):
            raise Invalid('token must be an object')
        if not isinstance(token.get('contract_address'), str) or re.fullmatch(r'0x[0-9a-fA-F]{40}', token['contract_address']) is None:
            raise Invalid('token address missing or malformed')
        if not isinstance(token.get('symbol'), str) or type(token.get('decimals')) is not int or not 0 <= token['decimals'] <= 255:
            raise Invalid('token display identity missing or malformed')
        fields.update(token.keys())
        identities.append({'address': token['contract_address'], 'symbol': token['symbol'], 'decimals': token['decimals']})
    return {'kind': 'captured_metadata_page', 'sha256': hashlib.sha256(body).hexdigest(),
            'bytes': len(body), 'record_count': len(identities),
            'has_next_page': bool(document.get('next_page_token')), 'inventory_complete': False,
            'field_presence': dict(sorted(fields.items())), 'identities': identities,
            'limitations': ['One supplied page only; no live freshness check.',
                            'Display metadata is not exact raw state or transition history.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect-metadata')
    inspect.add_argument('capture')
    run = commands.add_parser('evaluate')
    run.add_argument('fixtures')
    run.add_argument('--output', required=True)
    check = commands.add_parser('compare')
    check.add_argument('expected')
    check.add_argument('actual')
    args = parser.parse_args(argv)
    try:
        if args.command == 'evaluate':
            result = evaluate(load(args.fixtures))
            # Exclusive creation prevents input replacement or silent result overwrite.
            with Path(args.output).open('x', encoding='utf-8') as stream:
                json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write('\n')
            print(json.dumps({'written': args.output, 'origin': 'authored', 'cases': len(result['results'])}))
            return 0
        if args.command == 'compare':
            report = compare(load(args.expected), load(args.actual))
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report['matches'] else 1
        print(json.dumps(inspect_metadata(args.capture), indent=2, ensure_ascii=False))
        return 0
    except (Invalid, OSError, RecursionError) as exc:
        print('Input/output error: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
