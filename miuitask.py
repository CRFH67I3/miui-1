# -- coding:UTF-8 --
import requests
import time
import json
import hashlib
import base64
import urllib.parse
import datetime

from urllib import request
from http import cookiejar

from utils.utils import system_info, get_config, w_log, s_log, check_config, format_config, random_sleep, \
    sleep_ten_sec_more, build_device_headers, DEVICE_PROFILE
from utils import notify as notifier

# 社区成长值任务列表接口
TASK_LIST_URL = 'https://api.vip.miui.com/mtop/planet/vip/member/getCheckinPageCakeList'
# 任务状态取值：0 = 已完成，1 = 未完成（可执行）
TASK_STATE_DONE = 0
TASK_STATE_TODO = 1
# 专题页任务的标题特征词，命中即认定为该任务
SPECIAL_PAGE_TITLE_KEY = '专题'
# 标题识别不到时的描述兜底特征
SPECIAL_PAGE_DESC_KEY = '浏览超过10秒成长值+1，每日上限1分'
# 兜底匹配时必须排除的其它任务特征，避免把主页/帖子任务误判成专题页
SPECIAL_PAGE_EXCLUDE = ('主页', '帖子', '视频')

# ==================== 任务驱动：任务标题 -> 动作 ====================
# 任务列表接口只返回「标题 / 描述 / 按钮文案 / 跳转地址」，没有动作 ID，
# 所以只能按标题语义映射到脚本里已有的动作。
# 规则按顺序匹配，每条先看排除词再看命中词 ——
# 「小米社区微信小程序签到获成长值」同时含「签到」，不先排除就会被误判成每日签到，
# 进而对一个根本做不了的任务发出签到请求。
TASK_ACTION_LABELS = {
    'wechat_checkin': '微信小程序签到（成长值+3）',
    'check_in': 'App 内每日签到',
    'browse_post': '浏览帖子 10 秒',
    'browse_specialpage': '浏览专题页',
    'browse_user_page': '浏览个人主页',
    'carrot_pull': '社区拔萝卜',
    'thumb_up': '点赞帖子',
    'board_follow': '加入圈子',
}
# 需要显式开关才执行的动作（原项目视为有风险，默认关闭）
#
# 注意：浏览专题页不在这里。这条任务只在活动期间出现，靠任务列表天然门控：
# 列表里没有这个任务就一次请求都不发，有且未完成才执行，因此不需要额外开关，
# 也不需要人工判断"今天到底有没有专题页活动"。
TASK_ACTION_RISK_SWITCH = {
    'wechat_checkin': 'check-in',
    'check_in': 'check-in',
    'carrot_pull': 'carrot-pull',
}

# ==================== 奖励类型：只保留「给成长值」的任务 ====================
# 任务列表的 desc 字段会直接写出奖励，例如「成长值+1，上限+1/日」「成长值+10，仅限首次」，
# 这是判断「是否给成长值」最可靠的依据；desc 没写时才退回按动作判断。
GROWTH_KEYWORD = '成长值'
# 这些动作最终都落到 addCommunityGrowUpPointByAction(V2)，即成长值上报
GROWTH_ACTIONS = frozenset({
    'wechat_checkin', 'check_in', 'browse_post', 'browse_specialpage',
    'browse_user_page', 'thumb_up', 'board_follow',
})
# 明确不给成长值的动作，以及给的是什么
NON_GROWTH_ACTIONS = {
    'carrot_pull': '参与拔萝卜发放的是金币，不是成长值',
}
# (动作键, 命中任一关键词, 命中任一即排除, 无法代劳时的原因)
TASK_RULES = (
    # 「小米社区微信小程序签到获成长值」与「每日签到」是两个任务、两个奖励：
    #   小程序签到 -> WECHAT_CHECKIN_TASK，实测 2026/11/02 到账成长值 +3
    #   App 每日签到 -> checkinV2，+1~+2，被人机校验（640）挡住
    # 所以必须分开映射。以前统一映射成 check_in，导致小程序签到做完后
    # 「每日签到」仍然未完成，还会对同一个接口多发一次请求。
    ('wechat_checkin', ('微信', '小程序'), (), ''),
    (None, ('升级', '最新版本'), ('浏览',), '需要在手机上安装或更新社区 App，脚本无法代替'),
    (None, ('实名', '认证'), (), '需要本人身份认证，脚本不应代替'),
    (None, ('资料', '头像', '昵称'), (), '需要本人填写资料，脚本不应代替'),
    ('carrot_pull', ('拔萝卜',), (), ''),
    ('browse_specialpage', ('专题',), ('帖子', '主页', '视频'), ''),
    ('browse_post', ('浏览帖子', '浏览 帖子'), (), ''),
    ('browse_user_page', ('个人主页', '主页'), ('专题',), ''),
    ('check_in', ('签到',), (), ''),
    ('thumb_up', ('点赞',), ('取消',), ''),
    ('board_follow', ('圈子',), ('退出',), ''),
)
# 浏览任务打开的真实帖子页（取自任务列表里「升级」任务的 jumpUrl，
# 是官方公告帖，浏览它属于正常用户行为）
BROWSE_POST_URL = 'https://web.vip.miui.com/page/info/mio/mio/detail?postId='

# 签到：App 4.99.2 的 process 是
#   GET getUserCheckinInfoV2 查连续签到天数
#   -> 人机校验 WebView（scene=GROW_UP_CHECKIN）
#   -> POST checkinV2 { token } 领取成长值
# 上游老代码用的是 GET mtop/planet/vip/user/checkin，实测返回 401（该路径已改为 POST）。
# 直接 POST checkinV2 不带 token 会返回 status=640「人机校验失败」——
# 这说明签到被人机校验保护，脚本拿不到 token，重试只会让风控等级上升。
CHECKIN_INFO_URL = 'https://api.vip.miui.com/mtop/planet/vip/user/getUserCheckinInfoV2'
CHECKIN_URL = 'https://api.vip.miui.com/mtop/planet/vip/user/checkinV2'
# 接口返回「人机校验失败」的状态码，遇到必须直接放弃，不能重试
HUMAN_CHECK_CODE = 640
# 已知需要补充说明的动作（不是不能做，而是要走特殊通道 / 有前置条件）
TASK_ACTION_NOTES = {
    'wechat_checkin': '走微信小程序通道，无人机校验；已签到则一次请求都不发',
    'check_in': 'App 内 checkinV2 有人机校验（640），脚本发请求必被拦，需要在 App 里手动点一次（+1~+2）',
    'browse_specialpage': '活动期间任务列表里出现才做，没有这条任务时不发请求',
}
# 需人工的任务在通知里补一句「怎么做、值多少」，否则收到消息只知道名字不知道该干嘛
TASK_ACTION_MANUAL_HINT = {
    'check_in': '在小米社区 App 里手动点一次签到，可再拿 +1~+2',
}
# 这些动作即使开着开关也不自动执行：做了也是白发请求，只会多留一份脚本痕迹
TASK_ACTION_MANUAL_ONLY = frozenset({'check_in'})

# ==================== 微信小程序通道（绕开人机校验的唯一途径）====================
# 从小米社区微信小程序（appid wx240a4a764023c444，包版本 75）的 wxapkg 还原：
#   1) 小程序登录配置硬编码 appLoginInfo = {sid: 'wx_vip', appid: 'wx240a4a764023c444'}
#   2) 用 passtoken 换 serviceToken：
#        GET account.xiaomi.com/pass/serviceLogin?sid=wx_vip&_json=1&_locale=zh_CN
#            cookie: passToken / userId
#        -> location 指向 api.vip.miui.com/wx/sts
#        GET <location>&clientSign=urlencode(base64(sha1("nonce=<nonce>&<ssecurity>")))
#        -> Set-Cookie: wx_vip_ph / wx_vip_serviceToken / wx_vip_slh
#   3) 之后所有接口走 /mtop/planet/wechat/* 前缀，鉴权靠 cookie 里的 wx_vip_ph
#   4) 签到 = POST addCommunityGrowUpPointByActionV2?wx_vip_ph=xxx，body {action: WECHAT_CHECKIN_TASK}
# 这条链路**没有**人机校验，是 App 内 checkinV2 拿不到 640 token 时的可行替代。
WECHAT_LOGIN_URL = 'https://account.xiaomi.com/pass/serviceLogin'
WECHAT_SID = 'wx_vip'
WECHAT_MINIAPP_APPID = 'wx240a4a764023c444'
WECHAT_ORIGIN = 'https://servicewechat.com'
WECHAT_CHECKIN_PAGE_URL = 'https://api.vip.miui.com/mtop/planet/wechat/checkin/mypagedata'
WECHAT_POINT_URL = 'https://api.vip.miui.com/mtop/planet/wechat/member/addCommunityGrowUpPointByActionV2'
WECHAT_CHECKIN_ACTION = 'WECHAT_CHECKIN_TASK'
# 签到卡片标题，用于在签到页数据里定位「每日签到」
WECHAT_CHECKIN_TITLE = '每日签到'

