# coding=utf-8
"""各种告警策略处理逻辑的具体实现

1）Executor从远程队列获取任务，将任务内容等上下文信息传入run_alert_task方法
2）根据告警类型，选择相应的handler处理告警策略
3）handler（已注册）调用run方法处理，判断是否产生告警通知；如果有，投入到redis队列
4）handler更新对应策略的最新状态，保存到redis和mongo
5）redis中，对应的key为：
        strategy:{strategy_id}:state
        strategy:{strategy_id}:{group_str}:state
   strategy_id即mongo的ObjectId，group即分组的tag(eg. "host=web01")
   对应的value是json字符串，反序列化后的字典结构为：
        strategy:{strategy_id}:state
            - status
            - timestamp
            - group_keys
            - info (option)
                当只有一个group时，该group的info作为整个strategy的info
        strategy:{strategy_id}:{group}:state
            - status
            - timestamp
            - info

"""
from functools import wraps
from time import time
import asyncio
import json
from ast import literal_eval
from abc import ABCMeta, abstractmethod, abstractproperty
import urllib
import requests
from redis import Redis, ConnectionPool
from pymongo import MongoClient
from bson.objectid import ObjectId


from argus_alert.core.utils.log import timed_logger


LOG = timed_logger()
HandlerDict = {}


def run_alert_task(ctx):
    """根据告警类型type，选择相应的对象进行处理"""
    alert_type = ctx['strategy'].get('type', 'basic')
    try:
        Handler = HandlerDict.get(alert_type.upper())
    except KeyError:
        LOG.error(f'No handler found for alert, type: {alert_type}')
    else:
        Handler(ctx).run()


def register_handler(cls):
    """将告警策略处理对象注册到全局字典中，可作为装饰器使用
       （使用子类查找也可以实现，但不够优雅和安全）
    """
    if cls.TYPE not in HandlerDict:
        HandlerDict[cls.TYPE.upper()] = cls

    @wraps(cls)
    def _handler(*args, **kwargs):
        return cls(*args, **kwargs)

    return _handler


STATUS_OK = 'ok'
STATUS_ALERT = 'alert'
Chain_base_not_threhold_list = ['equal', 'not equal']


