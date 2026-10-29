"""离线协议测试；不连接真实课堂、不使用真实登录态、不调用模型。"""
import copy
import io
import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
import ykt_http as http


def response(data=None, status=200, headers=None):
    obj = Mock(status_code=status, headers=headers or {})
    obj.json.return_value = data
    return obj


class InlinePool:
    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    def shutdown(self, **kwargs):
        pass


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.cfg = copy.deepcopy(http.DEFAULT_CONFIG)
        self.cfg['llm']['allow_dsh_credentials'] = False
        self.cfg['http']['answers_file'] = str(self.tmp / 'answers.jsonl')
        self.session = requests.Session()
        self.addCleanup(self.session.close)
        self.session.cookies.set('csrftoken', 'fake-csrf', domain='changjiang.yuketang.cn')
        self.session.request = Mock(return_value=response({'code': 0, 'data': {}}))
        self.client = http.Client(self.cfg, self.session)
        self.journal = http.Journal(self.tmp / 'journal.jsonl')
        logger = patch.object(http, 'log')
        logger.start()
        self.addCleanup(logger.stop)

    def question(self, ptype=1, **extra):
        return http.to_question({'problemId': '9876543210987654321', 'problemType': ptype,
                                 'body': '<p>1 + 1 = ?</p>', 'options': ['1', '2', '3'], **extra}, self.cfg)

    def monitor(self, auto=True, once=False):
        args = SimpleNamespace(api_key='fake-key', once=once, auto_submit=auto, duration=1)
        obj = http.Monitor(self.client, self.cfg, 12, '34', args, self.journal)
        obj.pool.shutdown(wait=False)
        obj.pool = InlinePool()
        obj.ws = Mock()
        return obj

    def event(self, **extra):
        return {'op': 'probleminfo', 'problemid': '9876543210987654321',
                'limit': 60000, 'dt': 100000, 'now': 110000, **extra}

    def test_wire_payload_and_csrf(self):
        payload = http.make_payload(self.question(), 'B')
        self.assertEqual(self.journal.upload(self.client, 12, 34, payload)['status'], 'submitted')
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ('POST', 'https://changjiang.yuketang.cn/api/v3/lesson/problem/answer'))
        self.assertEqual(kwargs['json']['problemId'], '9876543210987654321')
        self.assertIsInstance(kwargs['json']['problemType'], int)
        self.assertIsInstance(kwargs['json']['dt'], int)
        self.assertEqual(kwargs['json']['result'], ['B'])
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['headers']['X-CSRFToken'], 'fake-csrf')

    def test_cookie_scope_and_duplicate_csrf(self):
        state = self.tmp / 'state.json'
        cookies = [
            {'name': 'sessionid', 'value': 'fake', 'domain': '.yuketang.cn', 'path': '/', 'secure': True, 'expires': -1},
            {'name': 'csrftoken', 'value': 'root', 'domain': '.yuketang.cn', 'path': '/', 'expires': -1},
            {'name': 'csrftoken', 'value': 'api-path', 'domain': 'changjiang.yuketang.cn', 'path': '/api/', 'expires': -1},
            {'name': 'expired', 'value': 'x', 'domain': '.yuketang.cn', 'expires': 1},
            {'name': 'other', 'value': 'x', 'domain': 'pro.yuketang.cn', 'expires': -1},
            {'name': 'evil', 'value': 'x', 'domain': 'notyuketang.cn', 'expires': -1}]
        state.write_text(json.dumps({'cookies': cookies}), encoding='utf-8')
        session = http.make_session(self.client.base, state)
        self.addCleanup(session.close)
        self.assertEqual({c.name for c in session.cookies}, {'sessionid', 'csrftoken'})
        self.assertTrue(next(c for c in session.cookies if c.name == 'sessionid').secure)
        session.request = Mock(return_value=response({'code': 0}))
        http.Client(self.cfg, session).request('POST', '/api/v3/lesson/problem/answer', json={})
        self.assertEqual(session.request.call_args.kwargs['headers']['X-CSRFToken'], 'api-path')

    def test_missing_state(self):
        with self.assertRaises(http.ApiError):
            http.make_session(self.client.base, self.tmp / 'absent.json')

    def test_credential_forwarding_rejected(self):
        for base in ['http://changjiang.yuketang.cn', 'https://yuketang.cn.evil.test',
                     'https://u:p@www.yuketang.cn', 'https://www.yuketang.cn/path']:
            with self.subTest(base=base), self.assertRaises(http.ApiError):
                http.validate_base(base)

    def test_type_mapping(self):
        for kind, expected in [(1, '单选'), (2, '多选'), (3, '投票'), (4, '填空'), (5, '简答')]:
            self.assertEqual(self.question(kind)['kind'], expected)

    def test_multiple_blank_subjective(self):
        self.assertEqual(http.make_payload(self.question(2), 'C,A,A')['result'], ['A', 'C'])
        self.assertEqual(http.make_payload(self.question(4, blanks=[{}, {}]), '甲；乙')['result'], ['甲', '乙'])
        self.assertEqual(http.make_payload(self.question(5), '文字')['result'], {'content': '文字', 'pics': []})

    def test_invalid_answers_not_filtered(self):
        for q, answer in [(self.question(), 'AB'), (self.question(2), 'ACX'), (self.question(), 'B because'),
                          (self.question(), ''), (self.question(4, blanks=[{}, {}]), '一空'), (self.question(99), 'B')]:
            with self.subTest(answer=answer), self.assertRaises(http.ApiError):
                http.make_payload(q, answer)

    def test_true_false_labels(self):
        q = self.question(options=[{'key': 'A', 'value': '错误'}, {'key': 'B', 'value': '正确'}])
        self.assertEqual(http.answer_result(q, '正确'), ['B'])

    def test_html_and_image(self):
        q = self.question(body="<p>x &lt; y &amp; z</p><img src='test.png'>")
        self.assertIn('x < y & z', q['stem'])
        self.assertTrue(q['has_images'])

    def test_restart_deduplication(self):
        payload = http.make_payload(self.question(), 'B')
        self.journal.upload(self.client, 12, 34, payload)
        result = http.Journal(self.journal.path).upload(self.client, 12, 34, payload)
        self.assertEqual(result['previous_status'], 'submitted')
        self.session.request.assert_called_once()

    def test_timeout_unknown_not_retried(self):
        self.session.request.side_effect = requests.Timeout()
        payload = http.make_payload(self.question(), 'B')
        self.assertEqual(self.journal.upload(self.client, 12, 34, payload)['status'], 'unknown')
        result = http.Journal(self.journal.path).upload(self.client, 12, 34, payload)
        self.assertEqual(result['previous_status'], 'unknown')
        self.session.request.assert_called_once()

    def test_explicit_success_required(self):
        for data, status in [({}, 200), ([], 200), ({'code': 9}, 200), ({'code': False}, 200), ({'code': 0}, 500)]:
            with self.subTest(data=data, status=status):
                self.session.request.return_value = response(data, status)
                self.assertNotEqual(self.client.submit({})['status'], 'submitted')

    def test_non_json_unknown(self):
        self.session.request.return_value.json.side_effect = ValueError()
        self.assertEqual(self.client.submit({})['status'], 'unknown')

    def test_authentication_redirect(self):
        for status in [302, 401, 403]:
            with self.subTest(status=status), self.assertRaises(http.AuthExpired):
                self.session.request.return_value = response(status=status)
                self.client.lessons()

    def test_checkin_authorization(self):
        self.session.request.return_value = response({'code': 0, 'data': {'lessonToken': 'fake-token'}}, headers={'Set-Auth': 'fake-bearer'})
        self.assertEqual(self.client.checkin('34'), 'fake-token')
        self.assertEqual(self.session.headers['Authorization'], 'Bearer fake-bearer')

    def test_millisecond_deadlines(self):
        self.assertEqual(http.remaining(self.event()), 50)
        self.assertIsNone(http.remaining({'limit': -1}))
        self.assertEqual(http.remaining(self.event(now=170000)), 0)
        with self.assertRaises(http.ApiError):
            http.remaining({'limit': 1000})

    def test_disk_failure_prevents_upload(self):
        with patch.object(http, 'append_json', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.journal.upload(self.client, 12, 34, http.make_payload(self.question(), 'B'))
        self.session.request.assert_not_called()

    def test_corrupted_journal(self):
        self.journal.path.write_text('{"key":', encoding='utf-8')
        with self.assertRaises(http.ApiError):
            http.Journal(self.journal.path)

    def test_lock_lifetime(self):
        path = self.tmp / 'process.lock'
        with http.submission_lock(path):
            with self.assertRaises(http.ApiError):
                with http.submission_lock(path):
                    pass
        with http.submission_lock(path):
            pass

    def test_complete_event_ai_upload_flow(self):
        monitor = self.monitor()
        q = self.question()
        monitor.cache[q['problem_id']] = q
        result = {'ok': True, 'data': {'answer': 'B', 'confidence': 0.9, 'reason': '2'}}
        with patch.object(http, 'ask_llm_blocking', return_value=result):
            monitor.receive(self.event())
        monitor.collect()
        self.session.request.assert_not_called()
        self.assertEqual(json.loads(monitor.ws.send.call_args.args[0])['op'], 'probleminfo')
        monitor.receive(self.event())
        self.session.request.assert_called_once()
        monitor.receive(self.event())
        self.session.request.assert_called_once()

    def test_upload_guards(self):
        for mode in ['dry', 'image', 'confidence', 'expired', 'disconnected', 'finished', 'no-options']:
            with self.subTest(mode=mode):
                monitor = self.monitor(auto=mode != 'dry')
                q = self.question()
                q['has_images'] = mode == 'image'
                if mode == 'no-options':
                    q['options'] = []
                monitor.active[q['problem_id']] = {'question': q,
                    'deadline': time.monotonic()-1 if mode == 'expired' else None,
                    'connection': -1 if mode == 'disconnected' else monitor.connection}
                monitor.finished = mode == 'finished'
                monitor.finish_answer(q, {'answer': 'B', 'confidence': 0.2 if mode == 'confidence' else 0.9, 'reason': 'test'})
        self.session.request.assert_not_called()

    def test_answered_expired_drop_pending(self):
        for extra in [{'result': ['A']}, {'now': 170000}]:
            monitor = self.monitor()
            q = self.question()
            monitor.cache[q['problem_id']] = q
            monitor.pending[q['problem_id']] = {'answer': 'B', 'confidence': 1, 'reason': 'test'}
            monitor.receive(self.event(**extra))
            self.assertFalse(monitor.pending)
        self.session.request.assert_not_called()

    def test_nan_confidence_rejected(self):
        monitor = self.monitor()
        q = self.question()
        monitor.cache[q['problem_id']] = q
        future = Future()
        future.set_result({'ok': True, 'data': {'answer': 'B', 'confidence': float('nan')}})
        monitor.jobs[q['problem_id']] = future
        monitor.collect()
        self.assertFalse(monitor.pending)
        self.session.request.assert_not_called()

    def test_direct_preview_without_session(self):
        with (patch.object(http, 'load_config', return_value=self.cfg),
              patch.object(http, 'execute') as execute, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(http.main(['--problem-id', '12', '--problem-type', '2', '--answer', 'AC']), 0)
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['result'], ['A', 'C'])

    def test_cli_constraints(self):
        with patch.object(http, 'load_config', return_value=self.cfg), redirect_stderr(io.StringIO()):
            self.assertEqual(http.main(['--once', '--auto-submit']), 1)
            self.assertEqual(http.main(['--duration', 'nan']), 1)
            self.assertEqual(http.main(['--problem-id', '12', '--problem-type', '1', '--answer', 'B', '--auto-submit']), 1)

    def test_null_and_subjective_list_rejected(self):
        for q, answer in [(self.question(4), None), (self.question(), True), (self.question(5), ['text'])]:
            with self.subTest(answer=answer), self.assertRaises(http.ApiError):
                http.make_payload(q, answer)

    def test_websocket_handshake_and_snapshot(self):
        import websocket
        monitor = self.monitor(once=True)
        self.client.checkin = Mock(return_value='fake-lesson-token')
        self.client.presentation = Mock(return_value={'slides': [{'problem': {
            'problemId': '9876543210987654321', 'problemType': 1, 'body': '1+1?', 'options': ['1', '2']}}]})
        connection = Mock()
        connection.recv.side_effect = [json.dumps({'op': 'hello', 'presentation': 56,
            'timeline': [], 'unlockedproblem': ['9876543210987654321']}), json.dumps(self.event())]
        with patch.object(websocket, 'create_connection', return_value=connection) as connect, redirect_stdout(io.StringIO()) as output:
            monitor.run()
        hello = json.loads(connection.send.call_args_list[0].args[0])
        self.assertEqual(hello['op'], 'hello')
        self.assertEqual(hello['auth'], 'fake-lesson-token')
        self.assertEqual(connect.call_args.args[0], 'wss://changjiang.yuketang.cn/wsapp/')
        self.assertEqual(json.loads(output.getvalue())[0]['countdown'], 50)
        connection.close.assert_called_once()
        self.session.request.assert_not_called()

    def test_websocket_disconnect_reconnects(self):
        import websocket
        monitor = self.monitor(once=True)
        self.client.checkin = Mock(return_value='fake-token')
        first, second = Mock(), Mock()
        first.recv.side_effect = websocket.WebSocketConnectionClosedException()
        second.recv.return_value = json.dumps({'op': 'hello', 'timeline': [], 'unlockedproblem': []})
        with (patch.object(websocket, 'create_connection', side_effect=[first, second]) as connect,
              patch.object(http.time, 'sleep'), redirect_stdout(io.StringIO())):
            monitor.run()
        self.assertEqual(connect.call_count, 2)
        first.close.assert_called_once()
        second.close.assert_called_once()
        self.session.request.assert_not_called()

    def test_dom_version_shared_helpers_still_import(self):
        import importlib
        with patch.dict(sys.modules, {'playwright': SimpleNamespace(),
                        'playwright.async_api': SimpleNamespace(async_playwright=Mock())}):
            watcher = importlib.import_module('ykt_watcher')
            self.assertIs(watcher.ask_llm_blocking, http.ask_llm_blocking)
            self.assertEqual(watcher.mmss(75), '01:15')
            self.assertIn('querySelector', watcher.PROBE_JS)
            self.assertIn('助教', watcher.SYSTEM_PROMPT)


if __name__ == '__main__':
    unittest.main()
