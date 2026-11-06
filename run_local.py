# -- coding:UTF-8 --
"""
本机试运行入口。

凭证只存在于内存中：不写 config.yaml、不写 .env、不落到任何文件。
唯一例外是登录成功后缓存到 .miui_cookie 的 Cookie（已加入 .gitignore），用来免去每天重新登录、
避免反复触发短信验证；加 --no-cache 即可完全不落盘。

用法：
    python run_local.py --net          仅检查网络、登录页与接口路由（不需要账号密码）
    python run_local.py --tasks        拉任务列表并解析「怎么完成」，只读，不发动作请求
    python run_local.py --check        同 --tasks（登录 + 任务列表 + 成长值，只读）
    python run_local.py                任务驱动执行：只做「存在且未完成」的任务，约 2~4 分钟
    python run_local.py --check-in --carrot-pull --specialpage
                                       额外开启签到 / 拔萝卜 / 专题页（默认关闭）
    python run_local.py --fast         把随机间隔压缩到 1~2 秒，仅用于调试
    python run_local.py --legacy       回到旧的固定序列（不参考任务列表，会发无效请求）

登录方式（三种，按需选择）：
    1. 账号密码：默认方式；若账号要求短信验证，脚本会自动下发验证码并在终端提示输入
    2. Cookie  ：python run_local.py --cookie ，粘贴浏览器里的社区 Cookie，完全跳过验证
    3. 本地缓存：登录成功过一次后自动复用 .miui_cookie，--login 可强制重新登录

凭证也可用环境变量传入，避免交互输入：
    MIUI_UID / MIUI_PASSWORD / MIUI_USER_AGENT / MIUI_COOKIE
"""
import argparse
import getpass
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 下统一输出为 UTF-8，避免中文日志经管道/重定向时变成乱码
if sys.platform == 'win32':
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass

# 依赖缺失时给出可执行的提示，而不是抛一堆 traceback
try:
    import requests  # noqa: F401
    import yaml  # noqa: F401
    import dotenv  # noqa: F401
except ImportError as exc:
    print('[中止] 缺少依赖：' + str(exc.name))
    print('       请先安装：python -m pip install -r requirements.txt')
    print('       当前解释器：' + sys.executable)
    sys.exit(1)

import miuitask
import utils.utils as utils
from miuitask import (MIUITask, start, analyze_tasks,
                      print_task_plan, _task_switches)
from utils.utils import md5_crypto, w_log
from utils import notify as notifier

# 红米 K70E（2311DRK48C，代号 duchamp）在 Chrome 下的真实 UA。
# 为什么看不到机型：Chrome 113 起 User-Agent Reduction 已全面生效，
# Android 版本被冻结为 "10"、设备型号被替换为字母 "K"。
# K70E 于 2023-11 发布、出厂即带 Chrome 119，从上市第一天起就发这个形态，
# 因此"红米 K70E + 2311DRK48C"的 Chrome UA 反而不存在、且与版本号自相矛盾。
DEFAULT_UA = ('Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36')


def read_input(prompt, secret=False):
    """读取交互输入；非交互环境（无 stdin）时给出明确提示而不是抛异常"""
    try:
        if secret:
            return getpass.getpass(prompt)
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print('\n[中止] 没有拿到输入。本脚本需要交互式终端，')
        print('       请在终端窗口里直接运行：python run_local.py --check')
        sys.exit(2)


def read_credential(prompt, env_key, secret=False):
    value = os.environ.get(env_key, '')
    if value.strip():
        return value.strip()
    return read_input(prompt, secret=secret)


# 登录成功后的 Cookie 缓存：省掉每天的登录与短信验证。已在 .gitignore 中
COOKIE_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.miui_cookie')


def load_cached_cookie():
    try:
        if not os.path.exists(COOKIE_CACHE):
            return ''
        with open(COOKIE_CACHE, 'r', encoding='utf-8') as fp:
            return fp.read().strip()
    except OSError:
        return ''


