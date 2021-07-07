import os
import json
import shutil
import re
import traceback
import random
import sys
import time
from datetime import datetime as timezone
from datetime import timedelta
from multiprocessing import cpu_count
from ansible import constants as C  # 用于获取ansible内置的一些常量
from ansible.module_utils.common.collections import ImmutableDict  # 用于自定制一些选项
from ansible import context  # 上下文管理器，他就是用来接收 ImmutableDict 的示例对象
from ansible.parsing.dataloader import DataLoader  # 解析 json/ymal/ini 格式的文件
from ansible.vars.manager import VariableManager  # 管理主机和主机组的变量
from ansible.playbook.play import Play  # 用于执行 Ad-hoc 的核心类，即ansible相关模块，命令行的ansible -m方式
from ansible.executor.task_queue_manager import TaskQueueManager  # ansible 底层用到的任务队列管理器
from ansible.inventory.manager import InventoryManager
from ansible.executor.playbook_executor import PlaybookExecutor  # 执行 playbook 的核心类，即命令行的ansible-playbook *.yml
from ansible.inventory.host import Host  # 单台主机类
from ansible.plugins.callback import CallbackBase  # 回调基类，处理ansible的成功失败信息，这部分对于二次开发自定义可以做比较多的自定义


ANSIBLE_CONNECTION_TYPE = 'paramiko'

ANSIBLE_DENY_VARIBLE_LISTS = [
    "vars",
    "hostvars",
    "ansible_ssh_pass",
    "ansible_password",
    "ansible_ssh_private_key_file",
    "ansible_private_key_file",
    "ansible_become_pass",
    "ansible_become_password",
    "ansible_enable_pass",
    "ansible_pass",
    "ansible_sudo_pass",
    "ansible_sudo_password",
    "ansible_su_pass",
    "ansible_su_password",
    "vault_password",
]