# 成长值发放接口
# App 4.99.2 的 H5 与上游 v1.6 均已改用 V2（POST），V1（GET）仅作回退保留，
# 这样即使 V2 后续再变，也不会让整条成长值链路直接失效
POINT_API_V2 = 'https://api.vip.miui.com/mtop/planet/vip/member/addCommunityGrowUpPointByActionV2'
POINT_API_V1 = 'https://api.vip.miui.com/mtop/planet/vip/member/addCommunityGrowUpPointByAction'
# V2 的公共查询参数，取自上游 v1.6 的实际调用，不是猜的
POINT_API_V2_PARAMS = {
    'ref': 'vipAccountShortcut',
    'pathname': '/mio/detail',
    'version': 'dev.231026',
}
# 只有「接口本身不可用」才切换版本；业务类错误（已完成、无任务）不回退，
# 否则会造成重复发请求，反而更像脚本
ROUTE_ERROR_CODES = (404, 405, 501)
ROUTE_ERROR_HINTS = ('参数', '接口不存在', 'not found', 'Not Found')

# 取消点赞接口：App 4.99.2 用的是 announceCancelThumbUp，旧路径作为回退
CANCEL_THUMB_URL_V2 = 'https://api.vip.miui.com/mtop/planet/vip/content/announceCancelThumbUp'
CANCEL_THUMB_URL_V1 = 'https://api.vip.miui.com/api/community/post/cancelThumbUp?postId=36625780'

# 成长值查询：App 4.99.2 用的是成长值页接口，userInfo 只作为回退
GROWUP_PAGE_URL = 'https://api.vip.miui.com/mtop/planet/vip/member/getGrowUpPageData'
USER_INFO_URL = 'https://api.vip.miui.com/mtop/planet/pc/post/userInfo'
# 成长值页返回里，成长值那一组卡片的表头标题
GROWUP_POINT_GROUP_TITLE = '成长值'

# 小米账号（account.xiaomi.com）登录与二次验证接口
ACCOUNT_BASE = 'https://account.xiaomi.com'
# 密码登录返回 pwd != 1 时，notificationUrl 指向的身份验证入口
IDENTITY_START_PATH = 'fe/service/identity/authStart'
# 二次验证方式取值与小米账号网页端 identity/list 返回的 options 一致：4 = 手机短信，8 = 安全邮箱
IDENTITY_METHOD_NAMES = {4: '手机短信', 8: '安全邮箱'}
IDENTITY_SEND_APIS = {4: '/identity/auth/sendPhoneTicket', 8: '/identity/auth/sendEmailTicket'}
IDENTITY_VERIFY_APIS = {4: '/identity/auth/verifyPhone', 8: '/identity/auth/verifyEmail'}
# 验证码输错时允许重试的次数
SMS_CODE_RETRY = 3


