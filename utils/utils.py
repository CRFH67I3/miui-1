import os
import random
import re
import time
import platform
import dotenv, yaml

from hashlib import md5
from urllib.request import getproxies

logs = ''
CONFIG_VERSION_REQUIRE: str = 'v1.5.2'


def md5_crypto(passwd: str) -> str:
    return md5(passwd.encode('utf8')).hexdigest()


def show_info(tip, info):
    return "{}: {}".format(tip, info)


def system_info():
    w_log(show_info('操作系统平台', platform.platform()))
    w_log(show_info('操作系统版本', platform.version()))
    w_log(show_info('操作系统名称', platform.system()))
    w_log(show_info('操作系统位元', platform.architecture()))
    w_log(show_info('操作系统类型', platform.machine()))
    w_log(show_info('处理器信息', platform.processor()))
    w_log(show_info('Python 版本', str(platform.python_version()) + ' ' + str(platform.python_build())))
    if getproxies():
        w_log(show_info('系统代理', getproxies()))


def get_config() -> dict:
    config = {'account': []}
    config_path_legacy = dotenv.find_dotenv(filename='config.env')
    config_path_yaml = dotenv.find_dotenv(filename='config.yaml')

    # the old legacy config
    if config_path_legacy:
        w_log('正在使用 ' + config_path_legacy + ' 作为配置文件')
        legacy_config = dotenv.dotenv_values(config_path_legacy)
        config['account'].append({'uid': legacy_config.get('MI_ID')})
        config['account'][0]['password'] = legacy_config.get('MI_PASSWORD')
        config['account'][0]['user-agent'] = legacy_config.get('USER_AGENT')
        if legacy_config.get('SIGN_IN') and legacy_config.get('SIGN_IN').upper() in ('Y', 'YES'):
            config['account'][0]['check-in'] = True
        else:
            config['account'][0]['check-in'] = False
        if legacy_config.get('CARROT_PULL') and legacy_config.get('CARROT_PULL').upper() in ('Y', 'YES'):
            config['account'][0]['carrot-pull'] = True
        else:
            config['account'][0]['carrot-pull'] = False
        if legacy_config.get('BROWSE_SPECIALPAGE') and legacy_config.get('BROWSE_SPECIALPAGE').upper() in ('Y', 'YES'):
            config['account'][0]['browse-specialpage'] = True
        else:
            config['account'][0]['browse-specialpage'] = False
        if legacy_config.get('LOG_SAVE') and legacy_config.get('LOG_SAVE').upper() in ('Y', 'YES'):
            config['logging'] = True
        else:
            config['logging'] = False
        return config

    # the new version yaml config
    elif config_path_yaml:
        w_log('正在加载 ' + config_path_yaml + ' 配置文件')
        with open(config_path_yaml, "rb") as stream:
            try:
                config = yaml.safe_load(stream)
                config_version: str = config.get('version')

                # check config file version
                # if config version not meet the requirement
                if CONFIG_VERSION_REQUIRE != config_version:
                    w_log('配置文件版本和程序运行要求版本不匹配，请检查配置文件')
                    w_log('配置文件版本: ' + config_version)
                    w_log('运行程序配置版本要求: ' + CONFIG_VERSION_REQUIRE)
                    exit(1)  # exit the program
                w_log('配置文件已成功加载，文件版本 ' + config_version)

            except yaml.YAMLError as e:
                w_log('配置文件载入错误')
                w_log(e)
            return config
    else:
        w_log('配置文件不存在')
        exit(1)


def w_log(text):
    global logs
    now_localtime = time.strftime("%H:%M:%S", time.localtime())
    logs += now_localtime + ' | ' + str(text) + '\n'
    print(now_localtime + ' | ' + str(text))


def s_log(flag):
    if flag:
        global logs
        folder = os.path.exists('./logs')
        if not folder:
            os.makedirs('./logs')
        now_localtime = time.strftime("%Y-%m-%d", time.localtime())
        fname = now_localtime + '.log'
        with open('./logs/' + fname, 'a+', encoding='utf-8') as f:
            f.write(logs)


def check_config(config: dict) -> bool:
    if config.get('accounts'):
        for i in config.get('accounts'):
            if not i.get('user-agent'):
                return False
            # 两种登录方式二选一：Cookie 登录不需要账号密码
            if not i.get('cookie') and (not i.get('uid') or not i.get('password')):
                return False
            if not isinstance(i.get('check-in'), bool):
                return False
            if not isinstance(i.get('carrot-pull'), bool):
                return False
            if not isinstance(i.get('browse-specialpage'), bool):
                return False
    else:
        return False
    if not isinstance(config.get('logging'), bool):
        return False
    return True