class IHandler(object):
    """告警任务处理的基类
    新实现的告警需要继承、实现run()方法和定义TYPE属性（即策略表中的type字段），并注册到字典中
    上下文对象可用的属性包括：
        self.ctx
            - strategy
            - task_time
            - tsd_addr
            - redis_addr
            - mongo_addr
            - redis_cli
            - mongo_cli
    """
    __metaclass__ = ABCMeta
    TYPE = '__base__'

    def __init__(self, ctx=None):
        self.ctx = ctx
        self.strategy = self.ctx['strategy']
        self.task_time = self.ctx['task_time']
        self.tsd_addr = self.ctx['tsd_addr']
        self.redis_addr = self.ctx['redis_addr']
        self.mongo_addr = self.ctx['mongo_addr']
        self._redis_client = None
        self._redis_pool = None
        self._mongo_client = None

    @property
    def redis_cli(self):
        if not self._redis_client:
            self._redis_client = Redis(connection_pool=self.redis_pool, decode_responses=True)
        return self._redis_client

    @property
    def redis_pool(self):
        if not self._redis_pool:
            self._redis_pool = ConnectionPool.from_url(self.redis_addr)
        return self._redis_pool

    @property
    def mongo_cli(self):
        if not self._mongo_client:
            self._mongo_client = MongoClient(self.mongo_addr)
        return self._mongo_client

    @staticmethod
    def unit_to_seconds(unit):
        """时间单位转换"""
        return {
            's': 1,
            'm': 60,
            'h': 60 * 60,
            'd': 60 * 60 * 24,
            'w': 60 * 60 * 24 * 7,
            'n': 60 * 60 * 24 * 30,
            'y': 60 * 60 * 24 * 365,
        }.get(unit)

    def query_time(self):
        """计算query url的start和end"""
        end_timestamp = self.task_time
        time_duration = self.strategy['tsd_rule'].get('time_duration', '1')
        time_unit = self.strategy['tsd_rule'].get('time_unit', 's')
        seconds = self.unit_to_seconds(time_unit) * float(time_duration)
        start_timestamp = int(end_timestamp - seconds)
        return start_timestamp, end_timestamp

    @property
    def groups_keys(self):
        """告警策略的分组tag的keys"""
        tags = self.strategy.get('tsd_rule', {}).get('group', [])
        return [kv['key'] for kv in tags]

    @staticmethod
    def comp(real_val, comparison, expect_val):
        """比较符判断"""
        # TODO: safety
        # return literal_eval(f'float({real_val}) {comparison} float({expect_val})')
        return eval(f'float({real_val}) {comparison} float({expect_val})')

    def push_channel(self,data,r):
        """
        In order to push message into the channel user selected to inform
        把对应的告警信息推送到对应的通知信道
        """
        channels = self.get_channel()
        for channel in channels:
            if channel == 'notice:slack':
                slack = self.get_slack_attr()
                data.update(**slack)
            elif channel == 'notice:mail':
                mail = self.get_mail_attr()
                data.update(**mail)
            elif channel == 'notice:wechat':
                wechat = self.get_wechat_attr()
                data.update(**wechat)
            r.publish(channel, json.dumps(data))
            LOG.debug(f'Message is published by channel({channel}) => {data}')


    def check_group_state(self, strategy, task_time, group_state,aggregation):
        """检查判断每个分组的状态，如有变化，发送通知到redis消息队列"""
        ### 测试的时候需要对mongo的策略添加通知组和通知个人字段
        ### 如果需要添加单独的组字段，只需进行吧group 添加到total_message中，即可，
        r = self.redis_cli
        STRATEGY_NOTICE = False
        if aggregation == True:
            alert_list = []
            t_message = {
                    'strategy_id': str(strategy['_id']),
                    'strategy_name': strategy['property']['name'],
                    'alert_time': task_time,
                    'alert_info': [],
                    'is_recover': True,
                    "level":strategy["level"],
                    "group":'',
                    "type":strategy['type']
                }
        for group, state in group_state.items():
            ret = r.getset(f'strategy:{strategy["_id"]}:{group}:state', json.dumps(state))
            GROUP_NOTICE = False
            if ret is None:
                if state['status'] == STATUS_ALERT:
                    GROUP_NOTICE = True
            else:
                last_group_state = json.loads(ret)
                if last_group_state['status'] != state['status']:
                    GROUP_NOTICE = True
            if GROUP_NOTICE:
                message = {
                    'strategy_id': str(strategy['_id']),
                    'strategy_name': strategy['property']['name'],
                    'alert_time': task_time,
                    'alert_info': f'{state["info"]}',
                    'is_recover': True if state['status'] == STATUS_OK else False,
                    "level":strategy["level"],
                    "group":group,
                    "type":strategy['type']
                }

            if GROUP_NOTICE and (not aggregation):
                STRATEGY_NOTICE = True
                alert_type = '告警产生' if state['status'] == STATUS_ALERT else '告警撤销'
                LOG.debug(f'message Group {group}')
                LOG.debug(f'message is {message}')
                total_message = {} ## 对告警Message进行重新封装，然后放到推送队列当中
                total_message['message'] = message
                push_group = strategy['notify']['notify_group'] # 把订阅组的组名获取然后去查mongo表
                # push_user = strategy['notify']['notify_user'] #  把特殊订阅的个人拿出来
                client = MongoClient('192.168.0.253', 27017)
                col = client['argus-users']['groups']
                LOG.debug("finding")
                # rec = col.find({"group_name": push_group})
                LOG.debug("find finish")
                # LOG.debug(f'rec is {rec}')
                push_list = []
                for _ in push_group:
                    user_list = _["group_names_check"]
                    for user in user_list:
                        push_list.append(user)
                # push_list.append(push_user)
                total_message['user'] = push_list
                total_message['aggregation'] = False
                self.push_channel(total_message,r)
                
            elif GROUP_NOTICE and aggregation:
                STRATEGY_NOTICE = True
                t_message['alert_info'].append(message)
                t_message['group'] += (message['group'] +' ')
                if message['is_recover'] == False:
                    t_message['is_recover'] = False
                elif message['is_recover'] == True and t_message['is_recover'] == True:
                    t_message['is_recover'] == True
                else:
                    t_message['is_recover'] == False
                
        if aggregation and STRATEGY_NOTICE:
            total_message = {}
            total_message['message'] = t_message
            total_message['aggregation'] = True
            push_group = strategy['notify']['notify_group'] # 把订阅组的组名获取然后去查mongo表
            client = MongoClient('192.168.0.253', 27017)
            col = client['argus-users']['groups']
            LOG.debug("finding")
            # rec = col.find({"group_name": push_group})
            LOG.debug("find finish")
            # LOG.debug(f'rec is {rec}')
            push_list = []
            for _ in push_group:
                user_list = _["group_names_check"]
                for user in user_list:
                    push_list.append(user)
            # push_list.append(push_user)
            total_message['user'] = push_list
            total_message['aggregation'] = True
            self.push_channel(total_message,r)

        return STRATEGY_NOTICE

    def set_strategy_status(self, strategy_id, status):
        """更新策略状态"""
        cli = self.mongo_cli
        db = cli['argus-alert']
        db['strategy'].update({'_id': ObjectId(strategy_id)},
                              {'$set': {
                                  'status': status
                              }}
                              )
        LOG.debug(f'Strategy({strategy_id}) status is updated to {status}.')

    def get_channel(self):
        """根据告警策略，获取publish的channel"""
        notify_methods = self.strategy['notify']['notify_method']
        return [f'notice:{_}' for _ in notify_methods]

    def get_slack_attr(self):
        """获取通知对象的slack配置"""
        # TODO：用户管理完善后再做
        return {
            'slack_hook': 'https://hooks.slack.com/services/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'
        }

    def get_mail_attr(self):
        """获取通知对象的mail配置"""
        # TODO
        return {
            'mail_addr': 'tangyingkang@useease.com'
        }

    def get_wechat_attr(self):
        """"""
        return {}

    def notify_message(self, comparison, threshold, real_value):
        return f'告警条件: {comparison} {threshold}, 检查值: {real_value}'

    @abstractmethod
    def run(self):
        raise NotImplementedError