def save_cached_cookie(cookie):
    try:
        # 只写本机，权限尽量收紧
        fd = os.open(COOKIE_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            fp.write(cookie)
        w_log('已缓存 Cookie 到 ' + COOKIE_CACHE + '（下次直接复用，--login 可强制重新登录）')
    except OSError as e:
        w_log('Cookie 缓存写入失败：' + str(e))


# 无需凭据的接口清单：未带 cookie 时，路由存在返回 401，路由不存在返回 code=404
NET_TARGETS = (
    ('任务列表', 'https://api.vip.miui.com/mtop/planet/vip/member/getCheckinPageCakeList'),
    ('成长值 V2', 'https://api.vip.miui.com/mtop/planet/vip/member/addCommunityGrowUpPointByActionV2'),
    ('成长值 V1', 'https://api.vip.miui.com/mtop/planet/vip/member/addCommunityGrowUpPointByAction'),
    ('用户信息', 'https://api.vip.miui.com/mtop/planet/pc/post/userInfo'),
    ('取消点赞', 'https://api.vip.miui.com/mtop/planet/vip/content/announceCancelThumbUp'),
)
# 必然不存在的路径，用来验证「路由不存在」到底长什么样，防止判定方法本身失效
NET_CONTROL_URL = 'https://api.vip.miui.com/mtop/planet/vip/member/zzzNotExistRoute12345'


def net_check():
    """
    不输入账号密码的自检：只验证网络可达、接口路由存在、设备头未被风控拦截。
    这类请求不带任何凭据，服务器只会回「未登录」，不会触碰账号数据。
    """
    headers = utils.build_device_headers(DEFAULT_UA)
    issues = []

    w_log('---------- 网络 ----------')
    import socket
    for host in ('account.xiaomi.com', 'api.vip.miui.com'):
        t0 = time.time()
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_INET)
            ips = sorted({i[4][0] for i in infos})
            w_log('%s -> %s（%.0f ms）' % (host, ','.join(ips[:2]), (time.time() - t0) * 1000))
        except Exception as e:
            issues.append(host + ' 域名解析失败')
            w_log('%s 解析失败：%s' % (host, e))

    w_log('---------- 登录页可达性 ----------')
    try:
        r = requests.get('https://account.xiaomi.com/fe/service/login/password',
                         headers=headers, timeout=25)
        ok = r.status_code == 200 and 'html' in r.headers.get('Content-Type', '').lower()
        w_log('account.xiaomi.com 登录页：HTTP %d，%d 字节 %s'
              % (r.status_code, len(r.content), '正常' if ok else '异常'))
        if not ok:
            issues.append('登录页响应异常')
    except Exception as e:
        issues.append('登录页不可访问：' + type(e).__name__)
        w_log('登录页访问失败：%s: %s' % (type(e).__name__, e))

    w_log('---------- 接口路由（不带凭据，预期 401） ----------')
    for name, url in NET_TARGETS:
        try:
            t0 = time.time()
            r = requests.get(url, headers=headers, timeout=25, allow_redirects=False)
            ms = (time.time() - t0) * 1000
            try:
                r_json = r.json()
            except Exception:
                r_json = None
            code = r_json.get('status', r_json.get('code')) if isinstance(r_json, dict) else None
            if code == 401 or r.status_code == 401:
                state = '路由存在，需登录（正常）'
            elif code == 404:
                state = '路由不存在（接口已变更）'
                issues.append(name + ' 路由不存在')
            elif r.status_code in (403, 429):
                state = '被拦截/限流'
                issues.append(name + ' 被拦截')
            else:
                state = '异常响应 HTTP %d / code=%s' % (r.status_code, code)
                issues.append(name + ' 响应异常')
            w_log('%-10s HTTP %-3d %6.0fms  %s' % (name, r.status_code, ms, state))
        except Exception as e:
            issues.append(name + ' 请求失败')
            w_log('%-10s 请求失败：%s: %s' % (name, type(e).__name__, e))

    w_log('---------- 判定方法自检 ----------')
    try:
        r = requests.get(NET_CONTROL_URL, headers=headers, timeout=20)
        r_json = r.json() if r.text else {}
        code = r_json.get('status', r_json.get('code')) if isinstance(r_json, dict) else None
        w_log('伪造路径返回 code=%s（应=404，说明上面「路由不存在」可被识别）' % code)
        if code != 404:
            issues.append('路由不存在的判定形态与预期不符')
    except Exception as e:
        w_log('对照请求失败：%s' % type(e).__name__)

    w_log('---------- 头部是否被风控拦 ----------')
    try:
        url = NET_TARGETS[0][1]
        r1 = requests.get(url, headers={'cookie': 'cUserId=x'}, timeout=20)
        r2 = requests.get(url, headers=headers, timeout=20)
        w_log('仅 cookie 无 UA：HTTP %d；完整设备头：HTTP %d' % (r1.status_code, r2.status_code))
        if r2.status_code == 403 and r1.status_code != 403:
            issues.append('设备头反而被拦截')
    except Exception as e:
        w_log('对比失败：%s' % type(e).__name__)

    w_log('---------- 短信验证通道 ----------')
    # 这两个是账号要求二次验证时才会用到的接口：GET 返回 405/10017 就说明路由还在
    identity_checks = (
        ('取验证方式', 'https://account.xiaomi.com/identity/list', (200,)),
        ('下发验证码', 'https://account.xiaomi.com/identity/auth/sendPhoneTicket', (200, 405)),
    )
    for name, url, ok_codes in identity_checks:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code in ok_codes:
                w_log('%-10s HTTP %-3d 路由存在' % (name, r.status_code))
            else:
                issues.append(name + ' 接口异常')
                w_log('%-10s HTTP %-3d 异常' % (name, r.status_code))
        except Exception as e:
            issues.append(name + ' 请求失败')
            w_log('%-10s 请求失败：%s' % (name, type(e).__name__))

    w_log('---------- 结论 ----------')
    if issues:
        for i in issues:
            w_log('问题：' + i)
        w_log('以上问题与账号无关，先解决环境再跑 --check')
        return 1
    w_log('网络、登录页、接口路由、请求头全部正常，可以进入 --check（需要输入账号密码）')
    return 0


