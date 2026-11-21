"""FU33: fake CLIs only. Never invoke an installed provider CLI."""
import base64
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hrusage as usage


def expiry_worker(conn, service, home, network):
    os.setsid()
    try:
        while True:
            now=conn.recv()
            conn.send(dict(usage.row(service,now,'err','expired'),token_expired=service=='grok',
                           next_probe_at=now+20,probe_at=now))
    except EOFError:
        pass


def hanging_refresh(conn, service, home):
    os.setsid()
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    Path(home,'child.json').write_text(json.dumps([os.getpid(),child.pid]))
    time.sleep(60)


def success_refresh(conn, service, home):
    """Exercise the real refresh worker with a fake CLI and a local quota source."""
    original=usage.run_refresh_cli
    def fetch(root,now):
        with (root/'fetch-calls').open('a') as stream:stream.write('fetch\n')
        if usage.stored_token_expired(service,root):raise usage.ExpiredToken('expired')
        return {'wk_pct':23,'wk_reset':now+1000},'fixture'
    def run(provider,root,fresh):
        return original(provider,root,fresh,commands={provider:[sys.executable,str(root/'fake-cli.py')]})
    with patch.object(usage,'limits_fetcher',return_value=fetch), \
         patch.object(usage,'cli_running',return_value=False),patch.object(usage,'run_refresh_cli',side_effect=run):
        usage._refresh_worker(conn,service,home)