# TODO：重构为从插件目录动态加载
@register_handler
class BasicAlert(IHandler):
    """基础告警
    - 过去N时段、某指标、聚合值、和阈值对比
    """
    TYPE = 'basic'      # 大小写不敏感

    def __init__(self, *args, **kwargs):
        IHandler.__init__(self, *args, **kwargs)
        self._query_url = ''
        self._tag_keys = []
        self._group = ''
        self._default_aggregate = 'sum'

    def check(self):
        """检查非空字段"""
        pass

    @property
    def query_url(self):
        """
        query url 构造，新增最新值的查询

        constr query url,add the latest value query
        """
        if not self._query_url:
            tsd_addr = self.tsd_addr
            tsd_rule = self.strategy.get('tsd_rule', {})
            metric = tsd_rule.get('metric', '')
            if self.strategy['latest'] == False:
                start, end = self.query_time()
                sample = tsd_rule.get('sample', '')
                aggregate = self._default_aggregate
                if sample:
                    sample = f':0all-{sample}'
                tags = tsd_rule.get('group', [])
                if tags:
                    tags_str = '{' + ','.join([f'{kv["key"]}={kv["value"]}' for kv in tags]) + '}'
                else:
                    tags_str = ''
                self._query_url = f'http://{tsd_addr}/api/query?start={start}&end={end}&m={aggregate}{sample}:{metric}{tags_str}'
            else:
                get_tuid_url = f'http://{tsd_addr}/api/query/last?timeseries={metric}'
                LOG.debug(f'get_tuid_url is {get_tuid_url}')
                tuids_data = urllib.request.urlopen(get_tuid_url)
                tuids_response = tuids_data.read()
                tuids_content = tuids_response.decode('ascii')
                tuids_content = json.loads(tuids_content)
                tuids = [_['tsuid'] for _ in tuids_content]
                tuids_str = ",".join(tuids)
                self._query_url = f'http://{tsd_addr}/api/query/last?tsuids={tuids_str}&back_scan=24&resolve=true'
                
        return self._query_url

    def run(self):
        """
        1）通过http请求tsd拿到指标的数值
        2）对比阈值条件，得到该告警每个分组的状态信息（是否告警）、以及该告警策略的状态信息
        3）将策略及其每个分组的状态保存到redis和mongo，如果状态发送变化的，则发送通知（告警产生/撤销）
        :return:
        """
        self.check()
        task_time = self.task_time
        strategy_id = str(self.strategy['_id'])
        q_url = self._query_url
        LOG.debug(f'query url is {q_url}')
        res = requests.get(self.query_url).json()   # TODO: 超时处理/连接池
        LOG.debug('Request: {}, Got: {}'.format(self.query_url, res))
        group_state = {}
        flag_strategy_ok = True
        if not res:
            strategy_state = {
                'status': STATUS_ALERT,
                'timestamp': int(task_time),
            }
        else:
            if self.strategy['latest'] == False:
                '''
                This part is for non latest alert, because of the query url is different 
                '''
                LOG.debug('in the none latest handler')
                for data in res:
                    # 分组信息
                    group = {k: v for k, v in data['tags'].items() if k in self.groups_keys}
                    group_str = ','.join([f'{k}={v}' for k, v in group.items()])
                    # 分组的实际结果
                    dps_values = list(data.get('dps', {}).values())
                    if not dps_values:
                        continue
                    else:
                        real_value = float(format(dps_values[0], '0.2f'))
                    # 真实值与阈值比较
                    comparison = self.strategy.get('tsd_rule', {}).get('comparison', '==')
                    threshold = self.strategy.get('tsd_rule', {}).get('threshold', '0')
                    if self.comp(real_value, comparison, threshold):
                        flag_strategy_ok = False
                        state = {'status': STATUS_ALERT}
                    else:
                        state = {'status': STATUS_OK}
                    # 保存该分组状态，字典序列化json
                    state['timestamp'] = int(task_time)
                    state['info'] = self.notify_message(comparison, threshold, real_value)
                    group_state[group_str] = state
                    LOG.debug(f'group: {group_str}, state: {state}')
                # 更新该策略的最新状态，写入redis，检查是否发送通知
                # 多个分组，记录OK或者Alert，以及分组keys
                strategy_state = {
                    'status': STATUS_OK if flag_strategy_ok else STATUS_ALERT,
                    'timestamp': int(task_time),
                    'group_keys': list(group_state.keys())  #每个分组group小项的信息
                }
                if len(list(group_state.keys())) == 1:
                    # 只有一个分组，分组的info即告警策略的info
                    group_info = list(group_state.values())[0]['info']
                    strategy_state.update({'info': group_info})
                # LOG.debug(f'strategy({strategy_id}) status: {strategy_state}')
            else:
                """
                This part is for latest query alert 
                待测试，需要把两种情况处理成函数进行处理
                """
                LOG.debug("in latest handler")
                for data in res:
                    group = {k: v for k, v in data['tags'].items() if k in self.groups_keys}
                    group_str = ','.join([f'{k}={v}' for k, v in group.items()])
                    comparison = self.strategy.get('tsd_rule', {}).get('comparison', '==')
                    threshold = self.strategy.get('tsd_rule', {}).get('threshold', '0')
                    value = data['value']
                    value = float(value)
                    value = round(value,2)
                    if self.comp(value,comparison,threshold):
                        flag_strategy_ok = False 
                        state = {'status': STATUS_ALERT}
                    else:
                        state = {'status': STATUS_OK}
                    state['timestamp'] = int(task_time)
                    state['info'] = self.notify_message(comparison,threshold,value)
                    group_state[group_str] = state
                    strategy_state = {
                    'status': STATUS_OK if flag_strategy_ok else STATUS_ALERT,
                    'timestamp': int(task_time),
                    'group_keys': list(group_state.keys())  #每个分组group小项的信息
                    }
                    if len(list(group_state.keys())) == 1:
                    # 只有一个分组，分组的info即告警策略的info
                        group_info = list(group_state.values())[0]['info']
                        strategy_state.update({'info': group_info})
        LOG.debug(f'strategy({strategy_id}) status: {strategy_state}')
        r = self.redis_cli
        # 更新redis中的策略状态
        r.set(f'strategy:{strategy_id}:state', json.dumps(strategy_state))
        aggregation = self.strategy['aggregation']
        strategy_notice = self.check_group_state(self.strategy, task_time, group_state,aggregation)
        if strategy_notice:
            self.set_strategy_status(strategy_id=strategy_id, status=strategy_state['status'])


