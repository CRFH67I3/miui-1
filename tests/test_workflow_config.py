# -*- coding: utf-8 -*-
"""
Action 工作流里「Generate config.yaml from secrets」这一步的离线测试。

不联网、不跑真实任务，只验证：把 mock 的 Secret 喂进那段生成脚本后，
产出的 config.yaml 是否符合预期。重点是几条容易静默出错的规则：

  1. 只配 Cookie 就能跑，不需要 uid / password
  2. uid 填了邮箱等非数字内容时，有 Cookie 应降级为警告而不是让整次运行失败
  3. uid 填了手机号（也是纯数字，isdigit 拦不住）时，应以 Cookie 里的 userId 为准，
     否则 device-id 会按错误 uid 派生，设备指纹漂移
  4. 什么都没配时必须明确报错，而不是生成一个空账号
  5. 开关默认值：check-in / logging 为 True，carrot-pull 为 False
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github' / 'workflows' / 'run-task.yml'

# 假的 Cookie，只保留会被解析的字段，不含任何真实凭据
FAKE_COOKIE = ('uLocale=zh_CN; cUserId=abc123; passToken=FAKE_TOKEN; '
               'userId=1000000001; miui_vip_ph=FAKE_PH; miui_vip_serviceToken=FAKE_ST;')


def extract_generate_step():
    """从 workflow 里抠出内嵌的 python 代码（heredoc 内容需要去掉 YAML 缩进）"""
    text = WORKFLOW.read_text(encoding='utf-8')
    marker = "python - <<'PY'\n"
    if marker not in text:
        raise AssertionError('workflow 里找不到 Generate 步骤的 heredoc')
    body = text.split(marker, 1)[1].split('\n          PY', 1)[0]
    return '\n'.join(line[10:] if line.startswith(' ' * 10) else line
                     for line in body.split('\n'))


class GenerateConfigTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='miui-wf-')
        cls.script = os.path.join(cls.tmp, '_gen.py')
        with open(cls.script, 'w', encoding='utf-8', newline='\n') as fp:
            fp.write(extract_generate_step())

    def run_step(self, uid='', password='', cookie='', extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith('MIUI_')}
        env.update({'MIUI_UID': uid, 'MIUI_PASSWORD': password, 'MIUI_COOKIE': cookie})
        if extra_env:
            env.update(extra_env)
        cfg_path = os.path.join(self.tmp, 'config.yaml')
        if os.path.exists(cfg_path):
            os.remove(cfg_path)
        proc = subprocess.run([sys.executable, self.script], cwd=self.tmp, env=env,
                              capture_output=True, text=True)
        config = None
        if proc.returncode == 0:
            with open(cfg_path, encoding='utf-8') as fp:
                config = yaml.safe_load(fp)
        return proc, config

    def test_cookie_only(self):
        """只配 Cookie：云端唯一推荐的方式"""
        proc, config = self.run_step(cookie=FAKE_COOKIE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        account = config['accounts'][0]
        self.assertEqual(account['cookie'], FAKE_COOKIE)
        self.assertNotIn('password', account)
        self.assertEqual(account['uid'], '1000000001')

    def test_uid_email_with_cookie_is_warning(self):
        """uid 填了邮箱：有 Cookie 时应继续跑，只告警"""
        proc, config = self.run_step(uid='me@example.com', cookie=FAKE_COOKIE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('不是纯数字', proc.stdout)
        self.assertEqual(config['accounts'][0]['uid'], '1000000001')

    def test_uid_phone_overridden_by_cookie(self):
        """uid 填了手机号：isdigit 拦不住，必须用 Cookie 里的 userId 纠正"""
        proc, config = self.run_step(uid='13800138000', cookie=FAKE_COOKIE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('以 Cookie 为准', proc.stdout)
        self.assertEqual(config['accounts'][0]['uid'], '1000000001')

    def test_uid_and_password_without_cookie(self):
        """回退到账号密码登录：这条路径下 uid 必须是纯数字"""
        proc, config = self.run_step(uid='1000000001', password='plain-pass')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        account = config['accounts'][0]
        self.assertEqual(account['password'], 'plain-pass')
        self.assertNotIn('cookie', account)

    def test_non_digit_uid_without_cookie_fails(self):
        """没有 Cookie 又没有合法 uid：必须明确失败，不能生成空账号"""
        proc, _ = self.run_step(uid='13800138000x', password='plain-pass')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('::error::', proc.stdout)

    def test_nothing_configured_fails(self):
        """一个凭据都没有：直接报错"""
        proc, _ = self.run_step()
        self.assertEqual(proc.returncode, 1)
        self.assertIn('缺少登录凭据', proc.stdout)

    def test_switches_default(self):
        """不配任何 Variable 时的默认值：签到开、日志开、拔萝卜关"""
        proc, config = self.run_step(cookie=FAKE_COOKIE)
        self.assertEqual(proc.returncode, 0)
        account = config['accounts'][0]
        self.assertIs(account['check-in'], True)
        self.assertIs(account['carrot-pull'], False)
        self.assertIs(account['browse-specialpage'], False)
        self.assertIs(config['logging'], True)

    def test_switches_can_be_disabled(self):
        """Variable 显式写 false 时能关掉签到"""
        proc, config = self.run_step(cookie=FAKE_COOKIE,
                                     extra_env={'MIUI_CHECK_IN': 'false'})
        self.assertEqual(proc.returncode, 0)
        self.assertIs(config['accounts'][0]['check-in'], False)

    def test_password_is_masked(self):
        """密码与 Cookie 都要走 ::add-mask::，避免出现在公开日志里"""
        proc, _ = self.run_step(uid='1000000001', password='plain-pass',
                                cookie=FAKE_COOKIE)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('::add-mask::plain-pass', proc.stdout)
        self.assertIn('::add-mask::' + FAKE_COOKIE, proc.stdout)

    def test_newline_in_cookie_is_squashed(self):
        """从网页复制 Cookie 容易带换行，生成时应被压成一行"""
        wormy = FAKE_COOKIE.replace('; ', ';\n')
        proc, config = self.run_step(cookie=wormy)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn('\n', config['accounts'][0]['cookie'])
        self.assertIn('userId=1000000001', config['accounts'][0]['cookie'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
