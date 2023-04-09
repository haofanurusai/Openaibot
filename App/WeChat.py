# -*- coding: utf-8 -*-
# @Time    : 3/15/23 11:45 AM
# @FileName: WeChat.py
# @Software: VSCodium
# @Github: haofanurusai

# TODO: 退出时自动清理线程；提示语英化；图片语音支持(待WeChatFerry添加)；
# TODO: 过滤掉官方账号（文件传输助手，微信支付等）避免翻车
# 备考: 通讯录列表contacts中的每个元素为如下字典：
'''
{
    'wxid': 'wxid_114514acceed', # wxid，tx指派的，唯一主键
    'code': 'yjsnpi_1919810', # 自定义微信号
    'name': '野兽', # 昵称
    'country': '', # 以下无用
    'province': '',
    'city': '',
    'gender': ''
}
'''

import asyncio
import binascii
import re
import time
from collections import deque
from copy import deepcopy
from threading import Thread
from wcferry import Wcf
from loguru import logger

from utils import Setting
from utils.Data import create_message
from utils.Frequency import Vitality
from App import Event

# 调试开关
WECHAT_DEBUG = False

WECHAT_SUFFIX_ID = 104
WECHAT_WAIT_SECONDS = 5  # 连接后等待秒数，低配电脑请设长一些

# 以下是微信端支持的命令
WECHAT_GENERAL_CMD = ('/chat', '/write', '/style', '/forgetme', '/remind')
# 以下超管命令的每个参数都是微信昵称或微信号，要打 CRC32 补丁
WECHAT_ADMIN_CMD = (
    '/add_block_group',
    '/del_block_group',
    '/add_block_user',
    '/del_block_user',
    '/add_white_group',
    '/add_white_user',
    '/del_white_group',
    '/del_white_user',
    '/reset_user_usage'
)
# 以下超管命令原样执行，无需补丁
WECHAT_ADMIN_CMD_NOPATCH = (
    '/set_user_cold',
    '/set_group_cold',
    '/set_token_limit',
    '/set_input_limit',
    '/update_detect',
    '/open_user_white_mode',
    '/open_group_white_mode',
    '/close_user_white_mode',
    '/close_group_white_mode',
    '/open',
    '/close',
    '/see_api_key',
    '/del_api_key',
    '/add_api_key',
    '/set_per_user_limit',
    '/set_per_hour_limit'
)
# 以下超管命令接收两个参数，第一个是微信昵称或微信号，需要打补丁，第二个是数值，无需补丁
# 即使原来是支持多参数，现在也只收两个参数，以免引起混乱
WECHAT_ADMIN_CMD_SP = (
    '/promote_user_limit'
)
# 以上四个列表没包含的命令未经检验，暂不支持

# 使用 deque 存储请求时间戳
request_timestamps = deque()
time_interval = 60 * 5