def wait_group_exit(pids, timeout=1.0):
    # killpg delivers a signal; grandchild exit isn't synchronous with return.
    # Preserve the all-owned-PIDs assertion with a bounded scheduling window.
    end=time.monotonic()+timeout
    def finished(pid):
        try:
            return (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[0]=='Z'
        except FileNotFoundError:
            return True
    while time.monotonic()<end and not all(finished(pid) for pid in pids):
        time.sleep(.005)
    return all(finished(pid) for pid in pids)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.home=Path(self.tmp.name)
        self.cli=self.home/'fake-cli.py'

    def tearDown(self):
        self.tmp.cleanup()

    def auth(self, service, expired=True):
        expiry=time.time()+(-100 if expired else 3600)
        if service=='claude':
            path=self.home/'.claude/.credentials.json'
            auth={'claudeAiOauth':{'accessToken':'fake-secret','expiresAt':expiry*1000}}
        elif service=='codex':
            path=self.home/'.codex/auth.json'
            token=base64.urlsafe_b64encode(json.dumps({'exp':expiry}).encode()).decode().rstrip('=')
            auth={'tokens':{'access_token':'fake.'+token+'.signature'}}
        elif service=='grok':
            path=self.home/'.grok/auth.json'
            auth={'login':{'auth_mode':'oidc','key':'fake-secret','expires_at':expiry}}
        else:
            path=self.home/'.gemini/antigravity-cli/antigravity-oauth-token'
            auth={'access_token':'fake-secret','expiry':expiry}
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(auth))
        return path,auth

    def fake(self, body):
        self.cli.write_text(body)
        return {s:[sys.executable,str(self.cli)] for s in usage.SERVICES}

    def run_cli(self, service, body, timeout=.5):
        return usage.run_refresh_cli(service,self.home,
            lambda:not usage.stored_token_expired(service,self.home),
            timeout=timeout,commands=self.fake(body))

    def test_each_fake_cli_can_write_fresh_expiry(self):
        for service in usage.SERVICES:
            with self.subTest(service=service), patch.object(usage,'gemini_auth',
                    side_effect=lambda home:json.loads((home/'.gemini/antigravity-cli/antigravity-oauth-token').read_text())):
                path,auth=self.auth(service,False)
                self.auth(service,True)
                result=self.run_cli(service,
                    'from pathlib import Path\nimport time\n'
                    f'Path({str(path)!r}).write_text({json.dumps(auth)!r})\n'
                    'time.sleep(10)\n')
                self.assertEqual(result,'success')
                self.assertFalse(usage.stored_token_expired(service,self.home))

    def test_timeout_and_output_flood_are_bounded(self):
        self.auth('grok')
        start=time.monotonic()
        result=self.run_cli('grok',"import os,time\nwhile True: os.write(1,b'x'*65536)\n",.2)
        self.assertEqual(result,'refresh timeout')
        self.assertLess(time.monotonic()-start,1)

    def test_failure_and_login_required_do_not_leak_output(self):
        self.auth('grok')
        self.assertEqual(self.run_cli('grok',"import sys; print('private-token'); sys.exit(1)"),'refresh failed')
        self.assertEqual(self.run_cli('grok',"import time; print('invalid_grant private-token',flush=True); time.sleep(5)"),'login required')
        self.assertEqual(self.run_cli('grok',"import sys; print('Run grok login to authenticate',flush=True); sys.exit(1)"),'login required')

    def test_codex_handshake_contains_only_explicit_auth_refresh(self):
        self.auth('codex')
        messages=self.home/'messages.json'
        result=self.run_cli('codex',
            "import sys,json\nfrom pathlib import Path\n"
            "a=[json.loads(sys.stdin.readline()) for _ in range(3)]\n"
            f"Path({str(messages)!r}).write_text(json.dumps(a))\n"
            "print(json.dumps({'id':2,'result':{'account':None}}),flush=True)\n"
            "import time; time.sleep(5)\n")
        self.assertEqual(result,'login required')
        self.assertEqual([m['method'] for m in json.loads(messages.read_text())],
                         ['initialize','initialized','account/read'])
        self.assertTrue(json.loads(messages.read_text())[2]['params']['refreshToken'])

    def test_shared_rate_limit_and_failure_backoff_survive_new_cache(self):
        now=time.time()
        with patch.object(usage,'cli_running',return_value=False):
            self.assertTrue(usage.reserve_refresh(self.home,'grok',now)[0])
            self.assertFalse(usage.reserve_refresh(self.home,'grok',now+1799)[0])
            usage.finish_refresh(self.home,'grok','refresh failed')
            self.assertTrue(usage.reserve_refresh(self.home,'grok',now+1801)[0])
            usage.finish_refresh(self.home,'grok','refresh failed')
        state=usage.refresh_read(self.home,'grok')
        self.assertEqual(state['failures'],2)
        self.assertGreaterEqual(state['next_at'],time.time()+3599)

    def test_abandoned_refresh_has_a_reason_during_cooldown(self):
        now=time.time()
        with patch.object(usage,'cli_running',return_value=False):
            usage.reserve_refresh(self.home,'grok',now)
            self.assertEqual(usage.reserve_refresh(self.home,'grok',now+21),
                             (False,'refresh stopped'))

    def test_running_cli_skips_without_consuming_attempt(self):
        with patch.object(usage,'cli_running',return_value=True):
            self.assertEqual(usage.reserve_refresh(self.home,'grok',time.time()),
                             (False,'expired (CLI running)'))
        self.assertFalse(usage.refresh_path(self.home,'grok').exists())

    def test_process_identity_checks_exe_and_script_but_never_prompts(self):
        root=self.home/'proc';root.mkdir()
        proc=root/'999999';proc.mkdir()
        (proc/'stat').write_text('999999 (fake) S '+'0 '*30)
        (proc/'exe').symlink_to('/usr/bin/python3')
        (proc/'cmdline').write_bytes(b'python3\0script.py\0claude\0')
        self.assertFalse(usage.cli_running('claude',root))
        (proc/'cmdline').write_bytes(b'node\0/opt/@anthropic-ai/claude-code/cli.js\0')
        self.assertTrue(usage.cli_running('claude',root))
        (proc/'cmdline').write_bytes(b'python3\0')
        (proc/'exe').unlink();(proc/'exe').symlink_to('/usr/bin/grok')
        self.assertTrue(usage.cli_running('grok',root))
        (proc/'stat').write_text('999999 (fake) Z '+'0 '*30)
        self.assertFalse(usage.cli_running('grok',root))

    def test_only_confirmed_expiry_is_refreshable(self):
        now=time.time()
        expired=Mock(side_effect=usage.ExpiredToken('fixture expired'))
        state=usage.shared_probe('grok',self.home,now,expired)
        self.assertTrue(state['token_expired'])
        regular=Mock(side_effect=usage.UsageError('limits HTTP 401',401))
        state=usage.shared_probe('grok',self.home,now+21,regular)
        self.assertFalse(state['token_expired'])
        self.assertEqual(state['error'],'limits HTTP 401')

    def test_retry_exactly_once_and_never_models_on_missing_login(self):
        self.auth('grok',False)
        conn=Mock()
        fetch=Mock(side_effect=[usage.ExpiredToken('expired'),({'wk_pct':7},'fixture')])
        with patch.object(usage.os,'setsid'),patch.object(usage.os,'getpgrp',return_value=-1), \
             patch.object(usage,'limits_fetcher',return_value=fetch), \
             patch.object(usage,'cli_running',return_value=False),patch.object(usage,'run_refresh_cli',return_value='success'):
            usage._refresh_worker(conn,'grok',self.home)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(conn.send.call_args.args[0]['good']['limits']['wk_pct'],7)
        self.assertEqual(conn.send.call_args.args[0]['outcome'],'success')

    def test_dead_refresh_token_is_login_required(self):
        self.auth('grok')
        fetch=Mock(side_effect=usage.ExpiredToken('expired'))
        conn=Mock()
        with patch.object(usage.os,'setsid'),patch.object(usage.os,'getpgrp',return_value=-1), \
             patch.object(usage,'limits_fetcher',return_value=fetch), \
             patch.object(usage,'cli_running',return_value=False),patch.object(usage,'run_refresh_cli',return_value='login required'):
            usage._refresh_worker(conn,'grok',self.home)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(conn.send.call_args.args[0],{'outcome':'login required','good':None})

    def test_worker_rereads_running_guard_immediately_before_launch(self):
        self.auth('grok')
        fetch=Mock(side_effect=usage.ExpiredToken('expired'))
        conn=Mock()
        with patch.object(usage.os,'setsid'),patch.object(usage.os,'getpgrp',return_value=-1), \
             patch.object(usage,'limits_fetcher',return_value=fetch), \
             patch.object(usage,'cli_running',return_value=True),patch.object(usage,'run_refresh_cli') as run:
            usage._refresh_worker(conn,'grok',self.home)
        run.assert_not_called()
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(conn.send.call_args.args[0]['outcome'],'expired (CLI running)')

    def test_fresh_token_after_external_refresh_skips_cli(self):
        conn=Mock()
        fetch=Mock(return_value=({'wk_pct':9},'fixture'))
        with patch.object(usage.os,'setsid'),patch.object(usage.os,'getpgrp',return_value=-1), \
             patch.object(usage,'limits_fetcher',return_value=fetch), \
             patch.object(usage,'run_refresh_cli') as run:
            usage._refresh_worker(conn,'grok',self.home)
        run.assert_not_called()
        self.assertEqual(conn.send.call_args.args[0]['outcome'],'already fresh')

    def test_success_publishes_numbers_to_collector_despite_old_deadline(self):
        now=time.time()
        collector=usage.Collector('grok',self.home)
        with patch.object(usage,'grok_limits',side_effect=usage.ExpiredToken('expired')):
            self.assertTrue(collector.collect(now)['token_expired'])
        good=dict(limits={'wk_pct':17,'wk_reset':now+1000},at=now,note='fixture')
        with patch.object(usage,'cli_running',return_value=False):
            usage.reserve_refresh(self.home,'grok',now)
        usage.finish_refresh(self.home,'grok','success',good)
        with patch.object(usage,'grok_limits') as fetch:
            item=collector.collect(now+.1)
        fetch.assert_not_called()
        self.assertEqual(item['wk_pct'],17)
        self.assertFalse(item['token_expired'])

    def test_retry_http_error_cannot_reauthorize_another_refresh(self):
        now=time.time()
        with patch.object(usage,'cli_running',return_value=False):
            usage.reserve_refresh(self.home,'grok',now)
        usage.finish_refresh(self.home,'grok','limits retry failed')
        self.assertFalse(usage.budget_read(usage.request_root(self.home)/'grok.json')['token_expired'])

    def test_deleted_or_malformed_credentials_are_never_fresh(self):
        for service in usage.SERVICES:
            with self.subTest(service=service),patch.object(usage,'gemini_auth',return_value={}):
                self.assertTrue(usage.stored_token_expired(service,self.home))
        path,_=self.auth('grok');path.write_text('{')
        self.assertTrue(usage.stored_token_expired('grok',self.home))

    def test_missing_cli_has_fixed_reason(self):
        self.auth('grok')
        result=usage.run_refresh_cli('grok',self.home,lambda:False,
                                    commands={'grok':[str(self.home/'missing-cli')]})
        self.assertEqual(result,'CLI unavailable')

    def test_environment_removes_api_credentials_and_disables_browser(self):
        with patch.dict(os.environ,{'ANTHROPIC_API_KEY':'private','GROK_API_KEY':'private',
                                   'CLAUDE_CODE_OAUTH_TOKEN':'private','GEMINI_API_KEY':'private'}):
            env=usage.refresh_environment(self.home)
        self.assertNotIn('ANTHROPIC_API_KEY',env)
        self.assertNotIn('GROK_API_KEY',env)
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN',env)
        self.assertNotIn('GEMINI_API_KEY',env)
        self.assertEqual(env['BROWSER'],'/bin/false')

    def test_supervisor_is_nonblocking_and_kills_only_created_group(self):
        # Test-owned sibling is a canary for accidental broad termination.
        canary=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        cache=usage.UsageCache(self.home,worker=expiry_worker,refresh_worker=hanging_refresh)
        try:
            with patch.object(usage,'cli_running',return_value=False), \
                 patch.object(usage,'REFRESH_TIMEOUT',.6),self.assertLogs('herdremote',level='INFO') as logs:
                cache.start()
                end=time.monotonic()+5
                while time.monotonic()<end:
                    rows={r['svc']:r for r in cache.snapshot()}
                    if rows['grok']['note']=='refreshing...':break
                    time.sleep(.01)
                self.assertEqual(rows['grok']['note'],'refreshing...')
                start=time.monotonic()
                for _ in range(100):cache.snapshot();cache.request()
                self.assertLess(time.monotonic()-start,.1)
                while time.monotonic()<end:
                    if {r['svc']:r for r in cache.snapshot()}['grok']['note']=='refresh timeout':break
                    time.sleep(.01)
                self.assertEqual({r['svc']:r for r in cache.snapshot()}['grok']['note'],'refresh timeout')
                pids=json.loads((self.home/'child.json').read_text())
                self.assertTrue(wait_group_exit(pids))
                self.assertIsNone(canary.poll())
                text=' '.join(logs.output)
                for key in ['provider=grok','method=models','duration=','outcome=refresh timeout']:
                    self.assertIn(key,text)
        finally:
            cache.close()
            canary.kill();canary.wait()

    def test_shutdown_reaps_refresh_group(self):
        cache=usage.UsageCache(self.home,network=True,refresh_worker=hanging_refresh)
        with patch.object(usage,'cli_running',return_value=False):
            cache._queue_refresh(mp.get_context('spawn'),'grok')
        end=time.monotonic()+3
        while time.monotonic()<end and not (self.home/'child.json').exists():time.sleep(.01)
        pids=json.loads((self.home/'child.json').read_text())
        with self.assertLogs('herdremote',level='INFO'):
            cache._finish_refresh('grok',dict(outcome='refresh stopped'))
        self.assertTrue(wait_group_exit(pids))
        self.assertEqual(usage.refresh_read(self.home,'grok')['outcome'],'refresh stopped')

    def test_real_worker_fake_cli_completes_and_publishes_fresh_numbers(self):
        path,fresh=self.auth('grok',False)
        self.auth('grok')
        self.fake('from pathlib import Path\n'+f'Path({str(path)!r}).write_text({json.dumps(fresh)!r})\n')
        cache=usage.UsageCache(self.home,refresh_worker=success_refresh)
        try:
            with patch.object(usage,'cli_running',return_value=False):
                cache._queue_refresh(mp.get_context('spawn'),'grok')
            proc,conn,_=cache.refreshes['grok']
            self.assertTrue(conn.poll(4))
            result=conn.recv()
            self.assertEqual(result['outcome'],'success')
            self.assertEqual(result['good']['limits']['wk_pct'],23)
            with self.assertLogs('herdremote',level='INFO'):
                cache._finish_refresh('grok',result)
            self.assertFalse(proc.is_alive())
            live={item['svc']:item for item in cache.snapshot()}['grok']
            self.assertEqual(live['wk_pct'],23)
            self.assertEqual(live['note'],'fixture')
            self.assertFalse(live['token_expired'])
            self.assertEqual((self.home/'fetch-calls').read_text().splitlines(),['fetch','fetch'])
            item=usage.Collector('grok',self.home).collect()
            self.assertEqual(item['wk_pct'],23)
            self.assertFalse(item['token_expired'])
        finally:
            if 'grok' in cache.refreshes:cache._finish_refresh('grok',dict(outcome='refresh stopped'))


if __name__=='__main__':
    unittest.main()