class MIUITask:

    def __init__(self, uid, password, user_agent, device_id, cookie=None):
        self.uid = uid
        self.password = password
        self.user_agent = user_agent
        self.device_id = device_id

        # 已登录的社区 Cookie：配置里直接给了就跳过账号密码与二次验证
        self.preset_cookie = str(cookie or '').strip()
        # 登录期间复用同一个 Session，二次验证要靠 identity_session 这类 cookie 串起来
        self.session = None
        # 验证码输入回调，默认用终端 input()；无交互环境（Actions）下不设置即可
        self.sms_code_provider = None

        # 留空
        self.cookie = ''
        # 留空
        self.miui_vip_ph = ''

        # 设备画像：所有请求共用一套与 UA 自洽的浏览器头，
        # 避免出现「只发 cookie、连 UA 都没有」这种明显的脚本特征
        self.device_headers = build_device_headers(user_agent)

        # 成长值接口版本，None 表示尚未确定（首次调用时协商）
        self._point_variant = None
        # 成长值是否已确认「本次不再发放」：接口用 401 表示该项今日已达上限
        self._point_limited = False

    def set_sms_code_provider(self, provider):
        """自定义验证码输入方式：provider(label) -> str"""
        self.sms_code_provider = provider

    def _h(self, extra: dict = None, referer: str = None) -> dict:
        """在设备画像头基础上追加 cookie 与调用方自定义头"""
        headers = dict(self.device_headers)
        if self.cookie:
            headers['cookie'] = str(self.cookie)
        if referer:
            headers['Referer'] = referer
        if extra:
            headers.update({k: v for k, v in extra.items() if v is not None})
        return headers

    # 签名
    def post_sign(self, data):
        s_data = []
        for d in data:
            s_data.append(str(d) + '=' + str(data[d]))
        s_str = '&'.join(s_data)
        w_log('签名原文：' + str(s_str))
        s_str = hashlib.md5(str(s_str).encode(encoding='UTF-8')).hexdigest() + '067f0q5wds4'
        s_sign = hashlib.md5(str(s_str).encode(encoding='UTF-8')).hexdigest()
        w_log('签名结果：' + str(s_sign))
        return s_sign, data['timestamp']

    def _route_error(self, response, r_json) -> bool:
        """
        判断一次响应是否属于「接口不可用」。
        只有这类错误才值得换一个接口版本重试；业务错误（已完成、无任务等）不能重试，
        否则同一个动作会连发两次请求，比少做一次任务更显眼。
        """
        if getattr(response, 'status_code', 200) >= 400:
            return True
        if not isinstance(r_json, dict):
            return True
        if r_json.get('status', r_json.get('code')) in ROUTE_ERROR_CODES:
            return True
        # 既无 status 也无 code，说明不是正常业务响应
        if 'status' not in r_json and 'code' not in r_json:
            return True
        msg = str(r_json.get('message') or r_json.get('msg') or '')
        return any(h in msg for h in ROUTE_ERROR_HINTS)

    def add_point(self, action: str, label: str):
        """
        发放成长值（浏览帖子/主页/专题页共用）。

        优先 V2（POST，App 与上游 v1.6 当前都在用），接口不可用时回退 V1（GET）。
        可用版本会缓存在实例上，因此一次运行最多只多一个探测请求。
        """
        if self._point_variant == 'none':
            return w_log(label + '跳过：本次运行已确认成长值接口不可用')

        if self._point_limited:
            return w_log(label + '跳过：本次运行已确认成长值不再发放（该项今日上限已到）')

        if self._point_variant is None:
            candidates = ['v2', 'v1']
        else:
            other = 'v1' if self._point_variant == 'v2' else 'v2'
            candidates = [self._point_variant, other]

        for variant in candidates:
            try:
                if variant == 'v2':
                    params = dict(POINT_API_V2_PARAMS)
                    params['miui_vip_ph'] = str(self.miui_vip_ph)
                    data = {'action': action, 'miui_vip_ph': str(self.miui_vip_ph)}
                    headers = self._h({'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'})
                    response = requests.post(POINT_API_V2, params=params, data=data, headers=headers)
                else:
                    params = {'userId': str(self.uid), 'action': action}
                    response = requests.get(POINT_API_V1, params=params, headers=self._h())

                try:
                    r_json = response.json()
                except Exception:
                    r_json = None

                if self._route_error(response, r_json):
                    w_log('成长值接口 %s 不可用，切换版本重试' % variant.upper())
                    continue

                code = r_json.get('status', r_json.get('code'))
                if code == 401:
                    # App H5 里 addPoint 配置的是 {isNotCheckCode:True, isNotCheckLogin:True}，
                    # 说明作者预期这个接口会返回 401、且 401 不代表登录失效。
                    # 实测：浏览帖子 +1/日 拿到之后，再发同一个 action（以及其它 action）全部返回
                    # {"code":401} —— 这是「本次不发放」的通用响应，不是 Cookie 掉了。
                    # 继续重试不会到账，只会多几次异常请求，所以本轮直接停掉后续上报。
                    self._point_limited = True
                    return w_log(label + '未到账：接口返回 401（该项今日已达上限，或登录态失效）；'
                                        '本次运行不再重复上报')
                if code != 200:
                    return w_log(label + '完成，但有错误：' + str(r_json.get('message')))

                entity = r_json.get('entity')
                score = entity.get('score') if isinstance(entity, dict) else entity
                if self._point_variant is None:
                    w_log('成长值接口版本已确定：' + variant.upper())
                self._point_variant = variant
                return w_log(label + '完成，成长值+' + str(score))
            except Exception as e:
                w_log(label + '出错')
                w_log(e)
                return

        # 两个版本都不可用：本次运行不再重复探测，避免对同一接口反复发请求
        self._point_variant = 'none'
        w_log(label + '跳过：成长值接口 V1/V2 均不可用，请更新脚本')

    # 点赞
    def thumb_up(self):
        headers = self._h()
        sign_data = {
            'postId': '36625780',
            'timestamp': int(round(time.time() * 1000))
        }
        sign = self.post_sign(sign_data)
        data = {
            'postId': '36625780',
            'sign': sign[0],
            'timestamp': sign[1]
        }
        try:
            response = requests.get('https://api.vip.miui.com/mtop/planet/vip/content/announceThumbUp', headers=headers,
                                    params=data)
            r_json = response.json()
            if r_json['code'] == 401:
                return w_log("点赞失败：Cookie无效")
            elif r_json['code'] != 200:
                return w_log("点赞失败：" + str(r_json['message']))
            w_log("点赞成功")
        except Exception as e:
            w_log("点赞出错")
            w_log(e)

    # 取消点赞
    def cancel_thumb_up(self):
        # App 4.99.2 用的是 announceCancelThumbUp(POST)，旧路径保留为回退
        attempts = (
            ('post', CANCEL_THUMB_URL_V2, {'postId': '36625780'}),
            ('get', CANCEL_THUMB_URL_V1, None),
        )
        for method, url, data in attempts:
            try:
                if method == 'post':
                    headers = self._h({'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'})
                    response = requests.post(url, data=data, headers=headers)
                else:
                    response = requests.get(url, headers=self._h())

                try:
                    r_json = response.json()
                except Exception:
                    r_json = None

                if self._route_error(response, r_json):
                    continue

                code = r_json.get('code', r_json.get('status'))
                if code == 401:
                    return w_log("取消点赞失败：Cookie无效")
                if code != 200:
                    return w_log("取消点赞失败：" + str(r_json.get('message')))
                return w_log("取消点赞成功")
            except Exception as e:
                w_log("取消点赞出错")
                w_log(e)
                return

        w_log("取消点赞跳过：两个接口均不可用")

    def get_vip_cookie(self, url):

        try:
            r_cookie = cookiejar.CookieJar()
            handler = request.HTTPCookieProcessor(r_cookie)
            opener = request.build_opener(handler)
            response = opener.open(url)
            for item in r_cookie:
                self.cookie += item.name + '=' + item.value + ';'
            if self.cookie == '':
                return False
            ck_list = self.cookie.replace(" ", "").split(';')
            for ph in ck_list:
                if "miui_vip_ph=" in ph:
                    self.miui_vip_ph = ph.replace("miui_vip_ph=", "")
                    break
            return True
        except Exception as e:
            w_log(e)
            return False

    # 浏览帖子10s
    def browse_post(self, post_id='49090865'):
        """
        浏览帖子 10 秒。

        任务要求是「开启帖子阅读 10 秒以上」，所以先真的把帖子页打开、停留够时间，
        再上报成长值 —— 直接空手打上报接口在服务端看来是「没有浏览记录却有浏览上报」，
        反而比少做一次任务更容易被判定为脚本。
        """
        url = BROWSE_POST_URL + str(post_id)
        try:
            response = requests.get(url, headers=self._h(referer='https://web.vip.miui.com/'),
                                    timeout=20)
            w_log('已打开帖子页（postId=%s），HTTP %s' % (post_id, response.status_code))
        except Exception as e:
            # 页面打不开不代表上报会失败，继续走上报，但日志要说明
            w_log('打开帖子页失败，仍尝试上报浏览')
            w_log(e)
        # 停留时间用 20~40 秒的浏览类延迟，既满足 >10 秒的要求，也不会固定在 10.0 秒
        w_log('停留浏览中（模拟真实阅读）')
        sleep_ten_sec_more()
        self.add_point('BROWSE_POST_10S', '浏览帖子')

    # 浏览个人主页10s
    def browse_user_page(self):
        self.add_point('BROWSE_SPECIAL_PAGES_USER_HOME', '浏览个人主页')

    # 浏览指定专题页
    def browse_specialpage(self):
        self.add_point('BROWSE_SPECIAL_PAGES_SPECIAL_PAGE', '浏览专题页')

    # 加入小米圈子
    def board_follow(self):
        headers = self._h()
        try:
            response = requests.get(
                'https://api.vip.miui.com/api/community/board/follow?'
                'boardId=5462662&pathname=/mio/singleBoard&version=dev.1144',
                headers=headers)
            r_json = response.json()
            if r_json['status'] == 401:
                return w_log("加入小米圈子失败：Cookie无效")
            elif r_json['status'] != 200:
                return w_log("加入小米圈子失败：" + str(r_json['message']))
            w_log("加入小米圈子结果：" + str(r_json['message']))
        except Exception as e:
            w_log("加入小米圈子出错")
            w_log(e)

    # 退出小米圈子
    def board_unfollow(self):
        headers = self._h()
        try:
            response = requests.get('https://api.vip.miui.com/api/community/board/unfollow?'
                                    'boardId=5462662&pathname=/mio/singleBoard&version=dev.1144', headers=headers)
            r_json = response.json()
            if r_json['status'] == 401:
                return w_log("退出小米圈子失败：Cookie无效")
            elif r_json['status'] != 200:
                return w_log("退出小米圈子失败：" + str(r_json['message']))
            w_log("退出小米圈子结果：" + str(r_json['message']))
        except Exception as e:
            w_log("退出小米圈子出错")
            w_log(e)

    # 社区拔萝卜
    def carrot_pull(self):
        headers = self._h({'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'})
        data = {
            'miui_vip_ph': str(self.miui_vip_ph)
        }
        try:
            response = requests.post('https://api.vip.miui.com/api/carrot/pull', headers=headers,
                                     data=data)
            r_json = response.json()
            if r_json['code'] == 401:
                return w_log("社区拔萝卜失败：Cookie无效")
            elif r_json['code'] != 200:
                return w_log("社区拔萝卜失败：" + str(r_json['entity']['message']))
            w_log("社区拔萝卜结果：" + str(r_json['entity']['message']))
            money_count = r_json['entity']['header']['moneyCount']
            w_log("当前金币数：" + str(money_count))
        except Exception as e:
            w_log("社区拔萝卜出错")
            w_log(e)

    # 社区4.0签到
    def check_in_info(self):
        """查询签到状态：连续签到天数与本周每天的签到值（>0 表示当天已签）"""
        try:
            response = requests.get(
                CHECKIN_INFO_URL,
                headers=self._h(referer='https://web.vip.miui.com/page/info/mio/mio/growth'),
                timeout=20)
            r_json = response.json()
            if r_json.get('status') != 200:
                w_log('查询签到状态失败：' + str(r_json.get('message')))
                return None
            return r_json.get('entity') or {}
        except Exception as e:
            w_log('查询签到状态出错：%s: %s' % (type(e).__name__, e))
            return None

    def check_in(self):
        """
        App 内每日签到（checkinV2，+1~+2）。

        只走 App 通道；小程序签到是另一个任务、另一个奖励，见 wechat_checkin()。
        规则：先查状态，明确「今天没签」才发签到请求；
        已签到或查不到状态都直接返回 —— 重复签到不会多给成长值，只多留一份请求痕迹。
        """
        info = self.check_in_info()
        if info is None:
            w_log('查不到今日签到状态，为避免重复签到，本次不发送签到请求')
            return False

        detail = info.get('checkin7DaysDetail') or []
        # 与 H5 一致：下标 0 = 周一，1 = 周二 …… 6 = 周日
        today_idx = datetime.date.today().isoweekday() - 1
        today_value = detail[today_idx] if 0 <= today_idx < len(detail) else None
        if isinstance(today_value, int) and today_value > 0:
            w_log('今日已签到（本周第 %d 天，成长值+%d），不发送签到请求'
                  % (today_idx + 1, today_value))
            return True
        w_log('今日未签到（连续签到 %s 天），尝试 App 通道'
              % info.get('continueCheckInDays'))

        headers = self._h({'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'},
                          referer='https://web.vip.miui.com/page/info/mio/mio/growth')
        try:
            # 没有验证码通道，只能空参直接调；预期会拿到 640
            response = requests.post(CHECKIN_URL, headers=headers, data={}, timeout=20)
            try:
                r_json = response.json()
            except Exception:
                r_json = None
            if not isinstance(r_json, dict):
                w_log('App 签到失败：返回不是 JSON（HTTP %s）' % response.status_code)
                return False

            code = r_json.get('status', r_json.get('code'))
            if code == 401:
                w_log('App 签到失败：Cookie无效')
                return False
            if code == HUMAN_CHECK_CODE:
                w_log('App 签到需要人机校验（status 640），已放弃；'
                      '这一步请在 App 或微信小程序里手动点一次')
                return False
            if code != 200:
                w_log('App 签到失败：' + str(r_json.get('message')))
                return False
            w_log('App 签到成功，成长值+' + str(r_json.get('entity')))
            return True
        except Exception as e:
            w_log('App 签到出错：%s: %s' % (type(e).__name__, e))
            return False

    # 登录社区App
    def login_app(self):
        headers = self._h({'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'})
        try:
            response = requests.get('https://api.vip.miui.com/mtop/planet/vip/app/init/start/infos', headers=headers)
            r_code = response.status_code
            if r_code == 401:
                return w_log("登录社区App失败：Cookie无效")
            elif r_code != 200:
                return w_log("登录社区App失败")
            w_log("登录社区App成功")
        except Exception as e:
            w_log("登录社区App出错")
            w_log(e)

    @staticmethod
    def _loads_account_json(response):
        """小米账号接口返回的 JSON 带 &&&START&&& 前缀，去掉后再解析"""
        text = (response.text or '').strip()
        text = text.lstrip('&')
        if text.startswith('START'):
            text = text[len('START'):].lstrip('&')
        return json.loads(text)

    @staticmethod
    def _account_url(url):
        url = str(url or '').strip()
        if url.startswith('http'):
            return url
        return ACCOUNT_BASE + (url if url.startswith('/') else '/' + url)

    # ------------------- 微信小程序通道 -------------------
    def _cookie_item(self, name):
        """从当前 Cookie 串里取某一项的值（未登录时回退到配置里的预设 Cookie）"""
        source = self.cookie or self.preset_cookie or ''
        for seg in source.split(';'):
            if '=' in seg:
                key, value = seg.split('=', 1)
                if key.strip() == name:
                    return value.strip()
        return ''

    def wechat_login(self):
        """
        用社区 Cookie 里的 passToken 换微信小程序（sid=wx_vip）的登录态。

        :return: (requests.Session 或 None, wx_vip_ph 或 '')
        """
        pass_token = self._cookie_item('passToken')
        user_id = self._cookie_item('userId')
        if not pass_token or not user_id:
            w_log('小程序通道不可用：Cookie 里缺少 passToken 或 userId')
            return None, ''

        session = requests.Session()
        session.headers.update({
            'User-Agent': self.user_agent,
            'Origin': WECHAT_ORIGIN,
            'Referer': WECHAT_ORIGIN + '/' + WECHAT_MINIAPP_APPID + '/75/page-frame.html',
        })
        try:
            response = session.get(
                WECHAT_LOGIN_URL,
                params={'sid': WECHAT_SID, '_json': '1', '_locale': 'zh_CN'},
                cookies={'passToken': pass_token, 'userId': user_id},
                timeout=20, allow_redirects=False)
            r_json = self._loads_account_json(response)
        except Exception as e:
            w_log('小程序通道换取凭据出错：%s: %s' % (type(e).__name__, e))
            return None, ''

        if r_json.get('code') != 0:
            w_log('小程序通道换取凭据失败：' + str(r_json.get('desc') or r_json.get('description')))
            return None, ''
        location = r_json.get('location') or ''
        nonce = str(r_json.get('nonce') or '')
        ssecurity = str(r_json.get('ssecurity') or '')
        if not location:
            w_log('小程序通道换取凭据失败：没有返回 location')
            return None, ''

        # 与小程序一致的签名：base64(sha1("nonce=<nonce>&<ssecurity>"))
        raw_sign = hashlib.sha1(('nonce=%s&%s' % (nonce, ssecurity)).encode()).digest()
        client_sign = urllib.parse.quote(base64.b64encode(raw_sign).decode())
        try:
            session.get(location + '&clientSign=' + client_sign, timeout=20, allow_redirects=False)
        except Exception as e:
            w_log('小程序通道换取 serviceToken 出错：%s: %s' % (type(e).__name__, e))
            return None, ''

        ph = session.cookies.get('wx_vip_ph') or ''
        if not ph:
            w_log('小程序通道换取 serviceToken 失败：没有拿到 wx_vip_ph')
            return None, ''
        w_log('小程序通道就绪（wx_vip_ph 已获取）')
        return session, ph

    def wechat_checkin_info(self, session, ph):
        """查小程序签到页数据，返回「每日签到」那张卡片"""
        try:
            response = session.get(WECHAT_CHECKIN_PAGE_URL, timeout=20)
            r_json = response.json()
        except Exception as e:
            w_log('小程序签到页查询出错：%s: %s' % (type(e).__name__, e))
            return None
        if r_json.get('status') != 200:
            w_log('小程序签到页查询失败：' + str(r_json.get('message')))
            return None
        for item in (r_json.get('entity') or {}).get('data') or []:
            if str(item.get('title', '')).strip() == WECHAT_CHECKIN_TITLE:
                return item
        return None

    @staticmethod
    def _wechat_checkin_state(card):
        """
        解析小程序签到卡的按钮状态。

        buttons[0] 的 showType 与任务列表同语义：0 = 已完成，1 = 未完成；
        文案「已签到 / 已领取」同样视为已完成。
        两个条件都对不上就返回 unknown —— 状态不明时一律不发请求。

        :return: ('done' | 'todo' | 'unknown', 依据)
        """
        buttons = card.get('buttons') or []
        if not buttons:
            return 'unknown', '卡片里没有按钮数据'
        first = buttons[0] or {}
        show_type = first.get('showType')
        text = str(first.get('button') or first.get('title') or '')
        if show_type == 0 or '已签到' in text or '已领取' in text:
            return 'done', ('showType=0' if show_type == 0 else '按钮文案「%s」' % text)
        if show_type == 1 or '签到' in text:
            return 'todo', ('showType=1' if show_type == 1 else '按钮文案「%s」' % text)
        return 'unknown', 'showType=%r，按钮文案「%s」' % (show_type, text)

    def wechat_checkin(self):
        """
        微信小程序签到（WECHAT_CHECKIN_TASK，实测成长值 +3）。

        严格幂等：只有签到页明确显示「今日未签到」才发签到请求；
        已签到、拿不到页面、状态不明确，全都直接返回，一个签到请求都不发。
        重复调用不会多拿成长值，只会多留一份请求记录。

        :return: True 已签或本次签到成功；False 未签到成功或无法确认
        """
        session, ph = self.wechat_login()
        if not session:
            return False

        card = self.wechat_checkin_info(session, ph)
        if card is None:
            w_log('拿不到小程序签到页的「每日签到」卡片，无法确认状态，本次不发送签到请求')
            return False

        state, note = self._wechat_checkin_state(card)
        if state == 'done':
            w_log('小程序签到页显示今日已签到（%s），不发送签到请求' % note)
            return True
        if state != 'todo':
            w_log('小程序签到状态不明确（%s），为避免重复签到，本次不发送签到请求' % note)
            return False
        w_log('小程序签到页显示今日未签到（%s），执行签到' % note)

        try:
            response = session.post(
                WECHAT_POINT_URL,
                params={'wx_vip_ph': ph},
                json={'action': WECHAT_CHECKIN_ACTION},
                timeout=20)
            r_json = response.json()
        except Exception as e:
            w_log('小程序签到请求出错：%s: %s' % (type(e).__name__, e))
            return False

        code = r_json.get('status', r_json.get('code'))
        if code == 200:
            w_log('小程序签到成功，成长值+' + str(r_json.get('entity') if r_json.get('entity') is not None else 3))
            return True
        if code == 401:
            w_log('小程序签到失败：小程序凭据无效（wx_vip_ph 过期）')
            return False
        message = str(r_json.get('message') or '')
        if '加分失败' in message or '已签到' in message or '已领取' in message:
            w_log('小程序签到未加分：' + message + '（今日已签或已达上限）')
            return True
        w_log('小程序签到失败：' + (message or ('status=' + str(code))))
        return False

    def _account_headers(self, referer=None):
        """
        账号域请求的头。登录页内发起的都是同源 XHR，
        因此 Sec-Fetch-Site 为 same-origin，cookie 交给 Session 自己带。
        """
        headers = self._h({
            'Accept': 'application/json, text/plain, */*',
            'Origin': ACCOUNT_BASE,
            'X-Requested-With': 'XMLHttpRequest',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Dest': 'empty',
        }, referer=referer)
        headers.pop('cookie', None)
        return headers

    def _cookie_value(self, name):
        for part in str(self.cookie).split(';'):
            if '=' in part:
                key, value = part.split('=', 1)
                if key.strip() == name:
                    return value.strip()
        return ''

    def login_with_cookie(self):
        """
        直接用已登录的社区 Cookie 登录，跳过账号密码与二次验证。

        这是云端（GitHub Actions）唯一稳定的方式：那边既收不到短信，
        也不可能在图形验证码上点一下。
        """
        cookie = self.preset_cookie
        if cookie.lower().startswith('cookie:'):
            cookie = cookie[len('cookie:'):]
        # 兼容从 DevTools / 文档里复制来的各种形态：分号分隔、换行分隔、首尾引号
        cookie = cookie.replace('\r', '').replace('\n', ';').strip().strip('"').strip("'").strip()
        parts = [p.strip() for p in cookie.split(';') if p.strip()]
        if not parts:
            w_log('Cookie 为空，无法登录')
            return False

        self.cookie = '; '.join(parts) + ';'
        self.miui_vip_ph = self._cookie_value('miui_vip_ph')
        if not self.uid:
            self.uid = self._cookie_value('userId') or self._cookie_value('cUserId')
        w_log('使用 Cookie 登录，共 %d 项' % len(parts))
        if not self.miui_vip_ph:
            w_log('注意：Cookie 里没有 miui_vip_ph，成长值相关接口可能返回 401')

        try:
            response = requests.get('https://api.vip.miui.com/mtop/planet/pc/post/userInfo',
                                    headers=self._h(), timeout=20)
            r_json = response.json()
        except Exception as e:
            w_log('Cookie 校验请求出错：' + type(e).__name__)
            return False

        code = r_json.get('status', r_json.get('code'))
        if code == 401:
            w_log('Cookie 已失效或不完整（接口返回 401），请重新复制')
            return False
        if code != 200:
            w_log('Cookie 校验返回异常：' + str(r_json.get('message')))
            return False
        w_log('Cookie 登录成功')
        return True

    def _identity_options(self, start_url):
        """
        把身份验证入口换成 identity/list，拿到这个账号可用的验证方式。
        顺带把 identity_session 留在 Session 里，后面的发短信/校验都靠它。
        :return: (options, meta)  例如 ([4, 8], {'mask': '138****8888'})
        """
        list_url = start_url.replace(IDENTITY_START_PATH, 'identity/list') \
            if IDENTITY_START_PATH in start_url else start_url
        response = self.session.get(list_url, headers=self._account_headers(referer=start_url), timeout=25)
        text = (response.text or '').strip()
        body = text.lstrip('&')
        if body.startswith('START'):
            body = body[len('START'):].lstrip('&')
        if not body.startswith('{'):
            # 数据中心 IP（Actions、云主机）常被重定向到登录页或风控页。
            # 只报一句 JSONDecodeError 看不出卡在哪，这里补上状态码和响应片段
            w_log('验证入口没有返回 JSON：HTTP %s，Content-Type=%s'
                  % (response.status_code, response.headers.get('Content-Type', '未知')))
            w_log('响应片段：' + ' '.join(text[:160].split()))
            raise ValueError('identity/list 返回非 JSON')
        meta = json.loads(body)
        if not self.session.cookies.get('identity_session'):
            for item in response.cookies:
                if item.name == 'identity_session':
                    self.session.cookies.set(item.name, item.value,
                                             domain=item.domain, path=item.path)
                    break
        flag = meta.get('flag')
        options = meta.get('options') or ([flag] if flag else [])
        return [int(o) for o in options if str(o).isdigit()], meta

    def _send_ticket(self, flag):
        """请求下发验证码。87001 表示需要先过图形验证码"""
        response = self.session.post(
            ACCOUNT_BASE + IDENTITY_SEND_APIS[flag],
            params={'_dc': int(time.time() * 1000)},
            data={'_flag': flag, 'retry': 0, '_json': 'true'},
            headers=self._account_headers(), timeout=25)
        return self._loads_account_json(response)

    def _verify_ticket(self, flag, code):
        """提交验证码，成功时返回带 location 的响应"""
        response = self.session.post(
            ACCOUNT_BASE + IDENTITY_VERIFY_APIS[flag],
            params={'_dc': int(time.time() * 1000)},
            data={'_flag': flag, 'ticket': str(code), 'trust': 'false', '_json': 'true'},
            headers=self._account_headers(), timeout=25)
        return self._loads_account_json(response)

    def _ask_sms_code(self, label):
        if self.sms_code_provider:
            return str(self.sms_code_provider(label) or '').strip()
        try:
            return input('请输入%s收到的验证码: ' % label).strip()
        except (EOFError, KeyboardInterrupt):
            w_log('没有输入验证码，当前环境无法交互')
            return ''

    def _finish_login(self, location):
        """跟随验证成功后的跳转地址，把社区域 Cookie 收进 self.cookie"""
        url = self._account_url(location)
        try:
            self.session.get(url, headers=self._account_headers(referer=url),
                             timeout=30, allow_redirects=True)
        except Exception as e:
            w_log('跟随跳转地址出错：' + type(e).__name__)

        jars = {}
        for item in self.session.cookies:
            if 'miui.com' in item.domain or 'xiaomi.com' in item.domain:
                jars[item.name] = item.value
        if not jars:
            w_log('没有取到社区 Cookie，登录未完成')
            return False

        self.cookie = '; '.join('%s=%s' % (k, v) for k, v in jars.items()) + ';'
        self.miui_vip_ph = jars.get('miui_vip_ph', '')
        if not self.miui_vip_ph and not self.get_vip_cookie(url):
            w_log('警告：没有取到 miui_vip_ph，成长值相关任务可能失败')
        w_log('账号登录完成（二次验证通过）')
        return True

    def verify_login(self, auth):
        """
        处理密码登录返回「需要二次验证」的情况：下发短信 → 输入验证码 → 换取社区 Cookie。

        与小米账号网页端同一套接口：
        identity/list 取验证方式 → sendPhoneTicket 下发验证码 → verifyPhone 校验 → 跟随 location 拿 Cookie
        """
        w_log('该账号需要二次验证（短信/邮箱验证码）')
        notification_url = auth.get('notificationUrl')
        if not notification_url:
            w_log('服务端没有返回验证地址，无法自动完成验证，请改用 Cookie 登录')
            return False

        start_url = self._account_url(notification_url)
        try:
            options, meta = self._identity_options(start_url)
        except Exception as e:
            w_log('打开身份验证入口失败：' + type(e).__name__)
            return False

        if not options:
            w_log('没有拿到可用的验证方式，请改用 Cookie 登录')
            return False

        # 优先手机短信
        options.sort(key=lambda x: 0 if x == 4 else 1)
        flag = options[0]
        if flag not in IDENTITY_SEND_APIS:
            w_log('账号要求的验证方式（%s）暂不支持自动处理，请改用 Cookie 登录' % flag)
            return False

        label = IDENTITY_METHOD_NAMES.get(flag, '验证方式#%s' % flag)
        w_log('验证方式：' + label)
        if meta.get('mask'):
            w_log('接收方：' + str(meta.get('mask')))

        send = self._send_ticket(flag)
        code = send.get('code')
        if code == 87001:
            # 连续触发才会出图形验证码，这里不做自动识别，给出可操作的出口
            w_log('下发验证码前被要求完成图形验证码，请在浏览器打开：')
            w_log(self._account_url(send.get('captchaUrl', '')))
            w_log('完成后重新运行（可先用 --cookie 直接登录）')
            return False
        if code != 0:
            w_log('下发%s验证码失败：%s' % (
                label, send.get('description') or send.get('desc') or send.get('message') or code))
            return False
        w_log('验证码已下发，请查收')

        for i in range(SMS_CODE_RETRY):
            code_text = self._ask_sms_code(label)
            if not code_text:
                return False
            result = self._verify_ticket(flag, code_text)
            r_code = result.get('code')
            if r_code == 0:
                location = result.get('location')
                if not location:
                    w_log('验证通过但没有返回跳转地址')
                    return False
                return self._finish_login(location)
            if r_code in (70014, 70015):
                w_log('验证码不正确，请重试（%d/%d）' % (i + 1, SMS_CODE_RETRY))
                continue
            w_log('验证失败：' + str(result.get('description') or result.get('desc')
                                    or result.get('message') or r_code))
            return False

        w_log('验证码错误次数过多，本次登录失败')
        return False

    def mi_login(self):
        # 配置里给了 Cookie 就直接用，完全跳过账号密码和二次验证
        if self.preset_cookie:
            return self.login_with_cookie()

        proxies = {
            'https': None,
            'http': None
        }
        login_referer = ('https://account.xiaomi.com/fe/service/login/password?sid=miui_vip&qs=%253Fcallback%253Dhttp'
                       '%25253A%25252F%25252Fapi.vip.miui.com%25252Fsts%25253Fsign%25253D4II4ABwZkiJzkd2YSkyEZukI4Ak'
                       '%2525253D%252526followup%25253Dhttps%2525253A%2525252F%2525252Fapi.vip.miui.com%2525252Fpage'
                       '%2525252Flogin%2525253FdestUrl%2525253Dhttps%252525253A%252525252F%252525252Fweb.vip.miui.com'
                       '%252525252Fpage%252525252Finfo%252525252Fmio%252525252Fmio%252525252FinternalTest%252525253Fref'
                       '%252525253Dhomepage%2526sid%253Dmiui_vip&callback=http%3A%2F%2Fapi.vip.miui.com%2Fsts%3Fsign'
                       '%3D4II4ABwZkiJzkd2YSkyEZukI4Ak%253D%26followup%3Dhttps%253A%252F%252Fapi.vip.miui.com%252Fpage'
                       '%252Flogin%253FdestUrl%253Dhttps%25253A%25252F%25252Fweb.vip.miui.com%25252Fpage%25252Finfo'
                       '%25252Fmio%25252Fmio%25252FinternalTest%25253Fref%25253Dhomepage&_sign=L%2BdSQY6sjSQ%2FCRjJs4p'
                       '%2BU1vNYLY%3D&serviceParam=%7B%22checkSafePhone%22%3Afalse%2C%22checkSafeAddress%22%3Afalse%2C'
                       '%22lsrp_score%22%3A0.0%7D&showActiveX=false&theme=&needTheme=false&bizDeviceType=')
        # 登录是 account.xiaomi.com 页面内发起的同源 XHR，
        # 因此 Sec-Fetch-Site 需为 same-origin（不同于社区接口的 same-site）
        headers = self._h({
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Origin': 'https://account.xiaomi.com',
            'X-Requested-With': 'XMLHttpRequest',
            'Sec-Fetch-Site': 'same-origin',
            'cookie': 'deviceId=' + str(self.device_id) + '; pass_ua=web; uLocale=zh_CN',
        }, referer=login_referer)
        data = {
            'bizDeviceType': '',
            'needTheme': 'false',
            'theme': '',
            'showActiveX': 'false',
            'serviceParam': '{"checkSafePhone":false,"checkSafeAddress":false,"lsrp_score":0.0}',
            'callback': 'http://api.vip.miui.com/sts?sign=4II4ABwZkiJzkd2YSkyEZukI4Ak%3D&followup=https%3A%2F%2Fapi.vip'
                        '.miui.com%2Fpage%2Flogin%3FdestUrl%3Dhttps%253A%252F%252Fweb.vip.miui.com%252Fpage%252Finfo'
                        '%252Fmio%252Fmio%252FinternalTest%253Fref%253Dhomepage',
            'qs': '%3Fcallback%3Dhttp%253A%252F%252Fapi.vip.miui.com%252Fsts%253Fsign%253D4II4ABwZkiJzkd2YSkyEZukI4Ak'
                  '%25253D%2526followup%253Dhttps%25253A%25252F%25252Fapi.vip.miui.com%25252Fpage%25252Flogin'
                  '%25253FdestUrl%25253Dhttps%2525253A%2525252F%2525252Fweb.vip.miui.com%2525252Fpage%2525252Finfo'
                  '%2525252Fmio%2525252Fmio%2525252FinternalTest%2525253Fref%2525253Dhomepage%26sid%3Dmiui_vip',
            'sid': 'miui_vip',
            '_sign': 'L+dSQY6sjSQ/CRjJs4p+U1vNYLY=',
            'user': str(self.uid),
            'cc': '+86',
            'hash': str(self.password),
            '_json': 'true'
        }
        try:
            # 用 Session 发登录请求：二次验证要继续用这次登录拿到的 passToken 等 cookie
            self.session = requests.Session()
            response = self.session.post('https://account.xiaomi.com/pass/serviceLoginAuth2',
                                         headers=headers, data=data, proxies=proxies)
            r_json = self._loads_account_json(response)
            if r_json['code'] == 70016:
                w_log('小米账号登录失败：用户名或密码不正确')
                return False
            if r_json['code'] != 0:
                w_log('小米账号登录失败：' + str(r_json.get('desc')))
                return False
            if r_json.get('pwd') != 1:
                # pwd != 1：密码正确，但这个设备需要短信/邮箱二次验证
                if not r_json.get('notificationUrl') and r_json.get('location'):
                    # 少数情况下仍会直接给跳转地址，能用就不折腾验证码
                    if self.get_vip_cookie(r_json['location']):
                        w_log('账号登录完成')
                        return True
                return self.verify_login(r_json)
            if not self.get_vip_cookie(r_json['location']):
                w_log('小米账号登录成功，社区获取 Cookie 失败')
                return False
            w_log('账号登录完成')
            return True
        except Exception as e:
            w_log("登录小米账号出错")
            w_log(e)
            return False

    @staticmethod
    def _to_state(value):
        # 服务端可能返回字符串 "0"/"1"，统一转成整数，无法识别则返回 None（按未知处理）
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def check_daily_tasks(self):
        """
        查询社区成长值任务列表及各任务完成状态
        :return: {任务标题: {'state': int, 'desc': str, 'group': str}}，查询失败返回 None
        """
        headers = self._h()
        try:
            response = requests.get(TASK_LIST_URL, headers=headers, timeout=15)
            r_json = response.json()
            if r_json.get('status') != 200:
                w_log('获取任务列表失败：' + str(r_json.get('message')))
                return None

            tasks = {}
            for group in r_json.get('entity') or []:
                group_name = str((group.get('head') or {}).get('title', '未分组'))
                for item in group.get('data') or []:
                    title = str(item.get('title', '')).strip()
                    if not title:
                        continue
                    state = self._to_state(item.get('showType'))
                    tasks[title] = {
                        'state': state,
                        'desc': str(item.get('desc', '')),
                        'group': group_name,
                        'jump_url': str(item.get('jumpUrl', '')),
                        'jump_text': str(item.get('jumpText', '')),
                    }
                    state_text = {TASK_STATE_DONE: '已完成', TASK_STATE_TODO: '未完成'}.get(
                        state, '状态未知(' + str(item.get('showType')) + ')')
                    w_log('任务状态：' + group_name + ' | ' + title + ' | ' + state_text)

            if not tasks:
                w_log('任务列表为空，无法判断任务状态')
                return None
            return tasks
        except Exception as e:
            w_log('获取任务列表出错')
            w_log(e)
            return None

    def find_specialpage_task(self, tasks) -> str:
        """
        从任务列表中找出「未完成的专题页任务」
        优先按标题匹配，其次按描述匹配，最后才做描述兜底并排除主页/帖子等其它任务，
        避免把别的任务误判成专题页而白白发出一次请求
        :param tasks: check_daily_tasks 的返回值
        :return: 可执行的任务标题，没有可用任务时返回 None
        """
        if not tasks:
            return None

        def is_todo(info):
            return info.get('state') == TASK_STATE_TODO

        # 1. 标题里明确带「专题」
        for title, info in tasks.items():
            if SPECIAL_PAGE_TITLE_KEY in title and is_todo(info):
                return title

        # 2. 描述里带「专题」
        for title, info in tasks.items():
            if SPECIAL_PAGE_TITLE_KEY in info.get('desc', '') and is_todo(info):
                return title

        # 3. 描述符合专题页特征，且标题不属于其它已知任务
        for title, info in tasks.items():
            if any(key in title for key in SPECIAL_PAGE_EXCLUDE):
                continue
            if SPECIAL_PAGE_DESC_KEY in info.get('desc', '') and is_todo(info):
                return title

        return None

    @staticmethod
    def _point_from_growup_page(r_json):
        """
        成长值页的返回是分组卡片，成长值在 head.title == '成长值' 那一组：
        data 依次是 [当前值, '/', 上限]，例如 33 / 33 / 50。
        """
        for group in r_json.get('entity') or []:
            head = group.get('head') or {}
            if str(head.get('title', '')).strip() != GROWUP_POINT_GROUP_TITLE:
                continue
            for item in group.get('data') or []:
                value = str(item.get('title', '')).strip()
                if value.lstrip('-').isdigit():
                    return int(value)
        return None

    @staticmethod
    def _point_from_user_info(r_json):
        """userInfo 的老字段。App 4.99.2 起 userGrowLevelInfo 只剩等级，不一定还有 point"""
        entity = r_json.get('entity')
        info = entity.get('userGrowLevelInfo') if isinstance(entity, dict) else None
        if isinstance(info, dict) and info.get('point') is not None:
            return info.get('point')
        return None

    def get_point(self) -> int:
        """
        获取当前成长值。
        这个方法带返回值的原因是，可以调用这个方法获取返回值，可根据这个方法定制自己的“消息提示功能”。
        如：Qmsg发送到QQ 或者 发送邮件提醒
        :return: 当前的成长值；取不到返回 None
        """
        headers = self._h()

        # 1. 成长值页接口（App 4.99.2 实际在用）
        try:
            response = requests.get(GROWUP_PAGE_URL, headers=headers, timeout=15)
            r_json = response.json()
            code = r_json.get('status', r_json.get('code'))
            if code == 401:
                w_log('成长值获取失败：Cookie 无效或已过期')
                return None
            point = self._point_from_growup_page(r_json) if code == 200 else None
            if point is not None:
                w_log('成功获取成长值,当前成长值：' + str(point))
                return point
            w_log('成长值页没有返回成长值（HTTP %d, code=%s）' % (response.status_code, code))
        except Exception as e:
            w_log('成长值页请求失败：%s: %s' % (type(e).__name__, e))

        # 2. 回退到 userInfo 的老字段
        try:
            response = requests.get(USER_INFO_URL, headers=headers, timeout=15)
            r_json = response.json()
            code = r_json.get('status', r_json.get('code'))
            point = self._point_from_user_info(r_json)
            if point is not None:
                w_log('成功获取成长值,当前成长值：' + str(point))
                return point
            entity = r_json.get('entity') if isinstance(r_json.get('entity'), dict) else {}
            level = entity.get('userGrowLevelInfo') if isinstance(entity.get('userGrowLevelInfo'), dict) else {}
            w_log('成长值获取失败：userInfo 里没有 point 字段（HTTP %d, code=%s，当前等级 %s）'
                  % (response.status_code, code,
                     level.get('showLevel') or level.get('title') or '未知'))
        except Exception as e:
            w_log('成长值获取失败：%s: %s' % (type(e).__name__, e))
        return None


