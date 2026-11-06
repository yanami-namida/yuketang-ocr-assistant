"""实际图片挑战、能力缓存和自动选择；全部模拟 API，不调用外部服务。"""
import copy
import json
import time
import unittest
from unittest.mock import Mock, patch

import requests
import ykt_llm as llm
import ykt_watcher as watcher
from tools import set_api_key


def config():
    return copy.deepcopy(watcher.DEFAULT_CONFIG)


def capability(cfg, supported=True, key='fake-key'):
    return {'binding':llm.capability_binding(cfg,key), 'checked_at':time.time(),
            'status':'supported' if supported else 'unsupported', 'supported':supported,
            'message':'测试通过' if supported else '图片不支持'}


class CapabilityTests(unittest.TestCase):
    def test_reads_random_pixels_instead_of_accepting_any_200(self):
        cfg = config()
        expected, data_url = llm.vision_challenge()
        response = Mock(status_code=200)
        response.json.return_value = {'choices':[{'message':{'content':json.dumps({'code':expected})}}]}
        with patch.object(llm,'vision_challenge',return_value=(expected,data_url)), \
                patch.object(llm.requests,'post',return_value=response) as post:
            result = llm.probe_vision_capability(cfg,'fake-key')
        self.assertTrue(result['supported'])
        blocks = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertNotIn(expected, blocks[0]['text'])
        self.assertEqual(blocks[1]['image_url']['url'],data_url)
        self.assertNotIn('fake-key',json.dumps(result))
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_gateway_ignoring_image_does_not_pass(self):
        response = Mock(status_code=200)
        response.json.return_value = {'choices':[{'message':{'content':'OK'}}]}
        with patch.object(llm.requests,'post',return_value=response):
            result = llm.probe_vision_capability(config(),'fake-key')
        self.assertEqual(result['status'],'inconclusive')
        self.assertIsNone(result['supported'])

    def test_duplicate_expected_codes_are_not_accepted(self):
        response = Mock(status_code=200)
        response.json.return_value = {'choices':[{'message':{'content':'222222 222222'}}]}
        with patch.object(llm,'vision_challenge',return_value=('222222','data:image/png;base64,test')), \
                patch.object(llm.requests,'post',return_value=response):
            self.assertIsNone(llm.probe_vision_capability(config(),'fake-key')['supported'])

    def test_rejection_and_temporary_errors_are_distinguished(self):
        for status, expected in [(400,'unsupported'),(415,'unsupported'),(422,'unsupported'),
                                 (429,'inconclusive'),(503,'inconclusive'),(401,'inconclusive')]:
            with self.subTest(status=status), patch.object(llm.requests,'post',return_value=Mock(status_code=status)):
                result = llm.probe_vision_capability(config(),'fake-key')
                self.assertEqual(result['status'],expected)
                self.assertNotIn('fake-key',result['message'])

    def test_network_and_non_chat_responses_are_inconclusive(self):
        with patch.object(llm.requests,'post',side_effect=requests.Timeout()):
            self.assertEqual(llm.probe_vision_capability(config(),'fake-key')['status'],'inconclusive')
        response = Mock(status_code=200)
        response.json.return_value = {'error':'not chat'}
        with patch.object(llm.requests,'post',return_value=response):
            self.assertEqual(llm.probe_vision_capability(config(),'fake-key')['status'],'inconclusive')

    def test_cached_results_are_bound_to_model_endpoint_key_and_reasoning(self):
        cfg = config()
        cfg['llm']['vision_capability'] = capability(cfg)
        self.assertIsNotNone(llm.cached_vision_capability(cfg,'fake-key'))
        self.assertIsNone(llm.cached_vision_capability(cfg,'different-key'))
        for field, value in [('model','different-model'),('base_url','https://example.test/v1'),('reasoning_effort','none')]:
            changed=copy.deepcopy(cfg)
            changed['llm'][field]=value
            self.assertIsNone(llm.cached_vision_capability(changed,'fake-key'))
        cfg['llm']['base_url'] += '/chat/completions'
        self.assertIsNotNone(llm.cached_vision_capability(cfg,'fake-key'))

    def test_expired_and_corrupt_records_are_not_reused(self):
        cfg=config()
        cfg['llm']['vision_capability']=capability(cfg)
        cfg['llm']['vision_capability']['checked_at']-=8*86400
        self.assertIsNone(llm.cached_vision_capability(cfg,'fake-key'))
        cfg['llm']['vision_capability']['checked_at']='invalid'
        self.assertIsNone(llm.cached_vision_capability(cfg,'fake-key'))

    def test_text_failure_skips_image_request(self):
        with patch.object(set_api_key,'check_key',return_value=(False,'unauthorized')), \
                patch.object(set_api_key,'probe_vision_capability') as probe:
            result=set_api_key.check_capabilities(config(),'fake-key')
        self.assertFalse(result['ok'])
        self.assertIsNone(result['vision'])
        probe.assert_not_called()

    def test_text_success_is_usable_when_image_is_unsupported(self):
        cfg=config()
        tested=capability(cfg,False)
        with patch.object(set_api_key,'check_key',return_value=(True,'OK')), \
                patch.object(set_api_key,'probe_vision_capability',return_value=tested):
            result=set_api_key.check_capabilities(cfg,'fake-key')
        self.assertTrue(result['ok'])
        self.assertFalse(result['vision']['supported'])


class ImageRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_detects_and_uses_image_only_when_verified(self):
        for status, supported in [('supported',True),('unsupported',False),('inconclusive',None)]:
            cfg=config()
            tested=capability(cfg,bool(supported))
            tested.update(status=status,supported=supported)
            with patch.object(watcher,'probe_vision_capability',return_value=tested) as probe, patch.object(watcher,'log'):
                await watcher.configure_image_mode(cfg,'fake-key')
                await watcher.configure_image_mode(cfg,'fake-key')
            self.assertEqual(cfg['vision'],supported is True)
            probe.assert_called_once()

    async def test_manual_choices_do_not_probe_or_use_stale_auto_result(self):
        for mode in ('vision','ocr'):
            cfg=config()
            cfg['image_mode']=mode
            cfg['llm']['vision_capability']=capability(cfg,mode=='ocr')
            with patch.object(watcher,'probe_vision_capability') as probe, patch.object(watcher,'log'):
                await watcher.configure_image_mode(cfg,'fake-key')
            self.assertEqual(cfg['vision'],mode=='vision')
            probe.assert_not_called()

    async def test_missing_key_uses_ocr_without_network(self):
        cfg=config()
        with patch.object(watcher,'probe_vision_capability') as probe, patch.object(watcher,'log'):
            await watcher.configure_image_mode(cfg,None)
        self.assertFalse(cfg['vision'])
        probe.assert_not_called()

    def test_legacy_switch_and_new_mode(self):
        self.assertEqual(llm.image_mode({'vision':False}),'ocr')
        self.assertEqual(llm.image_mode({'vision':True}),'auto')
        self.assertEqual(llm.image_mode({'vision':False,'image_mode':'auto'}),'auto')


if __name__=='__main__':
    unittest.main()