def format_config(config: dict) -> dict:
    for i in config.get('accounts'):
        i['uid'] = str(i.get('uid') or '')
        i['user-agent'] = str(i.get('user-agent'))
        if i.get('cookie'):
            # Cookie 登录：不需要密码，uid 缺失时由 Cookie 里的 userId 兜底
            i['cookie'] = str(i.get('cookie')).strip()
            if i.get('password'):
                i['password'] = str(i.get('password'))
            else:
                i['password'] = ''
        else:
            if len(i.get('password')) != 32:
                i['password'] = md5_crypto(i.get('password')).upper()
            else:
                i['password'] = str(i.get('password')).upper()
        if i.get('device-id'):
            i['device-id'] = str(i.get('device-id'))
        else:
            # 未配置时按账号派生一个稳定的 16 位十六进制设备 ID：
            # 既不会每次运行都变（频繁变化反而更可疑），也避免把 deviceId=None 发给登录接口
            i['device-id'] = md5_crypto('miui-task-device-' + str(i.get('uid') or 'cookie'))[:16]
    return config


# 任务之间的随机间隔，单位秒。想调节奏只改这两个值即可
SLEEP_MIN: int = 20
SLEEP_MAX: int = 40


def random_sleep():
    # 通用动作间隔：点赞、取消点赞、加圈退圈、浏览主页、取成长值
    time.sleep(random.randint(SLEEP_MIN, SLEEP_MAX))


def sleep_ten_sec_more():
    # 浏览类任务前置延迟：需大于等于 10 秒，与 SLEEP_MIN 取大值兜底
    time.sleep(random.randint(max(SLEEP_MIN, 10), max(SLEEP_MAX, 10)))


# ==================== 设备画像：红米 K70E（2311DRK48C / duchamp）====================
# Chrome 113 起 UA 缩减已把安卓 UA 里的系统版本冻结为 10、机型替换为字母 K，
# 所以「红米 K70E / 澎湃OS 3 / Android 16」写不进 UA（硬写就是伪造，且与版本号矛盾）。
# 真实设备靠 Client Hints 声明身份，其中机型、系统版本属于高熵项，
# 只有服务端通过 Accept-CH 主动索要才会发送 —— 正常请求不带，这里保持一致。
DEVICE_PROFILE = {
    'model': '2311DRK48C',   # 红米 K70E 国行型号
    'codename': 'duchamp',   # 小米内部代号，与 POCO X6 Pro 同款硬件
    'os': 'HyperOS 3',
    'android': '16',
}


def _chrome_major(user_agent: str) -> str:
    """从 UA 解析 Chromium 主版本，保证 sec-ch-ua 与 UA 版本不会对不上"""
    m = re.search(r'(?:Chrome|CriOS|EdgA|OPR)/(\d+)', user_agent or '')
    return m.group(1) if m else ''


def _supported_encodings() -> str:
    """按实际解压能力声明 Accept-Encoding，避免服务端返回 br/zstd 时解不开"""
    encodings = ['gzip', 'deflate']
    for module, token in (('brotli', 'br'), ('zstandard', 'zstd')):
        try:
            __import__(module)
            encodings.append(token)
        except ImportError:
            pass
    return ', '.join(encodings)


def build_device_headers(user_agent: str, mode: str = 'xhr', referer: str = None) -> dict:
    """
    构造与 UA 自洽的完整浏览器请求头。

    mode='xhr'：接口请求（fetch / XMLHttpRequest）
    mode='nav'：页面导航请求
    """
    ua = str(user_agent or '')
    headers = {'User-Agent': ua}
    is_mobile = ('Mobile' in ua) or ('Android' in ua) or ('iPhone' in ua)

    if mode == 'nav':
        headers['Accept'] = ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                             'image/avif,image/webp,image/apng,*/*;q=0.8,'
                             'application/signed-exchange;v=b3;q=0.7')
        headers['Upgrade-Insecure-Requests'] = '1'
        headers['Sec-Fetch-Site'] = 'same-origin'
        headers['Sec-Fetch-Mode'] = 'navigate'
        headers['Sec-Fetch-User'] = '?1'
        headers['Sec-Fetch-Dest'] = 'document'
        headers['Priority'] = 'u=0, i'
    else:
        headers['Accept'] = 'application/json, text/plain, */*'
        headers['Sec-Fetch-Site'] = 'same-site'
        headers['Sec-Fetch-Mode'] = 'cors'
        headers['Sec-Fetch-Dest'] = 'empty'
        headers['Priority'] = 'u=1, i'

    headers['Accept-Language'] = 'zh-CN,zh;q=0.9,en;q=0.8'
    headers['Accept-Encoding'] = _supported_encodings()

    # Client Hints 只有 Chromium 系会发；Safari / Firefox 带上反而是破绽
    major = _chrome_major(ua)
    if major:
        # GREASE 品牌由 Chromium 按种子随机，此值取自 Chrome 150 实测请求
        headers['sec-ch-ua'] = (f'"Not;A=Brand";v="8", "Chromium";v="{major}", '
                                f'"Google Chrome";v="{major}"')
        headers['sec-ch-ua-mobile'] = '?1' if is_mobile else '?0'
        if 'Android' in ua:
            headers['sec-ch-ua-platform'] = '"Android"'
        elif 'iPhone' in ua or 'iPad' in ua:
            headers['sec-ch-ua-platform'] = '"iOS"'
        elif 'Windows' in ua:
            headers['sec-ch-ua-platform'] = '"Windows"'
        else:
            headers['sec-ch-ua-platform'] = '"Linux"'

    if referer:
        headers['Referer'] = referer
    return headers