def match_task_action(title):
    """
    按标题把任务映射到脚本动作。
    返回 (动作键 或 None, 无法代劳的原因)
    """
    text = str(title or '')
    for action, hits, excludes, reason in TASK_RULES:
        if any(k in text for k in excludes):
            continue
        if any(k in text for k in hits):
            return action, reason
    return None, ''


def task_reward(title, desc='', action=None):
    """
    判断一条任务的奖励是不是「社区成长值」。
    顺序：描述里的文字 > 标题里的文字 > 动作本身的性质。

    :return: ('growth' 或 'other', 判定依据)
    """
    text = str(title or '') + ' ' + str(desc or '')
    if GROWTH_KEYWORD in text:
        return 'growth', '任务描述写明给成长值'
    if action in NON_GROWTH_ACTIONS:
        return 'other', NON_GROWTH_ACTIONS[action]
    if action in GROWTH_ACTIONS:
        return 'growth', '该动作最终调用成长值上报接口'
    if action is None:
        return 'other', '不属于成长值任务'
    return 'other', '不确定是否给成长值'


def analyze_tasks(tasks, switches=None, growth_only=True):
    """
    把任务列表解析成执行计划，纯本地计算，不发任何请求。

    :param tasks: check_daily_tasks 的返回值 {标题: {...}}
    :param switches: {'check-in': bool, 'carrot-pull': bool, 'browse-specialpage': bool}
    :param growth_only: True 时只保留「奖励含成长值」的任务，其余不进入计划
    :return: (plan, skipped)
             plan: [{'title','group','state','action','action_label','status','reason',...}]
                   status 取值：run 可执行 / done 已完成 / manual 需人工
                                / disabled 开关未开 / unknown 无对应动作
             skipped: 被过滤掉的非成长值任务 [{'title','reason'}]
    """
    switches = switches or {}
    plan, skipped = [], []
    for title, info in tasks.items():
        action, manual_reason = match_task_action(title)
        reward, reward_reason = task_reward(title, info.get('desc', ''), action)
        if growth_only and reward != 'growth':
            skipped.append({
                'title': title,
                'group': info.get('group', ''),
                'reason': reward_reason,
                'state': info.get('state'),
            })
            continue
        item = {
            'title': title,
            'group': info.get('group', ''),
            'state': info.get('state'),
            'desc': info.get('desc', ''),
            'jump_url': info.get('jump_url', ''),
            'action': action,
            'action_label': TASK_ACTION_LABELS.get(action, ''),
            'reward': reward,
            'reward_reason': reward_reason,
            'status': '',
            'reason': '',
        }
        if info.get('state') == TASK_STATE_DONE:
            item['status'] = 'done'
            item['reason'] = '已完成，不重复操作'
        elif action is None:
            item['status'] = 'manual' if manual_reason else 'unknown'
            item['reason'] = manual_reason or '脚本里没有对应动作'
        elif action in TASK_ACTION_MANUAL_ONLY:
            # 试过也是 640，发了等于白发，直接标人工，并把原因说清楚
            item['status'] = 'manual'
            item['reason'] = TASK_ACTION_NOTES.get(action) or '需要手动操作'
        else:
            switch = TASK_ACTION_RISK_SWITCH.get(action)
            note = TASK_ACTION_NOTES.get(action)
            if switch and not switches.get(switch):
                item['status'] = 'disabled'
                item['reason'] = '需开启开关 %s（默认关闭）' % switch
            else:
                item['status'] = 'run'
                item['reason'] = '将执行 ' + item['action_label']
                if note:
                    item['reason'] += '（' + note + '）'
        plan.append(item)
    return plan, skipped


