# -- coding:UTF-8 --
"""
运行结果通知：钉钉群自定义机器人（webhook，支持加签）。

凭据来源（优先级从高到低）：
  1. 环境变量 DINGTALK_TOKEN / DINGTALK_SECRET —— GitHub Actions 里用 Secrets 注入
  2. config.yaml 顶层 dingtalk: {token: ..., secret: ...} —— 本机调试用

没配 token 时静默跳过，不抛异常、不影响任务退出码。
钉钉机器人安全设置若选了「自定义关键词」，DINGTALK_SECRET 可以留空；
若选了「加签」，必须填与机器人一致的密钥。
"""
import base64
import hashlib
import hmac
import time
import urllib.parse

import requests

from utils.utils import w_log

DINGTALK_API = 'https://oapi.dingtalk.com/robot/send'


_CONFIG_CACHE = {}


def _config_value(key):
    """从 config.yaml 的 dingtalk 段取值；任何异常都当作没配。
    这里做一层缓存，避免 token / secret 各读一次配置文件、日志里重复打印加载信息。"""
    if 'conf' not in _CONFIG_CACHE:
        try:
            from utils.utils import get_config
            _CONFIG_CACHE['conf'] = get_config() or {}
        except Exception:
            _CONFIG_CACHE['conf'] = {}
    section = (_CONFIG_CACHE['conf'] or {}).get('dingtalk') or {}
    if not isinstance(section, dict):
        return ''
    return str(section.get(key) or '').strip()


def dingtalk_credentials():
    """返回 (token, secret)，任一为空表示未配置"""
    import os
    token = (os.environ.get('DINGTALK_TOKEN') or '').strip() or _config_value('token')
    secret = (os.environ.get('DINGTALK_SECRET') or '').strip() or _config_value('secret')
    return token, secret


def _signed_url(token, secret):
    """拼 webhook 地址；配了 secret 才走加签。timestamp 必须是毫秒"""
    if not secret:
        return '%s?access_token=%s' % (DINGTALK_API, urllib.parse.quote_plus(token))
    timestamp = str(int(time.time() * 1000))
    string_to_sign = '%s\n%s' % (timestamp, secret)
    digest = hmac.new(secret.encode('utf-8'),
                      string_to_sign.encode('utf-8'),
                      hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    return '%s?access_token=%s&timestamp=%s&sign=%s' % (
        DINGTALK_API, urllib.parse.quote_plus(token), timestamp, sign)


def send_dingtalk(title, text, timeout=15):
    """
    发送 markdown 消息。返回 True/False，永不抛异常 ——
    通知只是附属功能，失败了不能影响任务本身的结果。
    """
    token, secret = dingtalk_credentials()
    if not token:
        w_log('未配置钉钉通知（DINGTALK_TOKEN），跳过')
        return False

    payload = {
        'msgtype': 'markdown',
        'markdown': {'title': title, 'text': text},
    }
    try:
        # 显式关掉代理：本机常见的系统代理会让请求 502，直连最稳
        response = requests.post(_signed_url(token, secret), json=payload,
                                 timeout=timeout,
                                 proxies={'http': None, 'https': None})
        body = response.json()
    except Exception as e:
        w_log('钉钉通知发送失败：%s: %s' % (type(e).__name__, e))
        return False

    if body.get('errcode') == 0:
        w_log('钉钉通知已发送')
        return True

    errcode = body.get('errcode')
    errmsg = body.get('errmsg') or ''
    hint = ''
    if errcode == 310000:
        hint = '（加签或关键词校验不通过：确认 DINGTALK_SECRET 与机器人安全设置一致）'
    elif errcode == 300001:
        hint = '（token 无效或机器人已被移除）'
    w_log('钉钉通知发送失败：errcode=%s errmsg=%s%s' % (errcode, errmsg, hint))
    return False


def _line(label, value):
    return '**%s**：%s\n\n' % (label, value)


def format_report(entries, started_at=None):
    """
    把运行结果渲染成钉钉 markdown。
    entries 每项：uid / ok / error / base / final / executed / manual / failed / skipped / note
    """
    today = time.strftime('%Y-%m-%d', time.localtime(started_at or time.time()))
    lines = ['### 小米社区成长值任务 %s\n\n' % today]

    for entry in entries:
        uid = entry.get('uid') or '(来自 Cookie)'
        lines.append('---\n\n')
        lines.append(_line('账号', uid))

        base, final = entry.get('base'), entry.get('final')
        if isinstance(base, int) and isinstance(final, int):
            delta = final - base
            lines.append(_line('成长值', '%d → %d（%s%d）'
                               % (base, final, '+' if delta >= 0 else '', delta)))
        elif isinstance(final, int):
            lines.append(_line('成长值', str(final)))
        elif isinstance(base, int):
            lines.append(_line('成长值', '%d（本次未取到结束值）' % base))

        executed = entry.get('executed') or []
        if executed:
            lines.append(_line('已执行', '、'.join(executed)))
        elif entry.get('ok'):
            lines.append(_line('已执行', '无（相关任务今天都已完成或没有可自动完成的任务）'))

        manual = entry.get('manual') or []
        if manual:
            lines.append(_line('需人工', '、'.join(manual)))

        failed = entry.get('failed') or []
        if failed:
            lines.append(_line('执行出错', '、'.join(failed)))

        skipped = entry.get('skipped') or 0
        if skipped:
            lines.append(_line('已忽略', '非成长值任务 %d 条' % skipped))

        if entry.get('note'):
            lines.append(_line('备注', entry['note']))

        if entry.get('error'):
            lines.append(_line('结果', '失败：' + entry['error']))
        else:
            lines.append(_line('结果', '成功' if entry.get('ok') else '未完成'))

    if started_at:
        cost = max(0, int(time.time() - started_at))
        lines.append('---\n\n耗时 %d 分 %d 秒\n' % (cost // 60, cost % 60))

    return ''.join(lines)


def notify_run_result(entries, started_at=None):
    """运行结束后调用：组装并发送。未配置时打印一行日志就返回"""
    token, _ = dingtalk_credentials()
    if not token:
        w_log('未配置 DINGTALK_TOKEN，跳过钉钉通知')
        return False
    title = '小米社区任务结果 %s' % time.strftime('%m-%d %H:%M', time.localtime())
    return send_dingtalk(title, format_report(entries, started_at))