@register_handler
class ChainBaseAlertHandler(IHandler):
    TYPE = 'ChainBase'

    def __init__(self, *args, **kwargs):
        IHandler.__init__(self, *args, **kwargs)
        self._query_url = ''
        self._former_query_url = ''
        self._tag_keys = []
        self._group = ''
        self._default_aggregate = 'sum'

    def notify_message(self, relation, diff_value=None, threshold=None, result=None):
        # return f'告警条件: {comparison} {threshold}, 检查值: {real_value}'
        if relation not in Chain_base_not_threhold_list:
            show_diff = diff_value * 100
            return f'告警条件: {relation} {threshold}%, 检查值: {show_diff}%'
        else:
            return f'告警条件: {relation}, 检查值: {result}'

    @staticmethod
    def comp(now_val, relation, former_val, threshold=None):
        """比较符判断"""
        # return literal_eval(f'float({real_val}) {comparison} float({expect_val})')
        if relation in Chain_base_not_threhold_list:
            if relation == 'equal':
                result = ( now_val == former_val )
            else:
                result = ( now_val != former_val)
            return (result,None)
        else:
            n = float(now_val)
            f = float(former_val)
            real_diff = abs(n-f)
            percentage = float((real_diff/f))
            real_threshold = ( threshold / 100)
            result = ( percentage > real_threshold)
            return (result,percentage)

    @property
    def query_url(self):
        """
        query url 构造，环比上暂时不支持最新值的查询

        constr query url,so far not supprt the latest value query
        """
        if not self._query_url:
            tsd_addr = self.tsd_addr
            tsd_rule = self.strategy.get('tsd_rule', {})
            metric = tsd_rule.get('metric', '')
            hb_time_interval = tsd_rule.get('hb_interval')
            hb_time_unit = tsd_rule.get('hb_unit')
            time_diff = hb_time_interval * self.unit_to_seconds(hb_time_unit)
            if self.strategy['latest'] == False:
                start, end = self.query_time()
                sample = tsd_rule.get('sample', '')
                aggregate = self._default_aggregate
                if sample:
                    sample = f':0all-{sample}'
                tags = tsd_rule.get('group', [])
                if tags:
                    tags_str = '{' + ','.join([f'{kv["key"]}={kv["value"]}' for kv in tags]) + '}'
                else:
                    tags_str = ''
                self._query_url = f'http://{tsd_addr}/api/query?start={start}&end={end}&m={aggregate}{sample}:{metric}{tags_str}'
                former_start = start - time_diff
                former_end = end - time_diff
                self._former_query_url = f'http://{tsd_addr}/api/query?start={former_start}&end={former_end}&m={aggregate}{sample}:{metric}{tags_str}'
            # else:
            #     get_tuid_url = f'http://{tsd_addr}/api/query/last?timeseries={metric}'
            #     LOG.debug(f'get_tuid_url is {get_tuid_url}')
            #     tuids_data = urllib.request.urlopen(get_tuid_url)
            #     tuids_response = tuids_data.read()
            #     tuids_content = tuids_response.decode('ascii')
            #     tuids_content = json.loads(tuids_content)
            #     tuids = [_['tsuid'] for _ in tuids_content]
            #     tuids_str = ",".join(tuids)
            #     self._query_url = f'http://{tsd_addr}/api/query/last?tsuids={tuids_str}&back_scan=24&resolve=true'
                
        return (self._query_url,self._former_query_url)


    def get_compare_former_data(self,tag_dict,out_data):
        ## 获取与现在需要对比组具有相同tag的数据
        ## get the same data with same tags on it 
        old_dps_values = None
        for f_data in out_data:
            n_keys = tag_dict.keys()
            # n_tags = tag_dict.items()
            f_tags = f_data['tags']
            f_keys = f_tags.keys()
            FULL_tag = True
            for t in f_keys:
                if t not in n_keys:
                    FULL_tag = False
                else:
                    if f_tags[t] != tag_dict[t]:
                        FULL_tag = False
            if FULL_tag:
                old_dps_values = list(f_data.get('dps',{}).values())
                break
            else:
                continue
        return old_dps_values


    def get_relation_and_threshold(self):
        chain_relation = self.strategy.get('tsd_rule',{}).get('chain_relation')
        threshold = 0
        if chain_relation not in Chain_base_not_threhold_list:
            threshold = float(self.strategy.get('tsd_rule', {}).get('threshold', None))
        else:
            threshold = None
        return (chain_relation,threshold)


    def compare_not_latest(self, now_data, former_data):
        flag_strategy_ok = True
        task_time = self.task_time
        group_state = {}
        LOG.debug('in the none latest handler')
        for data in now_data:
            # 分组信息
            group = {k: v for k, v in data['tags'].items() if k in self.groups_keys}
            group_str = ','.join([f'{k}={v}' for k, v in group.items()])
            # 分组的实际结果
            dps_values = list(data.get('dps', {}).values())
            former_dps_values = self.get_compare_former_data(group,former_data)
            if not dps_values or not former_dps_values:
                continue
            else:
                real_value = float(format(dps_values[0], '0.2f'))
                former_value = float(format(former_dps_values[0],'0.2f'))
            chain_relation,threshold = self.get_relation_and_threshold()
            result,diff_value = self.comp(real_value, chain_relation, former_value,threshold)
            if result:
                flag_strategy_ok = False
                state = {'status': STATUS_ALERT}
            else:
                state = {'status': STATUS_OK}
            # 保存该分组状态，字典序列化json
            state['timestamp'] = int(task_time)
            ## bug in 610line
            if chain_relation not in Chain_base_not_threhold_list:
                state['info'] = self.notify_message(relation=chain_relation,diff_value=diff_value,threshold=threshold)
                # state['info'] = self.notify_message(chain_relation, threshold, diff_value)
            else:
                state['info'] = self.notify_message(chain_relation, result=result) 
            group_state[group_str] = state
            LOG.debug(f'group: {group_str}, state: {state}')
        # 更新该策略的最新状态，写入redis，检查是否发送通知
        # 多个分组，记录OK或者Alert，以及分组keys
        strategy_state = {
            'status': STATUS_OK if flag_strategy_ok else STATUS_ALERT,
            'timestamp': int(task_time),
            'group_keys': list(group_state.keys())  #每个分组group小项的信息
        }
        if len(list(group_state.keys())) == 1:
            # 只有一个分组，分组的info即告警策略的info
            group_info = list(group_state.values())[0]['info']
            strategy_state.update({'info': group_info})
        return strategy_state,group_state


    def run(self):
        """
        1）通过http请求tsd拿到指标的数值
        2）对比阈值条件，得到该告警每个分组的状态信息（是否告警）、以及该告警策略的状态信息
        3）将策略及其每个分组的状态保存到redis和mongo，如果状态发送变化的，则发送通知（告警产生/撤销）
        ##TODO 需要修改成适合环比的条件进行
        :return:
        """
        # self.check()
        task_time = self.task_time
        strategy_id = str(self.strategy['_id'])
        q_url,former_q_url  = self.query_url
        LOG.debug(f'query url is {q_url}')
        res = requests.get(q_url).json()  # TODO: 超时处理/连接池
        # res = json.loads(res)
        LOG.debug('Request: {}, Got: {}'.format(q_url, res))
        # urllib.urlopen()
        former_res_origin = urllib.request.urlopen(former_q_url).read()
        former_res = json.loads(former_res_origin)
        LOG.debug('former_res has get')
        group_state = {}
        # flag_strategy_ok = True
        if not res or not former_res:
            # 无数据，默认为OK # TODO: 无数据，是否应该设置为Unknown状态？
            strategy_state = {
                'status': STATUS_ALERT,
                'timestamp': int(task_time),
            }
        else:
            if self.strategy['latest'] == False:
                '''
                This part is for non latest alert, because of the query url is different 
                '''

                strategy_state,group_state = self.compare_not_latest(res,former_res)
                # strategy_state,group_state = do_compare_not_latest(res,former_res)

            # else:
            #     """
            #     This part is for latest query alert 
            #     
            #     """
            #     LOG.debug("in latest handler")
            #     for data in res:
            #         group = {k: v for k, v in data['tags'].items() if k in self.groups_keys}
            #         group_str = ','.join([f'{k}={v}' for k, v in group.items()])
            #         comparison = self.strategy.get('tsd_rule', {}).get('comparison', '==')
            #         threshold = self.strategy.get('tsd_rule', {}).get('threshold', '0')
            #         value = data['value']
            #         value = float(value)
            #         value = round(value,2)
            #         if self.comp(value,comparison,threshold):
            #             flag_strategy_ok = False 
            #             state = {'status': STATUS_ALERT}
            #         else:
            #             state = {'status': STATUS_OK}
            #         state['timestamp'] = int(task_time)
            #         state['info'] = self.notify_message(comparison,threshold,value)
            #         group_state[group_str] = state
            #         strategy_state = {
            #         'status': STATUS_OK if flag_strategy_ok else STATUS_ALERT,
            #         'timestamp': int(task_time),
            #         'group_keys': list(group_state.keys())  #每个分组group小项的信息
            #         }
            #         if len(list(group_state.keys())) == 1:
            #         # 只有一个分组，分组的info即告警策略的info
            #             group_info = list(group_state.values())[0]['info']
            #             strategy_state.update({'info': group_info})

        LOG.debug(f'strategy({strategy_id}) status: {strategy_state}')
        r = self.redis_cli
        # 更新redis中的策略状态
        r.set(f'strategy:{strategy_id}:state', json.dumps(strategy_state))
        aggregation = self.strategy['aggregation']
        strategy_notice = self.check_group_state(self.strategy, task_time, group_state,aggregation)
        if strategy_notice:
            self.set_strategy_status(strategy_id=strategy_id, status=strategy_state['status'])