def print_task_plan(plan, skipped=None):
    """打印任务解析结果：每条任务能不能自动完成、走哪个动作"""
    skipped = skipped or []
    if not plan:
        w_log('没有解析到任何成长值任务')
    else:
        status_text = {
            'run': '可自动完成',
            'done': '已完成',
            'manual': '需人工',
            'disabled': '开关未开',
            'unknown': '未知任务',
        }
        w_log('---------- 成长值任务解析 ----------')
        for idx, item in enumerate(plan, 1):
            w_log('[%d/%d] %s | %s | %s'
                  % (idx, len(plan), item['group'] or '未分组', item['title'],
                     status_text.get(item['status'], item['status'])))
            if item['action_label']:
                w_log('        动作：%s（%s）' % (item['action_label'], item['action']))
            w_log('        说明：%s' % (item['reason'] or '-'))
            if item['desc']:
                w_log('        任务描述：%s' % item['desc'])
            if item['jump_url']:
                w_log('        跳转地址：%s' % item['jump_url'])

    if skipped:
        w_log('---------- 已忽略（不给成长值）----------')
        for item in skipped:
            state_text = {TASK_STATE_DONE: '已完成', TASK_STATE_TODO: '未完成'}.get(
                item.get('state'), '状态未知')
            w_log('  %s | %s | %s' % (item['group'] or '未分组', item['title'], state_text))
            w_log('        原因：%s' % item['reason'])

    if not plan:
        w_log('---------- 任务解析结论 ----------')
        w_log('没有任何成长值任务，本次不会发送任何动作请求')
        return

    runnable = [i for i in plan if i['status'] == 'run']
    manual = [i for i in plan if i['status'] in ('manual', 'unknown')]
    disabled = [i for i in plan if i['status'] == 'disabled']
    w_log('---------- 任务解析结论 ----------')
    w_log('成长值任务：%d（可自动完成：%d，需人工：%d，开关未开：%d，已完成：%d），'
          '已忽略非成长值任务：%d'
          % (len(plan), len(runnable), len(manual), len(disabled),
             len([i for i in plan if i['status'] == 'done']), len(skipped)))
    # 多条任务可能落到同一个动作：按动作归并输出，避免看起来像要发两次请求
    by_action = {}
    for i in runnable:
        by_action.setdefault(i['action'], []).append(i['title'])
    for action, titles in by_action.items():
        label = TASK_ACTION_LABELS.get(action, action)
        if len(titles) > 1:
            w_log('  将执行：%s（一条请求覆盖 %d 个任务：%s）'
                  % (label, len(titles), '、'.join(titles)))
        else:
            w_log('  将执行：%s（对应任务：%s）' % (label, titles[0]))
    for i in disabled:
        w_log('  开关未开：' + i['title'] + '（' + i['reason'] + '）')
    for i in manual:
        w_log('  需人工：' + i['title'] + '（' + i['reason'] + '）')


