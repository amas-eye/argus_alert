# coding=utf-8
"""通知处理实现
"""
import json
import copy
from datetime import datetime

from pymongo import MongoClient

from argus_alert.core.utils.log import timed_logger
from argus_alert.core.notice.slack import send_slack_via_hook
from argus_alert.core.notice.wechat import send_wechat

from bson.objectid import ObjectId

## for vscode server debug
# import ptvsd
# ptvsd.settrace(None,('0.0.0.0',13999))

LOG = timed_logger()


def notify_worker(ctx):
    """
    根据channel返回对应渠道的通知处理对象

    :param ctx:
        {'item': <reids队列通知对象>, 'mongo_addr': <mongo地址>}
    :return:
    """
    return {
        'notice:db': DBNotifier(ctx),
        'notice:wechat': WechatNotifier(ctx),
        'notice:mail': MailNotifier(ctx),
        'notice:api': ApiNotifer(ctx),
        'notice:sms': SMSNotifier(ctx),
        'notice:slack': SlackNotifier(ctx)
    }.get(ctx['item']['channel'], DBNotifier(ctx))


class Notifier(object):
    """通知处理对象，需要实现send方法
    self.data对应告警的详细内容（json反序列化的字典）
    """

    def __init__(self, ctx):
        self._ctx = ctx
        try:
            self.data = json.loads(self._ctx['item']['data'])
        except TypeError:
            # 如：TypeError: the JSON object must be str, bytes or bytearray, not 'int'
            self.data = {}
            LOG.error('Ignore abnormal data from redis!')
        self.mongo_addr = self._ctx['mongo_addr']

    def Insert_into_mongo(self,client,query,record,aggregation):
        query_result = client.find_one(query)
        if query_result != None and record['is_recover'] == True:
            '''
             如果存在记录，则对存在记录进行更新，否则插入,需要把之前的alert_time进行保存，因此此处是对单条host或者其他的key
             进行处理，因此判断条件需要增加
            '''
            LOG.debug('record is_recover true')
            real_alert_time = query_result["alert_time"]
            record["alert_time"] = real_alert_time
            if aggregation == False:
                res = client.update(query, {'$set': {'is_recover': record['is_recover'],
                                                     'recover_time': record['recover_time'],
                                                     'alert_info': record['alert_info']
                                                     }})
            else:
                res = client.update(query, {'$set': {'is_recover': record['is_recover'],
                                                        'recover_time': record['recover_time'],
                                                        'alertInfos': record['alertInfos']
                                                        }})
            LOG.debug('a existed alert is recovered')
        elif query_result != None and record['is_recover'] == False:
            LOG.debug('a alert is exist and not recover')
        elif query_result == None and record['is_recover'] == False:
            res = client.insert_one(record)
            LOG.debug('a new alert is created')

    def send_mongo(self):
        """
        将告警通知插入到mongodb
        Insert alert item into mongodb
        """
        try:
            mongo_cli = MongoClient(self.mongo_addr)
            db = mongo_cli['argus-alert']
            collection = db['alert_history']
            LOG.debug('self.data')
            LOG.debug(self.data)
            d = self.data['message']
            # LOG.debug('self.data')
            # LOG.debug(self.data)
            record = {
                    'strategy_id': self.data['message']['strategy_id'],
                    'strategy_name': self.data['message']['strategy_name'],
                    'alert_time': self.data['message']['alert_time'],
                    'is_recover': self.data['message']['is_recover'],
                    'level': self.data['message']['level'],
                    'group': self.data['message']['group'],
                    'type':self.data['message']['type']
                }
            if self.data['aggregation'] == False:
                LOG.debug('in aggregation false')
                record['alert_info'] = self.data['message']['alert_info']
                record['aggregation'] = False
                
                if record['is_recover'] == True:
                    record["recover_time"] = self.data['message']['alert_time']
                query = {"strategy_id": self.data['message']['strategy_id'], "is_recover": False,\
                        "group":self.data['message']['group']
                }
                LOG.debug(f'query is{query}')
                self.Insert_into_mongo(collection,query,record,False)
            elif self.data['aggregation'] == True:
                LOG.debug('in aggregation True')
                record['alertInfos'] = self.data['message']['alert_info']
                record['aggregation'] = True
               
                if record['is_recover'] == True:
                    record["recover_time"] = self.data['message']['alert_time']
                query = {
                    "strategy_id": self.data['message']['strategy_id'], "is_recover": False,\
                    "aggregation":True
                }
                LOG.debug(f'record in aggregation is {record}')
                LOG.debug(f'query is{query}')
                self.Insert_into_mongo(collection,query,record,True)     
        except Exception as e:
            LOG.error(e, exc_info=True)

    def send(self):
        raise NotImplementedError


class DBNotifier(Notifier):
    """入库通知"""

    def send(self):
        self.send_mongo()


