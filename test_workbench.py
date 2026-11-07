"""Authored contract tests. These do not represent observed client defects."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import b20workbench as b

ROOT = Path(__file__).resolve().parent


def document(*cases):
    return {'format': 'b20-workbench-v1', 'origin': 'authored', 'cases': list(cases)}


def case(steps=None, active=None, pending=None):
    return {'id': 'test', 'kind': 'state',
            'initial': {'at': '0', 'active': str(active or b.WAD), 'pending': pending},
            'steps': [] if steps is None else steps}


def query(at='0', raw=None):
    return {'id': 'query', 'at': at, 'op': 'query', 'raw': ['1'] if raw is None else raw}


class StateTests(unittest.TestCase):
    def result(self, c):
        return b.evaluate(document(c))['results'][0]

    def test_exact_floor_and_zero(self):
        result = self.result(case([query(raw=['0', '1', '3'])], b.WAD // 2))
        self.assertEqual([x['ui'] for x in result['steps'][0]['conversions']], ['0', '0', '1'])

    def test_large_integer_precision(self):
        raw = str(2**100 + 1)
        result = self.result(case([query(raw=[raw])]))
        self.assertEqual(result['steps'][0]['conversions'][0]['ui'], raw)

    def test_maturity_without_event(self):
        pending = {'value': str(2 * b.WAD), 'effective_at': '5'}
        steps = [query('4'), dict(query('5'), id='boundary'), dict(query('6'), id='after')]
        result = self.result(case(steps, pending=pending))
        self.assertEqual([x['active'] for x in result['steps']], [str(b.WAD), str(2*b.WAD), str(2*b.WAD)])
        self.assertIsNone(result['steps'][1]['pending'])

    def test_mature_before_schedule(self):
        pending = {'value': '7', 'effective_at': '5'}
        step = {'id': 'new', 'op': 'schedule', 'at': '5', 'value': '9', 'effective_at': '6'}
        result = self.result(case([step], pending=pending))
        self.assertEqual(result['steps'][0]['active'], '7')
        self.assertEqual(result['steps'][0]['pending']['value'], '9')

    def test_cancel_removes_pending(self):
        pending = {'value': '7', 'effective_at': '5'}
        result = self.result(case([{'id': 'cancel', 'op': 'cancel', 'at': '4'}, query('6')], pending=pending))
        self.assertEqual(result['steps'][-1]['active'], str(b.WAD))
        self.assertIsNone(result['steps'][-1]['pending'])

    def test_mature_then_cancel_is_unsupported(self):
        result = self.result(case([{'id': 'cancel', 'op': 'cancel', 'at': '5'}], pending={'value': '7', 'effective_at': '5'}))
        self.assertEqual(result['reason'], 'no_live_pending_cancel')
        self.assertEqual(result['steps'], [])

    def test_pending_conflicts_are_explicit(self):
        pending = {'value': '7', 'effective_at': '5'}
        for op, reason in [('instant', 'pending_instant_conflict'), ('schedule', 'pending_schedule_conflict')]:
            step = {'id': 'operation', 'op': op, 'at': '1', 'value': '9'}
            if op == 'schedule':
                step['effective_at'] = '6'
            self.assertEqual(self.result(case([step], pending=pending))['reason'], reason)

    def test_overflow_no_partial_step(self):
        result = self.result(case([query(raw=['0', str(b.U256)])], 2))
        self.assertEqual(result['reason'], 'product_overflow_unqualified')
        self.assertEqual(result['steps'], [])

    def test_invalid_integer_types_and_domains(self):
        for value in ['00', '-1', '1.0', ' 1', 1, True, str(b.U256 + 1), '9'*5000]:
            with self.subTest(value=str(value)[:30]):
                self.assertEqual(self.result(case([query(raw=[value])]))['status'], 'invalid')

    def test_unknown_op_and_keys(self):
        for step in [dict(query(), typo='x'), dict(query(), op=[]), dict(query(), op='execute')]:
            self.assertEqual(self.result(case([step]))['status'], 'invalid')

    def test_schedule_bound_and_order(self):
        variants = [
            [{'id':'s','at':'2','op':'schedule','value':'1','effective_at':'2'}],
            [query('2'), dict(query('1'), id='back')],
            [query('1'), query('2')],
            [{'id':'i','at':'0','op':'instant','value':'0'}],
            [{'id':'i','at':'0','op':'instant','value':str(b.U128+1)}],
        ]
        for steps in variants:
            self.assertEqual(self.result(case(steps))['status'], 'invalid')

    def test_whole_case_validation_precedes_unsupported(self):
        steps = [{'id':'cancel','at':'0','op':'cancel'}, dict(query('1'), raw=[False])]
        self.assertEqual(self.result(case(steps))['status'], 'invalid')

    def test_no_input_mutation(self):
        original = document(case([query('5')], pending={'value':'7','effective_at':'5'}))
        before = copy.deepcopy(original)
        b.evaluate(original)
        self.assertEqual(original, before)

    def test_empty_arrays(self):
        self.assertEqual(b.evaluate(document())['results'], [])
        self.assertEqual(self.result(case())['steps'], [])
        self.assertEqual(self.result(case([query(raw=[])]))['steps'][0]['conversions'], [])


class AbiTests(unittest.TestCase):
    def test_canonical_words(self):
        data = '0x' + ''.join(f'{n:064x}' for n in [0, b.U256, 5])
        result = b.abi({'id':'abi','kind':'abi','topics':[b.UPDATE.upper().replace('0X','0x')], 'data':data.upper().replace('0X','0x')})
        self.assertEqual(result['values'], ['0', str(b.U256), '5'])
        self.assertEqual(result['encoded'], {'topics':[b.UPDATE], 'data':data})

    def test_invalid_payloads(self):
        for topics, data in [([b.UPDATE], '0x'), ([b.UPDATE, b.CANCEL], '0x'), ([b.CANCEL], '0xzz'), (['0x'+'0'*64], '0x'), ([False], '0x')]:
            result = b.evaluate(document({'id':'abi','kind':'abi','topics':topics,'data':data}))
            self.assertEqual(result['results'][0]['status'], 'invalid')

    def test_legacy_is_not_state_reconstruction(self):
        result = b.abi({'id':'legacy','kind':'abi','topics':[b.LEGACY], 'data':'0x'+'0'*64})
        self.assertEqual(result['status'], 'unsupported')


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.expected = b.evaluate(document(case([query()])))

    def test_reordered_cases_match_by_id(self):
        other = case(); other['id'] = 'other'
        e = b.evaluate(document(case(), other)); a = copy.deepcopy(e)
        a['results'].reverse()
        self.assertTrue(b.compare(e, a)['matches'])

    def test_missing_extra_duplicate_records(self):
        for records, reason in [([], 'missing'), (self.expected['results']*2, 'duplicate_actual')]:
            actual = dict(self.expected, results=records)
            self.assertFalse(b.compare(self.expected, actual)['matches'])
            self.assertIn(reason, [f['reason'] for f in b.compare(self.expected, actual)['failures']])
        actual = copy.deepcopy(self.expected); actual['results'][0]['id']='other'
        self.assertEqual({f['reason'] for f in b.compare(self.expected, actual)['failures']}, {'missing','extra'})

    def test_types_are_exact(self):
        self.assertFalse(b.exact(1, True)); self.assertFalse(b.exact(1, 1.0))
        actual = copy.deepcopy(self.expected)
        actual['results'][0]['steps'][0]['conversions'][0]['ui'] = 1
        with self.assertRaises(b.Invalid):
            b.compare(self.expected, actual)

    def test_malformed_results_never_compare_as_success(self):
        malformed = dict(self.expected, results=[{'id':'not-a-result'}])
        with self.assertRaises(b.Invalid):
            b.compare(malformed, malformed)
        with self.assertRaises(b.Invalid):
            b.compare(self.expected, malformed)
        with self.assertRaises(b.Invalid):
            b.compare(malformed, self.expected)

    def test_all_result_variants_require_full_structure(self):
        variants = [
            self.expected['results'][0],
            {'id':'x','kind':'state','status':'invalid','reason':'invalid_case'},
            {'id':'x','kind':'abi','status':'invalid','reason':'invalid_case'},
            {'id':'x','kind':'abi','status':'unsupported','reason':'legacy_emitter_ambiguous'},
            {'id':'x','kind':'state','status':'unsupported','at_step':'q',
             'reason':'no_live_pending_cancel','steps':[]},
            b.abi({'id':'x','kind':'abi','topics':[b.CANCEL],'data':'0x'+'0'*128}),
        ]
        for record in variants:
            b.validate_result(record)
            for key in record:
                bad = dict(record); del bad[key]
                with self.subTest(kind=record['kind'], status=record['status'], missing=key):
                    with self.assertRaises(b.Invalid):
                        b.validate_result(bad)

    def test_result_domains_and_nested_keys(self):
        for path, value in [('active','0'), ('active',str(b.U128+1)), ('pending',{}),
                            ('conversions',[{'raw':'0','ui':'00'}]), ('conversions',False)]:
            bad = copy.deepcopy(self.expected['results'][0])
            bad['steps'][0][path] = value
            with self.assertRaises(b.Invalid):
                b.validate_result(bad)

    def test_authored_fault_patterns_are_detected(self):
        pending = {'value':str(2*b.WAD),'effective_at':'2'}
        expected = b.evaluate(document(case([query('2')], pending=pending)))
        actual = copy.deepcopy(expected)
        actual['results'][0]['steps'][0]['active'] = str(b.WAD)  # event-only activation double
        self.assertFalse(b.compare(expected, actual)['matches'])
        actual = copy.deepcopy(self.expected)
        actual['results'][0]['steps'][0]['conversions'][0]['ui']='2'  # wrong arithmetic double
        report = b.compare(self.expected, actual)
        self.assertFalse(report['matches'])
        self.assertIn('conversions', report['failures'][0]['differences'][0]['path'])

    def test_four_explicit_compatibility_failures(self):
        # Independent literal expectations for illustrative faulty result writers.
        # These writers are not observed SDKs or live contract implementations.
        vectors = [
            ('absolute_not_relative', str(3*b.WAD), '1', '3', str(6*b.WAD), '6'),
            ('activation_without_event', str(2*b.WAD), '1', '2', str(b.WAD), '1'),
            ('binary_float_precision', str(b.WAD), '9007199254740993', '9007199254740993', str(b.WAD), '9007199254740992'),
            ('cancelled_pending', str(b.WAD), '1', '1', str(2*b.WAD), '2'),
        ]
        for name, active, raw, ui, wrong_active, wrong_ui in vectors:
            expected = {'format':'b20-workbench-results-v1','origin':'authored','results':[
                {'id':name,'kind':'state','status':'ok','steps':[
                    {'id':'query','active':active,'pending':None,'conversions':[{'raw':raw,'ui':ui}]}]}]}
            actual = copy.deepcopy(expected)
            actual['results'][0]['steps'][0]['active'] = wrong_active
            actual['results'][0]['steps'][0]['conversions'][0]['ui'] = wrong_ui
            with self.subTest(pattern=name):
                self.assertFalse(b.compare(expected, actual)['matches'])


class CliTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.tmp')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def write(self, name, data):
        path = self.folder/name
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT/'b20workbench.py'), *map(str,args)],
                              capture_output=True, text=True, check=False)

    def test_actual_evaluate_compare_and_exclusive_output(self):
        src = self.write('input.json', document(case([query()])))
        expected = self.write('expected.json', b.evaluate(document(case([query()]))))
        output = self.folder/'actual.json'
        run = self.run_cli('evaluate', src, '--output', output)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.run_cli('compare', expected, output).returncode, 0)
        before = output.read_bytes()
        self.assertEqual(self.run_cli('evaluate', src, '--output', output).returncode, 2)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(self.run_cli('evaluate', src, '--output', src).returncode, 2)

    def test_cli_mismatch_and_document_errors(self):
        e = self.write('e.json', b.evaluate(document(case())))
        a = self.write('a.json', b.evaluate(document()))
        self.assertEqual(self.run_cli('compare', e, a).returncode, 1)
        bad = self.write('bad.json', document(case(), case()))
        target = self.folder/'absent.json'
        self.assertEqual(self.run_cli('evaluate', bad, '--output', target).returncode, 2)
        self.assertFalse(target.exists())
        bad.write_text('{"format":1,"format":2}')
        self.assertEqual(self.run_cli('evaluate', bad, '--output', target).returncode, 2)
        bad.write_text('{"cases":NaN}')
        self.assertEqual(self.run_cli('evaluate', bad, '--output', target).returncode, 2)

    def test_malformed_result_cli_exit(self):
        malformed = self.write('malformed.json', {'format':'b20-workbench-results-v1',
                              'origin':'authored','results':[{'id':'not-a-result'}]})
        valid = self.write('valid.json', b.evaluate(document(case())))
        for expected, actual in [(malformed,malformed),(valid,malformed),(malformed,valid)]:
            outcome = self.run_cli('compare', expected, actual)
            self.assertEqual(outcome.returncode, 2)
            self.assertEqual(outcome.stdout, '')

    def test_metadata_optional_fields_and_no_inventory_claim(self):
        metadata = {'tokens':[{'contract_address':'0x'+'a'*40, 'symbol':'TEST', 'decimals':8}], 'next_page_token':'more'}
        path = self.write('metadata.json', metadata)
        result = self.run_cli('inspect-metadata', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['record_count'], 1)
        self.assertTrue(report['has_next_page'])
        self.assertFalse(report['inventory_complete'])
        self.assertNotIn('exact_multiplier', report)


if __name__ == '__main__':
    unittest.main(verbosity=2)