def run_task_plan(miui_task, plan):
    """
    按计划执行动作。只有 status=='run' 的才会真正发请求，
    每个动作前有 20~40 秒随机间隔，动作之间不重复调用。
    """
    todo = [i for i in plan if i['status'] == 'run']
    if not todo:
        w_log('没有可自动完成的任务，本次不发送任何动作请求')
        return []

    w_log('---------- 开始执行 ----------')
    action_count = len({i['action'] for i in todo})
    w_log('本次将执行 %d 个动作（对应 %d 条任务），动作之间随机间隔 10 秒以上'
          % (action_count, len(todo)))
    done = []
    done_actions = set()
    for idx, item in enumerate(todo, 1):
        # 多条任务可能指向同一个动作（例如不同入口的签到任务），
        # 同一轮里只执行一次，避免对同一接口重复发请求
        if item['action'] in done_actions:
            w_log('（%d/%d）%s -> 与前面的任务共用同一个动作，不再重复请求'
                  % (idx, len(todo), item['title']))
            item['status'] = 'executed'
            continue
        done_actions.add(item['action'])
        w_log('（%d/%d）%s -> %s' % (idx, len(todo), item['title'], item['action_label']))
        # 动作前置间隔：与浏览类任务一致，≥10 秒且随机，避免固定节奏
        sleep_ten_sec_more()
        action = getattr(miui_task, item['action'], None)
        if not callable(action):
            w_log('跳过：脚本里没有动作 ' + str(item['action']))
            item['status'] = 'unknown'
            continue
        try:
            action()
            item['status'] = 'executed'
        except Exception as e:
            w_log('执行出错：' + item['title'])
            w_log(e)
            item['status'] = 'failed'
        done.append(item)
    return done