class SlackNotifier(Notifier):
    """发送到Slack"""

    def send(self):
        self.send_mongo()
        LOG.debug('self.data')
        LOG.debug(self.data)
        if self.data['aggregation'] == False:
            text = '告警策略：{}\n告警时间：{}\n告警内容：{}'.format(
                self.data['message']['strategy_name'],
                datetime.fromtimestamp(self.data['message']['alert_time']),
                self.data['message']['alert_info']+self.data['message']['group']
            )
            send_slack_via_hook(hook_url=self.data['slack_hook'], text=text)
            LOG.info(f'Sent to slack: {text}')
        else:
            alert_message = [ _['alert_info'] +'&' +_['group'] for _ in self.data['message']['alert_info']]
            alert_message_str = '\n'.join(alert_message)
            text = '告警策略：{}\n告警时间：{}\n告警内容：{}...'.format(
                self.data['message']['strategy_name'],
                datetime.fromtimestamp(self.data['message']['alert_time']),
                alert_message_str
            )
            send_slack_via_hook(hook_url=self.data['slack_hook'], text=text)
            LOG.info(f'Sent to slack: {text}')


class MailNotifier(Notifier):
    """邮件通知"""

    def send(self):
        # TODO: 把touser修改成username，数据类型为list，里面放置的是用户的username
        self.send_mongo()
        LOG.info('Sent by mail: {}'.format(self.data))


class WechatNotifier(Notifier):
    """微信通知"""

    def send(self):
        # TODO: 把touser修改成username，数据类型为list，里面放置的是用户的username
        self.send_mongo()
        strategy_id = self.data['message']['strategy_id']
        mongo_cli = MongoClient(self.mongo_addr)
        strategy_collection = mongo_cli['argus-alert']['strategy']
        recover_status = self.data['message']['is_recover']
        if self.data['message']['level'] == 'minor':
            chinese_level = '一般'
        else:
            chinese_level = '严重'
        user_collection = mongo_cli['argus-users']['users']
        record = strategy_collection.find_one({"_id": ObjectId(strategy_id)})
        push_user_origin = []
        # push_user_origin = [check_user for check_user in record['notify']['notify_group']['group_names_check'] if check_user != "" ]
        for check_user in record['notify']['notify_group']:
            for user in check_user['group_names_check']:
                if user:
                    push_user_origin.append(user)
        tmp_user_list = user_collection.find({"username": {"$in": push_user_origin}})
        push_user_openid = [user['wechat_id'] for user in tmp_user_list]
        push_content = []

        push_content_model = {
            'touser': '',
            'template_id': 'gd-KEIOk9oEvRDWa_nRHqj3ELqogYtPK7laL8qyZ8vg',
            'data': {
                'keyword1': {
                    'value': '广州优亿信息科技有限公司',
                },
                'keyword2': {
                    'value': record['property']['name'],
                },
                'keyword3': {
                    'value': '',
                    'color': ''
                },
                'keyword4': {
                    'value': chinese_level,
                    'color': ''
                },
                'keyword5': {
                    'value': record['tsd_rule']['metric'],
                    'color': ''
                },
                'remark': {
                    'value': '',
                    'color': ''
                }
            }
        }

        if recover_status == True:
            color = '#00CD00'
            push_content_model['data']['keyword3']['value'] = '告警恢复'
            LOG.debug('self.data in notify, recovered')
            LOG.debug(self.data)
            if self.data['aggregation'] == False:
                push_content_model['data']['remark']['value'] = self.data['message']['alert_info']+'\n' \
                +self.data['message']['group'] +'\n备注：告警已恢复'
            else:
                alert_message = [ _['alert_info'] +'&'+ _['group']+'\n' for _ in self.data['message']['alert_info']]
                alert_message_str = '\n'.join(alert_message)
                push_content_model['data']['remark']['value'] = '此告警为聚合告警\n'+alert_message_str + '\n备注：告警产生，请尽快处理'

        else:
            color = '#FF0000'
            push_content_model['data']['keyword3']['value'] = '告警产生'
            LOG.debug('self.data in notify')
            LOG.debug(self.data)
            if self.data['aggregation'] == False:
                push_content_model['data']['remark']['value'] = self.data['message']['alert_info']\
                +'\n'+self.data['message']['group']+ '\n备注：告警产生，请尽快处理'
            else:
                alert_message = [ _['alert_info'] +'&'+ _['group']+'\n' for _ in self.data['message']['alert_info']]
                alert_message_str = '\n'.join(alert_message)
                push_content_model['data']['remark']['value'] = '此告警为聚合告警\n'+alert_message_str + '\n备注：告警产生，请尽快处理'

        for key in push_content_model['data']:
            if 'color' in push_content_model['data'][key]:
                push_content_model['data'][key]['color'] = color

        for openid in push_user_openid:
            model = copy.deepcopy(push_content_model)
            LOG.debug('model id')
            LOG.debug(id(model))
            model['touser'] = openid
            # push_content.insert(0,model)
            push_content.append(model)
            # LOG.debug('model is {model}')
            LOG.debug(f'openid is {openid}')
            # model = None

        LOG.debug(f'push_content is {push_content}')
        send_wechat(push_content)
        LOG.info('Sent by wechat: {}'.format(self.data))


class ApiNotifer(Notifier):
    """API回调"""

    def send(self):
        # TODO
        LOG.info('Sent by api: {}'.format(self.data))


class SMSNotifier(Notifier):
    """短信通知"""

    def send(self):
        LOG.info('Sent by sms: {}'.format(self.data))


if __name__ == '__main__':
    pass
