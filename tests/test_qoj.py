from email.message import Message
import hashlib
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
import sys
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import HTTPSHandler, Request, build_opener
from urllib.response import addinfourl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qoj


LOGIN_TOKEN = 'fixtureTokenForOfflineTests0123456789'
LOGIN_PAGE = '''<form id="form-login" method="post">
<input type="text" name="username" id="input-username">
<input type="password" name="password" id="input-password"></form>
<script>$.post('/login', {
_token: "''' + LOGIN_TOKEN + '''", login: '',
username: $('#input-username').val(),
password: md5($('#input-password').val()), trust: ''
}, function(msg) { if (msg == 'ok') { window.location.href = '/'; } });</script>'''


class ScriptedHTTPS(HTTPSHandler):
    """Run urllib's real cookie and redirect handlers without any network I/O."""
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)
        self.requests = []

    def https_open(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        body, status, headers = reply
        message = Message()
        for key, value in headers.items():
            message[key] = value
        if isinstance(body, str):
            body = body.encode('utf-8')
        response = addinfourl(BytesIO(body), message, request.full_url, status)
        response.msg = 'fixture response'
        return response


def login_response(body=LOGIN_PAGE, status=200, **headers):
    return body, status, headers


def submission_row(submission_id=10, problem_id=42, result='AC ✓', user='Lucius7', contest_id=None, date='2026-09-08 00:00:00'):
    problem_url = f'/contest/{contest_id}/problem/{problem_id}' if contest_id else f'/problem/{problem_id}'
    return f'''<tr><td><a href="/submission/{submission_id}">#{submission_id}</a></td>
      <td><a href="{problem_url}">Example &amp; Test</a></td>
      <td><a href="//qoj.ac/user/profile/{user}">{user}</a></td><td>{result}</td>
      <td>1ms</td><td>1MB</td><td>C++</td><td>1kb</td><td>{date}</td></tr>'''


def page(rows, next_page=None, current_page=1, pager=True):
    current_query = '' if current_page == 1 else f'&amp;page={current_page}'
    links = f'<li class="page-item active"><a href="submissions?submitter=Lucius7{current_query}">{current_page}</a></li>'
    if next_page:
        links += f'<li class="page-item"><a href="submissions?submitter=Lucius7&amp;page={next_page}">{next_page}</a></li>'
    pagination = f'<ul class="pagination">{links}</ul>' if pager else ''
    return f'<h2>Submissions</h2><table><tbody>{rows}</tbody></table>{pagination}'


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return self.responses[url]


class QojTests(unittest.TestCase):
    def authenticate_with(self, replies, username='fixture-user', password='fixture-password'):
        handler = ScriptedHTTPS(replies)
        with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
            client = qoj.QojClient.from_credentials(username, password)
        return client, handler

    def test_password_login_uses_current_form_and_rotated_session_cookie(self):
        client, handler = self.authenticate_with([
            login_response(**{'Set-Cookie': 'PHPSESSID=initial; Path=/; Secure; HttpOnly'}),
            login_response('ok', **{'Set-Cookie': 'PHPSESSID=authenticated; Path=/; Secure; HttpOnly'}),
            login_response(page(submission_row())),
        ], password='fixture-pässword')
        first, post = handler.requests
        self.assertEqual(first.full_url, 'https://qoj.ac/login')
        self.assertEqual(first.get_method(), 'GET')
        self.assertIsNone(first.get_header('Cookie'))
        self.assertEqual(post.full_url, 'https://qoj.ac/login')
        self.assertEqual(post.get_method(), 'POST')
        self.assertEqual(post.get_header('Cookie'), 'PHPSESSID=initial')
        self.assertEqual(post.get_header('Origin'), 'https://qoj.ac')
        self.assertEqual(post.get_header('Referer'), 'https://qoj.ac/login')
        self.assertEqual(post.get_header('Content-type'), 'application/x-www-form-urlencoded')
        self.assertEqual(parse_qs(post.data.decode('ascii'), keep_blank_values=True), {
            '_token': [LOGIN_TOKEN], 'login': [''], 'username': ['fixture-user'],
            'password': [hashlib.md5('fixture-pässword'.encode(), usedforsecurity=False).hexdigest()], 'trust': [''],
        })
        self.assertNotIn('fixture-pässword'.encode(), post.data)
        self.assertIsNone(client.cookie)
        self.assertNotIn('password', vars(client))
        with patch.object(qoj.time, 'sleep'):
            rows, _ = qoj.parse_submissions(client.get('https://qoj.ac/submissions?submitter=Lucius7&page=1'), 'Lucius7', 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(handler.requests[-1].get_header('Cookie'), 'PHPSESSID=authenticated')

    def test_password_login_rejects_missing_credentials_before_requests(self):
        for username, password in [('', 'test'), ('test', ''), (None, 'test'), ('test', None), ('bad\nname', 'test'), ('test', 'bad\rpassword')]:
            with self.subTest(username=username, password=password), patch.object(qoj, 'build_opener') as opener:
                with self.assertRaisesRegex(qoj.AuthenticationRequired, 'both QOJ_USERNAME and QOJ_PASSWORD'):
                    qoj.QojClient.from_credentials(username, password)
                opener.assert_not_called()

    def test_password_login_requires_verified_form_token_and_md5_protocol(self):
        invalid = [
            '<h2>Login unavailable</h2>',
            LOGIN_PAGE.replace('form-login', 'other-form'),
            LOGIN_PAGE.replace('method="post"', 'method="get"'),
            LOGIN_PAGE.replace('method="post"', 'method'),
            LOGIN_PAGE.replace('method="post"', 'method="post" action="https://example.com/login"'),
            LOGIN_PAGE.replace('method="post"', 'method="post" action="http://qoj.ac/login"'),
            LOGIN_PAGE.replace('method="post"', 'method="post" action="https://qoj.ac:private-response/login"'),
            LOGIN_PAGE.replace('method="post"', 'method="post" action="https://[broken/login"'),
            LOGIN_PAGE.replace('name="username"', 'name="email"'),
            LOGIN_PAGE.replace(LOGIN_TOKEN, ''),
            LOGIN_PAGE.replace('_token:', 'another_token:'),
            LOGIN_PAGE.replace('login: \'\'', '_token: "anotherFixtureToken1234567890", login: \'\''),
            LOGIN_PAGE.replace("$.post('/login'", "$.post('/other'"),
            LOGIN_PAGE.replace("md5($('#input-password').val())", "md5($('#input-password').val(), 'salt')"),
        ]
        for document in invalid:
            with self.subTest(document=document):
                handler = ScriptedHTTPS([login_response(document)])
                with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
                    with self.assertRaises(qoj.AuthenticationRequired):
                        qoj.QojClient.from_credentials('fixture-user', 'fixture-password')
                self.assertEqual(len(handler.requests), 1)

    def test_password_login_known_failures_stop_after_one_post(self):
        outcomes = {'failed': 'username or password', 'expired': 'session expired', 'banned': 'no login permission',
                    'secure': 'security check', '2fa': 'two-factor', 'pending_2fa': 'additional verification'}
        for result, expected in outcomes.items():
            with self.subTest(result=result):
                handler = ScriptedHTTPS([login_response(), login_response(result)])
                with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
                    with self.assertRaisesRegex(qoj.AuthenticationRequired, expected):
                        qoj.QojClient.from_credentials('fixture-user', 'fixture-password')
                self.assertEqual([req.get_method() for req in handler.requests], ['GET', 'POST'])

    def test_password_login_requires_exact_success_and_does_not_echo_response(self):
        secret = 'private-fixture-password'
        digest = hashlib.md5(secret.encode(), usedforsecurity=False).hexdigest()
        for response in ['ok\n', ' ok', '<html>ok</html>', f'failed: {secret} {digest} {LOGIN_TOKEN}', b'\xffprivate-cookie']:
            with self.subTest(response=response):
                handler = ScriptedHTTPS([login_response(), login_response(response)])
                with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
                    try:
                        qoj.QojClient.from_credentials('fixture-user', secret)
                    except qoj.AuthenticationRequired:
                        output = traceback.format_exc()
                    else:
                        self.fail('Unverified login response was accepted')
                for private in (secret, digest, LOGIN_TOKEN, 'private-cookie'):
                    self.assertNotIn(private, output)
                self.assertEqual(len(handler.requests), 2)

    def test_login_network_errors_are_sanitized_and_never_retried(self):
        secret = 'private-fixture-password'
        digest = hashlib.md5(secret.encode(), usedforsecurity=False).hexdigest()
        detail = f'{secret} {digest} private-cookie'
        failures = [URLError(detail), TimeoutError(detail), OSError(detail), ValueError(detail), IncompleteRead(detail.encode())]
        for failure in failures:
            for post in (False, True):
                with self.subTest(failure=type(failure).__name__, post=post):
                    handler = ScriptedHTTPS(([login_response()] if post else []) + [failure])
                    with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
                        try:
                            qoj.QojClient.from_credentials('fixture-user', secret)
                        except qoj.AuthenticationRequired:
                            output = traceback.format_exc()
                        else:
                            self.fail('Failed login request was accepted')
                    for private in (secret, digest, 'private-cookie'):
                        self.assertNotIn(private, output)
                    self.assertEqual(len(handler.requests), 2 if post else 1)

    def test_login_http_denials_and_errors_are_not_retried(self):
        for status in (401, 403, 429, 500):
            for post in (False, True):
                with self.subTest(status=status, post=post):
                    handler = ScriptedHTTPS(([login_response()] if post else []) + [login_response('private-response', status)])
                    with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
                        with self.assertRaisesRegex(qoj.AuthenticationRequired, f'HTTP {status}') as error:
                            qoj.QojClient.from_credentials('fixture-user', 'fixture-password')
                    self.assertNotIn('private-response', str(error.exception))
                    self.assertEqual(len(handler.requests), 2 if post else 1)

    def test_login_access_checks_stop_before_posting_credentials(self):
        for marker in ['cf-chl-token', 'Just a moment...', 'Verify you are human', 'g-recaptcha', 'h-captcha', 'cf-turnstile']:
            with self.subTest(marker=marker):
                handler = ScriptedHTTPS([login_response(LOGIN_PAGE + marker)])
                with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
                    with self.assertRaisesRegex(qoj.AuthenticationRequired, 'normal browser session'):
                        qoj.QojClient.from_credentials('fixture-user', 'fixture-password')
                self.assertEqual(len(handler.requests), 1)

    def test_login_post_redirects_never_forward_credentials(self):
        for status in (301, 302, 303, 307, 308):
            for destination in ('https://qoj.ac/', 'https://qoj.ac/login/2fa', 'https://example.com/login', 'http://qoj.ac/login'):
                with self.subTest(status=status, destination=destination):
                    handler = ScriptedHTTPS([login_response(), login_response('', status, Location=destination)])
                    with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
                        with self.assertRaisesRegex(qoj.AuthenticationRequired, 'without forwarding credentials'):
                            qoj.QojClient.from_credentials('fixture-user', 'fixture-password')
                    self.assertEqual(len(handler.requests), 2)

    def test_login_get_cannot_redirect_outside_https_qoj(self):
        for destination in ('https://example.com/login', 'http://qoj.ac/login', 'https://qoj.ac:8443/login'):
            with self.subTest(destination=destination):
                handler = ScriptedHTTPS([login_response('', 302, Location=destination)])
                with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
                    with self.assertRaises(qoj.AuthenticationRequired):
                        qoj.QojClient.from_credentials('fixture-user', 'fixture-password')
                self.assertEqual(len(handler.requests), 1)

    def test_existing_cookie_mode_does_not_request_login(self):
        handler = ScriptedHTTPS([login_response(page(submission_row()))])
        with patch.object(qoj, 'build_opener', side_effect=lambda *handlers: build_opener(handler, *handlers)), patch.object(qoj.time, 'sleep'):
            client = qoj.QojClient('fixture=legacy')
            client.get('https://qoj.ac/submissions?submitter=Lucius7&page=1')
        self.assertEqual(len(handler.requests), 1)
        self.assertEqual(handler.requests[0].get_header('Cookie'), 'fixture=legacy')
        self.assertIn('/submissions?', handler.requests[0].full_url)

    def test_raw_username_span_is_checked_before_virtual_contest_marker(self):
        original = '<a href="//qoj.ac/user/profile/Lucius7">Lucius7</a>'
        raw = '<span class="uoj-username" data-nickname="Lucius7" data-rated="1">Lucius7</span><sup><a href="/contest/2581">#</a></sup>'
        document = page(submission_row().replace(original, raw))
        rows, _ = qoj.parse_submissions(document, 'Lucius7', 1)
        self.assertEqual(len(rows), 1)
        for replacement in [raw.replace('>Lucius7</span>', '>SomeoneElse</span>'), 'Lucius7 #', raw + '<a href="/user/profile/Other">Other</a>']:
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, 'another user'):
                qoj.parse_submissions(page(submission_row().replace(original, replacement)), 'Lucius7', 1)

    def test_ac_score_is_exact_and_time_uses_utc_plus_eight(self):
        ac_forms = ['AC', 'Accepted', 'AC ✓', 'Accepted ✔', '<a class="uoj-score" data-full="100.000000" data-score="100.000000">AC ✓</a>', '<a class="uoj-score" data-full="200.0" data-score="200.0">200 ✔</a>']
        for result in ac_forms:
            rows, more = qoj.parse_submissions(page(submission_row(result=result)), 'Lucius7', 1)
            self.assertTrue(rows[0]['accepted'])
            self.assertEqual(rows[0]['epoch'], 1788796800)
            self.assertFalse(more)
        for result in ['Wrong Answer', '1000', '99.9', '100 ✓', '<a data-score="99.9">100</a>', '<a data-full="200" data-score="100">AC ✓</a>', '<a data-full="100" data-score="0">WA</a>']:
            rows, _ = qoj.parse_submissions(page(submission_row(result=result)), 'Lucius7', 1)
            self.assertFalse(rows[0]['accepted'])

    def test_invalid_scores_cannot_become_accepted(self):
        for score, full in [('NaN', '100'), ('Infinity', '100'), ('oops', '100'), ('0', '0'), ('100', 'NaN'), ('100', '-100')]:
            with self.subTest(score=score, full=full), self.assertRaisesRegex(ValueError, 'Invalid QOJ'):
                qoj.parse_submissions(page(submission_row(result=f'<a data-score="{score}" data-full="{full}">AC ✓</a>')), 'Lucius7', 1)

    def test_all_submissions_including_failed_are_retained_and_problem_ids_are_global(self):
        rows, _ = qoj.parse_submissions(page(submission_row(contest_id=7) + submission_row(9, result='Wrong Answer')), 'Lucius7', 1)
        self.assertEqual([row['problemId'] for row in rows], [42, 42])
        self.assertEqual(rows[0]['contestId'], 7)
        self.assertIsNone(rows[1]['contestId'])
        self.assertFalse(rows[1]['accepted'])
        self.assertEqual(rows[0]['name'], 'Example & Test')

    def test_next_page_is_read_from_pagination_and_duplicate_rows_are_deduplicated(self):
        client = FakeClient({
            'https://qoj.ac/submissions?submitter=Lucius7&page=1': page(submission_row(10) + submission_row(9), 2),
            'https://qoj.ac/submissions?submitter=Lucius7&page=2': page(submission_row(9) + submission_row(8), current_page=2),
        })
        self.assertEqual(len(qoj.get_submissions(client, 'Lucius7')), 3)
        self.assertEqual(len(client.urls), 2)

    def test_repeated_last_page_does_not_silently_truncate(self):
        client = FakeClient({
            'https://qoj.ac/submissions?submitter=Lucius7&page=1': page(submission_row(10), 2),
            'https://qoj.ac/submissions?submitter=Lucius7&page=2': page(submission_row(10), current_page=2),
        })
        with self.assertRaisesRegex(ValueError, 'did not advance'):
            qoj.get_submissions(client, 'Lucius7')

    def test_real_pagination_first_and_last_pages(self):
        rows = ''.join(submission_row(100 - index) for index in range(10))
        links = '<li class="page-item disabled"><a href="#"><span class="glyphicon glyphicon-backward"></span></a></li>'
        links += '<li class="page-item active"><a href="submissions?submitter=Lucius7">1</a></li>'
        links += ''.join(f'<li class="page-item"><a href="submissions?submitter=Lucius7&amp;page={number}">{number}</a></li>' for number in range(2, 8))
        links += '<li class="page-item"><a href="submissions?submitter=Lucius7&amp;page=2"><span class="glyphicon glyphicon-forward"></span></a></li>'
        document = page(rows, pager=False) + f'<ul class="pagination">{links}</ul>'
        parsed, more = qoj.parse_submissions(document, 'Lucius7', 1)
        self.assertEqual(len(parsed), 10)
        self.assertTrue(more)
        _, more = qoj.parse_submissions(page(rows, current_page=7), 'Lucius7', 7)
        self.assertFalse(more)

    def test_pagination_fails_closed_on_missing_current_page_or_skipped_next_page(self):
        rows = ''.join(submission_row(100 - index) for index in range(10))
        with self.assertRaisesRegex(ValueError, 'pagination missing'):
            qoj.parse_submissions(page(rows, pager=False), 'Lucius7', 1)
        with self.assertRaisesRegex(ValueError, 'pagination missing'):
            qoj.parse_submissions(page(submission_row(), pager=False), 'Lucius7', 2)
        malformed = page(rows, 2).replace('page-item active', 'page-item')
        with self.assertRaisesRegex(ValueError, 'current page is missing'):
            qoj.parse_submissions(malformed, 'Lucius7', 1)
        with self.assertRaisesRegex(ValueError, 'different current page'):
            qoj.parse_submissions(page(rows, 2), 'Lucius7', 2)
        with self.assertRaisesRegex(ValueError, 'skipped the next page'):
            qoj.parse_submissions(page(rows, 3), 'Lucius7', 1)
        wrong_user = page(rows, 2).replace('submitter=Lucius7&amp;page=2', 'submitter=Other&amp;page=2')
        with self.assertRaisesRegex(ValueError, 'selected user'):
            qoj.parse_submissions(wrong_user, 'Lucius7', 1)
        wrong_arrow = page(rows, 2).replace('</ul>', '<li><a href="submissions?submitter=Lucius7&amp;page=3"><span class="glyphicon-forward"></span></a></li></ul>')
        with self.assertRaisesRegex(ValueError, 'not consecutive'):
            qoj.parse_submissions(wrong_arrow, 'Lucius7', 1)

    def test_login_page_wrong_user_and_unknown_markup_are_not_empty_success(self):
        with self.assertRaises(qoj.AuthenticationRequired):
            qoj.parse_submissions('<form id="form-login"><input type="password"></form>', 'Lucius7', 1)
        with self.assertRaisesRegex(ValueError, 'another user'):
            qoj.parse_submissions(page(submission_row(user='someone_else')), 'Lucius7', 1)
        with self.assertRaisesRegex(ValueError, 'table missing'):
            qoj.parse_submissions('<h2>Error</h2>', 'Lucius7', 1)

    def test_session_cannot_be_forwarded_to_another_origin(self):
        request = Request('https://qoj.ac/submissions', headers={'Cookie': 'test-session'})
        redirect = qoj.SafeRedirect()
        for destination in ('https://example.com/login', 'http://qoj.ac/login', 'https://qoj.ac/login', 'https://qoj.ac:8443/submissions', 'https://someone@qoj.ac/submissions'):
            with self.assertRaises(qoj.AuthenticationRequired):
                redirect.redirect_request(request, None, 302, '', {}, destination)

    def test_rate_limit_stops_without_retrying_or_exposing_cookie(self):
        client = qoj.QojClient('private-test-cookie')
        client.opener = Mock()
        client.opener.open.side_effect = HTTPError('https://qoj.ac/submissions', 429, 'private-test-cookie', {}, None)
        with patch.object(qoj.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 429') as error:
                client.get('https://qoj.ac/submissions')
        self.assertEqual(client.opener.open.call_count, 1)
        sleep.assert_not_called()
        self.assertNotIn('private-test-cookie', str(error.exception))

    def test_transient_failures_have_bounded_retries_and_are_sanitized(self):
        for failure in (URLError('private-test-cookie'), IncompleteRead(b'private-test-cookie')):
            with self.subTest(failure=type(failure).__name__):
                client = qoj.QojClient('private-test-cookie')
                client.opener = Mock()
                client.opener.open.side_effect = failure
                with patch.object(qoj.time, 'sleep'):
                    with self.assertRaisesRegex(RuntimeError, 'QOJ request failed') as error:
                        client.get('https://qoj.ac/submissions')
                self.assertEqual(client.opener.open.call_count, 3)
                self.assertNotIn('private-test-cookie', str(error.exception))

    def test_problem_provenance_and_complete_contest_table(self):
        problem = qoj.parse_problem('''<html><body><aside><a href="/contest/999">Unrelated</a></aside><h2>#42. Example &amp; Test</h2><div><p>The problem was used in the following contests:</p><ul><li><a href="/contest/7">Example Contest</a></li></ul></div></body></html>''', 42)
        self.assertEqual(problem, {'name': 'Example & Test', 'relatedContestIds': [7]})
        contest = qoj.parse_contest('''<h1>QOJ</h1><h2>Example Contest</h2><table><thead><tr><th>#</th><th>Problem</th></tr></thead><tr><td>A</td><td><a href="/contest/7/problem/42">First</a></td></tr><tr><td>B</td><td><a href="/contest/7/problem/43">Second</a></td></tr></table>''', 7)
        self.assertEqual(contest['name'], 'Example Contest')
        self.assertEqual([p['id'] for p in contest['problems']], ['qoj:42', 'qoj:43'])

    def test_real_contest_layout_ignores_notice_and_question_tables(self):
        title = 'The 2026 ICPC China Shenyang National Invitational Programming Contest'
        rows = ''.join(f'<tr><td>{chr(65 + offset)}</td><td><a href="/contest/3945/problem/{19013 + offset}">Problem {offset}</a></td></tr>' for offset in range(13))
        document = f'<h1>QOJ.ac</h1><h1>{title}</h1><table><thead><tr><th>#</th><th>Problem</th></tr></thead><tbody>{rows}</tbody></table>'
        document += '<h2>Notice</h2><table><thead><tr><th>#</th><th>Message</th></tr></thead><tr><td>1</td><td><a href="/contest/3945/problem/99999">Unrelated link</a></td></tr></table>'
        document += '<h2>My Questions</h2><table><tr><td>1</td><td><a href="/contest/3945/problem/99998">Question link</a></td></tr></table>'
        contest = qoj.parse_contest(document, 3945)
        self.assertEqual(contest['name'], title)
        self.assertEqual(len(contest['problems']), 13)
        self.assertEqual([problem['index'] for problem in contest['problems']], list('ABCDEFGHIJKLM'))

    def test_only_contests_associated_with_submissions_are_fetched_including_wa_only(self):
        client = FakeClient({
            'https://qoj.ac/submissions?submitter=Lucius7&page=1': page(submission_row(10, contest_id=7, result='Wrong Answer')),
            'https://qoj.ac/contest/7': '<h2>Submitted Contest</h2><table><thead><tr><th>#</th><th>Problem</th></tr></thead><tr><td>A</td><td><a href="/contest/7/problem/42">First</a></td></tr><tr><td>B</td><td><a href="/contest/7/problem/43">Second</a></td></tr></table>',
        })
        result = qoj.collect(client, 'Lucius7', None)
        self.assertEqual(list(result['contests']), ['qoj:7'])
        self.assertTrue(result['contests']['qoj:7']['hasSubmissions'])
        self.assertEqual(result['accepted'], [])
        self.assertEqual(result['attempted'], {'qoj:42'})
        self.assertIsNone(result['profile']['lastSuccess'])
        self.assertFalse(any(url == 'https://qoj.ac/contests' for url in client.urls))

    def test_reused_problem_does_not_imply_participation_or_change_contest_activity_time(self):
        client = FakeClient({
            'https://qoj.ac/submissions?submitter=Lucius7&page=1': page(
                submission_row(12, contest_id=8, date='2026-09-08 02:00:00')
                + submission_row(11, date='2026-09-08 01:00:00')
                + submission_row(10, contest_id=7)),
            'https://qoj.ac/contest/7': '<h2>First Contest</h2><table><thead><tr><th>#</th><th>Problem</th></tr></thead><tr><td>A</td><td><a href="/contest/7/problem/42">First</a></td></tr></table>',
            'https://qoj.ac/contest/8': '<h2>Second Contest</h2><table><thead><tr><th>#</th><th>Problem</th></tr></thead><tr><td>B</td><td><a href="/contest/8/problem/42">First</a></td></tr></table>',
        })
        previous = {'problems': [{'id': 'qoj:42', 'platform': 'qoj', 'contestId': 'qoj:99', 'index': '42', 'name': 'First', 'url': 'https://qoj.ac/problem/42', 'difficulty': None, 'relatedContestIds': [7, 8, 99]}], 'contests': []}
        result = qoj.collect(client, 'Lucius7', previous)
        self.assertEqual(set(result['contests']), {'qoj:7', 'qoj:8'})
        self.assertEqual(result['contests']['qoj:7']['lastSubmissionEpoch'], 1788796800)
        self.assertEqual(result['contests']['qoj:8']['lastSubmissionEpoch'], 1788804000)
        self.assertEqual(result['problems']['qoj:42']['contestId'], 'qoj:8')
        self.assertNotIn('relatedContestIds', result['problems']['qoj:42'])
        self.assertTrue(all('/problem/' not in url for url in client.urls))

    def test_practice_only_submissions_remain_visible_without_invented_contest_membership(self):
        client = FakeClient({'https://qoj.ac/submissions?submitter=Lucius7&page=1': page(submission_row())})
        result = qoj.collect(client, 'Lucius7', None)
        self.assertEqual(result['contests'], {})
        self.assertEqual(result['attempted'], {'qoj:42'})
        self.assertEqual(len(result['accepted']), 1)
        self.assertEqual(len(client.urls), 1)


if __name__ == '__main__':
    unittest.main()