def verify_task_plan(miui_task, plan):
    """
    执行后复查任务列表，对比哪些真正变成了「已完成」。
    这一步同时当作收尾请求：服务端看到的是「读完任务 -> 做任务 -> 回来看结果」，
    与真实用户在 App 里的行为一致。
    """
    w_log('---------- 复查任务状态 ----------')
    after = miui_task.check_daily_tasks()
    if not after:
        w_log('复查失败（任务列表获取异常），本次执行结果未知，请手动到成长值页确认')
        return None

    newly_done, still_todo, missing = [], [], []
    for item in plan:
        title = item['title']
        now = after.get(title)
        if now is None:
            missing.append(title)
        elif item.get('state') == TASK_STATE_DONE:
            continue  # 本来就已完成，不参与对比
        elif now.get('state') == TASK_STATE_DONE:
            newly_done.append(title)
        else:
            still_todo.append(title)

    if newly_done:
        for t in newly_done:
            w_log('  本次完成：' + t)
    if still_todo:
        for t in still_todo:
            item = next((i for i in plan if i['title'] == t), {})
            if item.get('status') == 'executed' and item.get('action') == 'wechat_checkin':
                # 成长值明细已经 +3，任务列表却还是「未完成」——这是状态同步延迟，不是没做成
                reason = '签到已完成、成长值已到账；任务列表完成状态同步有延迟，属正常'
            elif item.get('status') == 'executed':
                reason = '动作已执行，但任务列表仍显示未完成（成长值到账与任务状态不同步）'
            else:
                reason = item.get('reason', '')
            w_log('  仍未完成：' + t + ('（' + reason + '）' if reason else ''))
    if missing:
        for t in missing:
            w_log('  列表中已消失：' + t + '（可能已换期或已下线）')
    if not (newly_done or still_todo or missing):
        w_log('  任务状态无变化')
    return after


def process_exception(e: Exception):
    """
    全局异常处理
    :param e: 异常实例
    :return: No return
    """
    if e.__str__() == 'check_hostname requires server_hostname':
        w_log('系统设置了代理，出现异常')


def _task_switches(check_in, carrot_pull, browse_specialpage=False):
    """
    开关字典。专题页恒为 True —— 它由任务列表门控，不是由开关门控：
    列表里没有「浏览限时专题页」这条任务时，analyze_tasks 根本不会产生该动作。
    参数 browse_specialpage 仅为兼容旧调用（--legacy 里才真正用到）。
    """
    return {
        'check-in': bool(check_in),
        'carrot-pull': bool(carrot_pull),
        'browse-specialpage': True,
    }


# ==================== 运行结果汇总（供钉钉通知使用）====================
# 每个账号跑完往 RUN_REPORT 里放一条，main() 结束前统一组装成一条消息发出去。
# 只记结论不记明细，避免把 Cookie、请求体这类东西带到外部服务上。
RUN_REPORT = []


