# coding=utf-8
"""启动配置文件
"""
import os
from multiprocessing import cpu_count

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DefaultConfig(object):
    """默认配置，可通过指定启动场景参数（生产/开发/测试）覆盖"""
    OPENTSDB_ADDR = 'opentsdb:4242'
    REDIS_ADDR = 'redis:///@redis:6379/0'
    MONGO_ADDR = 'mongodb://mongo:27017/'

    DRIVER_HOST = ''
    DRIVER_PORT = 7001
    AUTH_KEY = 'argus@useease'
    EXECUTOR_WORKER = cpu_count()
    NOTIFIER_WORKER = cpu_count()
    PUSH_ALERT_URL = "http://114.215.85.142/argus-internal/controller/push_alert"

class ProdConfig(DefaultConfig):
    """生产环境"""


class TestConfig(DefaultConfig):
    """测试环境"""
    OPENTSDB_ADDR = '10.17.35.43:4242'
    MONGO_ADDR = 'mongodb://10.17.35.43:27017/'


class DevConfig(DefaultConfig):
    """开发环境"""
    EXECUTOR_WORKER = 1
    NOTIFIER_WORKER = 1


class RunningConfig(object):
    """运行时配置"""
    LOG_LEVEL = 'DEBUG'
    LOG_DIR = os.path.join(BASE_DIR, 'logs')

    EXECUTOR_COROUTINES = 2
    EXECUTOR_THREADS = 1

    NOTIFIER_THREADS = 1

