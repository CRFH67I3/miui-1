# -*- coding: utf-8 -*-
"""签到幂等 + 任务映射离线测试（不发任何真实请求）"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import miuitask as m


def make_task():
    return m.MIUITask('1', '', 'UA', 'device')


class TestMapping(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(m.match_task_action('每日签到')[0], 'check_in')
        self.assertEqual(m.match_task_action('小米社区微信小程序签到获成长值')[0], 'wechat_checkin')
        self.assertEqual(m.match_task_action('浏览帖子超过10秒')[0], 'browse_post')
        self.assertIsNone(m.match_task_action('升级小米社区App至最新版本')[0])

    def test_analyze(self):
        tasks = {
            '每日签到': {'group': '每日任务', 'state': m.TASK_STATE_TODO,
                     'desc': '签到奖励随机，以实际获得为准', 'jump_url': ''},
            '小米社区微信小程序签到获成长值': {'group': '每日任务', 'state': m.TASK_STATE_TODO,
                                   'desc': '成长值+3', 'jump_url': ''},
        }
        plan, skipped = m.analyze_tasks(tasks, {'check-in': True})
        by_title = {i['title']: i for i in plan}
        # 小程序签到：可自动完成
        self.assertEqual(by_title['小米社区微信小程序签到获成长值']['status'], 'run')
        self.assertEqual(by_title['小米社区微信小程序签到获成长值']['action'], 'wechat_checkin')
        # App 每日签到：人机校验做不了，不应发请求
        self.assertEqual(by_title['每日签到']['status'], 'manual')
        self.assertEqual(skipped, [])


class TestAppCheckIn(unittest.TestCase):
    def test_already_signed_sends_nothing(self):
        t = make_task()
        t.cookie = 'x=1'
        import datetime
        idx = datetime.date.today().isoweekday() - 1
        detail = [0] * 7
        detail[idx] = 2
        with mock.patch.object(t, 'check_in_info', return_value={'checkin7DaysDetail': detail,
                                                                'continueCheckInDays': 4}), \
                mock.patch('miuitask.requests.post') as post:
            self.assertTrue(t.check_in())
            post.assert_not_called()

    def test_unknown_state_sends_nothing(self):
        t = make_task()
        with mock.patch.object(t, 'check_in_info', return_value=None), \
                mock.patch('miuitask.requests.post') as post:
            self.assertFalse(t.check_in())
            post.assert_not_called()

    def test_not_signed_sends_once(self):
        t = make_task()
        t.cookie = 'x=1'
        with mock.patch.object(t, 'check_in_info', return_value={'checkin7DaysDetail': [0] * 7,
                                                                'continueCheckInDays': 0}), \
                mock.patch('miuitask.requests.post') as post:
            post.return_value.json.return_value = {'status': 640, 'message': '人机校验失败'}
            self.assertFalse(t.check_in())
            self.assertEqual(post.call_count, 1)


class TestWechatCheckIn(unittest.TestCase):
    def _run(self, card):
        t = make_task()
        session = mock.Mock()
        session.post.return_value.json.return_value = {'status': 200, 'entity': 3}
        with mock.patch.object(t, 'wechat_login', return_value=(session, 'ph')), \
                mock.patch.object(t, 'wechat_checkin_info', return_value=card):
            ok = t.wechat_checkin()
        return ok, session

    def test_done_by_showtype(self):
        ok, s = self._run({'buttons': [{'showType': 0, 'button': '去签到'}]})
        self.assertTrue(ok)
        s.post.assert_not_called()

    def test_done_by_text(self):
        ok, s = self._run({'buttons': [{'showType': 1, 'button': '已签到'}]})
        self.assertTrue(ok)
        s.post.assert_not_called()

    def test_no_card(self):
        ok, s = self._run(None)
        self.assertFalse(ok)
        s.post.assert_not_called()

    def test_unknown_state(self):
        ok, s = self._run({'buttons': [{'button': '去看看'}]})
        self.assertFalse(ok)
        s.post.assert_not_called()

    def test_todo_posts_once(self):
        ok, s = self._run({'buttons': [{'showType': 1, 'button': '签到'}]})
        self.assertTrue(ok)
        self.assertEqual(s.post.call_count, 1)
        self.assertEqual(s.post.call_args.kwargs['json'], {'action': 'WECHAT_CHECKIN_TASK'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