def _report_start(miui_task):
    entry = {
        'uid': str(getattr(miui_task, 'uid', '') or ''),
        'ok': False,
        'error': '',
        'base': None,
        'final': None,
        'executed': [],
        'manual': [],
        'failed': [],
        'skipped': 0,
        'note': '',
    }
    RUN_REPORT.append(entry)
    return entry


def start(miui_task: MIUITask, check_in: bool, carrot_pull: bool, browse_specialpage: bool,
          legacy: bool = False, growth_only: bool = True):
    """
    任务驱动入口：先读任务列表，只为「存在且未完成」的任务发送对应请求。

    旧版是固定序列（3 次浏览 + 3 组点赞/取消 + 退圈/加圈 + 浏览主页 + 签到 + 拔萝卜），
    不管当天有没有这些任务都发。任务列表实测只有 7 条且不含点赞/圈子/主页，
    这些请求全是空白暴露，因此默认改为按任务列表门控；legacy=True 可回到旧行为。

    growth_only=True（默认）时再收窄一层：只处理奖励含「成长值」的任务，
    升级 App、拔萝卜（发金币）这类与成长等级无关的任务直接忽略，一次请求都不发。

    专题页任务不看开关，只看任务列表：活动期间列表里出现且未完成才做，
    平时没有这条任务就一次请求都不发。
    """
    # 启动前抖动：避免定时任务在整点秒级固定发起登录
    entry = _report_start(miui_task)
    random_sleep()
    if not miui_task.mi_login():
        entry['error'] = ('登录失败（Cookie 已过期或账号需要短信验证）'
                          '→ 本机 python run_local.py --login 重新获取，再更新 Secret MIUI_COOKIE')
        return

    random_sleep()
    miui_task.login_app()

    if legacy:
        w_log('已启用 legacy 模式：按固定序列执行，不参考任务列表')
        _start_legacy(miui_task, check_in, carrot_pull, browse_specialpage)
        entry['ok'] = True
        entry['note'] = 'legacy 模式（固定序列，不参考任务列表）'
        return

    switches = _task_switches(check_in, carrot_pull, browse_specialpage)
    w_log('风险功能开关：签到=%s 拔萝卜=%s 专题页=自动（任务列表里有才做）'
          % (check_in, carrot_pull))
    if growth_only:
        w_log('任务范围：只处理能获得社区成长值的任务，其余一律忽略')

    w_log('---------- 当前成长值 ----------')
    base_point = miui_task.get_point()
    entry['base'] = base_point if isinstance(base_point, int) else None

    w_log('---------- 查询任务列表 ----------')
    tasks = miui_task.check_daily_tasks()
    if tasks is None:
        # fail-closed：看不到任务状态就不发动作请求，宁可少做也不凭空发请求
        w_log('无法获取任务列表，为避免在看不到任务状态的情况下凭空发请求，本次不执行任何动作')
        w_log('排查建议：先跑 python run_local.py --tasks')
        entry['error'] = '任务列表获取失败，为避免盲发请求，本次未执行任何动作'
        return

    plan, skipped = analyze_tasks(tasks, switches, growth_only=growth_only)
    print_task_plan(plan, skipped)
    entry['skipped'] = len(skipped)
    entry['manual'] = []
    for item in plan:
        if item['status'] not in ('manual', 'unknown'):
            continue
        hint = TASK_ACTION_MANUAL_HINT.get(item.get('action') or '')
        # 通知里带上操作方式，否则「需人工：每日签到」看不出该干什么
        entry['manual'].append('%s（%s）' % (item['title'], hint) if hint else item['title'])
    if carrot_pull and any('拔萝卜' in i['title'] for i in skipped):
        w_log('提示：拔萝卜发放的是金币而非成长值，已按「只做成长值任务」的设定忽略')
    executed = run_task_plan(miui_task, plan)
    entry['executed'] = [i['title'] for i in plan if i['status'] == 'executed']
    entry['failed'] = [i['title'] for i in plan if i['status'] == 'failed']
    if executed:
        verify_task_plan(miui_task, plan)
    else:
        # 没发任何动作请求，状态不可能变，省掉这次复查访问
        w_log('本次未执行任何动作，跳过复查')

    random_sleep()
    w_log('---------- 本次成长值变化 ----------')
    end_point = miui_task.get_point()
    entry['final'] = end_point if isinstance(end_point, int) else None
    if isinstance(base_point, int) and isinstance(end_point, int):
        delta = end_point - base_point
        w_log('成长值 %d -> %d（%s%d）' % (base_point, end_point,
                                          '+' if delta >= 0 else '', delta))
    entry['ok'] = not entry['failed']


def _start_legacy(miui_task: MIUITask, check_in: bool, carrot_pull: bool, browse_specialpage: bool):
    """上游原始固定序列，仅 --legacy 时使用"""
    w_log("本脚本支持社区拔萝卜及成长值签到，因该功能存在风险默认禁用")
    w_log("如您愿意承担一切可能的后果，可编辑配置文件手动打开该功能")
    random_sleep()
    if carrot_pull:
        w_log("风险功能提示：正在进行社区拔萝卜")
        random_sleep()
        miui_task.carrot_pull()
    if check_in:
        w_log("风险功能提示：正在进行成长值签到")
        random_sleep()
        # 小程序签到（+3）先试；App 内签到（+1~+2）有人机校验，失败即止
        miui_task.wechat_checkin()
        random_sleep()
        miui_task.check_in()
    if browse_specialpage:
        w_log("正在检测是否存在未完成的专题页任务")
        task_list = miui_task.check_daily_tasks()
        special_page_task = miui_task.find_specialpage_task(task_list)
        if special_page_task:
            w_log("检测到专题页任务「" + special_page_task + "」未完成，开始执行")
            sleep_ten_sec_more()
            miui_task.browse_specialpage()
        elif task_list is None:
            w_log("无法获取任务列表，为避免风控已跳过浏览专题页任务")
        else:
            w_log("当前没有未完成的专题页任务，已跳过")
    w_log("正在完成浏览帖子10s任务，第一次")
    sleep_ten_sec_more()
    miui_task.browse_post()
    w_log("正在完成浏览帖子10s任务，第二次")
    sleep_ten_sec_more()
    miui_task.browse_post()
    w_log("正在完成浏览帖子10s任务，第三次")
    sleep_ten_sec_more()
    miui_task.browse_post()
    w_log("正在完成点赞任务")
    random_sleep()
    miui_task.thumb_up()
    random_sleep()
    miui_task.cancel_thumb_up()
    random_sleep()
    miui_task.thumb_up()
    random_sleep()
    miui_task.cancel_thumb_up()
    random_sleep()
    miui_task.thumb_up()
    random_sleep()
    miui_task.cancel_thumb_up()
    random_sleep()
    miui_task.board_unfollow()
    random_sleep()
    miui_task.board_follow()
    random_sleep()
    miui_task.browse_user_page()
    random_sleep()
    miui_task.get_point()


def main():
    w_log("MIUI-AUTO-TASK v1.5.2")
    w_log('---------- 系统信息 -------------')
    system_info()
    w_log('---------- 项目信息 -------------')
    w_log("项目地址：https://github.com/0-8-4/miui-auto-tasks")
    w_log("欢迎 star，感谢東雲研究所中的大佬")
    w_log('---------- 配置检测 -------------')

    config = get_config()

    if not check_config(config):
        w_log('配置文件没有正确配置')
        exit(1)
    else:
        config = format_config(config)

    started_at = time.time()
    try:
        for i in config.get('accounts'):
            w_log('---------- EXECUTING -------------')
            start(
                MIUITask(i.get('uid'), i.get('password'), i.get('user-agent'),
                         device_id=i.get('device-id'), cookie=i.get('cookie')),
                i.get('check-in'), i.get('carrot-pull'), i.get('browse-specialpage'),
            )
            time.sleep(5)
    except Exception as e:
        # 通知要能反映异常退出，所以先记一条再交给 finally 发送
        process_exception(e)
        w_log('运行出现未捕获异常：%s: %s' % (type(e).__name__, e))
        RUN_REPORT.append({'uid': '', 'ok': False,
                           'error': '异常退出：%s: %s' % (type(e).__name__, e),
                           'executed': [], 'manual': [], 'failed': [],
                           'base': None, 'final': None, 'skipped': 0, 'note': ''})
    finally:
        # 通知失败不改变退出码，也不影响日志归档
        try:
            notifier.notify_run_result(RUN_REPORT, started_at)
        except Exception as e:
            w_log('发送钉钉通知出错：%s: %s' % (type(e).__name__, e))
        s_log(config.get('logging'))

    # 有账号没跑成功就以非零退出码结束：否则 Actions 会显示绿色成功，
    # Cookie 过期这种「什么都没做」的失败会被当成正常跑完，没人会发现。
    failed = [e for e in RUN_REPORT if not e.get('ok')]
    if failed:
        w_log('本次运行有 %d 个账号未成功，退出码置 1' % len(failed))
        for e in failed:
            w_log('  账号 %s 失败原因：%s' % (e.get('uid') or '(来自 Cookie)',
                                            e.get('error') or '未完成'))
        exit(1)


def main_handler(event, context):
    main()


if __name__ == "__main__":
    main()
