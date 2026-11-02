# -*- coding: utf-8 -*-
"""钉钉通知离线测试：文案组装 + 加签 URL（不发任何真实请求）"""
import base64
import os
import sys
import time
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

from utils import notify


SAMPLE = [{
    'uid': '12345678',
    'ok': True,
    'error': '',
    'base': 37,
    'final': 40,
    'executed': ['小米社区微信小程序签到获成长值', '浏览帖子超过10秒'],
    'manual': ['每日签到'],
    'failed': [],
    'skipped': 3,
    'note': '',
}]


class TestFormatReport(unittest.TestCase):
    def test_contains_core_fields(self):
        text = notify.format_report(SAMPLE, time.time())
        self.assertIn('小米社区成长值任务', text)
        self.assertIn('12345678', text)
        self.assertIn('37 → 40（+3）', text)
        self.assertIn('微信小程序签到', text)
        self.assertIn('每日签到', text)
        self.assertIn('非成长值任务 3 条', text)
        self.assertIn('成功', text)
        self.assertIn('耗时', text)

    def test_failure_entry(self):
        text = notify.format_report([{
            'uid': '', 'ok': False, 'error': '登录失败（Cookie 可能已过期）',
            'base': None, 'final': None, 'executed': [], 'manual': [],
            'failed': [], 'skipped': 0, 'note': '',
        }], None)
        self.assertIn('失败：登录失败', text)
        self.assertIn('(来自 Cookie)', text)

    def test_no_executed_when_all_done(self):
        text = notify.format_report([{
            'uid': 'u', 'ok': True, 'error': '', 'base': 40, 'final': 40,
            'executed': [], 'manual': [], 'failed': [], 'skipped': 0, 'note': '',
        }], None)
        self.assertIn('已执行', text)
        self.assertIn('无（相关任务今天都已完成', text)

    def test_failed_action_listed(self):
        text = notify.format_report([{
            'uid': 'u', 'ok': False, 'error': '', 'base': 40, 'final': 40,
            'executed': ['浏览帖子超过10秒'], 'manual': [],
            'failed': ['浏览帖子超过10秒'], 'skipped': 0, 'note': '',
        }], None)
        self.assertIn('执行出错', text)
        self.assertIn('未完成', text)


class TestSignedUrl(unittest.TestCase):
    def test_without_secret(self):
        url = notify._signed_url('tok123', '')
        self.assertEqual(url, 'https://oapi.dingtalk.com/robot/send?access_token=tok123')
        self.assertNotIn('sign=', url)

    def test_with_secret(self):
        url = notify._signed_url('tok123', 'SECxxxxx')
        self.assertIn('timestamp=', url)
        self.assertIn('sign=', url)
        sign = urllib.parse.unquote(urllib.parse.parse_qs(url.split('?', 1)[1])['sign'][0])
        # HMAC-SHA256 的 base64 固定 44 字符（含结尾 =）
        self.assertEqual(len(sign), 44)
        self.assertTrue(sign.endswith('='))

    def test_sign_matches_manual_hmac(self):
        """用固定 timestamp 复算一次，防止以后改坏加签算法"""
        import hashlib
        import hmac

        secret = 'SECabc'
        timestamp = '1700000000000'
        expected = urllib.parse.quote_plus(base64.b64encode(
            hmac.new(secret.encode(), ('%s\n%s' % (timestamp, secret)).encode(),
                     hashlib.sha256).digest()))
        with mock.patch.object(notify.time, 'time', return_value=1700000000.0):
            url = notify._signed_url('tok', secret)
        self.assertIn('sign=' + expected, url)


class TestCredentials(unittest.TestCase):
    def test_env_takes_priority(self):
        with mock.patch.dict(os.environ, {'DINGTALK_TOKEN': 'env-token',
                                          'DINGTALK_SECRET': 'env-secret'}):
            self.assertEqual(notify.dingtalk_credentials(), ('env-token', 'env-secret'))

    def test_empty_env(self):
        with mock.patch.dict(os.environ, {'DINGTALK_TOKEN': '', 'DINGTALK_SECRET': ''}):
            token, secret = notify.dingtalk_credentials()
            # config.yaml 里没有 dingtalk 段时应当为空
            self.assertIsInstance(token, str)
            self.assertIsInstance(secret, str)

    def test_send_skipped_without_token(self):
        with mock.patch.dict(os.environ, {'DINGTALK_TOKEN': ''}), \
                mock.patch.object(notify, '_config_value', return_value=''):
            self.assertFalse(notify.send_dingtalk('t', 'x'))


if __name__ == '__main__':
    unittest.main()
