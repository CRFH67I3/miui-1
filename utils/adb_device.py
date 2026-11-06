"""ADB 设备发现：**永远先找真机**，确认没有真机之后才允许启用模拟器。

这条规则是硬约束，不是建议：模拟器里的 App 行为、风控判定、WebView 环境
都和真机不同，能拿到真机就绝不用模拟器。
"""

import os
import re
import subprocess

SDK_CANDIDATES = [
    os.path.expandvars(r'%LOCALAPPDATA%\Android\Sdk'),
    os.path.expandvars(r'%ANDROID_HOME%'),
    os.path.expandvars(r'%ANDROID_SDK_ROOT%'),
    r'/usr/local/share/android-sdk',
    os.path.expanduser('~/Library/Android/sdk'),
]


class DeviceError(RuntimeError):
    pass


def _adb_path():
    override = os.environ.get('ADB')
    if override and os.path.exists(override):
        return override
    for root in SDK_CANDIDATES:
        if not root or not os.path.isdir(root):
            continue
        exe = os.path.join(root, 'platform-tools',
                           'adb.exe' if os.name == 'nt' else 'adb')
        if os.path.exists(exe):
            return exe
    return 'adb'  # 交给 PATH


ADB = _adb_path()


def _run(args, timeout=20):
    try:
        out = subprocess.run([ADB] + args, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise DeviceError('找不到 adb，请确认 Android platform-tools 已安装，'
                          '或用环境变量 ADB 指定路径')
    except subprocess.TimeoutExpired:
        raise DeviceError('adb 命令超时：%s' % ' '.join(args))
    return out.stdout.decode('utf-8', 'replace'), out.stderr.decode('utf-8', 'replace')


def _prop(serial, key):
    out, _ = _run(['-s', serial, 'shell', 'getprop', key])
    return out.strip()


def list_devices():
    """返回 [{serial, kind, model, android, online}]，kind 为 physical / emulator。"""
    out, _ = _run(['devices'])
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('List of devices'):
            continue
        parts = re.split(r'\s+', line)
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        online = state == 'device'
        is_emu = serial.startswith('emulator-')
        item = {
            'serial': serial,
            'kind': 'emulator' if is_emu else 'physical',
            'online': online,
            'model': '',
            'android': '',
        }
        if online:
            # ro.kernel.qemu=1 也是模拟器的铁证，防止改过 serial 的模拟器混进来
            qemu = _prop(serial, 'ro.kernel.qemu')
            if qemu == '1':
                item['kind'] = 'emulator'
            item['model'] = _prop(serial, 'ro.product.model')
            item['android'] = _prop(serial, 'ro.build.version.release')
        devices.append(item)
    return devices


def pick(prefer_physical=True):
    """挑一台设备。默认真机优先；没有真机就报错，不静默退回模拟器。"""
    devices = list_devices()
    online = [d for d in devices if d['online']]
    if not online:
        raise DeviceError('没有可用的 ADB 设备。请先用 USB 连接手机并打开「USB 调试」。')
    physical = [d for d in online if d['kind'] == 'physical']
    if physical:
        return physical[0]
    if prefer_physical:
        raise DeviceError(
            '只找到模拟器（%s），没有连接真机。'
            '按项目约定，模拟器需先经人工确认才能用。'
            % ', '.join(d['serial'] for d in online))
    return online[0]


def require_physical():
    """给「必须真机」的场景用：拿不到真机直接抛错。"""
    return pick(prefer_physical=True)


def main():
    devices = list_devices()
    if not devices:
        print('没有检测到任何 ADB 设备')
        return 1
    print('检测到 %d 台设备：' % len(devices))
    for d in devices:
        flag = '真机' if d['kind'] == 'physical' else '模拟器'
        state = '在线' if d['online'] else '离线'
        print('  [%s] %s  %s  Android %s  (%s)'
              % (flag, d['serial'], d['model'] or '-', d['android'] or '-', state))
    try:
        best = pick()
    except DeviceError as e:
        print('\n不可用：%s' % e)
        return 1
    print('\n将使用：%s（%s）' % (best['serial'], best['model'] or best['kind']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