class BotRunner(object):
    # # # 构造函数 # # #
    def __init__(self, _config):
        self.config = _config
        self.wcf = None
        self.wxid_whitelist = []
        self.cache_wxid = {}
        self.cache_crc = {}

    # # # 开始运行 # # #
    def run(self, pLock=None):
        global WECHAT_DEBUG

        if self.config.debug is None:
            WECHAT_DEBUG = False
        else:
            WECHAT_DEBUG = self.config.debug

        if not self.config.host_port:
            logger.info('APP:微信 Config/app.toml中未指定host_port参数')
            return
        try:
            # 初始化 Wcf 实例
            self.wcf = Wcf(host_port=self.config.host_port,
                           debug=self.config.debug)
            time.sleep(WECHAT_WAIT_SECONDS)
            if not self.wcf:
                logger.info('APP:微信 连接失败')
                return
            elif not self.wcf.is_login():
                logger.info('APP:微信 成功连接到远端，但是客户端没有登录')
                return
        except Exception as e:
            logger.warning(str(e))
            logger.info('APP:微信 WcFerry登录失败')
            return
        logger.success(f'APP:微信 {self.wcf.get_self_wxid()} 连接成功')

        self.build_cache(init=True)

        # 开启消息接收
        self.wcf.enable_receiving_msg()

        # 给收消息线程配一个事件循环
        self.loop = asyncio.new_event_loop()

        # 给发消息专门开一个线程
        # 不知何故跑在本线程的 asyncio.create_task 不好使
        t = Thread(target=self.loop_thread,
                   name=f'{self.wcf.get_self_wxid()}_LoopThread')
        t.start()

        # 主线程监视消息
        self.msg_loop()

    # # # 事件循环跑在另一线程里 # # #
    def loop_thread(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # # # 给事件循环加任务 # # #
    def add_async_task(self, coro, callback=None):
        task = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if callback is not None:
            task.add_done_callback(lambda fut: callback(fut.result()))

    # # # 消息分发处理 # # #
    def msg_loop(self):
        logger.success(f'APP:微信 {self.wcf.get_self_wxid()} 开始监听消息')
        while self.wcf.is_receiving_msg():
            # 处理每条消息
            try:
                msg = self.wcf.get_msg()
            except:
                continue

            # WeChatFerry BUGFIX: 自己在群里发的消息，id是群的id
            if msg.from_self() and msg.sender != self.wxid_whitelist[0]:
                msg.roomid = msg.sender
                msg.sender = self.wxid_whitelist[0]
                msg._is_group = True

            # 打印消息
            if WECHAT_DEBUG:
                # 调试输出
                logger.info(str(msg))
            # 友好输出
            logger.info('APP:微信 %s%s%s 说: %s' %
                        (
                            self.wxid_conv('name', msg.sender),
                            ' (@群:' if msg.from_group() else '',
                            self.wxid_conv('name', msg.roomid)+')'
                            if msg.from_group() else '',
                            repr(msg.content)
                        )
                        )

            # 群消息
            if msg.from_group():
                self.add_async_task(coro=self.on_msg_group(msg))

            # 个人消息
            else:
                txt = msg.content
                if txt.startswith('/about'):
                    self.add_async_task(
                        coro=Event.About(self.config),
                        callback=lambda res: self.wcf.send_text(
                            res, msg.sender)
                    )
                elif txt.startswith('/start'):
                    self.add_async_task(
                        coro=Event.Start(self.config),
                        callback=lambda res: self.wcf.send_text(
                            res, msg.sender)
                    )
                elif txt.startswith('/help'):
                    self.add_async_task(
                        coro=Event.Help(self.config),
                        callback=lambda res: self.wcf.send_text(
                            res, msg.sender)
                    )
                elif msg.is_text():
                    self.add_async_task(coro=self.on_msg_single(msg))

    # # # 用wxid查询其它字段 # # #
    def wxid_conv(self, dest_key: str, wxid: str, layer: int = 0):
        if layer > 5:
            raise LookupError('未查询到信息')
        try:
            return self.cache_wxid[wxid][dest_key]
        except Exception as e:
            logger.warning(str(e))
            self.build_cache()
            return self.wxid_conv(dest_key=dest_key, wxid=wxid, layer=layer + 1)

    # # # 建立 wxid, crc 速查表、白名单等缓存 # # #
    def build_cache(self, init: bool = False):
        self.wxid_whitelist = []
        # 更新缓存
        # 首先加入自己
        my_wxid = self.wcf.get_self_wxid()
        my_name = self.config.my_name
        my_crc = self.crc(my_wxid)
        my_elem = {
            'code': my_wxid,
            'name': my_name + '(自己)',
            'crc': my_crc
        }
        self.cache_wxid[my_wxid] = my_elem
        self.cache_crc[str(my_crc)] = my_elem
        self.wxid_whitelist.append(my_wxid)

        # 然后按微信号进行匹配
        for elem in self.wcf.get_contacts():
            wxid = elem['wxid']
            crc = elem['crc'] = self.crc(wxid)
            self.cache_wxid[wxid] = elem
            self.cache_wxid[str(crc)] = elem
            if elem['code'] in self.config.master:
                self.wxid_whitelist.append(wxid)

        # 最后将管理员 wxid 翻为 CRC32 供 Event 部分调用
        self.config.master = []
        for wxid in self.wxid_whitelist:
            self.config.master.append(self.cache_wxid[wxid]['crc'])

        if WECHAT_DEBUG:
            print('======== wxid cache ========')
            for i in self.cache_wxid:
                print(i,': ',self.cache_wxid[i])
                
            print('\r\n======== crc32 cache ========')
            for i in self.cache_crc:
                print(i,': ',self.cache_crc[i])

        if init:
            # 注册机器人配置
            self.profile_mgr = Setting.ProfileManager()
            self.profile_mgr.access_wechat(
                bot_name=my_name,
                bot_id=self.config.master[0],
                init=True
            )

    # # # 字符串 CRC32 # # #
    def crc(self, txt: str = ''):
        # TODO: 建议各种user_id支持字符串类型
        # 写死为int导致了很多不便
        # 还没看后续怎么利用这个user_id，不敢乱改
        # 用 CRC32 的方式先凑合出个哈希id临时顶下
        return binascii.crc32(txt.encode('utf8')) & 0xFFFFFFFF

    # # # 构造用户消息对象 # # #
    def get_user_message(self, msg: Wcf.WxMsg):
        return create_message(
            state=WECHAT_SUFFIX_ID,
            user_id=self.wxid_conv('crc', msg.sender),
            user_name=self.wxid_conv('name', msg.sender),
            group_id=self.wxid_conv('crc', msg.roomid) if msg.from_group(
            ) else self.wxid_conv('crc', msg.sender),
            group_name=self.wxid_conv(
                'name', msg.roomid) if msg.from_group() else 'Group',
            text=msg.content
        )

    # # # 给机器人回的消息打补丁，把 CRC32 变回原本昵称 # # #
    def msg_patch(self, txt, wxid=None):
        if wxid is None:
            match = re.search(r'\d+', txt)
            if match:
                old_str = match.group()
                crc_id = old_str[:-3]
                wxid = self.cache_crc[crc_id]['wxid']
            else:
                return txt
        else:
            old_str = f'{self.wxid_conv("crc", wxid)}{WECHAT_SUFFIX_ID}'
        new_str = self.wxid_conv('name', wxid)
        if WECHAT_DEBUG:
            print(f'消息补丁: {old_str} -> {new_str}')
        return txt.replace(old_str, new_str, 1)

    # # # 管理员给机器人发的命令需要打补丁，把昵称/微信号翻为 CRC32 # # #
    def cmd_patch(self, txt):
        # 有人的昵称中间可能带空格，如‘目 力 先 辈’
        # 在命令中会被视为四个昵称
        # 为了最大化避免匹配不上的问题，采用仅匹配开头的方式
        # 这样有可能错误地把其他开头是‘目’的人也给操作了
        # 不过概率不大，暂不做特殊处理
        # 遇到这样的昵称建议使用微信号来取代昵称
        args = txt.strip().split()
        new_cmd_args = [args[0]]
        if not args[0].startswith('/'):
            raise ValueError('不是命令')
        else:
            ending = len(args)
            if args[0] in WECHAT_ADMIN_CMD_NOPATCH:
                return ' '.join(args)

            elif args[0] in WECHAT_ADMIN_CMD_SP:
                ending = 2

            elif args[0] not in WECHAT_ADMIN_CMD:
                raise NameError('暂不支持该命令')

            wx_list = deepcopy(args[1:ending])
            
            for crc_str in self.cache_crc:
                elem = self.cache_crc[crc_str]

                i = 0
                while i < len(wx_list):
                    it = wx_list[i]
                    if elem['name'].startswith(it) or\
                            elem['wxid'] == it or\
                            elem['code'] == it:
                        new_cmd_args.append(f'{crc_str}{WECHAT_SUFFIX_ID}')
                        del wx_list[i]
                    else:
                        i += 1
                
                if len(wx_list) == 0:
                    break

            for item in args[ending:]:
                new_cmd_args.append(item)

        return ' '.join(new_cmd_args)

    # # # 处理私聊消息 # # #
    async def on_msg_single(self, msg: Wcf.WxMsg):
        request_timestamps.append(time.time())
        _hand = self.get_user_message(msg)
        if not _hand.text.startswith('/'):
            _hand.text = f'/chat {_hand.text}'
        if WECHAT_DEBUG:
            print('私聊')
            print(_hand)

        # 交谈
        if _hand.text.startswith(WECHAT_GENERAL_CMD):
            # TODO: 处理语音、图片消息
            _friend_msg = await Event.Friends(
                Message=_hand,
                config=self.config,
                bot_profile=self.profile_mgr.access_wechat(init=False)
            )
            if _friend_msg.status:
                if _friend_msg.reply:
                    if WECHAT_DEBUG:
                        print('正常回')
                    _caption = f'{_friend_msg.reply}\r\n{self.config.INTRO}'
                    self.wcf.send_text(_caption, msg.sender)
                else:
                    if WECHAT_DEBUG:
                        print('未正常回复')
                    _trigger_msg = await Event.Silent(_hand, self.config)
                    if not _trigger_msg.status:
                        self.wcf.send_text(self.msg_patch(
                            _friend_msg.msg, msg.sender), msg.sender)

        # 管理员消息
        elif msg.sender in self.wxid_whitelist:
            await self.on_msg_admin(msg, _hand)

    # # # 管理员消息 # # #
    async def on_msg_admin(self, msg: Wcf.WxMsg, _hand):
        try:
            _hand.text = self.cmd_patch(_hand.text)
        except ValueError as ve:
            return
        except NameError as ne:
            self.wcf.send_text(str(ne), msg.sender)
            return
        except Exception as e:            
            logger.warning(str(e))
            self.wcf.send_text('命令中存在错误，检查参数个数是否匹配', msg.sender)
            return

        if WECHAT_DEBUG:
            print('超管命令，打补丁后如下')
            print(_hand.text)

        _res = await Event.MasterCommand(
            user_id=self.wxid_conv('crc', msg.sender),
            Message=_hand,
            config=self.config
        )
        if _res:
            for item in _res:
                self.wcf.send_text(
                    item if _hand.text.startswith(
                        WECHAT_ADMIN_CMD_NOPATCH) else self.msg_patch(item),
                    msg.roomid if msg.from_group() else msg.sender
                )

    # # # 处理群消息 # # #
    # TODO: 帮助命令也引进来
    # !!! NOT FULLY TESTED !!!
    async def on_msg_group(self, msg: Wcf.WxMsg):
        _hand = self.get_user_message(msg)
        if not _hand.text.startswith('/'):
            _hand.text = f'/chat {_hand.text}'
        if WECHAT_DEBUG:
            print('群聊')
            print(_hand)

        started = False
        if _hand.text.startswith(WECHAT_GENERAL_CMD):
            started = True
        # 管理员消息还按私聊的方式处理
        elif msg.sender in self.wxid_whitelist:
            await self.on_msg_admin(msg, _hand)
            return
        # TODO: 处理@消息、引用消息

        if not started:
            try:
                _trigger_msg = await Event.Trigger(_hand, self.config)
                if _trigger_msg.status:
                    _GroupTrigger = Vitality(group_id=_hand.from_chat.id)
                    _GroupTrigger.trigger(Message=_hand, config=self.config)
                    _check = _GroupTrigger.check(Message=_hand)
                    if _check:
                        _hand.text = f"/catch {_hand.text}"
                        started = True
            except Exception as e:
                logger.warning(
                    f"{e}\r\nThis is a trigger Error,may [trigger] typo [tigger],try to check your config?")

        if started:
            request_timestamps.append(time.time())
            _friend_msg = await Event.Group(
                Message=_hand,
                bot_profile=self.profile_mgr.access_wechat(init=False),
                config=self.config
            )
            if _friend_msg.status:
                if _friend_msg.reply:
                    if WECHAT_DEBUG:
                        print('正常回')
                    _caption = f'{_friend_msg.reply}\r\n{self.config.INTRO}'
                    self.wcf.send_text(_caption, msg.roomid)
                else:
                    if WECHAT_DEBUG:
                        print('未正常回复')
                    _trigger_msg = await Event.Silent(_hand, self.config)
                    if not _trigger_msg.status:
                        self.wcf.send_text(self.msg_patch(
                            _friend_msg.msg, msg.roomid), msg.roomid)

    # # # 获得请求频率 # # #
    # 暂时不用，等待给 setAnalysis 实现微信接口
    '''
    def get_request_frequency():
        # 检查队列头部是否过期
        while request_timestamps and request_timestamps[0] < time.time() - time_interval:
            request_timestamps.popleft()
        # 计算请求频率
        request_frequency = len(request_timestamps)
        DefaultData().setAnalysis(qq=request_frequency)
        return request_frequency
    '''
