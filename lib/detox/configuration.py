from common.configuration import paths, Configuration

delete_old = Configuration()
delete_old.threshold = (1.5, 'y')

delete_unpopular = Configuration()
delete_unpopular.threshold = 1.

html_path = '/home/cmsprod/dynamo/detox/policylog.html'
