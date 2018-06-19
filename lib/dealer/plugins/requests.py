import collections
import logging
import fnmatch
import random
import re

from dynamo.dealer.plugins.base import BaseHandler, DealerRequest
from dynamo.utils.interface.mysql import MySQL
from dynamo.dataformat import Configuration, Dataset, Block, DatasetReplica, BlockReplica
from dynamo.dataformat.exceptions import ObjectError

LOG = logging.getLogger(__name__)

class CopyRequestsHandler(BaseHandler):
    """Process direct transfer requests made to the registry."""

    def __init__(self, config):
        BaseHandler.__init__(self, 'DirectRequests')

        registry_config = Configuration(config.registry)
        registry_config['reuse_connection'] = True # need to work with table locks
        
        self.registry = MySQL(registry_config)
        self.history = MySQL(config.history)

        # maximum size that can be requested
        self.max_size = config.max_size * 1.e+12

        # convert block-level requests to dataset-level if requested size is greater than
        # dataset size * block_request_max
        self.block_request_max = config.block_request_max

        # list of group names from which ownership of blocks can be taken away
        self.overwritten_groups = config.get('overwritten_groups', [])

    def get_requests(self, inventory, history, policy): # override
        """
        1. Request all active transfers in new state (these were not queued in the last cycle)
        2. Find all transfer requests with status new.
        3. Decide whether to accept the request. Set status accordingly.
        4. Find the destinations if wildcard was used.
        """

        partition = inventory.partitions[policy.partition_name]

        overwritten_groups = [inventory.groups[name] for name in self.overwritten_groups]
        
        # full list of blocks to be proposed to Dealer
        blocks_to_propose = {} # {site: {dataset: set of blocks}}

        # re-request all new active copies
        if not self.read_only:
            self.registry.lock_tables(write = ['active_copies'], retries = 1)

        sql = 'SELECT `request_id`, `item`, `site` FROM `active_copies` WHERE `status` = \'new\''
        sql += ' ORDER BY `site`, `item`'

        def record_failure(request_id, item_name, site_name):
            if self.read_only:
                return

            sql = 'UPDATE `active_copies` SET `status` = \'failed\''
            sql += ' WHERE `request_id` = %s AND `item` = %s AND `site` = %s'
            self.registry.query(sql, request_id, item_name, site_name)

        active_requests = []

        _dataset_name = ''
        for request_id, item_name, site_name in self.registry.query(sql):
            try:
                site = inventory.sites[site_name]
            except KeyError:
                # shouldn't happen but for safety
                record_failure(request_id, item_name, site_name)
                continue

            try:
                dataset_name, block_name = Block.from_full_name(item_name)

            except ObjectError:
                # item_name is (supposed to be) a dataset name

                _dataset_name = ''

                try:
                    dataset = inventory.datasets[item_name]
                except KeyError:
                    # shouldn't happen but for safety
                    record_failure(request_id, item_name, site_name)
                    continue

                active_requests.append((dataset, site))

            else:
                # item_name is a block name

                if dataset_name != _dataset_name:
                    _dataset_name = dataset_name
                    active_requests.append(([], site))

                try:
                    dataset = inventory.datasets[dataset_name]
                except KeyError:
                    # shouldn't happen but for safety
                    record_failure(request_id, item_name, site_name)
                    continue

                block = dataset.find_block(block_name)

                if block is None:
                    # shouldn't happen but for safety
                    record_failure(request_id, item_name, site_name)
                    continue

                active_requests[-1][0].append(block)

        for item, site in active_requests:
            try:
                site_blocks = blocks_to_propose[site]
            except KeyError:
                site_blocks = blocks_to_propose[site] = {}

            # item is a dataset or a list of blocks
            if type(item) is Dataset:
                site_blocks[item] = set(item.blocks)
            else:
                dataset = item[0].dataset
                try:
                    blocks = site_blocks[dataset]
                except KeyError:
                    blocks = site_blocks[dataset] = set()

                blocks.update(item)

        if not self.read_only:
            self.registry.unlock_tables()

        ## deal with new requests

        if not self.read_only:
            self.registry.lock_tables(write = ['copy_requests', ('copy_requests', 'r'), ('copy_request_sites', 's'), ('copy_request_items', 'i'), 'active_copies', ('active_copies', 'a')])

        # group into (group, # copies, request count, request time, [list of sites], [list of items])
        grouped_requests = {} # {request_id: copy info}

        sql = 'SELECT `id`, `group`, `num_copies`, `request_count`, `first_request_time` FROM `copy_requests`'
        sql += ' WHERE `status` = \'new\''

        for request_id, group_name, num_copies, request_count, request_time in self.registry.xquery(sql):
            if request_id not in grouped_requests:
                grouped_requests[request_id] = (group_name, num_copies, request_count, request_time, [], [])

        sql = 'SELECT s.`request_id`, s.`site` FROM `copy_request_sites` AS s'
        sql += ' INNER JOIN `copy_requests` AS r ON r.`id` = s.`request_id`'
        sql += ' WHERE r.`status` = \'new\''

        for request_id, site_name in self.registry.xquery(sql):
            grouped_requests[request_id][4].append(site_name)

        sql = 'SELECT i.`request_id`, i.`item` FROM `copy_request_items` AS i'
        sql += ' INNER JOIN `copy_requests` AS r ON r.`id` = i.`request_id`'
        sql += ' WHERE r.`status` = \'new\''

        for request_id, item_name in self.registry.xquery(sql):
            grouped_requests[request_id][5].append(item_name)

        def activate(request_id, item, site, status):
            if self.read_only:
                return

            activate_sql = 'INSERT INTO `active_copies` (`request_id`, `item`, `site`, `status`, `created`, `updated`)'
            activate_sql += ' VALUES (%s, %s, %s, %s, NOW(), NOW())'

            # item is a dataset or a list of blocks
            if type(item) is Dataset:
                self.registry.query(activate_sql, request_id, item.name, site.name, status)
            else:
                for block in item:
                    self.registry.query(activate_sql, request_id, block.full_name(), site.name, status)

        def reject(request_id, reason):
            if self.read_only:
                return

            sql = 'UPDATE `copy_requests` AS r SET r.`status` = \'rejected\', r.`rejection_reason` = %s'
            sql += ' WHERE r.`id` = %s'
            self.history.query(sql, reason, request_id)

            sql = 'DELETE FROM r, a, i, s USING `copy_requests` AS r'
            sql += ' INNER JOIN `active_copies` AS a ON a.`request_id` = r.`id`'
            sql += ' INNER JOIN `copy_request_items` AS i ON i.`request_id` = r.`id`'
            sql += ' INNER JOIN `copy_request_sites` AS s ON s.`request_id` = r.`id`'
            sql += ' WHERE r.`id` = %s'
            self.registry.query(sql, request_id)

        # loop over requests and find items and destinations
        for request_id, (group_name, num_copies, request_count, request_time, site_names, item_names) in grouped_requests.iteritems():
            try:
                group = inventory.groups[group_name]
            except KeyError:
                reject(request_id, 'Invalid group name %s' % group_name)
                continue

            sites = [] # list of sites

            for site_name in site_names:
                if '*' in site_name:
                    site_pat = re.compile(fnmatch.translate(site_name))
                    for site in policy.target_sites:
                        if site_pat.match(site.name):
                            sites.append(site)
                else:
                    try:
                        site = inventory.sites[site_name]
                    except KeyError:
                        continue

                    if site in policy.target_sites:
                        sites.append(site)

            if len(sites) == 0:
                reject(request_id, 'No valid site name in list')
                continue

            # randomize site ordering
            random.shuffle(sites)

            rejected = False

            items = [] # list of datasets or (list of blocks from a dataset)

            _dataset_name = ''
            # sorted(item_names) -> assuming dataset name comes first in the block full name so blocks get automatically clustered in the listing
            for item_name in sorted(item_names):
                try:
                    dataset_name, block_name = Block.from_full_name(item_name)

                except ObjectError:
                    # item_name is (supposed to be) a dataset name

                    _dataset_name = ''

                    # this is a dataset
                    try:
                        dataset = inventory.datasets[item_name]
                    except KeyError:
                        reject(request_id, 'Dataset %s not found' % item_name)
                        rejected = True
                        break

                    items.append(dataset)

                else:
                    # item_name is a block name

                    if dataset_name != _dataset_name:
                        # of a new dataset
                        try:
                            dataset = inventory.datasets[dataset_name]
                        except KeyError:
                            # if any of the dataset name is invalid, reject the entire request
                            reject(request_id, 'Dataset %s not found' % dataset_name)
                            rejected = True
                            break

                        _dataset_name = dataset_name
                        items.append([])

                    block = dataset.find_block(block_name)
                    if block is None:
                        reject(request_id, 'Block %s not found' % item_name)
                        rejected = True
                        break

                    # last element of the items list is a list
                    items[-1].append(block)

            if rejected:
                continue

            # each element of items is either a dataset or a list of blocks
            # convert to DealerRequests

            proto_dealer_requests = []

            # process the items list
            for item in items:
                if type(item) is Dataset:
                    if dataset.size > self.max_size:
                        reject(request_id, 'Dataset %s is too large (>%.0f TB)' % (dataset.name, self.max_size * 1.e-12))
                        rejected = True
                        break

                else:
                    dataset = item[0].dataset

                    total_size = sum(b.size for b in item)

                    if total_size > self.max_size:
                        reject(request_id, 'Request size for %s too large (>%.0f TB)' % (dataset.name, self.max_size * 1.e-12))
                        rejected = True
                        break

                    if total_size > float(dataset.size) * self.block_request_max:
                        # if the total size of requested blocks is large enough, just copy the dataset
                        # covers the case where we actually have the full list of blocks (if block_request_max is less than 1)
                        item = dataset

                proto_dealer_requests.append(DealerRequest(item, group = group))

            if rejected:
                continue

            new_requests = []
            completed_requests = []

            # find destinations (num_copies times) for each item
            for proto_request in proto_dealer_requests:
                # try to make a dealer request for all requests, except when there is a full copy of the item

                if num_copies == 0:
                    # make one copy at each site

                    for destination in sites:
                        dealer_request = DealerRequest(proto_request.item(), destination = destination)

                        if dealer_request.item_already_exists() == 2:
                            completed_requests.append(dealer_request)
                        else:
                            rejection_reason = policy.check_destination(dealer_request, partition)
                            if rejection_reason is not None:
                                reject(request_id, 'Cannot copy %s to %s' % (dealer_request.item_name(), destination.name))
                                rejected = True
                                break
        
                            new_requests.append(dealer_request)

                else:
                    # total of n copies
                    candidate_sites = []
                    num_new = num_copies

                    # bring sites where the item already exists first (may want to just "flip" the ownership)
                    sites_and_existence = []
                    for destination in sites:
                        exists = proto_request.item_already_exists(destination) # 0, 1, or 2
                        if exists != 0:
                            sites_and_existence.insert(0, (destination, exists))
                        else:
                            sites_and_existence.append((destination, exists))

                    for destination, exists in sites_and_existence:
                        if num_new == 0:
                            break

                        dealer_request = DealerRequest(proto_request.item(), destination = destination)

                        # consider copies proposed by other requests as complete
                        try:
                            proposed_blocks = blocks_to_propose[destination][dealer_request.dataset]
                        except KeyError:
                            pass
                        else:
                            if dealer_request.blocks is not None:
                                if set(dealer_request.blocks) <= proposed_blocks:
                                    num_new -= 1
                                    completed_requests.append(dealer_request)
                                    continue

                            else:
                                if dealer_request.dataset.blocks == proposed_blocks:
                                    num_new -= 1
                                    completed_requests.append(dealer_request)
                                    continue

                        # if the item already exists, it's a complete copy too
                        if exists == 2:
                            num_new -= 1
                            completed_requests.append(dealer_request)
                        elif exists == 1:
                            # if the current group can be overwritten, make a request
                            # otherwise skip
                            single_owner = dealer_request.item_owned_by() # None if owned by multiple groups
                            if single_owner is not None and len(overwritten_groups) != 0 and single_owner in overwritten_groups:
                                new_requests.append(dealer_request)
                                num_new -= 1
                        else:
                            candidate_sites.append(destination)

                    for icopy in range(num_new):
                        dealer_request = DealerRequest(proto_request.item())
                        policy.find_destination_for(dealer_request, partition, candidates = candidate_sites)
    
                        if dealer_request.destination is None:
                            # if any of the item cannot find any of the num_new destinations, reject the request
                            reject(request_id, 'Destination %d for %s not available' % (icopy, dealer_request.item_name()))
                            rejected = True
                            break
    
                        candidate_sites.remove(destination)
                        new_requests.append(dealer_request)

                # if num_copies == 0, else

                if rejected:
                    break

            # for each item in request

            if rejected:
                continue

            # finally add to the returned requests
            for dealer_request in new_requests:
                activate(request_id, dealer_request.item(), dealer_request.destination, 'new')

                try:
                    site_blocks = blocks_to_propose[dealer_request.destination]
                except KeyError:
                    site_blocks = blocks_to_propose[dealer_request.destination] = {}

                if dealer_request.blocks is not None:
                    try:
                        blocks = site_blocks[dealer_request.dataset]
                    except KeyError:
                        blocks = site_blocks[dealer_request.dataset] = set()
    
                    blocks.update(dealer_request.blocks)

                else:
                    site_blocks[dealer_request.dataset] = set(dealer_request.dataset.blocks)

            for dealer_request in completed_requests:
                activate(request_id, dealer_request.item(), dealer_request.destination, 'completed')

            if not self.read_only:
                self.registry.query('UPDATE `copy_requests` SET `status` = \'activated\' WHERE `id` = %s', request_id)

        if not self.read_only:
            self.registry.unlock_tables()

        # throw away all the DealerRequest objects we've been using and form the final proposal
        dealer_requests = []
        for site, block_list in blocks_to_propose.iteritems():
            for dataset, blocks in block_list.iteritems():
                if blocks == dataset.blocks:
                    dealer_requests.append(DealerRequest(dataset, destination = site))
                else:
                    dealer_requests.append(DealerRequest(list(blocks), destination = site))

        return dealer_requests

    def postprocess(self, cycle_number, copy_list): # override
        """
        Create active copy entries for accepted copies.
        """

        if self.read_only:
            return

        sql = 'UPDATE `active_copies` SET `status` = \'queued\', `updated` = NOW() WHERE `item` LIKE %s AND `site` = %s AND `status` = \'new\''

        for replica in copy_list:
            # active copies with dataset name
            self.registry.query(sql, replica.dataset.name, replica.site.name)

            # active copies with block name
            self.registry.query(sql, Block.to_full_name(replica.dataset.name, '%'), replica.site.name)
