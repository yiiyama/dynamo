import os
import sys
from collections import OrderedDict as OD
import json

thisdir = os.path.dirname(os.path.realpath(__file__))

def generate_local_board_conf(conf_str):
    conf = json.loads(conf_str)

    with open(thisdir + '/grants.json') as source:
        grants_conf = json.load(source)

    if 'host' not in conf:
        host = 'localhost'
    else:
        host = conf['host']

    user = conf['user']

    if 'passwd' in conf:
        passwd = conf['passwd']
    else:
        passwd = grants_conf[user]['passwd']

    board_conf = OD([('module', 'mysqlboard:MySQLUpdateBoard'), ('config', OD())])
    
    board_conf['config']['db_params'] = OD([('host', host), ('user', user), ('passwd', passwd), ('db', 'dynamoserver'), ('scratch_db', 'dynamo_tmp')])

    return board_conf

def generate_store_conf(conf_str):
    conf = json.loads(conf_str)

    with open(thisdir + '/grants.json') as source:
        grants_conf = json.load(source)

    if 'host' not in conf:
        host = 'localhost'
    else:
        host = conf['host']

    user = conf['user']
    readuser = conf['readuser']
   
    if 'passwd' in conf:
        passwd = conf['passwd']
    else:
        passwd = grants_conf[user]['passwd']

    if 'readpasswd' in conf:
        readpasswd = conf['readpasswd']
    else:
        readpasswd = grants_conf[readuser]['passwd']

    store_conf = OD([('module', 'mysqlstore:MySQLInventoryStore'), ('config', OD()), ('readonly_config', OD())])

    store_conf['config']['db_params'] = OD([
        ('host', host),
        ('db', 'dynamo'),
        ('user', user),
        ('passwd', passwd),
        ('scratch_db', 'dynamo_tmp')
    ])

    store_conf['readonly_config']['db_params'] = OD([
        ('host', host),
        ('db', 'dynamo'),
        ('user', readuser),
        ('passwd', readpasswd),
        ('scratch_db', 'dynamo_tmp')
    ])

    return store_conf

def generate_master_conf(conf_str, master = True):
    conf = json.loads(conf_str)

    with open(thisdir + '/grants.json') as source:
        grants_conf = json.load(source)

    if 'host' not in conf:
        host = 'localhost'
    else:
        host = conf['host']

    user = conf['user']

    if 'readuser' in conf:
        readuser = conf['readuser']
    else:
        readuser = None
   
    if 'passwd' in conf:
        passwd = conf['passwd']
    else:
        passwd = grants_conf[user]['passwd']

    master_conf = OD([
        ('module', 'mysqlmaster:MySQLMasterServer'),
        ('config', OD())
    ])

    master_conf['config']['db_params'] = OD([
        ('host', host),
        ('db', 'dynamoserver'),
        ('user', user),
        ('passwd', passwd),
        ('scratch_db', 'dynamo_tmp')
    ])

    if master:
        master_conf['config']['applock'] = OD()
        master_conf['config']['applock']['db_params'] = OD([
            ('host', host),
            ('db', 'dynamoregister'),
            ('user', user),
            ('passwd', passwd),
            ('scratch_db', 'dynamo_tmp')
        ])

    if readuser is not None:
        if 'readpasswd' in conf:
            readpasswd = conf['readpasswd']
        else:
            readpasswd = grants_conf[readuser]['passwd']

        master_conf['readonly_config'] = OD()
        master_conf['readonly_config']['db_params'] = OD([
            ('host', host),
            ('db', 'dynamoserver'),
            ('user', readuser),
            ('passwd', readpasswd),
            ('scratch_db', 'dynamo_tmp')
        ])

    return master_conf

try:
    __namespace__.generate_store_conf = generate_store_conf
    __namespace__.generate_master_conf = generate_master_conf
    __namespace__.generate_local_board_conf = generate_local_board_conf
    __namespace__.generate_fom_conf = generate_fom_conf
except NameError:
    pass