# 回调类
class CallbackModule(CallbackBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.host_ok = []
        self.host_unreachable = []
        self.host_failed = []
        self.error = ''

    def v2_runner_on_unreachable(self, result):
        # print(result.__dict__.items())
        self.host_unreachable.append({'host': result._host.name, 'task_name': result.task_name,
                                      'result': result._result, 'success': False, 'msg': 'unreachable'})

    def v2_runner_on_ok(self, result, *args, **kwargs):
        # print(result.__dict__.items())
        self.host_ok.append({'host': result._host.name, 'task_name': result.task_name,
                             'result': result._result, 'success': True, 'msg': 'ok'})


    def v2_runner_on_failed(self, result, *args, **kwargs):
        # print(result.__dict__.items())
        self.host_failed.append({'host': result._host.name, 'task_name': result.task_name,
                                 'result': result._result, 'success': False, 'msg': 'failed'})

    def v2_playbook_on_no_hosts_matched(self):
        self.error = 'skipping: No match hosts.'

    def get_res(self):
        return self.host_ok, self.host_failed, self.host_unreachable, self.error


# 主机类
class BaseHost(Host):
    """
    处理单个主机
    """

    def __init__(self, host_data):
        self.host_data = host_data
        hostname = host_data.get('hostname') or host_data.get('ip')
        port = host_data.get('port') or 22
        super().__init__(hostname, port)
        self.__set_required_variables()
        self.__set_extra_variables()

    def __set_required_variables(self):
        host_data = self.host_data

        # 设置 connection 插件连接方式
        self.set_variable('ansible_connection', ANSIBLE_CONNECTION_TYPE)

        # ssh 连接参数，提升速度， 仅到连接插件为 ssh 时生效，paramiko 模式下不生效
        if ANSIBLE_CONNECTION_TYPE == 'ssh':
            self.set_variable('ansible_ssh_args', '-C -o ControlMaster=auto -o ControlPersist=60s')

        # self.set_variable('ansible_host_key_checking', False)
        self.set_variable('ansible_ssh_host_key_checking', False)
        self.set_variable('ansible_host', host_data['ip'])
        self.set_variable('ansible_port', host_data['port'])

        if host_data.get('username'):
            self.set_variable('ansible_user', host_data['username'])

        # 添加密码和秘钥
        if host_data.get('password'):
            # self.set_variable('ansible_ssh_pass', decrypt(host_data['password']))
            self.set_variable('ansible_ssh_pass', host_data['password'])
        if host_data.get('private_key'):
            self.set_variable('ansible_ssh_private_key_file', host_data['private_key'])

        if ANSIBLE_CONNECTION_TYPE == 'ssh':
            self.set_variable('ansible_ssh_pipelining', True)

        # 添加become支持
        become = host_data.get('become', False)
        if become:
            self.set_variable('ansible_become', True)
            self.set_variable('ansible_become_method', become.get('method', 'sudo'))
            if become.get('method', 'sudo') == 'sudo':
                if ANSIBLE_CONNECTION_TYPE == 'ssh':
                    # ansible_ssh_pipelining 可以加快执行速度，但是不兼容 sudo，仅到连接插件为 ssh 时生效，paramiko 不生效
                    self.set_variable('ansible_ssh_pipelining', False)
            self.set_variable('ansible_become_user', become.get('user', 'root'))
            self.set_variable('ansible_become_pass', become.get('pass', ''))
        else:
            self.set_variable('ansible_become', False)

    def __set_extra_variables(self):
        for k, v in self.host_data.get('vars', {}).items():
            self.set_variable(k, v)

    def __repr__(self):
        return self.name


# 资源类
class BaseInventory(InventoryManager):
    """
    生成 Ansible inventory 对象
    """
    loader_class = DataLoader
    variable_manager_class = VariableManager
    host_manager_class = BaseHost

    def __init__(self, host_list=None):
        if host_list is None:
            host_list = []
        self.host_list = host_list
        assert isinstance(host_list, list)
        self.loader = self.loader_class()
        self.variable_manager = self.variable_manager_class()
        super().__init__(self.loader)

    def get_groups(self):
        return self._inventory.groups

    def get_group(self, name):
        return self._inventory.groups.get(name, None)

    def parse_sources(self, cache=False):
        group_all = self.get_group('all')
        ungrouped = self.get_group('ungrouped')

        for host_data in self.host_list:
            host = self.host_manager_class(host_data=host_data)
            self.hosts[host_data['hostname']] = host
            groups_data = host_data.get('groups')
            if groups_data:
                for group_name in groups_data:
                    group = self.get_group(group_name)
                    if group is None:
                        self.add_group(group_name)
                        group = self.get_group(group_name)
                    group.add_host(host)
            else:
                ungrouped.add_host(host)
            group_all.add_host(host)

    def get_matched_hosts(self, pattern):
        return self.get_hosts(pattern)


# 运行类
class AnsibleAPI:

    def __init__(self, check=False, remote_user='root', private_key_file=None, forks=cpu_count() * 2,extra_vars=None, dynamic_inventory=None, callback=None):
        """
        可以选择性的针对业务场景在初始化中加入用户定义的参数
        """
        # 运行前检查，即命令行的-C
        self.check = check
        # key登陆文件
        self.private_key_file = private_key_file
        # 并发连接数
        self.forks = forks
        # 远端登陆用户
        self.remote_user = remote_user
        # 数据解析器
        self.loader = DataLoader()
        # 必须有此参数，假如通过了公钥信任，可以为空dict
        self.passwords = {}
        # 回调结果
        self.results_callback = callback
        # 组和主机相关，处理动态资产
        self.dynamic_inventory = dynamic_inventory
        # 变量管理器
        self.variable_manager = VariableManager(loader=self.loader, inventory=self.dynamic_inventory)
        self.variable_manager._extra_vars = extra_vars if extra_vars is not None else {}
        # 自定义选项的初始化
        self.__init_options()

    def __init_options(self):
        """
        自定义选项，不用默认值的话可以加入到__init__的参数中
        """
        # constants里面可以找到这些参数，ImmutableDict代替了较老的版本的nametuple的方式
        context.CLIARGS = ImmutableDict(
            # connection 表示与主机的连接类型.比如: local, ssh 或者 paramiko.
            # 如果设置为 local 的话只会在配置管理的节点上执行，不会在远程主机执行
            # ssh 连接类型需要管理节点安装了 sshpass
            # Ansible1.2 以前默认使用 paramiko.
            # 使用 paramiko 使用主要在主机变量中设置 ansible_ssh_host_key_checking 或者 ansible_host_key_checking 为 True
            # 1.2 以后默认使用 smart, smart 方式会根据是否支持 ControlPersist, 来判断 ssh 方式是否可行.
            connection=ANSIBLE_CONNECTION_TYPE,  # 设置全局默认连接插件，主机还可以使用变量ansible_connection设置
            remote_user=self.remote_user,
            ack_pass=None,
            sudo=True,
            sudo_user='root',
            ask_sudo_pass=False,
            module_path=None,
            become=True,
            become_method='sudo',
            become_user='root',
            check=self.check,
            listhosts=None,
            listtasks=None,
            listtags=None,
            syntax=None,
            diff=True,
            subset=None,
            timeout=15,
            private_key_file=self.private_key_file,
            host_key_checking=False,
            forks=self.forks,
            ssh_common_args='-o StrictHostKeyChecking=no',
            ssh_extra_args='-o StrictHostKeyChecking=no',
            verbosity=0,
            start_at_task=None,
        )

    @classmethod
    def check_ansible_variable(cls, content):
        """
        防止执行 ansible 任务时使用类似内置变量 {{ ansible_ssh_pass }} 等获取到主机密码
        """
        content = content.replace('vars:', '+++')  # ansible playbook 中内置参数 vars 和 vars_files 与变量 vars 冲突
        content = content.replace('vars_files:', '+++')
        pattern = re.compile(r'^[\S\s]*?(?P<ansible>[\S\s]?(%s)[\S\s]?)[\S\s]*$' %
                             '|'.join(ANSIBLE_DENY_VARIBLE_LISTS), re.I)
        res = pattern.search(content)
        if res:
            info = res.groupdict()
            info = info['ansible'].strip()
            if info.startswith('=') or info.startswith('{'):
                info = info[1:]
            if info.endswith('}'):
                info = info[:-1]
            if info in ANSIBLE_DENY_VARIBLE_LISTS:
                return True, info
        return False, None

    def run_playbook(self, playbook_yml, group=None, script=None):
        """
        运行 playbook
        """
        playbook = None
        try:
            with open(playbook_yml) as f:
                playbook_content = f.read()
            check, variable = self.check_ansible_variable(playbook_content)
            if check:
                data = '<code style="color: #FF0000">playbook 中包含非法 ansible 变量: [{}]，禁止运行</code>'.format(variable)
                data2 = '\033[01;31mplaybook 中包含非法 ansible 变量: [{}]，禁止运行\r\n\r\n\033[0m'.format(variable)
                delay = round(time.time() - self.results_callback.start_time, 6)
                self.results_callback.res.append(json.dumps([delay, 'o', data2]))
                message = dict()
                message['status'] = 0
                message['message'] = data
                message = json.dumps(message)
                print({
                          "type": "send.message",
                          "text": message,
                      }.__dict__.items())
            else:
                playbook = PlaybookExecutor(
                    playbooks=[playbook_yml],
                    inventory=self.dynamic_inventory,
                    variable_manager=self.variable_manager,
                    loader=self.loader,
                    passwords=self.passwords,
                )
                playbook._tqm._stdout_callback = self.results_callback
                playbook.run()
        except Exception as err:
            data = '<code style="color: #FF0000">{}</code>'.format(str(err))
            data2 = '\033[01;31m{}\r\n\r\n\033[0m'.format(str(err).strip().replace('\n', '\r\n'))
            delay = round(time.time() - self.results_callback.start_time, 6)
            self.results_callback.res.append(json.dumps([delay, 'o', data2]))
            message = dict()
            message['status'] = 0
            message['message'] = data
            message = json.dumps(message)
            print({
                "type": "send.message",
                "text": message,
            })
        finally:
            if group:
                message = dict()
                message['status'] = 0
                message['message'] = '执行完毕...'
                message = json.dumps(message)
                print({
                    "type": "close.channel",
                    "text": message,
                })
                # if self.results_callback.res:
                #     save_res(self.results_callback.res_file, self.results_callback.res)
                # batchcmd_log(
                #     user=self.results_callback.user,
                #     hosts=self.results_callback.hosts,
                #     cmd=self.results_callback.playbook,
                #     detail=self.results_callback.res_file,
                #     address=self.results_callback.client,
                #     useragent=self.results_callback.user_agent,
                #     start_time=self.results_callback.start_time_django,
                #     type=4,
                #     script=script,
                # )
            if playbook._tqm is not None:
                playbook._tqm.cleanup()

    def run_module(self, module_name, module_args, hosts='all'):
        """
        运行 module
        """
        play_source = dict(
            name='Ansible Run Module',
            hosts=hosts,
            gather_facts='no',
            tasks=[
                {'action': {'module': module_name, 'args': module_args}},
            ]
        )
        play = Play().load(play_source, variable_manager=self.variable_manager, loader=self.loader)
        tqm = None
        try:
            tqm = TaskQueueManager(
                inventory=self.dynamic_inventory,
                variable_manager=self.variable_manager,
                loader=self.loader,
                passwords=self.passwords,
                stdout_callback=self.results_callback,
            )
            tqm.run(play)
            # self.result_row = self.results_callback.result_row
        except Exception:
            print(traceback.format_exc())
        finally:
            if tqm is not None:
                tqm.cleanup()
            # 这个临时目录会在 ~/.ansible/tmp/ 目录下
            shutil.rmtree(C.DEFAULT_LOCAL_TMP, True)

    def run_modules(self, cmds, module='command', hosts='all', group=None):
        """
        运行命令有三种 raw 、command 、shell
        1.command 模块不是调用的shell的指令，所以没有bash的环境变量
        2.raw很多地方和shell类似，更多的地方建议使用shell和command模块。
        3.但是如果是使用老版本python，需要用到raw，又或者是客户端是路由器，因为没有安装python模块，那就需要使用raw模块了
        """
        try:
            check, variable = self.check_ansible_variable(cmds)
            if check:
                data = '<code style="color: #FF0000">参数中包含非法 ansible 变量: [{}]，禁止运行</code>'.format(variable)
                data2 = '\033[01;31m参数中包含非法 ansible 变量: [{}]，禁止运行\r\n\r\n\033[0m'.format(variable)
                delay = round(time.time() - self.results_callback.start_time, 6)
                self.results_callback.res.append(json.dumps([delay, 'o', data2]))
                message = dict()
                message['status'] = 0
                message['message'] = data
                message = json.dumps(message)
                print({
                    "type": "send.message",
                    "text": message,
                })
            else:
                module_name = module
                self.run_module(module_name, cmds, hosts)
        except Exception as err:
            data = '<code style="color: #FF0000">{}</code>'.format(str(err))
            data2 = '\033[01;31m{}\r\n\r\n\033[0m'.format(str(err).strip().replace('\n', '\r\n'))
            delay = round(time.time() - self.results_callback.start_time, 6)
            self.results_callback.res.append(json.dumps([delay, 'o', data2]))
            message = dict()
            message['status'] = 0
            message['message'] = data
            message = json.dumps(message)
            print({
                "type": "send.message",
                "text": message,
            })
        finally:
            if group:
                message = dict()
                message['status'] = 0
                message['message'] = '执行完毕...'
                message = json.dumps(message)
                print({
                    "type": "close.channel",
                    "text": message,
                })
                if self.results_callback.res:
                    save_res(self.results_callback.res_file, self.results_callback.res)
                # batchcmd_log(
                #     user=self.results_callback.user,
                #     hosts=self.results_callback.hosts,
                #     cmd=self.results_callback.cmd,
                #     detail=self.results_callback.res_file,
                #     address=self.results_callback.client,
                #     useragent=self.results_callback.user_agent,
                #     start_time=self.results_callback.start_time_django,
                #     type=5,
                # )

    def run_cmd(self, cmds, hosts='all', group=None):
        """
        运行命令有三种 raw 、command 、shell
        1.command 模块不是调用的shell的指令，所以没有bash的环境变量
        2.raw很多地方和shell类似，更多的地方建议使用shell和command模块。
        3.但是如果是使用老版本python，需要用到raw，又或者是客户端是路由器，因为没有安装python模块，那就需要使用raw模块了
        """
        try:
            check, variable = self.check_ansible_variable(cmds)
            if check:
                data = '<code style="color: #FF0000">参数中包含非法 ansible 变量: [{}]，禁止运行</code>'.format(variable)
                data2 = '\033[01;31m参数中包含非法 ansible 变量: [{}]，禁止运行\r\n\r\n\033[0m'.format(variable)
                delay = round(time.time() - self.results_callback.start_time, 6)
                self.results_callback.res.append(json.dumps([delay, 'o', data2]))
                message = dict()
                message['status'] = 0
                message['message'] = data
                message = json.dumps(message)
                print({
                    "type": "send.message",
                    "text": message,
                })
            else:
                module_name = 'shell'
                self.run_module(module_name, cmds, hosts)
        except Exception as err:
            data = '<code style="color: #FF0000">{}</code>'.format(str(err))
            data2 = '\033[01;31m{}\r\n\r\n\033[0m'.format(str(err).strip().replace('\n', '\r\n'))
            delay = round(time.time() - self.results_callback.start_time, 6)
            self.results_callback.res.append(json.dumps([delay, 'o', data2]))
            message = dict()
            message['status'] = 0
            message['message'] = data
            message = json.dumps(message)
            print({
                "type": "send.message",
                "text": message,
            })
        finally:
            if group:
                message = dict()
                message['status'] = 0
                message['message'] = '执行完毕...'
                message = json.dumps(message)
                print({
                    "type": "close.channel",
                    "text": message,
                })
                # if self.results_callback.res:
                #     save_res(self.results_callback.res_file, self.results_callback.res)
                # batchcmd_log(
                #     user=self.results_callback.user,
                #     hosts=self.results_callback.hosts,
                #     cmd=self.results_callback.cmd,
                #     detail=self.results_callback.res_file,
                #     address=self.results_callback.client,
                #     useragent=self.results_callback.user_agent,
                #     start_time=self.results_callback.start_time_django,
                # )

    def run_script(self, cmds, hosts='all', group=None, script=None):
        try:
            check, variable = self.check_ansible_variable(cmds)
            if check:
                data = '<code style="color: #FF0000">参数中包含非法 ansible 变量: [{}]，禁止运行</code>'.format(variable)
                data2 = '\033[01;31m参数中包含非法 ansible 变量: [{}]，禁止运行\r\n\r\n\033[0m'.format(variable)
                delay = round(time.time() - self.results_callback.start_time, 6)
                self.results_callback.res.append(json.dumps([delay, 'o', data2]))
                message = dict()
                message['status'] = 0
                message['message'] = data
                message = json.dumps(message)
                print({
                    "type": "send.message",
                    "text": message,
                })
            else:
                module_name = 'script'
                self.run_module(module_name, cmds, hosts)
        except Exception as err:
            data = '<code style="color: #FF0000">{}</code>'.format(str(err))
            data2 = '\033[01;31m{}\r\n\r\n\033[0m'.format(str(err).strip().replace('\n', '\r\n'))
            delay = round(time.time() - self.results_callback.start_time, 6)
            self.results_callback.res.append(json.dumps([delay, 'o', data2]))
            message = dict()
            message['status'] = 0
            message['message'] = data
            message = json.dumps(message)
            print({
                "type": "send.message",
                "text": message,
            })
        finally:
            if group:
                message = dict()
                message['status'] = 0
                message['message'] = '执行完毕...'
                message = json.dumps(message)
                print({
                    "type": "close.channel",
                    "text": message,
                })
                # if self.results_callback.res:
                #     save_res(self.results_callback.res_file, self.results_callback.res)
                # batchcmd_log(
                #     user=self.results_callback.user,
                #     hosts=self.results_callback.hosts,
                #     cmd=self.results_callback.cmd,
                #     detail=self.results_callback.res_file,
                #     address=self.results_callback.client,
                #     useragent=self.results_callback.user_agent,
                #     start_time=self.results_callback.start_time_django,
                #     type=2,
                #     script=script,
                # )

    def run_copy(self, cmds, hosts='all', group=None):
        try:
            check, variable = self.check_ansible_variable(cmds)
            if check:
                data = '<code style="color: #FF0000">参数中包含非法 ansible 变量: [{}]，禁止运行</code>'.format(variable)
                data2 = '\033[01;31m参数中包含非法 ansible 变量: [{}]，禁止运行\r\n\r\n\033[0m'.format(variable)
                delay = round(time.time() - self.results_callback.start_time, 6)
                self.results_callback.res.append(json.dumps([delay, 'o', data2]))
                message = dict()
                message['status'] = 0
                message['message'] = data
                message = json.dumps(message)
                print({
                    "type": "send.message",
                    "text": message,
                })
            else:
                module_name = 'copy'
                self.run_module(module_name, cmds, hosts)
        except Exception as err:
            data = '<code style="color: #FF0000">{}</code>'.format(str(err))
            data2 = '\033[01;31m{}\r\n\r\n\033[0m'.format(str(err).strip().replace('\n', '\r\n'))
            delay = round(time.time() - self.results_callback.start_time, 6)
            self.results_callback.res.append(json.dumps([delay, 'o', data2]))
            message = dict()
            message['status'] = 0
            message['message'] = data
            message = json.dumps(message)
            print({
                "type": "send.message",
                "text": message,
            })
        finally:
            if group:
                message = dict()
                message['status'] = 0
                message['message'] = '执行完毕...'
                message = json.dumps(message)
                print({
                    "type": "close.channel",
                    "text": message,
                })
                # if self.results_callback.res:
                #     save_res(self.results_callback.res_file, self.results_callback.res)
                # batchcmd_log(
                #     user=self.results_callback.user,
                #     hosts=self.results_callback.hosts,
                #     cmd='上传文件 {} 到 {}'.format(self.results_callback.src.split('/')[-1], self.results_callback.dst),
                #     detail=self.results_callback.res_file,
                #     address=self.results_callback.client,
                #     useragent=self.results_callback.user_agent,
                #     start_time=self.results_callback.start_time_django,
                #     type=3,
                # )
            try:
                os.remove(self.results_callback.src)
            except Exception:
                print(traceback.format_exc())

    def get_server_info(self, hosts='all'):
        """
        获取主机信息
        """
        self.run_module('setup', '', hosts)
        ok, failed, unreach, error = self.results_callback.get_res()
        infos = []
        if ok:
            for i in ok:
                info = dict()
                info['host'] = i['host']
                info['hostname'] = i['result']['ansible_facts']['ansible_hostname']
                info['cpu_model'] = i['result']['ansible_facts']['ansible_processor'][-1]
                info['cpu_number'] = int(i['result']['ansible_facts']['ansible_processor_count'])
                info['vcpu_number'] = int(i['result']['ansible_facts']['ansible_processor_vcpus'])
                info['kernel'] = i['result']['ansible_facts']['ansible_kernel']
                info['system'] = '{} {} {}'.format(i['result']['ansible_facts']['ansible_distribution'],
                                                   i['result']['ansible_facts']['ansible_distribution_version'],
                                                   i['result']['ansible_facts']['ansible_userspace_architecture'])
                info['server_model'] = i['result']['ansible_facts']['ansible_product_name']
                info['ram_total'] = round(int(i['result']['ansible_facts']['ansible_memtotal_mb']) / 1024)
                info['swap_total'] = round(int(i['result']['ansible_facts']['ansible_swaptotal_mb']) / 1024)
                info['disk_total'], disk_size = 0, 0
                for k, v in i['result']['ansible_facts']['ansible_devices'].items():
                    if k[0:2] in ['sd', 'hd', 'ss', 'vd']:
                        if 'G' in v['size']:
                            disk_size = float(v['size'][0: v['size'].rindex('G') - 1])
                        elif 'T' in v['size']:
                            disk_size = float(v['size'][0: v['size'].rindex('T') - 1]) * 1024
                        info['disk_total'] += round(disk_size, 2)
                info['filesystems'] = []
                for filesystem in i['result']['ansible_facts']['ansible_mounts']:
                    tmp = dict()
                    tmp['mount'] = filesystem['mount']
                    tmp['size_total'] = filesystem['size_total']
                    tmp['size_available'] = filesystem['size_available']
                    tmp['fstype'] = filesystem['fstype']
                    info['filesystems'].append(tmp)

                info['interfaces'] = []
                interfaces = i['result']['ansible_facts']['ansible_interfaces']
                for interface in interfaces:
                    # lvs 模式时 lo 也可能会绑定 IP 地址
                    if re.match(r"^(eth|bond|bind|eno|ens|em|ib)\d+?", interface) or interface == 'lo':
                        tmp = dict()
                        tmp['network_card_name'] = i['result']['ansible_facts']['ansible_{}'.format(interface)].get(
                            'device')
                        tmp['network_card_mac'] = i['result']['ansible_facts']['ansible_{}'.format(interface)].get(
                            'macaddress')
                        tmp['network_card_ipv4'] = i['result']['ansible_facts']['ansible_{}'.format(interface)].get(
                            'ipv4') if 'ipv4' in i['result']['ansible_facts'][
                            'ansible_{}'.format(interface)] else 'unknown'

                        tmp['network_card_ipv4_secondaries'] = i['result']['ansible_facts'][
                            'ansible_{}'.format(interface)].get(
                            'ipv4_secondaries') if 'ipv4_secondaries' in i['result']['ansible_facts'][
                            'ansible_{}'.format(interface)] else 'unknown'

                        tmp['network_card_ipv6'] = i['result']['ansible_facts']['ansible_{}'.format(interface)].get(
                            'ipv6') if 'ipv6' in i['result']['ansible_facts'][
                            'ansible_{}'.format(interface)] else 'unknown'

                        tmp['network_card_model'] = i['result']['ansible_facts']['ansible_{}'.format(interface)].get(
                            'type')
                        tmp['network_card_mtu'] = i['result']['ansible_facts']['ansible_{}'.format(interface)].get(
                            'mtu')
                        tmp['network_card_status'] = i['result']['ansible_facts']['ansible_{}'.format(interface)].get(
                            'active')
                        tmp['network_card_speed'] = i['result']['ansible_facts']['ansible_{}'.format(interface)].get(
                            'speed')
                        info['interfaces'].append(tmp)
                infos.append(info)
        return infos, failed, unreach, error

    def get_result(self):
        ok, failed, unreach, error = self.results_callback.get_res()
        if ok:
            return [json.dumps(i) for i in ok ][0]

        if failed:
            return [json.dumps(i) for i in failed ][0]

        if unreach:
            return [json.dumps(i) for i in unreach ][0]

        if error:
            return [json.dumps(i) for i in error ][0]


if __name__ == '__main__':
    host_data = [
        {
            'hostname': 'git',
            'ip': '192.168.5.2',
            'port': 22,
            'username': 'root',
            'password': '@23123',
            'become': {
                'method': 'sudo',
                'user': 'root',
                'pass': '11',
            },
            'groups': ['gituwen', 'test'],
            'vars': {'love': 'yes'},
        },
        {
            'hostname': 'k8s',
            'ip': '192.168.55.2',
            'port': 22,
            'username': 'root',
            'password': '123123',
            'become': {
                'method': 'sudo',
                'user': 'root',
                'pass': '@123123',
            },
            'groups': ['gituwen', 'test'],
            'vars': {'love': 'yes'},
        },
    ]
    playbook_yml = './hello.yml'
    private_key_file = './id_rsa'
    remote_user = 'root'
    extra_vars = {
        'var': 'test'
    }

    inventory = BaseInventory(host_data)

    callback = CallbackModule()

    ansible_api = AnsibleAPI(
        # private_key_file=private_key_file,
        # extra_vars=extra_vars,
        # remote_user=remote_user,
        dynamic_inventory=inventory,
        callback=CallbackBase(),
    )

    # ansible_api.run_playbook(playbook_yml=playbook_yml)
    ansible_api.run_module(module_name="shell", module_args="touch /tmp/test1 ", hosts="k8s")
    # cmd = '. /etc/profile &> /dev/null; . ~/.bash_profile &> /dev/null; ip a;'
    # ansible_api.run_module(module_name='shell', module_args=cmd, hosts='all')
    # ansible_api.run_cmd(cmds=cmd, hosts='k8s')
    # ansible_api.run_module(module_name='setup', module_args='', hosts='k8s')
    # ansible_api.get_server_info(hosts='lvs')
    ansible_api.get_result()
