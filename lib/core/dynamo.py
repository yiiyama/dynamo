import sys
import time
import logging
import hashlib
import multiprocessing
import Queue

from core.inventory import DynamoInventory
from common.interface.mysql import MySQL
from common.configuration import common_config
from common.control import sigint

LOG = logging.getLogger(__name__)

class Dynamo(object):
    """Main daemon class."""

    CMD_UPDATE, CMD_DELETE = range(2)

    def __init__(self, config):
        """
        @param config  Content of root-privileged config file.
        """

        common_db_config = dict(common_config.mysql)
        common_db_config.update(config.mysql)

        registry_config = dict(common_db_config)
        registry_config.update(config.registry.db_params)
        registry_config['reuse_connection'] = False
        self.registry = MySQL(**registry_config)

        store_config = dict(common_db_config)
        store_config.update(config.inventory.persistency)
        self.inventory = DynamoInventory(persistency_config = store_config, load = False)

    def run(self):
        """
        Infinite-loop main body of the daemon.
        Step 1: Poll the registry for one uploaded script.
        Step 2: If a script is found, check the authorization of the script.
        Step 3: Spawn a child process for the script with either a partition or the inventory as an argument.
        Step 4: Collect completed child processes.
        Step 5: Sleep for N seconds.
        """

        LOG.info('Started dynamo daemon.')

        self.inventory.load()

        child_processes = []

        LOG.info('Start polling for executables.')

        try:
            first_wait = True
            while True:
                LOG.debug('Polling for executables.')
    
                sql = 'SELECT s.`id`, s.`title`, s.`file`, s.`user_id`, u.`name`, s.`content`, s.`type`, s.`write_request`'
                sql += ' FROM `action` AS s INNER JOIN `users` AS u ON u.`id` = s.`user_id`'
                sql += ' WHERE s.`status` = \'new\''
                sql += ' ORDER BY s.`timestamp` LIMIT 1'
                result = self.registry.query(sql)

                if len(result) == 0:
                    if len(child_processes) == 0 and first_wait:
                        LOG.info('Waiting for executables.')
                        first_wait = False

                    sleep_time = 5

                    LOG.debug('No executable found, sleeping for %d seconds.', sleep_time)

                else:
                    exec_id, title, path, user_id, user_name, content, content_type, write_request = result[0]
                    self.registry.query('UPDATE `action` SET `status` = \'run\' WHERE `id` = %s', exec_id)

                    if content_type == 'deletion_policy':
                        # this is a detox test run
                        pass
                    elif content_type == 'executable':
                        LOG.info('Found executable %s from user %s (write request: %s)', title, user_name, write_request)

                        if write_request:
                            if not self.check_write_auth(title, user_id, content):
                                LOG.warning('Executable %s from user %s is not authorized for write access.', title, user_name)
                                # send a message
                                continue
        
                            queue = multiprocessing.Queue(64)
                        else:
                            queue = None
        
                        proc = multiprocessing.Process(target = self._run_one, name = title, args = (content, queue))
                        child_processes.append((exec_id, proc, user_name, path, queue))
        
                        proc.daemon = True
                        proc.start()
        
                        LOG.info('Started executable %s (%s) from user %s (PID %d).', title, path, user_name, proc.pid)

                    first_wait = True
                    sleep_time = 0

                self.collect_processes(child_processes)
   
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            LOG.info('Main process interrupted with SIGINT.')

        except:
            # log the exception
            LOG.warning('Exception in main process. Terminating all child processes.')
            raise

        finally:
            for exec_id, proc, user_name, path, pipe in child_processes:
                LOG.warning('Terminating %s (%s) requested by %s (PID %d)', proc.name, path, user_name, proc.pid)
                proc.terminate()
                proc.join(5)
                if proc.is_alive():
                    LOG.warning('Child process %d did not return after 5 seconds.', proc.pid)

                self.registry.query('UPDATE `action` SET `status` = \'killed\' where `id` = %s', exec_id)

    def check_write_auth(self, title, user, content):
        # check authorization
        if content is None:
            checksum = None
        else:
            checksum = hashlib.md5(content).hexdigest()
        
        sql = 'SELECT `user_id` FROM `authorized_executables` WHERE `title` = %s AND `checksum` = %s'
        for auth_user_id in self.registry.query(sql, title, checksum):
            if auth_user_id == 0 or auth_user_id == user_id:
                return True

        return False

    def collect_processes(self, child_processes):
        ichild = 0
        while ichild != len(child_processes):
            exec_id, proc, user_name, path, queue = child_processes[ichild]
    
            if queue is not None and not queue.empty():
                # the child process is wrapping up and wants to send us the list of updated objects
                # pool all updated objects into a list first
                updated_objects = []
                deleted_objects = []
    
                while True:
                    try:
                        cmd, obj = queue.get(block = True, timeout = 1)
                    except Queue.Empty:
                        if proc.is_alive():
                            # still trying to say something
                            continue
                        else:
                            break
                    else:
                        if cmd == Dynamo.CMD_UPDATE:
                            updated_objects.append(obj)
                        elif cmd == Dynamo.CMD_DELETE:
                            deleted_objects.append(obj)
    
                if len(updated_objects) != 0 or len(deleted_objects) != 0:
                    # process data
                    sigint.block()
                    for obj in updated_objects:
                        self.inventory.update(obj, write = True)
                    for obj in deleted_objects:
                        self.inventory.delete(obj, write = True)
                    sigint.unblock()
    
            if proc.is_alive():
                ichild += 1
            else:
                LOG.info('Executable %s (%s) from user %s completed (Exit code %d).', proc.name, path, user_name, proc.exitcode)
                child_processes.pop(ichild)

                self.registry.query('UPDATE `action` SET `status` = %s where `id` = %s', 'done' if proc.exitcode == 0 else 'failed', exec_id)

        
    def _run_one(self, executable, queue):
        if queue is not None:
            # create a list of updated objects the executable can fill
            self.inventory._updated_objects = []
            self.inventory._deleted_objects = []

        # Redirect STDOUT and STDERR to file, close STDIN
        stdout = sys.stdout
        stderr = sys.stderr
        sys.stdout = open('/tmp/test.out', 'w')
        sys.stderr = open('/tmp/test.err', 'w')
        sys.stdin.close()

        # Restart logging
        logging.shutdown()
        reload(logging)

        # Re-initialize the inventory store with read-only connection
        # This is for security and simply for concurrency - multiple processes
        # should not share the same DB connection
        self.inventory.init_store()

        exec(executable, {'dynamo': self.inventory})

        if queue is not None:
            for obj in self.inventory._updated_objects:
                queue.put((Dynamo.CMD_UPDATE, obj))
            for obj in self.inventory._deleted_objects:
                queue.put((Dynamo.CMD_DELETE, obj))

            # can we close and quit here?
            while not queue.empty():
                time.sleep(1)

        sys.stdout.close()
        sys.stderr.close()
        sys.stdout = stdout
        sys.stderr = stderr
        