def enable_fast_mode():
    """把 20~40 秒的等待压缩到 1~2 秒，仅用于本机调试"""
    miuitask.random_sleep = lambda: miuitask.time.sleep(1)
    miuitask.sleep_ten_sec_more = lambda: miuitask.time.sleep(2)
    utils.random_sleep = lambda: utils.time.sleep(1)
    utils.sleep_ten_sec_more = lambda: utils.time.sleep(2)
    w_log('已启用 fast 模式：动作间隔压缩为 1~2 秒，仅用于调试')


def check_only(task, switches=None, growth_only=True):
    """只登录 + 查任务列表 + 解析计划，不执行任何会改变账号状态的动作"""
    w_log('---------- 登录 ----------')
    if not task.mi_login():
        w_log('登录失败。可依次尝试：')
        w_log('  1) 重跑一次，短信验证码可能只是这一次生效')
        w_log('  2) python run_local.py --cookie  直接复用浏览器里的社区 Cookie')
        return 1

    task.login_app()

    w_log('---------- 查询任务列表 ----------')
    tasks = task.check_daily_tasks()
    if tasks is None:
        w_log('任务列表获取失败，无法解析任务；正式运行时会 fail-closed 跳过全部动作')
        return 1

    # 解析成执行计划：哪条能自动做、走哪个动作、哪条只能人工
    plan, skipped = analyze_tasks(tasks, switches or {}, growth_only=growth_only)
    print_task_plan(plan, skipped)

    w_log('---------- 当前成长值 ----------')
    task.get_point()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='MIUI Task 本机试运行（凭证仅存内存，不落盘）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例：\n'
               '  python run_local.py --tasks            只拉任务列表并解析哪些能给成长值（只读）\n'
               '  python run_local.py                    任务驱动执行，签到默认开，只做能给成长值的任务\n'
               '  python run_local.py --all-tasks        不成长值过滤，列出全部任务\n'
               '  python run_local.py --no-check-in      关闭签到，只做浏览类任务\n'
               '  python run_local.py --cookie --tasks   用浏览器 Cookie 登录并解析任务\n'
               '  python run_local.py --legacy           回到旧的固定序列\n')
    parser.add_argument('--net', action='store_true', help='不输账号密码，仅检查网络与接口可达性')
    parser.add_argument('--check', action='store_true', help='仅登录并查询任务列表，不执行任务')
    parser.add_argument('--tasks', action='store_true',
                        help='拉取任务列表并解析「怎么完成」，只读，不发任何动作请求（同 --check）')
    parser.add_argument('--legacy', action='store_true',
                        help='回到旧的固定序列执行（不参考任务列表，会发无效请求）')
    parser.add_argument('--all-tasks', action='store_true',
                        help='不做成长值过滤，把任务列表里所有任务都列出来（默认只处理给成长值的）')
    parser.add_argument('--fast', action='store_true', help='压缩随机间隔到 1~2 秒，仅用于调试')
    parser.add_argument('--check-in', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--no-check-in', action='store_true',
                        help='关闭成长值签到（默认是开启的）')
    parser.add_argument('--carrot-pull', action='store_true',
                        help='启用社区拔萝卜（发金币不发成长值，成长值模式下会被忽略；仅 --legacy 生效）')
    parser.add_argument('--specialpage', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--ua', default='', help='自定义 User-Agent，默认用内置值')
    parser.add_argument('--cookie', action='store_true',
                        help='用已登录的社区 Cookie 登录（跳过账号密码与短信验证）')
    parser.add_argument('--login', action='store_true', help='忽略本地 Cookie 缓存，强制账号密码登录')
    parser.add_argument('--auto', action='store_true',
                        help='无人值守模式：只用本地缓存 Cookie，全程不提示输入（供计划任务调用）')
    parser.add_argument('--no-cache', action='store_true', help='不读写本地 Cookie 缓存 .miui_cookie')
    parser.add_argument('--show-cookie', action='store_true',
                        help='打印缓存的 Cookie，便于填到 Actions 的 MIUI_COOKIE')
    parser.add_argument('--notify-test', action='store_true',
                        help='发一条钉钉测试消息，验证 DINGTALK_TOKEN / DINGTALK_SECRET 是否正确')
    args = parser.parse_args()

    if args.net:
        return net_check()

    if args.notify_test:
        token, secret = notifier.dingtalk_credentials()
        print('钉钉 token：' + ('已配置' if token else '未配置（DINGTALK_TOKEN）'))
        print('加签密钥：' + ('已配置' if secret else '未配置（安全设置用关键词/白名单时可留空）'))
        sample = [{
            'uid': '测试账号', 'ok': True, 'error': '',
            'base': 37, 'final': 40,
            'executed': ['微信小程序签到（测试消息）'],
            'manual': ['每日签到（测试消息）'],
            'failed': [], 'skipped': 3,
            'note': '收到这条消息说明钉钉通知配置正确',
        }]
        ok = notifier.notify_run_result(sample, time.time())
        print('[结果] ' + ('发送成功，去钉钉群看看' if ok else '发送失败，检查上面的错误码'))
        return 0 if ok else 1

    if args.show_cookie:
        value = load_cached_cookie()
        if not value:
            print('[提示] 还没有缓存 Cookie，先在终端成功登录一次（会生成 .miui_cookie）')
            return 1
        print(value)
        return 0

    print('=' * 56)
    print(' MIUI Task 本机试运行')
    print(' 提示：密码输入时不回显；账号要求短信验证时会提示输入验证码')
    print('=' * 56)

    # Cookie 来源优先级：--cookie 粘贴 > 环境变量 > 本地缓存
    cookie = ''
    if args.auto:
        # 无人值守：只认缓存/环境变量，绝不 prompts；没有就直接失败，
        # 让计划任务的退出码与钉钉消息都明确指向「该重新登录了」
        cookie = os.environ.get('MIUI_COOKIE', '').strip() or load_cached_cookie()
        if not cookie:
            print('[中止] --auto 需要可用的 Cookie，但本地缓存为空。')
            print('       请先在本机运行一次：python run_local.py --login')
            return 3
        w_log('无人值守模式：使用本地缓存 Cookie')
    elif args.cookie:
        cookie = read_credential(
            '粘贴社区 Cookie（浏览器 F12 > Network > 任意 api.vip.miui.com 请求 > Cookie）: ',
            'MIUI_COOKIE')
        if not cookie:
            print('[中止] Cookie 不能为空')
            return 2
    elif not args.login and not args.no_cache:
        cookie = os.environ.get('MIUI_COOKIE', '').strip() or load_cached_cookie()
        if cookie:
            w_log('检测到本地缓存 Cookie，本次直接复用（--login 可强制重新登录）')

    cookie_mode = bool(cookie)

    if cookie_mode:
        # 无人值守时 uid 交给 Cookie 里的 userId，不弹输入框
        uid = '' if args.auto else read_credential(
            '小米账户 ID（直接回车则从 Cookie 读取）: ', 'MIUI_UID')
        raw_password = ''
        password = ''
    else:
        uid = read_credential('小米账户 ID（纯数字）: ', 'MIUI_UID')
        if not uid:
            print('[中止] 账户 ID 不能为空')
            return 2
        if not uid.isdigit():
            print('[中止] 账户 ID 必须是纯数字，当前输入不是')
            return 2

        raw_password = read_credential('小米账户密码: ', 'MIUI_PASSWORD', secret=True)
        if not raw_password:
            print('[中止] 密码不能为空')
            return 2
        # 与 format_config 保持一致：32 位视为已加密，否则转大写 MD5
        password = raw_password if len(raw_password) == 32 else md5_crypto(raw_password).upper()

    if args.auto:
        ua = os.environ.get('MIUI_USER_AGENT', '').strip() or args.ua or DEFAULT_UA
    else:
        ua = read_credential('User-Agent（直接回车用默认值）: ', 'MIUI_USER_AGENT') or args.ua or DEFAULT_UA

    w_log('---------- 本次运行配置 ----------')
    w_log('登录方式：' + ('Cookie（跳过密码与短信验证）' if cookie_mode else '账号密码（需要时会走短信验证）'))
    w_log('账户 ID：' + (uid or '（从 Cookie 读取）'))
    w_log('User-Agent：' + ua[:60])
    if args.check or args.tasks:
        mode_text = 'tasks（只查不执行）'
    elif args.legacy:
        mode_text = 'legacy（固定序列，会发无效请求）'
    elif args.fast:
        mode_text = 'fast（调试）'
    else:
        mode_text = '常规（任务驱动）'
    w_log('模式：' + mode_text)
    w_log('任务范围：' + ('全部任务（--all-tasks）' if args.all_tasks else '只做能给社区成长值的任务'))
    # 签到默认开启：签到前会先查状态，已签或状态不明都不发请求，开着不会造成重复签到
    check_in = not args.no_check_in
    w_log('开关：签到=%s 拔萝卜=%s 专题页=自动（任务列表里有才做）'
          % (check_in, args.carrot_pull))

    if args.fast:
        enable_fast_mode()

    # device-id 与 format_config 保持一致：按账号派生稳定值，
    # 否则登录请求里会漏出 deviceId=None（真实设备这里是一个 16 位十六进制串）
    device_id = md5_crypto('miui-task-device-' + str(uid or 'cookie'))[:16]
    w_log('设备 ID：' + device_id)

    task = MIUITask(uid, password, ua, device_id, cookie=cookie)
    switches = _task_switches(check_in, args.carrot_pull, args.specialpage)
    growth_only = not args.all_tasks

    # 只读模式（--check / --tasks）不发通知，免得调试时刷屏
    started_at = time.time()
    code = 0
    try:
        if args.check or args.tasks:
            code = check_only(task, switches, growth_only=growth_only)
        else:
            start(task,
                  check_in=check_in,
                  carrot_pull=args.carrot_pull,
                  browse_specialpage=args.specialpage,
                  legacy=args.legacy,
                  growth_only=growth_only)
            # 与 miuitask.main 保持一致：只要有一个账号没成功就返回非零，
            # 计划任务靠这个退出码判断该不该弹提示
            code = 1 if [e for e in miuitask.RUN_REPORT if not e.get('ok')] else 0
    finally:
        # 登录成功后缓存 Cookie，下次免登录、少一次短信验证
        if task.cookie and not args.no_cache:
            save_cached_cookie(task.cookie)
        if not (args.check or args.tasks):
            sent = False
            try:
                sent = notifier.notify_run_result(miuitask.RUN_REPORT, started_at)
            except Exception as e:
                w_log('发送钉钉通知出错：%s: %s' % (type(e).__name__, e))
            # 无人值守时没人盯着终端，失败必须能在桌面上看见
            if args.auto and code != 0 and not sent:
                failed = [e for e in miuitask.RUN_REPORT if not e.get('ok')]
                reason = failed[0].get('error') if failed else '运行未成功，详见 logs 目录'
                notifier.popup('小米社区任务失败', str(reason)[:120])
        # 归档日志便于回看；logs/ 已在 .gitignore 中
        utils.s_log(True)

    return code


if __name__ == '__main__':
    sys.exit(main())