# @register_handler
# class AlwaysAlertHandler(IHandler):
#     """总是告警的策略类型（用于测试）"""
#     TYPE = 'test'
#     _status = STATUS_OK

#     def change_status(self):
#         if self._status == STATUS_OK:
#             self._status = STATUS_ALERT
#         else:
#             self._status = STATUS_OK

#     def run(self):
#         """"""
#         self.change_status()
#         status = self._status
#         strategy_id = str(self.strategy['_id'])
#         task_time = int(self.task_time)
#         strategy_state = {
#             'status': status,
#             'timestamp': task_time,
#             'info': 'Alert happened.' if status == STATUS_ALERT else 'Alert recovered.'
#         }
#         # 更新mongodb
#         self.set_strategy_status(
#             strategy_id=strategy_id,
#             status='on' if status == STATUS_OK else 'alert'
#         )
#         # 更新到redis
#         r = self.redis_cli
#         r.set(f'strategy:{strategy_id}:state', json.dumps(strategy_state))
#         # 发布到redis队列
#         alert_type = '告警产生' if status == STATUS_ALERT else '告警撤销'
#         message = {
#             'strategy_id': strategy_id,
#             'strategy_name': self.strategy['property']['name'],
#             'alert_time': task_time,
#             'alert_info': f'【{alert_type}】\nInfo: {strategy_state["info"]}',
#             'is_recover': True if status == STATUS_OK else False,
#         }
#         channels = self.get_channel()
#         for channel in channels:
#             if channel == 'notice:slack':
#                 slack = self.get_slack_attr()
#                 message.update(**slack)
#             elif channel == 'notice:mail':
#                 mail = self.get_mail_attr()
#                 message.update(**mail)
#             r.publish(channel, json.dumps(message))
#             LOG.debug(f'Message is published by channel({channel}) => {message}')


# @register_handler
# class DSLHandler(IHandler):
#     """处理dsl语法定义的告警策略"""
#     TYPE = 'dsl'

#     def run(self):
#         """"""
#         # TODO


if __name__ == '__main__':
    pass
    # import aiohttp
    #
    # res = []
    #
    # async def foo():
    #     async with aiohttp.ClientSession() as s:
    #         async with s.get('http://localhost:8888/api/alert/version') as resp:
    #             res.append(await resp.json())
    #
    #
    # loop = asyncio.get_event_loop()
    # loop.run_until_complete(asyncio.wait([foo()]))
    # loop.close()



